import argparse
import sys

from nexus.registry.database import initialize_database
from nexus.registry.projects import list_projects, sync_projects
from nexus.registry.runs import count_active_runs, list_runs
from nexus.registry.agents import count_active_agents, list_agents
from nexus.orchestration.demo import create_demo_run
from nexus.orchestration.go import GoError, run_go
from nexus.orchestration.show import RunNotFoundError, show_run


def _bootstrap() -> None:
    initialize_database()
    sync_projects()


def status_command() -> None:
    projects = list_projects()
    active_runs = count_active_runs()
    active_agents = count_active_agents()

    print()
    print("NEXUS")
    print("=" * 60)
    print(f"Projects      : {len(projects)}")
    print(f"Active runs   : {active_runs}")
    print(f"Active agents : {active_agents}")

    agents = list_agents(active_only=True)

    if agents:
        print()
        print("ACTIVE AGENTS")
        print("-" * 100)
        print(
            f"{'ID':<15} "
            f"{'PROJECT':<15} "
            f"{'ROLE':<12} "
            f"{'MODEL':<35} "
            f"{'STATUS':<12}"
        )
        print("-" * 100)

        for agent in agents:
            print(
                f"{agent['id']:<15} "
                f"{agent['project_name']:<15} "
                f"{agent['role']:<12} "
                f"{agent['model']:<35} "
                f"{agent['status']:<12}"
            )

    print()


def projects_command() -> None:
    projects = list_projects()

    print()
    print("PROJECTS")
    print("-" * 100)

    for project in projects:
        status = "ENABLED" if project["enabled"] else "DISABLED"
        print(
            f"{project['id']:<15} "
            f"{project['name']:<20} "
            f"{status:<10} "
            f"{project['path']}"
        )

    print()


def runs_command() -> None:
    runs = list_runs()

    print()
    print("RUNS")
    print("-" * 100)

    if not runs:
        print("No runs registered.")
        print()
        return

    print(
        f"{'RUN':<15} "
        f"{'PROJECT':<15} "
        f"{'INTENT':<10} "
        f"{'STATUS':<20} "
        f"{'AGENTS':<8}"
    )

    print("-" * 100)

    for run in runs:
        print(
            f"{run['id']:<15} "
            f"{run['project_name']:<15} "
            f"{run['intent']:<10} "
            f"{run['status']:<20} "
            f"{run['agent_count']:<8}"
        )

    print()


def agents_command() -> None:
    agents = list_agents()

    print()
    print("AGENTS")
    print("-" * 120)

    if not agents:
        print("No agents registered.")
        print()
        return

    print(
        f"{'ID':<15} "
        f"{'RUN':<15} "
        f"{'PROJECT':<15} "
        f"{'ROLE':<12} "
        f"{'PROVIDER':<12} "
        f"{'MODEL':<35} "
        f"{'STATUS':<12}"
    )

    print("-" * 120)

    for agent in agents:
        print(
            f"{agent['id']:<15} "
            f"{agent['run_id']:<15} "
            f"{agent['project_name']:<15} "
            f"{agent['role']:<12} "
            f"{agent['provider']:<12} "
            f"{agent['model']:<35} "
            f"{agent['status']:<12}"
        )

    print()


def demo_command() -> None:
    run_id, agent_ids = create_demo_run()

    print()
    print("DEMO CREATED")
    print("=" * 60)
    print(f"Run    : {run_id}")
    print(f"Agents : {len(agent_ids)}")

    for agent_id in agent_ids:
        print(f"  - {agent_id}")

    print()
    print("Run `python -m nexus status` to inspect the simulation.")
    print()


def go_command(request: str, project: str | None = None) -> None:
    try:
        result = run_go(request, project_query=project)

    except GoError as error:
        print()
        print(f"NEXUS GO FAILED: {error.code}")
        print("=" * 60)
        print(str(error))

        if error.run_id:
            print(f"Run : {error.run_id}")

        print()
        sys.exit(1)
        return

    print()
    print("NEXUS GO COMPLETE")
    print("=" * 60)
    print(f"Run    : {result['run_id']}")
    print(f"Status : {result['status']}")
    print()


def show_command(run_id: str) -> None:
    try:
        show_run(run_id)

    except RunNotFoundError as error:
        print()
        print("NEXUS SHOW FAILED: RUN_NOT_FOUND")
        print("=" * 60)
        print(str(error))
        print()
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nexus",
        description="Nexus multi-project agent orchestration control plane.",
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("status", help="Show orchestration overview.")
    subparsers.add_parser("projects", help="List registered projects.")
    subparsers.add_parser("runs", help="List Runs.")
    subparsers.add_parser("agents", help="List Agents.")
    subparsers.add_parser("demo", help="Create a simulated demo Run.")

    go_parser = subparsers.add_parser(
        "go",
        help="Route, plan, and execute a natural-language request.",
    )
    go_parser.add_argument(
        "request",
        help="Complete natural-language request, e.g. 'No Norte corrija o problema X'.",
    )
    go_parser.add_argument(
        "--project",
        dest="project",
        default=None,
        help="Explicit project id, name, or alias to route this request to.",
    )

    show_parser = subparsers.add_parser(
        "show",
        help="Show full detail for a single Run.",
    )
    show_parser.add_argument(
        "run_id",
        help="Run id, e.g. RUN-XXXXXXXX.",
    )

    args = parser.parse_args()

    command = args.command or "status"

    _bootstrap()

    if command == "go":
        go_command(args.request, project=args.project)
        return

    if command == "show":
        show_command(args.run_id)
        return

    commands = {
        "status": status_command,
        "projects": projects_command,
        "runs": runs_command,
        "agents": agents_command,
        "demo": demo_command,
    }

    commands[command]()


if __name__ == "__main__":
    main()
