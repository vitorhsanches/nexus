import json

from nexus.dispatchers.manager import run_manager
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
    print("NEXUS MANAGER POC")
    print("=" * 70)
    print(f"Run     : {run_id}")
    print(f"Project : {project['name']}")
    print(f"Repo    : {project['path']}")

    result = run_manager(
        run_id=run_id,
        repo=project["path"],
        task=TASK,
        model="gpt-5.6-luna",
        effort="low",
    )

    if result["status"] != "COMPLETED":
        update_run_status(run_id, "BLOCKED")

        print()
        print("MANAGER RESULT")
        print("=" * 70)
        print(f"Status : {result['status']}")
        print(f"Error  : {result.get('error')}")
        return

    update_run_status(run_id, "COMPLETED")

    print()
    print("NEXUS PLAN")
    print("=" * 70)
    print(
        json.dumps(
            result["plan"],
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
