import json

import nexus.orchestration.progressive as progressive
from nexus.dispatchers.review import review_worker as real_review_worker
from nexus.registry.agents import (
    create_agent,
    update_agent_status,
)
from nexus.registry.database import initialize_database
from nexus.registry.projects import get_project, sync_projects
from nexus.registry.runs import create_run, update_run_status


TASK = """
Fix the bug in calculator.py so all existing tests pass.

Requirements:
- Inspect the relevant implementation and tests.
- Make the smallest correct change.
- Do not modify tests.
- Run the relevant validation.
- Do not commit or publish.
""".strip()


PLANNED_WORKER = {
    "route_class": "mechanical",
    "execution_path": "OMNIROUTE",
    "provider": "omniroute",
    "model": "oc/big-pickle",
    "effort": "low",
    "scope": (
        "Modify only calculator.py so multiply(a, b) returns the product; "
        "do not modify tests, commit, publish, or integrate. "
        "Run python test_calculator.py."
    ),
}


def main() -> None:
    initialize_database()
    sync_projects()

    project = get_project("orchestrator-poc")

    if project is None:
        raise RuntimeError(
            "Project orchestrator-poc is not registered."
        )

    run_id = create_run(
        project_id=project["id"],
        input_text="Progressive escalation POC: big-pickle -> Sonnet",
        intent="GO",
        status="RUNNING",
        risk="LOW",
    )

    coordinator_id = create_agent(
        run_id=run_id,
        role="TestCoordinator",
        provider="nexus",
        model="progressive-poc",
        effort="n/a",
        status="RUNNING",
    )

    review_calls = 0

    def controlled_review_worker(
        run_id: str,
        worker_id: str,
        worktree: str,
        original_task: str,
        worker_scope: str,
        model: str = "gpt-5.6-luna",
        effort: str = "low",
    ) -> dict:
        nonlocal review_calls
        review_calls += 1

        # POC-only deterministic trigger:
        # reject attempt 1 so we can prove automatic escalation.
        if review_calls == 1:
            gate_id = create_agent(
                run_id=run_id,
                role="TestReviewGate",
                provider="nexus",
                model="forced-escalation",
                effort="n/a",
                status="RUNNING",
                parent_agent_id=worker_id,
            )

            update_agent_status(
                gate_id,
                "COMPLETED",
            )

            print()
            print("NEXUS POC → FORCED REVIEW")
            print("=" * 70)
            print("Verdict : ESCALATE")
            print("Failure : CAPABILITY_FAILURE")
            print(
                "Reason  : deterministic POC trigger; "
                "not a real quality judgment."
            )

            return {
                "reviewer_id": gate_id,
                "status": "COMPLETED",
                "exit_code": 0,
                "review": {
                    "verdict": "ESCALATE",
                    "failure_class": "CAPABILITY_FAILURE",
                    "summary": (
                        "Forced escalation for deterministic "
                        "Progressive Capability Escalation POC."
                    ),
                    "evidence": [
                        (
                            "POC deliberately rejects attempt 1 "
                            "to verify automatic routing to the "
                            "next approved capability tier."
                        )
                    ],
                },
            }

        # Attempt 2 uses the real Luna review.
        return real_review_worker(
            run_id=run_id,
            worker_id=worker_id,
            worktree=worktree,
            original_task=original_task,
            worker_scope=worker_scope,
            model=model,
            effort=effort,
        )

    original_review_worker = progressive.review_worker
    progressive.review_worker = controlled_review_worker

    try:
        result = progressive.execute_progressively(
            run_id=run_id,
            repo=project["path"],
            manager_id=coordinator_id,
            original_task=TASK,
            planned_worker=PLANNED_WORKER,
        )

    finally:
        progressive.review_worker = original_review_worker

    if result["status"] == "COMPLETED":
        update_agent_status(
            coordinator_id,
            "COMPLETED",
        )
        update_run_status(
            run_id,
            "COMPLETED",
        )
    elif result["status"] == "FAILED":
        update_agent_status(
            coordinator_id,
            "FAILED",
        )
        update_run_status(
            run_id,
            "FAILED",
        )
    else:
        update_agent_status(
            coordinator_id,
            "BLOCKED",
        )
        update_run_status(
            run_id,
            "BLOCKED",
        )

    print()
    print("NEXUS PROGRESSIVE RESULT")
    print("=" * 70)
    print(f"Run    : {run_id}")
    print(f"Status : {result['status']}")
    print()

    print("ATTEMPT HISTORY")
    print("-" * 70)

    for attempt in result.get("history", []):
        print(
            f"Attempt {attempt['attempt']}: "
            f"{attempt['model']} "
            f"→ {attempt['verdict']} "
            f"({attempt.get('failure_class')})"
        )

    print()
    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
