import argparse
import sys
from pathlib import Path

from nexus.registry.database import initialize_database
from nexus.registry.projects import list_projects, sync_projects
from nexus.registry.runs import count_active_runs, list_runs
from nexus.registry.agents import count_active_agents, list_agents
from nexus.orchestration.demo import create_demo_run
from nexus.orchestration.go import GoError, run_go
from nexus.orchestration.recovery import recover_all
from nexus.orchestration.resume import ResumeError, resume_run
from nexus.orchestration.show import RunNotFoundError, show_run
from nexus.intake import UnsupportedTaskError, dispatch_request
from nexus.program.bootstrap import BootstrapError
from nexus.program.runner import ProgramBlockedError, resume_program, start_program
from nexus.program.state import (
    ProgramNotFoundError,
    ProgramOwnershipError,
    list_programs,
)


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


def go_command(
    request: str, project: str | None = None, prepare_dependencies: bool = False
) -> None:
    try:
        result = run_go(
            request, project_query=project, prepare_dependencies=prepare_dependencies
        )

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


def run_command(args) -> None:
    try:
        dispatched = dispatch_request(
            args.request,
            project=args.project,
            requirements_path=args.requirements,
            name=args.name,
            workspace_root=Path(args.workspace) if args.workspace else None,
            autonomy=args.autonomy,
        )
    except (
        UnsupportedTaskError, ProgramBlockedError, ProgramOwnershipError,
        BootstrapError, GoError, OSError,
    ) as error:
        print()
        print(f"NEXUS RUN FAILED: {type(error).__name__}")
        print("=" * 60)
        print(str(error))
        print()
        sys.exit(1)
        return

    result = dispatched["result"]
    identity = result.get("program_id") or result.get("run_id")
    print()
    print("NEXUS RUN")
    print("=" * 60)
    print(f"Type   : {dispatched['task_contract']['task_type']}")
    print(f"Mode   : {dispatched['execution_mode']}")
    print(f"ID     : {identity}")
    print(f"Status : {result['status']}")
    failure_class = result.get("failure_class")
    if failure_class:
        print(f"Failure: {failure_class}")
    acceptance_criteria = result.get("acceptance")
    if acceptance_criteria:
        satisfied = sum(1 for c in acceptance_criteria if c["status"] == "SATISFIED")
        print(f"Accept : {satisfied}/{len(acceptance_criteria)} criteria satisfied")
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


def resume_command(run_id: str) -> None:
    try:
        result = resume_run(run_id)

    except ResumeError as error:
        print()
        print(f"NEXUS RESUME FAILED: {error.code}")
        print("=" * 60)
        print(str(error))

        if error.run_id:
            print(f"Run : {error.run_id}")

        print()
        sys.exit(1)
        return

    print()
    print("NEXUS RESUME COMPLETE")
    print("=" * 60)
    print(f"Run    : {result['run_id']}")
    print(f"Status : {result['status']}")
    print()


def build_command(args) -> None:
    try:
        if args.resume:
            program = resume_program(args.resume)
        else:
            if not args.name or not args.requirements:
                print("nexus build needs --name and a requirements file (or --resume).")
                sys.exit(2)
            requirements = Path(args.requirements).read_text(encoding="utf-8-sig")
            program = start_program(
                args.name,
                requirements,
                workspace_root=Path(args.workspace) if args.workspace else None,
                stop_after_spec=args.spec_only,
            )
    except (
        ProgramBlockedError, ProgramOwnershipError, ProgramNotFoundError,
        BootstrapError, OSError,
    ) as error:
        print()
        print(f"NEXUS BUILD FAILED: {type(error).__name__}")
        print("=" * 60)
        print(str(error))
        program_id = getattr(error, "program_id", None)
        if program_id:
            print(f"Program : {program_id}")
            print(f"Resume  : python -m nexus build --resume {program_id}")
        print()
        sys.exit(1)
        return

    print()
    print("NEXUS BUILD")
    print("=" * 60)
    print(f"Program : {program['program_id']}")
    print(f"Status  : {program['status']}")
    print(f"Repo    : {program['path']}")
    print()


def programs_command() -> None:
    print()
    print("PROGRAMS")
    print("=" * 60)
    for program in list_programs():
        phases = program.get("phases") or []
        done = sum(1 for phase in phases if phase["status"] == "DONE")
        print(
            f"{program['program_id']}  {program['status']:<11} "
            f"{done}/{len(phases)} phases  {program['name']}  {program['path']}"
        )
    print()


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

    run_parser = subparsers.add_parser(
        "run",
        help="Normalize and dispatch a general request as a Task or Program.",
    )
    run_parser.add_argument("request", help="Natural-language goal.")
    run_parser.add_argument("--project", default=None)
    run_parser.add_argument(
        "--requirements",
        help="Requirements file for a complete software-project request.",
    )
    run_parser.add_argument("--name", help="Program/project name.")
    run_parser.add_argument("--workspace", help="Parent directory for a new Program.")
    run_parser.add_argument(
        "--autonomy",
        choices=("local",),
        help="Grant bounded local autonomous execution; never grants push/deploy.",
    )
    go_parser.add_argument(
        "--project",
        dest="project",
        default=None,
        help="Explicit project id, name, or alias to route this request to.",
    )
    go_parser.add_argument(
        "--prepare-dependencies",
        action="store_true",
        default=False,
        help=(
            "Opt in to explicit JavaScript dependency readiness (npm ci "
            "--ignore-scripts + a baseline test run) before the Worker "
            "launches. No-op for non-npm projects. Disabled by default."
        ),
    )

    show_parser = subparsers.add_parser(
        "show",
        help="Show full detail for a single Run.",
    )
    show_parser.add_argument(
        "run_id",
        help="Run id, e.g. RUN-XXXXXXXX.",
    )

    build_parser = subparsers.add_parser(
        "build",
        help="Build a new project autonomously from a requirements file.",
    )
    build_parser.add_argument(
        "requirements",
        nargs="?",
        help="Path to a text/markdown file with the full project requirements.",
    )
    build_parser.add_argument("--name", help="Project name (also the folder slug).")
    build_parser.add_argument(
        "--workspace",
        help="Parent directory for the new project (default: Nexus's parent folder).",
    )
    build_parser.add_argument(
        "--spec-only",
        action="store_true",
        help="Stop after the Architect spec is written, without executing phases.",
    )
    build_parser.add_argument(
        "--resume",
        metavar="PROGRAM_ID",
        help="Continue an existing Program from its first unfinished phase.",
    )

    subparsers.add_parser("programs", help="List Programs.")

    resume_parser = subparsers.add_parser(
        "resume",
        help="Resume an existing Run from its durable checkpoint history.",
    )
    resume_parser.add_argument(
        "run_id",
        help="Run id to resume, e.g. RUN-XXXXXXXX.",
    )

    recover_parser = subparsers.add_parser(
        "recover",
        help="Recover crashed runs with dead same-host ownership.",
    )
    recover_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List recoverable runs without executing.",
    )

    args = parser.parse_args()

    command = args.command or "status"

    _bootstrap()

    if command == "go":
        go_command(
            args.request,
            project=args.project,
            prepare_dependencies=args.prepare_dependencies,
        )
        return

    if command == "run":
        run_command(args)
        return

    if command == "build":
        build_command(args)
        return

    if command == "show":
        show_command(args.run_id)
        return

    if command == "resume":
        resume_command(args.run_id)
        return

    if command == "recover":
        result = recover_all(dry_run=args.dry_run)
        print()
        print("NEXUS RECOVER")
        print("=" * 60)
        print(f"Recovered: {result['recovered']}")
        print(f"Skipped: {result['skipped']}")
        print(f"Failed: {result['failed']}")
        print()
        return

    commands = {
        "status": status_command,
        "projects": projects_command,
        "runs": runs_command,
        "agents": agents_command,
        "demo": demo_command,
        "programs": programs_command,
    }

    commands[command]()


if __name__ == "__main__":
    main()
