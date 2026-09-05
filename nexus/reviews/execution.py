"""Reviewed Task execution service for Nexus Real Review Gate V1.

Drives a single Task through the review/retry/escalation state machine on
top of the existing Task execution pipeline
(``nexus.web.execution.execute_task`` + ``AgentExecutor``). This module
owns exactly one thing: turning a sequence of hold-for-review executions
plus reviewer verdicts into a bounded loop that ends with the Task either
COMPLETED (PASS) or FAILED (BLOCKED / escalation unavailable). It never
selects agents or adapters itself and never creates a second Attempt
registry -- both stay owned by the Task Registry / AgentExecutor."""

from copy import deepcopy
from dataclasses import dataclass
from typing import Optional

from nexus.policies.escalation import (
    ROUTE_LADDERS,
    failure_context,
    route_class_for_policy,
)
from nexus.reviews.policy import decide_next_action
from nexus.reviews.service import (
    apply_escalation_unavailable,
    apply_review_decision,
    apply_review_failure,
    build_review_evidence,
    review_task_attempt,
)
from nexus.tasks import registry as task_registry
from nexus.web import execution as execution_service

# Mechanical-tier capability set mirrors
# nexus.agents.adapters.omniroute.MECHANICAL_CAPABILITIES so route-class
# derivation stays a single, non-duplicated source of truth in spirit;
# imported lazily below to avoid a hard import-time dependency loop.

# Deterministic hard bound on review/retry/escalation iterations for a
# single Task, independent of the same-tier retry budget. Prevents any
# policy bug from causing an unbounded loop.
MAX_REVIEW_ITERATIONS = 8

# Deterministic bound on how much prior-review context text is forwarded
# to the next Worker attempt.
MAX_FAILURE_CONTEXT_CHARS = 4000


class ReviewedTaskFailedError(RuntimeError):
    """Raised when a reviewed Task ends terminally FAILED."""

    def __init__(self, task_id, verdict, reason=None):
        self.task_id = task_id
        self.verdict = verdict
        self.reason = reason
        message = f"Task {task_id!r} failed review (verdict={verdict!r})"
        if reason:
            message += f": {reason}"
        super().__init__(message)


class ReviewLoopExhaustedError(RuntimeError):
    """Raised if a Task exceeds MAX_REVIEW_ITERATIONS (defensive, should
    never trigger given the bounded same-tier retry + ladder policy)."""


@dataclass(slots=True, frozen=True)
class ReviewedTaskResult:
    """Outcome of a full reviewed Task execution."""

    task_id: str
    status: str
    attempts: list
    final_verdict: Optional[str] = None


def _route_class_for_task(task):
    """Resolve the Task's capability ladder from central policy."""
    return route_class_for_policy(
        task.execution_policy
    )


def _initial_route(task):
    """Return the deep-copied first rung of the approved ladder for a Task."""
    route_class = _route_class_for_task(task)
    ladder = ROUTE_LADDERS[route_class]
    route = deepcopy(ladder[0])
    route["route_class"] = route_class
    return route


def _route_override(route):
    return {
        "route_class": route["route_class"],
        "model": route["model"],
        "effort": route["effort"],
    }


def _review_dict(decision):
    return {
        "verdict": decision.verdict,
        "failure_class": decision.failure_class,
        "summary": decision.summary,
        "evidence": list(decision.evidence),
    }


def _refresh_attempt_summary(summary):
    """Refresh a captured execution summary from terminal Attempt state."""
    refreshed = dict(summary)

    attempt_id = refreshed.get("attempt_id")
    if not attempt_id:
        return refreshed

    attempt = task_registry.get_attempt(
        attempt_id
    )

    refreshed["status"] = attempt.status
    refreshed["output"] = attempt.result

    return refreshed


def _build_next_mission_context(mission_context, decision):
    """Fold bounded review feedback into the mission_context for a retry."""
    feedback = failure_context(_review_dict(decision))[:MAX_FAILURE_CONTEXT_CHARS]
    base = dict(mission_context) if isinstance(mission_context, dict) else {}
    base["review_feedback"] = feedback
    return base


def execute_reviewed_task(
    task_id,
    reviewer,
    mission_context=None,
    adapter=None,
    mission=None,
):
    """Execute a Task through the full review/retry/escalation loop.

    Each iteration executes the Task with ``hold_for_review=True`` at the
    current route, asks ``reviewer`` for a verdict over the resulting
    Attempt, then applies that verdict centrally via
    ``apply_review_decision``/``apply_escalation_unavailable``. Returns a
    ReviewedTaskResult on PASS. Raises ``ReviewedTaskFailedError`` when the
    Task ends FAILED (BLOCKED verdict or escalation unavailable), so
    callers (including Mission execution) can fail closed the same way
    they already do for any other Task execution failure.
    """
    current_route = _initial_route(task_registry.get_task(task_id))
    same_tier_retries = 0
    current_mission_context = mission_context
    attempts = []

    for _ in range(MAX_REVIEW_ITERATIONS):
        summary = execution_service.execute_task(
            task_id,
            mission_context=current_mission_context,
            hold_for_review=True,
            route_override=_route_override(current_route),
            adapter=adapter,
        )
        attempts.append(summary)
        attempt_id = summary["attempt_id"]

        task = task_registry.get_task(task_id)
        attempt = task_registry.get_attempt(attempt_id)

        evidence = build_review_evidence(
            mission=mission,
            task=task,
            attempt=attempt,
            worker_output=attempt.result,
            routed_model=attempt.model,
            runtime_context=current_mission_context,
        )
        try:
            decision = review_task_attempt(
                reviewer,
                evidence,
            )
        except Exception as review_error:
            apply_review_failure(
                task_id,
                attempt_id,
                (
                    f"{type(review_error).__name__}: "
                    f"{review_error}"
                ),
            )
            attempts[-1] = _refresh_attempt_summary(
                attempts[-1]
            )
            raise

        next_action = decide_next_action(
            decision,
            current_route,
            same_tier_retries,
        )

        if next_action.action == "COMPLETE":
            apply_review_decision(
                task_id,
                attempt_id,
                decision,
            )
            attempts[-1] = _refresh_attempt_summary(
                attempts[-1]
            )
            return ReviewedTaskResult(
                task_id=task_id,
                status="COMPLETED",
                attempts=attempts,
                final_verdict=decision.verdict,
            )

        if next_action.action == "STOP":
            if next_action.reason and next_action.reason.startswith("ESCALATION_UNAVAILABLE"):
                apply_escalation_unavailable(
                    task_id, attempt_id, decision, next_action.reason
                )
            else:
                apply_review_decision(
                    task_id,
                    attempt_id,
                    decision,
                )

            attempts[-1] = _refresh_attempt_summary(
                attempts[-1]
            )

            raise ReviewedTaskFailedError(
                task_id,
                decision.verdict,
                next_action.reason,
            )

        # RETRY_SAME_TIER or ESCALATE: Task goes back to READY, a new
        # Attempt will be created on the next loop iteration.
        apply_review_decision(
            task_id,
            attempt_id,
            decision,
        )

        attempts[-1] = _refresh_attempt_summary(
            attempts[-1]
        )

        current_mission_context = _build_next_mission_context(
            current_mission_context, decision
        )

        if next_action.action == "RETRY_SAME_TIER":
            same_tier_retries += 1
        elif next_action.action == "ESCALATE":
            current_route = next_action.route
            same_tier_retries = 0

    raise ReviewLoopExhaustedError(
        f"Task {task_id!r} exceeded {MAX_REVIEW_ITERATIONS} review iterations."
    )
