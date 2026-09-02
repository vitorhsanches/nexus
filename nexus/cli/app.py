import argparse

from nexus.registry.database import initialize_database
from nexus.registry.projects import list_projects, sync_projects
from nexus.registry.runs import count_active_runs, list_runs
from nexus.registry.agents import count_active_agents, list_agents
from nexus.orchestration.demo import create_demo_run


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


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nexus",
        description="Nexus multi-project agent orchestration control plane.",
    )

    parser.add_argument(
        "command",
        nargs="?",
        default="status",
        choices=[
            "status",
            "projects",
            "runs",
            "agents",
            "demo",
        ],
    )

    args = parser.parse_args()

    _bootstrap()

    commands = {
        "status": status_command,
        "projects": projects_command,
        "runs": runs_command,
        "agents": agents_command,
        "demo": demo_command,
    }

    commands[args.command]()


if __name__ == "__main__":
    main()

