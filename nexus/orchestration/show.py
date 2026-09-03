from nexus.registry.agents import list_agents_for_run
from nexus.registry.runs import get_run


class RunNotFoundError(Exception):
    """Raised when the requested Run id does not exist."""


def _format(value):
    return value if value not in (None, "") else "-"


def format_run_report(run_id: str) -> str:
    """Build a human-readable report for a single Run and its Agents."""
    run = get_run(run_id)

    if run is None:
        raise RunNotFoundError(f"Run not found: {run_id}")

    agents = list_agents_for_run(run_id)

    lines = []
    lines.append("")
    lines.append("RUN")
    lines.append("=" * 70)
    lines.append(f"ID          : {run['id']}")
    lines.append(f"Project     : {run['project_name']} ({run['project_id']})")
    lines.append(f"Input       : {run['input']}")
    lines.append(f"Intent      : {_format(run['intent'])}")
    lines.append(f"Risk        : {_format(run['risk'])}")
    lines.append(f"Status      : {run['status']}")
    lines.append(f"Created at  : {_format(run['created_at'])}")
    lines.append(f"Started at  : {_format(run['started_at'])}")
    lines.append(f"Finished at : {_format(run['finished_at'])}")
    lines.append(f"Commit SHA  : {_format(run['commit_sha'])}")

    if run["result"]:
        lines.append(f"Result      : {run['result']}")

    lines.append("")
    lines.append("AGENTS")
    lines.append("-" * 100)

    if not agents:
        lines.append("No agents recorded for this Run.")
        lines.append("")
        return "\n".join(lines)

    lines.append(
        f"{'ID':<15} "
        f"{'ROLE':<16} "
        f"{'PROVIDER':<12} "
        f"{'MODEL':<24} "
        f"{'EFFORT':<8} "
        f"{'STATUS':<12} "
        f"{'PARENT':<15} "
        f"{'BRANCH':<28}"
    )
    lines.append("-" * 100)

    for agent in agents:
        lines.append(
            f"{agent['id']:<15} "
            f"{agent['role']:<16} "
            f"{agent['provider']:<12} "
            f"{agent['model']:<24} "
            f"{agent['effort']:<8} "
            f"{agent['status']:<12} "
            f"{_format(agent['parent_agent_id']):<15} "
            f"{_format(agent['branch']):<28}"
        )

        if agent["worktree"]:
            lines.append(f"    Worktree : {agent['worktree']}")

    lines.append("")
    lines.append("EXECUTION FLOW")
    lines.append("-" * 100)

    by_id = {agent["id"]: agent for agent in agents}
    roots = [agent for agent in agents if not agent["parent_agent_id"]]

    def render(agent, depth):
        arrow = "-> " if depth > 0 else ""
        lines.append(
            f"{'  ' * depth}{arrow}{agent['role']} "
            f"({agent['model']}) [{agent['status']}]"
        )

        children = [
            candidate
            for candidate in agents
            if candidate["parent_agent_id"] == agent["id"]
        ]

        for child in children:
            render(child, depth + 1)

    for root in roots:
        render(root, 0)

    lines.append("")

    return "\n".join(lines)


def show_run(run_id: str) -> None:
    print(format_run_report(run_id))
