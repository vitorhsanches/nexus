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


def execute_progressively(
    run_id: str,
    repo: str,
    manager_id: str,
    original_task: str,
    planned_worker: dict,
) -> dict:
    current_worker = deepcopy(planned_worker)

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
            model="gpt-5.6-luna",
            effort="low",
        )

        if review_result["status"] != "COMPLETED":
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
