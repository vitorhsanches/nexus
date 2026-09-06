from copy import deepcopy

from nexus.dispatchers.review import review_worker
from nexus.orchestration.executor import PlanExecutionError, execute_worker
from nexus.policies.escalation import (
    MAX_SAME_TIER_RETRIES,
    EscalationUnavailable,
    failure_context,
    next_route,
)
from nexus.registry.runs import update_run_status
from nexus.routing.router import (
    InvalidRoutingRequestError,
    NoEligibleRouteError,
)
from nexus.routing.service import (
    AdaptiveRoutingService,
    AdaptiveRoutingUnavailableError,
    InvalidRiskLevelError,
)


INITIAL_ROUTE_CAPABILITY_BY_CLASS = {
    "mechanical": "mechanical",
    "standard-coding": "standard-coding",
    "complex-coding": "advanced-coding",
}


class InitialRoutingUnavailable(RuntimeError):
    """Raised when the initial Worker route cannot be safely resolved."""


def resolve_initial_worker_route(
    planned_worker: dict,
    plan_risk=None,
    routing_service=None,
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
            f"route_class={route_class!r}."
        )

    service = (
        routing_service
        if routing_service is not None
        else AdaptiveRoutingService()
    )

    try:
        decision = service.select_route_for_capability(
            capability,
            risk_level=plan_risk,
        )
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
            f"risk={plan_risk!r}: {error}"
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
) -> dict:
    try:
        current_worker, initial_routing = resolve_initial_worker_route(
            planned_worker=planned_worker,
            plan_risk=plan_risk,
            routing_service=routing_service,
        )
    except InitialRoutingUnavailable as error:
        return {
            "status": "BLOCKED",
            "reason": "INITIAL_ROUTING_UNAVAILABLE",
            "error": str(error),
            "history": [],
        }

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

    history = []
    attempt = 1
    same_tier_retries = 0
    previous_failure = None

    while True:
        update_run_status(
            run_id,
            "RUNNING",
        )

        try:
            worker_result = execute_worker(
                run_id=run_id,
                repo=repo,
                manager_id=manager_id,
                worker=current_worker,
                worker_index=attempt,
                previous_failure=previous_failure,
            )
        except PlanExecutionError as error:
            return {
                "status": "FAILED",
                "reason": "WORKER_EXECUTION_FAILED",
                "history": history,
                "worker": {"status": "FAILED", "error": str(error)},
            }

        if worker_result["status"] != "COMPLETED":
            return {
                "status": "FAILED",
                "reason": "WORKER_EXECUTION_FAILED",
                "history": history,
                "worker": worker_result,
            }

        update_run_status(
            run_id,
            "REVIEWING",
        )

        review_result = review_worker(
            run_id=run_id,
            worker_id=worker_result["agent_id"],
            worktree=worker_result["worktree"],
            original_task=original_task,
            worker_scope=current_worker["scope"],
            risk_level=plan_risk,
        )

        if review_result["status"] != "COMPLETED":
            return {
                "status": "BLOCKED",
                "reason": "REVIEW_FAILED",
                "history": history,
                "review": review_result,
            }

        review = review_result["review"]

        review_routing = review_result.get("routing") or {}

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

        print()
        print("NEXUS → ESCALATION DECISION")
        print("=" * 70)
        print(f"Attempt : {attempt}")
        print(f"Model   : {current_worker['model']}")
        print(f"Verdict : {verdict}")
        print(
            f"Failure : "
            f"{review.get('failure_class')}"
        )

        if verdict == "PASS":
            return {
                "status": "COMPLETED",
                "verdict": "PASS",
                "history": history,
                "worker": worker_result,
                "review": review,
            }

        if verdict == "BLOCKED":
            return {
                "status": "BLOCKED",
                "verdict": "BLOCKED",
                "history": history,
                "review": review,
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
                f"→ {next_worker['model']}"
            )

            current_worker = next_worker
            same_tier_retries = 0
            attempt += 1

            continue

        return {
            "status": "BLOCKED",
            "reason": "UNKNOWN_REVIEW_VERDICT",
            "history": history,
            "review": review,
        }
