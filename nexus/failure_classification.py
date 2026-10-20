"""Deterministic failure classification for the Nexus Program layer.

This module answers WHAT happened, never WHAT TO DO about it. Retry,
fallback, escalation, and recovery all remain owned by the systems that
already implement them (nexus/orchestration/progressive.py,
nexus/policies/*, nexus/orchestration/recovery.py). This module only maps
an already-terminal outcome (a GoError from run_go, a failed verification,
or a capability/authority preflight result) onto a small, stable
vocabulary so the Program corrective loop and telemetry can react to the
right *kind* of failure without re-deriving it from raw error text.

Classes are intentionally coarse. Two independent, richer vocabularies
already exist and are reused rather than duplicated:
  - the Reviewer's ``failure_class`` (TRANSIENT, TOOL_FAILURE,
    PROVIDER_FAILURE, VALIDATION_FAILURE, SCOPE_VIOLATION,
    CAPABILITY_FAILURE, REQUIREMENT_FAILURE, UNKNOWN) --
    nexus/dispatchers/review.py.
  - the Worker's transport-level ``reason`` (TRANSIENT_PROVIDER_ERROR,
    WORKER_TIMEOUT, WORKER_RESULT_MISSING, WORKER_RESULT_EMPTY) --
    nexus/dispatchers/omniroute.py, nexus/process_timeout.py.
This module's job is only to fold those, plus GoError/IntegrationError
codes, into the single outer taxonomy the Program loop reasons about.
"""

from __future__ import annotations


TRANSIENT_PROVIDER = "TRANSIENT_PROVIDER"
ROUTE_UNAVAILABLE = "ROUTE_UNAVAILABLE"
CAPABILITY_INSUFFICIENT = "CAPABILITY_INSUFFICIENT"
VERIFICATION_FAILED = "VERIFICATION_FAILED"
ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
POLICY_BLOCKED = "POLICY_BLOCKED"
AUTHORITY_REQUIRED = "AUTHORITY_REQUIRED"
UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
REQUIREMENT_UNSATISFIED = "REQUIREMENT_UNSATISFIED"
UNKNOWN_FAILURE = "UNKNOWN_FAILURE"

FAILURE_CLASSES = frozenset({
    TRANSIENT_PROVIDER,
    ROUTE_UNAVAILABLE,
    CAPABILITY_INSUFFICIENT,
    VERIFICATION_FAILED,
    ENVIRONMENT_FAILURE,
    POLICY_BLOCKED,
    AUTHORITY_REQUIRED,
    UNSUPPORTED_CAPABILITY,
    REQUIREMENT_UNSATISFIED,
    UNKNOWN_FAILURE,
})

# Classes that must never trigger a corrective *coding* mission: they are
# either a hard policy/authority boundary or a capability Nexus does not
# implement at all. Existing systems already made this determination;
# the Program must stop, not retry around it.
_NO_CODING_RETRY = frozenset({
    ROUTE_UNAVAILABLE,
    POLICY_BLOCKED,
    AUTHORITY_REQUIRED,
    UNSUPPORTED_CAPABILITY,
})

# Classes for which the existing retry/fallback/escalation layer inside
# run_go/progressive.py has already been exhausted by the time a GoError
# reaches the Program. Retrying here means re-running the SAME mission
# (a fresh attempt), never a "fix the code" mission -- there is no known
# code defect to fix.
_TRANSIENT_LIKE = frozenset({TRANSIENT_PROVIDER, CAPABILITY_INSUFFICIENT, ENVIRONMENT_FAILURE})

_REVIEW_FAILURE_CLASS_MAP = {
    "TRANSIENT": TRANSIENT_PROVIDER,
    "PROVIDER_FAILURE": TRANSIENT_PROVIDER,
    "TOOL_FAILURE": ENVIRONMENT_FAILURE,
    "CAPABILITY_FAILURE": CAPABILITY_INSUFFICIENT,
    "SCOPE_VIOLATION": POLICY_BLOCKED,
    "VALIDATION_FAILURE": VERIFICATION_FAILED,
    "REQUIREMENT_FAILURE": REQUIREMENT_UNSATISFIED,
    "UNKNOWN": UNKNOWN_FAILURE,
    None: UNKNOWN_FAILURE,
}

# A Worker's Codex process exiting nonzero with no transient marker (see
# nexus/dispatchers/omniroute.py's _looks_transient): the agent runtime/tool
# layer itself broke (e.g. "Failed to create unified exec process"), never
# the repository content the Worker was asked to write. Distinct from
# WORKER_RESULT_MISSING/EMPTY (exit 0, no/empty output), which the existing
# code treats as plausibly transient transport noise and stays
# coding-retry-eligible.
WORKER_PROCESS_FAILURE = "WORKER_PROCESS_FAILURE"
WORKER_LAUNCH_ENCODING_FAILURE = "WORKER_LAUNCH_ENCODING_FAILURE"

_WORKER_REASON_MAP = {
    "TRANSIENT_PROVIDER_ERROR": TRANSIENT_PROVIDER,
    "WORKER_TIMEOUT": TRANSIENT_PROVIDER,
    "WORKER_RESULT_MISSING": ENVIRONMENT_FAILURE,
    "WORKER_RESULT_EMPTY": ENVIRONMENT_FAILURE,
    WORKER_PROCESS_FAILURE: ENVIRONMENT_FAILURE,
    WORKER_LAUNCH_ENCODING_FAILURE: ENVIRONMENT_FAILURE,
}

# Worker reasons that are deterministic worker-runtime/infrastructure
# failures: retrying with a fresh Manager plan and a new Worker cannot
# repair them (nothing about repository content influences whether the
# host's Codex sandbox/tool-process layer works), so Program correction
# must fail fast instead of spending its bounded attempt budget on an
# unwinnable retry. Intentionally the single, narrowly-evidenced case from
# the Phase 4.6 audit -- NOT a blanket "all ENVIRONMENT_FAILURE" rule.
_DETERMINISTIC_WORKER_INFRASTRUCTURE_REASONS = frozenset({WORKER_PROCESS_FAILURE, WORKER_LAUNCH_ENCODING_FAILURE})

_GO_ERROR_CODE_MAP = {
    "INITIAL_ROUTING_UNAVAILABLE": ROUTE_UNAVAILABLE,
    "ESCALATION_UNAVAILABLE": ROUTE_UNAVAILABLE,
    "CHECKPOINT_PERSISTENCE_FAILED": ENVIRONMENT_FAILURE,
    "GIT_TOOL_FAILURE": ENVIRONMENT_FAILURE,
    "PRIMARY_REPO_CHANGED_UNEXPECTEDLY": ENVIRONMENT_FAILURE,
    "INTEGRATION_CONFLICT": ENVIRONMENT_FAILURE,
    "INTEGRATION_DIRTY_OVERLAP": ENVIRONMENT_FAILURE,
    "INTEGRATION_INTERNAL_ERROR": ENVIRONMENT_FAILURE,
    "READ_ONLY_WORKER_MODIFIED_REPOSITORY": POLICY_BLOCKED,
    "SCOUT_STAGE_FAILED": ENVIRONMENT_FAILURE,
    "PROJECT_NOT_FOUND": ENVIRONMENT_FAILURE,
    "PROJECT_AMBIGUOUS": ENVIRONMENT_FAILURE,
    "PLAN_INVALID": UNKNOWN_FAILURE,
}


def classify_go_error(error) -> str:
    """Classify a ``nexus.orchestration.go.GoError`` into the outer taxonomy.

    Reads ``error.code`` (always present) and ``error.detail`` (the raw
    outcome/checkpoint dict attached at the raise site, when available) to
    recover the finer-grained classification already computed by the
    Reviewer or the Worker dispatcher, without re-parsing free text.
    """
    code = getattr(error, "code", None)
    detail = getattr(error, "detail", None) or {}

    if code == "WORKER_EXECUTION_FAILED":
        worker = detail.get("worker") or {}
        return _WORKER_REASON_MAP.get(worker.get("reason"), ENVIRONMENT_FAILURE)

    if code == "REVIEW_BLOCKED":
        review = detail.get("review") or {}
        return _REVIEW_FAILURE_CLASS_MAP.get(review.get("failure_class"), UNKNOWN_FAILURE)

    if code == "MANAGER_BLOCKED":
        return ENVIRONMENT_FAILURE if "launch" in str(error).lower() else UNKNOWN_FAILURE

    return _GO_ERROR_CODE_MAP.get(code, UNKNOWN_FAILURE)


def _clip(value, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def describe_go_error(error, limit: int = 500) -> str:
    """Concise, evidence-bearing description of a GoError for a corrective
    mission's context. Reads ``error.detail`` directly -- never re-parses
    the human-readable exception text (``str(error)``), which for a
    REVIEW_BLOCKED/WORKER_EXECUTION_FAILED GoError carries only the code
    and the first diagnostic field (e.g. "REVIEW_BLOCKED
    (worker_ordinal=1)"), not the Reviewer's or Worker's real reason.
    """
    code = getattr(error, "code", None)
    detail = getattr(error, "detail", None) or {}

    if code == "REVIEW_BLOCKED":
        review = detail.get("review") or {}
        bits = [f"verdict={review.get('verdict') or 'BLOCKED'}"]
        if review.get("failure_class"):
            bits.append(f"review_failure={review['failure_class']}")
        if review.get("summary"):
            bits.append(f"reason={review['summary']}")
        return _clip(f"{code} " + ", ".join(bits), limit)

    if code == "WORKER_EXECUTION_FAILED":
        worker = detail.get("worker") or {}
        bits = []
        if worker.get("reason"):
            bits.append(f"worker_reason={worker['reason']}")
        if worker.get("reason") in _DETERMINISTIC_WORKER_INFRASTRUCTURE_REASONS:
            bits.append("layer=worker_runtime/codex_tool_process")
        if worker.get("error"):
            bits.append(f"error={worker['error']}")
        return _clip(f"{code} " + (", ".join(bits) or "no further detail"), limit)

    if code in ("ESCALATION_UNAVAILABLE", "INITIAL_ROUTING_UNAVAILABLE"):
        review = detail.get("review") or {}
        bits = []
        if review.get("failure_class"):
            bits.append(f"review_failure={review['failure_class']}")
        if detail.get("error"):
            bits.append(f"error={detail['error']}")
        return _clip(f"{code} " + (", ".join(bits) or "no further detail"), limit)

    return _clip(f"{code}: {error}", limit)


def classify_verification_failure() -> str:
    """A deterministic setup/build/test command failed. Always this class;
    kept as a function (not a bare constant use-site) so callers read the
    same way for every failure source in this module."""
    return VERIFICATION_FAILED


def classify_authority(authority_result: str) -> str | None:
    """Map ``nexus.authority.AuthorityResult`` onto the outer taxonomy.

    ``AUTHORIZED`` has no failure to classify (returns ``None``).
    """
    if authority_result == "NEEDS_APPROVAL":
        return AUTHORITY_REQUIRED
    if authority_result == "BLOCKED":
        return POLICY_BLOCKED
    return None


def blocks_coding_retry(failure_class: str) -> bool:
    """True when no corrective *coding* mission should ever be attempted.

    A route already exhausted by run_go, a policy/authority boundary, or an
    unsupported capability is a deterministic terminal block. More coding
    attempts cannot repair any of them.
    """
    return failure_class in _NO_CODING_RETRY


def is_deterministic_worker_infrastructure_failure(error) -> bool:
    """True for a Worker failure classified ENVIRONMENT_FAILURE for a
    deterministic worker-runtime/infrastructure reason (Phase 4.6) --
    narrower than ``blocks_coding_retry``'s outer-class check, since most
    ENVIRONMENT_FAILURE sources (GIT_TOOL_FAILURE, CHECKPOINT_PERSISTENCE_
    FAILED, WORKER_RESULT_MISSING/EMPTY) may plausibly be transient and
    must stay coding-retry-eligible. Reads ``error.detail`` directly, the
    same structured payload ``classify_go_error``/``describe_go_error``
    already use -- no new exception architecture.
    """
    if getattr(error, "code", None) != "WORKER_EXECUTION_FAILED":
        return False
    detail = getattr(error, "detail", None) or {}
    worker = detail.get("worker") or {}
    return worker.get("reason") in _DETERMINISTIC_WORKER_INFRASTRUCTURE_REASONS


def is_transient_like(failure_class: str) -> bool:
    """True when the failure is infrastructure/routing/capability-tier in
    nature: the existing retry/fallback/escalation layer already handled
    what it could before this classification ever runs. The Program's own
    response must be a fresh attempt of the *same* mission, never a
    corrective "fix the code" mission -- there is no known code defect."""
    return failure_class in _TRANSIENT_LIKE
