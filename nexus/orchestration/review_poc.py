import json

from nexus.dispatchers.manager import run_manager
from nexus.dispatchers.review import review_worker
from nexus.orchestration.executor import (
    PlanExecutionError,
    execute_plan,
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
        input_text=TASK,
        intent="GO",
        status="ROUTING",
        risk="LOW",
    )

    print()
    print("NEXUS END-TO-END REVIEW POC")
    print("=" * 70)
    print(f"Run     : {run_id}")
    print(f"Project : {project['name']}")

    manager = run_manager(
        run_id=run_id,
        repo=project["path"],
        task=TASK,
        model="gpt-5.6-luna",
        effort="low",
    )

    if manager["status"] != "COMPLETED":
        update_run_status(run_id, "BLOCKED")
        raise RuntimeError(
            f"Manager planning failed: {manager}"
        )

    plan = manager["plan"]

    update_run_status(
        run_id,
        "RUNNING",
    )

    try:
        workers = execute_plan(
            run_id=run_id,
            repo=project["path"],
            manager_id=manager["manager_id"],
            plan=plan,
        )

    except PlanExecutionError as error:
        update_run_status(run_id, "FAILED")
        raise RuntimeError(str(error)) from error

    update_run_status(
        run_id,
        "REVIEWING",
    )

    if len(workers) != 1:
        raise RuntimeError(
            "Review POC currently expects exactly one Worker."
        )

    worker_result = workers[0]
    worker_plan = plan["workers"][0]

    review_result = review_worker(
        run_id=run_id,
        worker_id=worker_result["agent_id"],
        worktree=worker_result["worktree"],
        original_task=TASK,
        worker_scope=worker_plan["scope"],
        model="gpt-5.6-luna",
        effort="low",
    )

    print()
    print("NEXUS REVIEW RESULT")
    print("=" * 70)

    if review_result["status"] != "COMPLETED":
        update_run_status(
            run_id,
            "BLOCKED",
        )

        print(
            json.dumps(
                review_result,
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    review = review_result["review"]

    print(
        json.dumps(
            review,
            indent=2,
            ensure_ascii=False,
        )
    )

    verdict = review["verdict"]

    if verdict == "PASS":
        update_run_status(
            run_id,
            "COMPLETED",
        )

    elif verdict in {"RETRY", "ESCALATE"}:
        # Escalation engine comes next.
        update_run_status(
            run_id,
            "BLOCKED",
        )

    else:
        update_run_status(
            run_id,
            "BLOCKED",
        )

    print()
    print("FINAL")
    print("=" * 70)
    print(f"Run     : {run_id}")
    print(f"Verdict : {verdict}")


if __name__ == "__main__":
    main()
