import json

from nexus.dispatchers.manager import run_manager
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
    print("NEXUS MANAGER → WORKER POC")
    print("=" * 70)
    print(f"Run     : {run_id}")
    print(f"Project : {project['name']}")
    print(f"Repo    : {project['path']}")

    manager = run_manager(
        run_id=run_id,
        repo=project["path"],
        task=TASK,
        model="gpt-5.6-luna",
        effort="low",
    )

    if manager["status"] != "COMPLETED":
        update_run_status(run_id, "BLOCKED")

        print()
        print("MANAGER BLOCKED")
        print("=" * 70)
        print(manager)
        return

    plan = manager["plan"]

    print()
    print("APPROVED PLAN")
    print("=" * 70)
    print(
        json.dumps(
            plan,
            indent=2,
            ensure_ascii=False,
        )
    )

    update_run_status(run_id, "RUNNING")

    try:
        workers = execute_plan(
            run_id=run_id,
            repo=project["path"],
            manager_id=manager["manager_id"],
            plan=plan,
        )

    except PlanExecutionError as error:
        update_run_status(run_id, "FAILED")

        print()
        print("PLAN EXECUTION FAILED")
        print("=" * 70)
        print(str(error))
        return

    update_run_status(run_id, "COMPLETED")

    print()
    print("NEXUS EXECUTION RESULT")
    print("=" * 70)
    print(f"Run     : {run_id}")
    print(f"Manager : {manager['manager_id']}")
    print(f"Workers : {len(workers)}")

    for worker in workers:
        print()
        print(f"Worker    : {worker['agent_id']}")
        print(f"Route     : {worker['route_class']}")
        print(f"Status    : {worker['status']}")
        print(f"Exit code : {worker['exit_code']}")
        print(f"Branch    : {worker['branch']}")
        print(f"Worktree  : {worker['worktree']}")


if __name__ == "__main__":
    main()
