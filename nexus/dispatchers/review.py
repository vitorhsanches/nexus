"""Real, read-only operational Manager Review dispatcher.

Adaptive reviewer routing (Nexus v2.0-D)
-----------------------------------------
The reviewer's model/provider/effort selection is delegated to the existing
v2.0 Adaptive Router via ``AdaptiveRoutingService.select_route_for_capability``
using the explicit ``review`` capability class. This intentionally never
calls ``select_route_for_task`` (which would misclassify review work as
standard-coding).

``AdaptiveRoutingService`` and the process launcher are both injectable so
this module never performs real network/subprocess calls during unit tests.
When no routing service is injected, production code lazily constructs the
real ``AdaptiveRoutingService`` (real OmniRoute telemetry) only at call
time -- never at import time.

Execution frontend vs. resource identity
-----------------------------------------
``route.provider`` is the resource/model provider identity (e.g. ``claude``).
``route.execution_path`` is ``OMNIROUTE`` -- the OmniRoute execution
transport. The Codex CLI ``model_provider`` config key is the OmniRoute
execution *gateway*, and is passed explicitly as ``model_provider="omniroute"``
whenever the selected route's ``execution_path`` is ``OMNIROUTE``. These
three identities are never collapsed into each other.

The reviewer continues to inspect the EXISTING Worker worktree, read-only,
directly via the Codex CLI. It never invokes ``omniroute-worker.ps1``
(that wrapper creates a NEW Worker worktree).
"""

import json
import os
import re
import subprocess
from pathlib import Path

from nexus.registry.agents import (
    create_agent,
    update_agent_execution,
    update_agent_status,
)
from nexus.routing.catalog import default_catalog
from nexus.routing.models import CapabilityClass
from nexus.routing.router import NoEligibleRouteError, select_best_route
from nexus.routing.models import RoutingRequest
from nexus.routing.service import (
    AdaptiveRoutingService,
    AdaptiveRoutingUnavailableError,
    InvalidRiskLevelError,
    RoutingDecision,
    normalize_risk_level,
)


REVIEW_PATTERN = re.compile(
    r"NEXUS_REVIEW_BEGIN\s*(\{.*?\})\s*NEXUS_REVIEW_END",
    re.DOTALL,
)


REVIEW_CAPABILITY = CapabilityClass.REVIEW.value


def _find_codex() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")

    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA is not available.")

    base = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
    candidates = list(base.glob("*/codex.exe"))

    if not candidates:
        raise FileNotFoundError(
            f"Codex executable not found under {base}"
        )

    return max(
        candidates,
        key=lambda path: path.stat().st_mtime,
    )


def _validate_review(review: dict) -> dict:
    allowed_verdicts = {
        "PASS",
        "RETRY",
        "ESCALATE",
        "BLOCKED",
    }

    allowed_failure_classes = {
        None,
        "TRANSIENT",
        "TOOL_FAILURE",
        "PROVIDER_FAILURE",
        "VALIDATION_FAILURE",
        "SCOPE_VIOLATION",
        "CAPABILITY_FAILURE",
        "REQUIREMENT_FAILURE",
        "UNKNOWN",
    }

    verdict = review.get("verdict")

    if verdict not in allowed_verdicts:
        raise ValueError(
            f"Invalid review verdict: {verdict!r}"
        )

    failure_class = review.get("failure_class")

    if failure_class not in allowed_failure_classes:
        raise ValueError(
            f"Invalid failure_class: {failure_class!r}"
        )

    summary = review.get("summary")

    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Review summary is missing.")

    evidence = review.get("evidence")

    if not isinstance(evidence, list) or not evidence:
        raise ValueError(
            "Review must contain evidence."
        )

    for item in evidence:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                "Review evidence contains an invalid item."
            )

    if verdict == "PASS" and failure_class is not None:
        raise ValueError(
            "PASS review cannot contain a failure_class."
        )

    return review


def _extract_review(output: str) -> dict:
    matches = list(REVIEW_PATTERN.finditer(output))

    if not matches:
        raise ValueError(
            "Manager did not return a review envelope."
        )

    errors = []

    for match in reversed(matches):
        try:
            review = json.loads(match.group(1))
            return _validate_review(review)
        except (json.JSONDecodeError, ValueError) as error:
            errors.append(str(error))

    raise ValueError(
        "No valid review envelope found. "
        + "; ".join(errors[:3])
    )


class ReviewRoutingError(RuntimeError):
    """Raised when a reviewer route cannot be safely resolved.

    Covers both adaptive routing failure (no eligible telemetry-aware
    route) and an explicit model override that is not an approved,
    review-capable, risk-eligible route.
    """


def _explicit_override_decision(
    model: str,
    effort: str | None,
    risk_level: str,
) -> RoutingDecision:
    """Validate an explicit reviewer model/effort override.

    Any explicit reviewer route MUST still be an approved, review-capable,
    risk-eligible, non-experimental, Terra-safe route. This is never a
    bypass around Nexus approval/risk/Terra rules: the override is
    revalidated through the exact same ``select_best_route`` hard gates
    used by adaptive routing, restricted to the requested model.
    """

    catalog = default_catalog()

    matching = tuple(
        route
        for route in catalog
        if route.model_id == model
        and (effort is None or route.effort == effort)
    )

    if not matching:
        if effort is not None:
            raise ReviewRoutingError(
                f"Explicit reviewer override model={model!r} "
                f"effort={effort!r} is not present in the approved "
                "catalog."
            )

        raise ReviewRoutingError(
            f"Explicit reviewer model override {model!r} is not present "
            "in the approved catalog."
        )

    request = RoutingRequest(
        capability=REVIEW_CAPABILITY,
        risk_level=risk_level,
    )

    try:
        selected = select_best_route(
            request,
            catalog=matching,
            resources={},
        )
    except NoEligibleRouteError as error:
        if effort is not None:
            raise ReviewRoutingError(
                f"Explicit reviewer override model={model!r} "
                f"effort={effort!r} is not an approved review-capable "
                f"route for risk_level={risk_level!r}: {error}"
            ) from error

        raise ReviewRoutingError(
            f"Explicit reviewer model override {model!r} is not an "
            f"approved review-capable route for risk_level={risk_level!r}: "
            f"{error}"
        ) from error

    route = selected.route

    return RoutingDecision(
        model=route.model_id,
        provider=route.provider,
        effort=route.effort,
        execution_path=route.execution_path,
        reason=(
            "Explicit reviewer model override validated against the "
            "approved review-capable catalog. " + selected.reason
        ),
        fallbacks=(),
        degraded=False,
    )


def _resolve_reviewer_route(
    model: str | None,
    effort: str | None,
    risk_level,
    routing_service,
) -> RoutingDecision:
    """Resolve the reviewer route adaptively, or validate an override.

    ``risk_level`` is normalized through the central routing risk
    validation; unknown explicit risk fails closed.
    """

    normalized_risk = normalize_risk_level(risk_level)

    if model is not None:
        return _explicit_override_decision(
            model,
            effort,
            normalized_risk,
        )

    service = routing_service or AdaptiveRoutingService()

    return service.select_route_for_capability(
        REVIEW_CAPABILITY,
        risk_level=normalized_risk,
    )


def _default_process_launcher(command):
    return subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def review_worker(
    run_id: str,
    worker_id: str,
    worktree: str,
    original_task: str,
    worker_scope: str,
    model: str | None = None,
    effort: str | None = None,
    risk_level=None,
    routing_service=None,
    process_launcher=None,
) -> dict:
    """Execute a real, read-only Manager review for a Worker.

    ``model``/``effort`` remain optional backward-compatible overrides.
    When omitted, the reviewer route is selected adaptively via
    ``AdaptiveRoutingService.select_route_for_capability`` using the
    explicit ``review`` capability class and the (normalized) ``risk_level``
    propagated from the Nexus GO plan. When provided, ``model``/``effort``
    are validated against the approved review-capable catalog before use --
    never a bypass around approval/risk/Terra rules.

    ``routing_service``/``process_launcher`` are injectable for tests; when
    omitted, production defaults (real ``AdaptiveRoutingService`` / real
    ``subprocess.Popen``) are used, constructed/invoked lazily so no network
    or subprocess call ever happens during unit test discovery.
    """

    try:
        decision = _resolve_reviewer_route(
            model,
            effort,
            risk_level,
            routing_service,
        )
    except (
        ReviewRoutingError,
        NoEligibleRouteError,
        InvalidRiskLevelError,
        AdaptiveRoutingUnavailableError,
    ) as error:
        return {
            "reviewer_id": None,
            "status": "BLOCKED",
            "review": None,
            "exit_code": None,
            "error": str(error),
            "routing": {
                "model": None,
                "provider": None,
                "effort": None,
                "execution_path": None,
                "reason": None,
                "degraded": False,
            },
        }

    routed_model = decision.model
    routed_effort = decision.effort
    routed_provider = decision.provider
    routed_execution_path = decision.execution_path

    routing_metadata = {
        "model": routed_model,
        "provider": routed_provider,
        "effort": routed_effort,
        "execution_path": routed_execution_path,
        "reason": decision.reason,
        "degraded": decision.degraded,
    }

    reviewer_id = create_agent(
        run_id=run_id,
        role="ManagerReview",
        provider="codex",
        model=routed_model,
        effort=routed_effort,
        status="RUNNING",
        parent_agent_id=worker_id,
    )

    update_agent_execution(
        reviewer_id,
        branch=json.dumps(routing_metadata),
    )

    try:
        codex = _find_codex()
    except (RuntimeError, FileNotFoundError) as error:
        update_agent_status(reviewer_id, "FAILED")

        return {
            "reviewer_id": reviewer_id,
            "status": "LAUNCH_FAILED",
            "review": None,
            "exit_code": None,
            "error": str(error),
            "routing": routing_metadata,
        }

    prompt = f"""
You are performing a narrow Manager review for a Nexus Worker.

REVIEW ONLY.

Do not:
- modify files;
- commit;
- publish;
- integrate;
- create Workers.

Original request:

{original_task}

Assigned Worker scope:

{worker_scope}

Review the actual Worker worktree.

You MUST inspect:
- git status;
- git diff;
- changed files;
- scope compliance;
- relevant validation evidence.

Run safe read-only validation when useful.

Determine whether the Worker result should be accepted.

Verdicts:

PASS
The implementation is correct, within scope, and sufficiently validated.

RETRY
The failure appears transient or fixable using the same capability tier.

ESCALATE
The implementation failed because the current capability tier appears
insufficient.

BLOCKED
The result cannot safely proceed without external action, missing
requirements, authorization, or a broader replan.

Return exactly:

NEXUS_REVIEW_BEGIN
{{
  "verdict": "PASS|RETRY|ESCALATE|BLOCKED",
  "failure_class": null,
  "summary": "short review conclusion",
  "evidence": [
    "concrete evidence"
  ]
}}
NEXUS_REVIEW_END

For PASS, failure_class must be null.

For non-PASS use one of:
TRANSIENT
TOOL_FAILURE
PROVIDER_FAILURE
VALIDATION_FAILURE
SCOPE_VIOLATION
CAPABILITY_FAILURE
REQUIREMENT_FAILURE
UNKNOWN

Do not place markdown fences around the envelope.
""".strip()

    command = [
        str(codex),
        "exec",
        "--ephemeral",
        "-C",
        worktree,
        "--sandbox",
        "read-only",
    ]

    if routed_execution_path == "OMNIROUTE":
        command += [
            "-c",
            'model_provider="omniroute"',
        ]

    command += [
        "-c",
        f'model="{routed_model}"',
        "-c",
        f'model_reasoning_effort="{routed_effort}"',
        "-c",
        'windows.sandbox="elevated"',
        prompt,
    ]

    print()
    print("NEXUS -> MANAGER REVIEW")
    print("=" * 70)
    print(f"Reviewer  : {reviewer_id}")
    print(f"Worker    : {worker_id}")
    print(f"Model     : {routed_model}")
    print(f"Provider  : {routed_provider}")
    print(f"Effort    : {routed_effort}")
    print(f"Path      : {routed_execution_path}")
    print(f"Degraded  : {decision.degraded}")
    print(f"Worktree  : {worktree}")
    print()

    launcher = process_launcher or _default_process_launcher

    try:
        process = launcher(command)
    except OSError as error:
        update_agent_status(reviewer_id, "FAILED")

        return {
            "reviewer_id": reviewer_id,
            "status": "LAUNCH_FAILED",
            "review": None,
            "exit_code": None,
            "error": str(error),
            "routing": routing_metadata,
        }

    output_lines = []

    assert process.stdout is not None

    for line in process.stdout:
        print(line, end="")
        output_lines.append(line)

    exit_code = process.wait()
    output = "".join(output_lines)

    update_agent_execution(
        reviewer_id,
        result=output[-16000:],
    )

    if exit_code != 0:
        update_agent_status(
            reviewer_id,
            "FAILED",
        )

        return {
            "reviewer_id": reviewer_id,
            "status": "FAILED",
            "review": None,
            "exit_code": exit_code,
            "routing": routing_metadata,
        }

    try:
        review = _extract_review(output)

    except Exception as error:
        update_agent_status(
            reviewer_id,
            "BLOCKED",
        )

        return {
            "reviewer_id": reviewer_id,
            "status": "BLOCKED",
            "review": None,
            "exit_code": exit_code,
            "error": str(error),
            "routing": routing_metadata,
        }

    update_agent_status(
        reviewer_id,
        "COMPLETED",
    )

    return {
        "reviewer_id": reviewer_id,
        "status": "COMPLETED",
        "review": review,
        "exit_code": exit_code,
        "routing": routing_metadata,
    }
