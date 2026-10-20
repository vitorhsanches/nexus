import time
from copy import deepcopy

from nexus.failure_contract import (
    build_failure,
    review_failure,
    sanitize_error_text,
    worker_failure,
)
from nexus.dispatchers.omniroute import LAUNCH_FAILED_REASON
from nexus.dispatchers.review import review_worker
from nexus.orchestration.executor import PlanExecutionError, execute_worker
from nexus.orchestration.js_dependency_prep import format_test_evidence, run_baseline_tests
from nexus.policies.escalation import (
    MAX_SAME_TIER_RETRIES,
    EscalationUnavailable,
    failure_context,
    next_route,
)
from nexus.registry.checkpoints import record_checkpoint
from nexus.registry.runs import update_run_status
from nexus.policies.reasoning_effort import (
    REASONING_FAILURE_CLASSES,
    select_reasoning_effort,
)
from nexus.routing.router import (
    InvalidRoutingRequestError,
    NoEligibleRouteError,
)
from nexus.routing.service import (
    AdaptiveRoutingService,
    AdaptiveRoutingUnavailableError,
    InvalidRiskLevelError,
)
from nexus.telemetry import record_call


INITIAL_ROUTE_CAPABILITY_BY_CLASS = {
    "mechanical": "mechanical",
    "standard-coding": "standard-coding",
    "complex-coding": "advanced-coding",
    "review-critical": "review",
    # security-critical intentionally reuses the "review" capability class:
    # the approved catalog has no separate "security" capability, and no
    # new model/capability is invented to create one. The existing
    # HIGH-risk-qualified review route (cc/claude-sonnet-5-high) is the
    # correct, already-approved authority for this class too.
    "security-critical": "review",
}

# Maps route_class to cognitive complexity for reasoning effort policy.
# Mechanical tasks are LOW; standard coding is MEDIUM; complex/security are HIGH.
_ROUTE_CLASS_COMPLEXITY = {
    "mechanical": "LOW",
    "standard-coding": "MEDIUM",
    "complex-coding": "HIGH",
    "review-critical": "MEDIUM",
    "security-critical": "HIGH",
}


def _effort_rank(effort: str) -> int:
    """Return the numeric rank of an effort level (higher = more compute)."""
    return {"low": 0, "medium": 1, "high": 2}.get(effort, 0)


class InitialRoutingUnavailable(RuntimeError):
    """Raised when the initial Worker route cannot be safely resolved.

    Carries structured diagnostic context (route_class/capability/risk and
    the originating exception's type) so callers and durable checkpoints
    can preserve the real router cause instead of collapsing it to a bare
    reason code. Never carries credentials or telemetry secrets --
    nexus.routing.telemetry never places those in exception messages, and
    this class only stores the already-sanitized values used to build the
    routing request.
    """

    def __init__(
        self,
        message,
        *,
        route_class=None,
        capability=None,
        risk=None,
        cause=None,
    ):
        super().__init__(message)
        self.route_class = route_class
        self.capability = capability
        self.risk = risk
        self.cause_type = type(cause).__name__ if cause is not None else None
        self.route_rejections = tuple(getattr(cause, "rejections", ()))


def _checkpoint_route_context(worker: dict) -> dict:
    """Return durable execution-route fields for checkpoint payloads."""

    return {
        "route_class": worker.get("route_class"),
        "model": worker.get("model"),
        "provider": worker.get("provider"),
        "effort": worker.get("effort"),
        "execution_path": worker.get("execution_path"),
    }


def _launch_failure_checkpoint_fields(worker_result: dict, worker_ordinal: int) -> dict:
    """Safe launch diagnostics for a WORKER_ATTEMPT checkpoint, else empty.

    The fields are the bounded, redacted values produced by
    run_omniroute_worker(); no environment or auth data reaches this point.
    """

    if worker_result.get("reason") != LAUNCH_FAILED_REASON:
        return {}

    return {
        "reason": LAUNCH_FAILED_REASON,
        "phase": worker_result.get("phase"),
        "exception_type": worker_result.get("exception_type"),
        "error": worker_result.get("error"),
        "worker_ordinal": worker_ordinal,
    }


def resolve_initial_worker_route(
    planned_worker: dict,
    plan_risk=None,
    routing_service=None,
    exclude_models: frozenset[str] = frozenset(),
    exclude_providers: frozenset[str] = frozenset(),
) -> tuple[dict, dict]:
    """Resolve the initial Worker route through the Adaptive Router.

    The Manager remains responsible for decomposition, route_class and risk.
    Its provider/model/effort fields are advisory only for the initial
    execution and are replaced by the route selected by Nexus policy.

    This function runs once per progressive Worker lifecycle. RETRY and
    ESCALATE continue from the resolved Worker and therefore preserve the
    existing explicit escalation semantics.
    """

    worker = deepcopy(planned_worker)

    route_class = worker.get("route_class")
    capability = INITIAL_ROUTE_CAPABILITY_BY_CLASS.get(route_class)

    if capability is None:
        raise InitialRoutingUnavailable(
            "No executable adaptive capability mapping exists for "
            f"route_class={route_class!r}.",
            route_class=route_class,
            risk=plan_risk,
        )

    service = (
        routing_service
        if routing_service is not None
        else AdaptiveRoutingService()
    )

    try:
        route_kwargs = dict(risk_level=plan_risk, blocked_models=exclude_models)
        if exclude_providers:
            route_kwargs["blocked_providers"] = exclude_providers
        decision = service.select_route_for_capability(capability, **route_kwargs)
    except (
        AdaptiveRoutingUnavailableError,
        InvalidRiskLevelError,
        InvalidRoutingRequestError,
        NoEligibleRouteError,
    ) as error:
        raise InitialRoutingUnavailable(
            "Initial adaptive routing failed for "
            f"route_class={route_class!r}, "
            f"capability={capability!r}, "
            f"risk={plan_risk!r}: {error}",
            route_class=route_class,
            capability=capability,
            risk=plan_risk,
            cause=error,
        ) from error

    worker.update(
        {
            "execution_path": decision.execution_path,
            "provider": decision.provider,
            "model": decision.model,
            "effort": decision.effort,
        }
    )

    routing = {
        "capability": capability,
        "risk": plan_risk,
        "model": decision.model,
        "provider": decision.provider,
        "effort": decision.effort,
        "execution_path": decision.execution_path,
        "reason": decision.reason,
        "degraded": decision.degraded,
    }

    return worker, routing


def execute_progressively(
    run_id: str,
    repo: str,
    manager_id: str,
    original_task: str,
    planned_worker: dict,
    plan_risk=None,
    routing_service=None,
    worker_ordinal: int | None = None,
    resume_state: dict | None = None,
    dependency_context: str | None = None,
    exclude_models: frozenset[str] = frozenset(),
    run_js_tests: bool = False,
) -> dict:
    """Execute (or resume) one Worker's progressive lifecycle.

    ``run_js_tests``, when True (opt-in JS dependency readiness; see
    nexus.orchestration.integration.run_integrated_plan's
    ``prepare_dependencies``), runs the project's declared test command
    once against ``repo`` -- the Worker's own sub-worktree, after the
    Worker's changes, on the host -- immediately before each Reviewer
    call. This gives the Reviewer independent TEST_STATUS evidence
    without requiring the Reviewer's own read-only sandbox to execute
    project tests itself. Default False preserves existing behavior
    exactly: no test command is ever run, and review_worker() is called
    with no test evidence, same as before this parameter existed.

    ``resume_state``, when provided, lets an existing Run resume without
    re-resolving the initial adaptive route or re-recording the
    ``EXECUTION_START`` checkpoint. Supported keys:

    - ``current_worker`` (required): the fully-resolved route/worker dict
      to continue from (as previously recorded on durable checkpoints).
    - ``attempt``, ``same_tier_retries``, ``previous_failure``, ``history``:
      resumed loop state.
    - ``pending_worker_result``: when present, the loop's first iteration
      reuses this already-COMPLETED Worker result instead of calling
      ``execute_worker`` again -- the existing Worker Agent/worktree is
      never re-executed; only review is retried.
    """

    if resume_state is not None:
        current_worker = resume_state["current_worker"]
        history = resume_state.get("history", [])
        attempt = resume_state.get("attempt", 1)
        same_tier_retries = resume_state.get("same_tier_retries", 0)
        previous_failure = resume_state.get("previous_failure")
        pending_worker_result = resume_state.get("pending_worker_result")
        pending_review_result = resume_state.get("pending_review_result")
        # Phase 4.2: models already tried in this Worker lifecycle (initial
        # route + any transient fallback already taken), so a resumed run
        # never blindly re-selects one of them. Falls back to just the
        # resumed route when an older checkpoint predates this field.
        tried_models = set(
            resume_state.get("tried_models") or [current_worker.get("model")]
        )
    else:
        try:
            current_worker, initial_routing = resolve_initial_worker_route(
                planned_worker=planned_worker,
                plan_risk=plan_risk,
                routing_service=routing_service,
                exclude_models=exclude_models,
            )
        except InitialRoutingUnavailable as error:
            return {
                "status": "BLOCKED",
                "reason": "INITIAL_ROUTING_UNAVAILABLE",
                "error": str(error),
                "error_type": error.cause_type,
                "route_class": error.route_class,
                "capability": error.capability,
                "risk": error.risk,
                "route_rejections": error.route_rejections,
                "history": [],
            }

        # Phase 4.2: track every model this Worker lifecycle has already
        # tried, so a same-tier-retry-exhausted transient failure can fall
        # back to a genuinely different eligible route instead of blindly
        # reselecting the one that just failed twice.
        tried_models = {current_worker["model"]}
        exhausted_providers: set[str] = set()

        print()
        print("NEXUS ? INITIAL ADAPTIVE ROUTE")
        print("=" * 70)
        print(f"Class      : {current_worker['route_class']}")
        print(f"Capability : {initial_routing['capability']}")
        print(f"Risk       : {plan_risk}")
        print(f"Model      : {current_worker['model']}")
        print(f"Provider   : {current_worker['provider']}")
        print(f"Effort     : {current_worker['effort']}")
        print(f"Path       : {current_worker['execution_path']}")
        print(f"Degraded   : {initial_routing['degraded']}")

        record_checkpoint(
            run_id=run_id,
            boundary="EXECUTION_START",
            payload={
                **_checkpoint_route_context(current_worker),
                "manager_id": manager_id,
                "capability": initial_routing["capability"],
                "risk": plan_risk,
                "degraded": initial_routing["degraded"],
                "execution_identity_source": "adaptive_router",
            },
            worker_ordinal=worker_ordinal,
        )

        history = []
        attempt = 1
        same_tier_retries = 0
        previous_failure = None
        pending_worker_result = None
        pending_review_result = None

    while True:
        update_run_status(
            run_id,
            "RUNNING",
        )

        if pending_review_result is not None:
            worker_result = pending_review_result["worker_result"]
            review_result = pending_review_result["review_result"]
            pending_review_result = None
            review_checkpoint_already_persisted = True
        else:
            review_checkpoint_already_persisted = False
            if pending_worker_result is not None:
                worker_result = pending_worker_result
                pending_worker_result = None
            else:
                try:
                    worker_result = execute_worker(
                        run_id=run_id,
                        repo=repo,
                        manager_id=manager_id,
                        worker=current_worker,
                        worker_index=attempt,
                        previous_failure=previous_failure,
                        dependency_context=dependency_context,
                    )
                    record_call(
                        run_id, role="WORKER", model=current_worker.get("model"),
                        provider=current_worker.get("provider"),
                        route_class=current_worker.get("route_class"),
                        capability=initial_routing.get("capability") if 'initial_routing' in locals() else None,
                        attempt_number=attempt,
                        reason="RETRY" if attempt > 1 else "INITIAL",
                        result=worker_result.get("status"),
                        usage=worker_result.get("usage"),
                    )
                except (PlanExecutionError, UnicodeEncodeError) as error:
                    record_checkpoint(
                        run_id=run_id,
                        boundary="WORKER_ATTEMPT",
                        payload={
                            **_checkpoint_route_context(current_worker),
                            "manager_id": manager_id,
                            "attempt": attempt,
                            "status": "FAILED",
                            "error": sanitize_error_text(error),
                            "failure": build_failure(
                                stage="EXECUTION",
                                code="WORKER_EXECUTION_FAILED",
                                message=str(error),
                                worker_ordinal=worker_ordinal,
                            ),
                        },
                        worker_ordinal=worker_ordinal,
                    )
                    return {
                        "status": "FAILED",
                        "reason": "WORKER_EXECUTION_FAILED",
                        "history": history,
                        "worker": {"status": "FAILED", "reason": "WORKER_LAUNCH_ENCODING_FAILURE" if isinstance(error, UnicodeEncodeError) else None, "error": str(error)},
                    }

                if worker_result["status"] != "COMPLETED":
                    # A subprocess-level failure (non-zero exit from the
                    # OmniRoute/codex CLI call) never reaches the reviewer,
                    # so it previously could never trigger the same-tier
                    # retry that a reviewer RETRY verdict gets below. When
                    # the failure looks transient (provider/proxy admission
                    # shedding, rate limiting, dropped connection -- see
                    # _looks_transient in dispatchers/omniroute.py) retry
                    # the same capability tier, reusing the existing
                    # same-tier retry budget. A failure with no transient
                    # signal (e.g. a real crash) still hard-fails below.
                    is_transient = (
                        worker_result.get("reason")
                        == "TRANSIENT_PROVIDER_ERROR"
                    )
                    if (
                        is_transient
                        and same_tier_retries < MAX_SAME_TIER_RETRIES
                    ):
                        same_tier_retries += 1
                        attempt += 1
                        record_checkpoint(
                            run_id=run_id,
                            boundary="WORKER_ATTEMPT",
                            payload={
                                **_checkpoint_route_context(current_worker),
                                "manager_id": manager_id,
                                "attempt": attempt - 1,
                                "status": worker_result["status"],
                                "reason": "TRANSIENT_PROVIDER_ERROR",
                                "decision": "RETRY_SAME_TIER",
                                "worker_id": worker_result.get("agent_id"),
                                "worktree": worker_result.get("worktree"),
                            },
                            worker_ordinal=worker_ordinal,
                        )
                        backoff = 30 * same_tier_retries  # 30s, 60s, ...
                        print(
                            f"Decision: transient provider failure -- "
                            f"retry same capability tier after {backoff}s backoff."
                        )
                        time.sleep(backoff)
                        continue

                    is_quota_exhausted = worker_result.get("reason") == "QUOTA_EXHAUSTED"
                    if is_transient or is_quota_exhausted:
                        if is_quota_exhausted:
                            exhausted_providers.add(current_worker["provider"])
                        # Phase 4.2: the same-tier retry budget for THIS
                        # route is exhausted, but retry != fallback --
                        # provider unavailability is not a coding defect,
                        # so fall back to a different eligible route for
                        # the SAME capability (reusing the exact
                        # blocked_models exclusion mechanism Phase 4.1
                        # added) instead of hard-failing straight to
                        # Program. Still cost-aware ranked among whatever
                        # remains eligible; never a hardcoded model.
                        tried_models.add(current_worker["model"])
                        record_checkpoint(
                            run_id=run_id,
                            boundary="WORKER_ATTEMPT",
                            payload={
                                **_checkpoint_route_context(current_worker),
                                "manager_id": manager_id,
                                "attempt": attempt,
                                "status": worker_result["status"],
                                "reason": worker_result.get("reason"),
                                "decision": "FALLBACK_ROUTE_ATTEMPT",
                                "worker_id": worker_result.get("agent_id"),
                                "worktree": worker_result.get("worktree"),
                                "tried_models": sorted(tried_models),
                            },
                            worker_ordinal=worker_ordinal,
                        )
                        try:
                            fallback_kwargs = dict(
                                planned_worker=planned_worker,
                                plan_risk=plan_risk,
                                routing_service=routing_service,
                                exclude_models=frozenset(tried_models) | exclude_models,
                            )
                            if exhausted_providers:
                                fallback_kwargs["exclude_providers"] = frozenset(exhausted_providers)
                            fallback_worker, fallback_routing = resolve_initial_worker_route(**fallback_kwargs)
                        except InitialRoutingUnavailable as fallback_error:
                            # Every eligible route for this capability has
                            # now either failed or is excluded: a truthful
                            # infrastructure/provider blocker, never a
                            # coding correction, never an infinite retry.
                            record_checkpoint(
                                run_id=run_id,
                                boundary="WORKER_ATTEMPT",
                                payload={
                                    **_checkpoint_route_context(current_worker),
                                    "manager_id": manager_id,
                                    "attempt": attempt,
                                    "status": "BLOCKED",
                                    "reason": "INITIAL_ROUTING_UNAVAILABLE",
                                    "decision": "FALLBACK_EXHAUSTED",
                                    "tried_models": sorted(tried_models),
                                    "error": str(fallback_error),
                                },
                                worker_ordinal=worker_ordinal,
                            )
                            return {
                                "status": "BLOCKED",
                                "reason": "INITIAL_ROUTING_UNAVAILABLE",
                                "error": str(fallback_error),
                                "error_type": fallback_error.cause_type,
                                "route_class": fallback_error.route_class,
                                "capability": fallback_error.capability,
                                "risk": fallback_error.risk,
                                "route_rejections": fallback_error.route_rejections,
                                "history": history,
                            }

                        print(
                            f"Decision: provider route {current_worker['model']} "
                            f"unavailable -- falling back to {fallback_worker['model']}."
                        )
                        current_worker = fallback_worker
                        initial_routing = fallback_routing
                        same_tier_retries = 0
                        attempt += 1
                        continue

                    record_checkpoint(
                        run_id=run_id,
                        boundary="WORKER_ATTEMPT",
                        payload={
                            **_checkpoint_route_context(current_worker),
                            "manager_id": manager_id,
                            "attempt": attempt,
                            "status": worker_result["status"],
                            "worker_id": worker_result.get("agent_id"),
                            "worktree": worker_result.get("worktree"),
                            # Launch diagnostics appear only for a Worker that
                            # never started; other attempts are unchanged.
                            **_launch_failure_checkpoint_fields(
                                worker_result, worker_ordinal
                            ),
                            "failure": worker_failure(
                                {
                                    **worker_result,
                                    "model": current_worker.get("model"),
                                    "provider": current_worker.get("provider"),
                                },
                                worker_ordinal,
                            ),
                        },
                        worker_ordinal=worker_ordinal,
                    )
                    return {
                        "status": "FAILED",
                        "reason": "WORKER_EXECUTION_FAILED",
                        "history": history,
                        "worker": {
                            **worker_result,
                            "model": current_worker.get("model"),
                            "provider": current_worker.get("provider"),
                        },
                    }

                record_checkpoint(
                    run_id=run_id,
                    boundary="WORKER_ATTEMPT",
                    payload={
                        **_checkpoint_route_context(current_worker),
                        "manager_id": manager_id,
                        "attempt": attempt,
                        "status": "COMPLETED",
                        "worker_id": worker_result.get("agent_id"),
                        "worktree": worker_result.get("worktree"),
                    },
                    worker_ordinal=worker_ordinal,
                )

            update_run_status(
                run_id,
                "REVIEWING",
            )

            # Opt-in JS dependency readiness: run the project's declared
            # test command once against this Worker's own sub-worktree
            # (post-change, host-run) so the Reviewer gets independent
            # TEST_STATUS evidence instead of needing to execute tests
            # itself inside its read-only sandbox. TEST_STATUS is recorded
            # as its own checkpoint fact, deliberately separate from
            # whatever REVIEW_VERDICT the Reviewer reaches below.
            test_evidence = None
            if run_js_tests:
                test_result = run_baseline_tests(repo)
                test_evidence = format_test_evidence(test_result)
                record_checkpoint(
                    run_id=run_id,
                    boundary="JS_TEST_EVIDENCE",
                    payload={
                        "command": test_result["command"],
                        "exit_code": test_result["exit_code"],
                        "timed_out": test_result["timed_out"],
                        "passed": test_result["passed"],
                        "output_tail": test_result["output_tail"][-2000:],
                    },
                    worker_ordinal=worker_ordinal,
                )

            # Phase 4.3D: pass the Manager contract and Worker semantic
            # output to the reviewer. All three are optional; callers that
            # predate 4.3D (tests, etc.) do not need to be updated.
            review_result = review_worker(
                run_id=run_id,
                worker_id=worker_result["agent_id"],
                worktree=worker_result["worktree"],
                original_task=original_task,
                worker_scope=current_worker["scope"],
                risk_level=plan_risk,
                deliverable=current_worker.get("deliverable"),
                acceptance_criteria=current_worker.get("acceptance_criteria"),
                test_evidence=test_evidence,
                worker_output=worker_result.get("output"),
            )
            review_routing = review_result.get("routing") or {}
            record_call(
                run_id, role="REVIEWER", model=review_routing.get("model"),
                provider=review_routing.get("provider"),
                execution_strategy=None, attempt_number=attempt,
                result=review_result.get("status"), usage=review_result.get("usage"),
            )

        review_routing = review_result.get("routing") or {}

        if review_result["status"] != "COMPLETED":
            record_checkpoint(
                run_id=run_id,
                boundary="REVIEW",
                payload={
                    **_checkpoint_route_context(current_worker),
                    "attempt": attempt,
                    "status": review_result["status"],
                    "worker_id": worker_result.get("agent_id"),
                    "reviewer_id": review_result.get("reviewer_id"),
                    "reviewer_model": review_routing.get("model"),
                    "reviewer_provider": review_routing.get("provider"),
                    "reviewer_effort": review_routing.get("effort"),
                    "reviewer_execution_path": review_routing.get(
                        "execution_path"
                    ),
                    "reviewer_routing_reason": review_routing.get("reason"),
                    "reviewer_degraded": review_routing.get("degraded"),
                    "error": review_result.get("error"),
                    "failure": review_failure(
                        "REVIEW_FAILED",
                        review_result,
                        reviewer_id=review_result.get("reviewer_id"),
                        reviewer_model=review_routing.get("model"),
                        reviewer_provider=review_routing.get("provider"),
                        worker_ordinal=worker_ordinal,
                            worktree=worker_result.get("worktree"),
                    ),
                },
                worker_ordinal=worker_ordinal,
            )
            return {
                "status": "BLOCKED",
                "reason": "REVIEW_FAILED",
                "history": history,
                "review": review_result,
            }

        review = review_result["review"]

        history.append(
            {
                "attempt": attempt,
                "worker_id": worker_result["agent_id"],
                "model": current_worker["model"],
                "worktree": worker_result["worktree"],
                "reviewer_id": review_result["reviewer_id"],
                "reviewer_model": review_routing.get("model"),
                "reviewer_provider": review_routing.get("provider"),
                "reviewer_effort": review_routing.get("effort"),
                "reviewer_execution_path": review_routing.get(
                    "execution_path"
                ),
                "reviewer_routing_reason": review_routing.get("reason"),
                "reviewer_degraded": review_routing.get("degraded"),
                "verdict": review["verdict"],
                "failure_class": review.get(
                    "failure_class"
                ),
                "summary": review["summary"],
            }
        )

        verdict = review["verdict"]

        if not review_checkpoint_already_persisted:
            record_checkpoint(
                run_id=run_id,
                boundary="REVIEW",
                payload={
                    **_checkpoint_route_context(current_worker),
                    "attempt": attempt,
                    "status": "COMPLETED",
                    "worker_id": worker_result.get("agent_id"),
                    "reviewer_id": review_result.get("reviewer_id"),
                    "reviewer_model": review_routing.get("model"),
                    "reviewer_provider": review_routing.get("provider"),
                    "reviewer_effort": review_routing.get("effort"),
                    "reviewer_execution_path": review_routing.get(
                        "execution_path"
                    ),
                    "reviewer_routing_reason": review_routing.get("reason"),
                    "reviewer_degraded": review_routing.get("degraded"),
                    "verdict": verdict,
                    "failure_class": review.get("failure_class"),
                    "summary": review.get("summary"),
                    **(
                        {
                            "failure": review_failure(
                                f"REVIEW_{verdict}",
                                review,
                                reviewer_id=review_result.get("reviewer_id"),
                                reviewer_model=review_routing.get("model"),
                                reviewer_provider=review_routing.get("provider"),
                                worker_ordinal=worker_ordinal,
                                worktree=worker_result.get("worktree"),
                            )
                        }
                        if verdict != "PASS"
                        else {}
                    ),
                },
                worker_ordinal=worker_ordinal,
            )

        print()
        print("NEXUS -> ESCALATION DECISION")
        print("=" * 70)
        print(f"Attempt : {attempt}")
        print(f"Model   : {current_worker['model']}")
        print(f"Verdict : {verdict}")
        print(
            f"Failure : "
            f"{review.get('failure_class')}"
        )

        if verdict == "PASS":
            record_checkpoint(
                run_id=run_id,
                boundary="LIFECYCLE",
                payload={
                    **_checkpoint_route_context(current_worker),
                    "attempt": attempt,
                    "verdict": "PASS",
                    "worker_id": worker_result.get("agent_id"),
                    "reviewer_id": review_result.get("reviewer_id"),
                },
                worker_ordinal=worker_ordinal,
            )
            return {
                "status": "COMPLETED",
                "verdict": "PASS",
                "history": history,
                "worker": worker_result,
                "review": review,
            }

        if verdict == "BLOCKED":
            record_checkpoint(
                run_id=run_id,
                boundary="LIFECYCLE",
                payload={
                    **_checkpoint_route_context(current_worker),
                    "attempt": attempt,
                    "verdict": "BLOCKED",
                    "worker_id": worker_result.get("agent_id"),
                    "reviewer_id": review_result.get("reviewer_id"),
                },
                worker_ordinal=worker_ordinal,
            )
            return {
                "status": "BLOCKED",
                "verdict": "BLOCKED",
                "history": history,
                "review": review,
                # Phase 4.1 hardening: the exact route this Worker used, so
                # a CAPABILITY_FAILURE-classified caller (Program) can
                # exclude it from the SAME capability's next route
                # selection via the existing blocked_models routing gate,
                # instead of relying only on natural-language guidance.
                "worker_route": _checkpoint_route_context(current_worker),
            }

        previous_failure = failure_context(
            review
        )

        if verdict == "RETRY":
            if (
                same_tier_retries
                < MAX_SAME_TIER_RETRIES
            ):
                same_tier_retries += 1
                attempt += 1

                # Phase B: effort escalation on reasoning failures (RETRY path).
                # Model stays the same (same tier); effort may increase if the
                # failure class indicates insufficient reasoning depth.
                _retry_failure_class = review.get("failure_class")
                _retry_effort_decision = select_reasoning_effort(
                    role="WORKER",
                    complexity=_ROUTE_CLASS_COMPLEXITY.get(
                        current_worker.get("route_class"), "MEDIUM"
                    ),
                    risk=plan_risk,
                    route_class=current_worker.get("route_class"),
                    previous_effort=current_worker.get("effort", "low"),
                    failure_class=_retry_failure_class,
                )
                if _effort_rank(_retry_effort_decision.effort) > _effort_rank(
                    current_worker.get("effort", "low")
                ):
                    current_worker["effort"] = _retry_effort_decision.effort

                record_checkpoint(
                    run_id=run_id,
                    boundary="LIFECYCLE",
                    payload={
                        **_checkpoint_route_context(current_worker),
                        "attempt": attempt - 1,
                        "verdict": "RETRY",
                        "worker_id": worker_result.get("agent_id"),
                        "reviewer_id": review_result.get("reviewer_id"),
                        "next_attempt": attempt,
                        "reasoning_effort_reason": _retry_effort_decision.reason,
                    },
                    worker_ordinal=worker_ordinal,
                )

                print(
                    "Decision: retry same capability tier."
                )

                continue

            print(
                "Same-tier retry budget exhausted. "
                "Escalating capability."
            )

        if verdict in {
            "ESCALATE",
            "RETRY",
        }:
            try:
                next_worker = next_route(
                    current_worker
                )

            except EscalationUnavailable as error:
                record_checkpoint(
                    run_id=run_id,
                    boundary="LIFECYCLE",
                    payload={
                        **_checkpoint_route_context(current_worker),
                        "attempt": attempt,
                        "verdict": verdict,
                        "worker_id": worker_result.get("agent_id"),
                        "reviewer_id": review_result.get("reviewer_id"),
                        "reason": "ESCALATION_UNAVAILABLE",
                        "error": str(error),
                    },
                    worker_ordinal=worker_ordinal,
                )
                return {
                    "status": "BLOCKED",
                    "verdict": verdict,
                    "reason": (
                        "ESCALATION_UNAVAILABLE"
                    ),
                    "error": str(error),
                    "history": history,
                    "review": review,
                }

            print(
                f"Escalation: "
                f"{current_worker['model']} "
                f"-> {next_worker['model']}"
            )

            # Phase B: effort escalation on reasoning failures (ESCALATE path).
            # next_route() handles model escalation; also apply effort
            # escalation when the failure class indicates insufficient
            # reasoning depth -- take the max of next_worker's catalog effort
            # and the policy-computed escalated effort so we never downgrade
            # an already-elevated effort from the ladder.
            _escalate_failure_class = review.get("failure_class")
            _escalate_prev_effort = current_worker.get("effort", "low")
            _escalate_effort_decision = select_reasoning_effort(
                role="WORKER",
                complexity=_ROUTE_CLASS_COMPLEXITY.get(
                    next_worker.get("route_class"), "MEDIUM"
                ),
                risk=plan_risk,
                route_class=next_worker.get("route_class"),
                previous_effort=_escalate_prev_effort,
                failure_class=_escalate_failure_class,
            )
            _policy_effort = _escalate_effort_decision.effort
            _ladder_effort = next_worker.get("effort", "low")
            if _effort_rank(_policy_effort) > _effort_rank(_ladder_effort):
                next_worker["effort"] = _policy_effort

            current_worker = next_worker
            same_tier_retries = 0
            attempt += 1

            record_checkpoint(
                run_id=run_id,
                boundary="LIFECYCLE",
                payload={
                    **_checkpoint_route_context(current_worker),
                    "attempt": attempt - 1,
                    "verdict": "ESCALATE",
                    "worker_id": worker_result.get("agent_id"),
                    "reviewer_id": review_result.get("reviewer_id"),
                    "next_attempt": attempt,
                    "next_model": current_worker["model"],
                    "next_provider": current_worker.get("provider"),
                    "next_effort": current_worker.get("effort"),
                    "next_execution_path": current_worker.get(
                        "execution_path"
                    ),
                    "reasoning_effort_reason": _escalate_effort_decision.reason,
                },
                worker_ordinal=worker_ordinal,
            )

            continue

        record_checkpoint(
            run_id=run_id,
            boundary="LIFECYCLE",
            payload={
                **_checkpoint_route_context(current_worker),
                "attempt": attempt,
                "verdict": "UNKNOWN",
                "worker_id": worker_result.get("agent_id"),
                "reviewer_id": review_result.get("reviewer_id"),
            },
            worker_ordinal=worker_ordinal,
        )

        return {
            "status": "BLOCKED",
            "reason": "UNKNOWN_REVIEW_VERDICT",
            "history": history,
            "review": review,
        }
