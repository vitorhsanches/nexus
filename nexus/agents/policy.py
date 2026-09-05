"""Execution policy resolution boundary for Nexus Real Board Task Execution V1.

Resolves which ``ExecutionAdapter`` and workspace target a Board-triggered
task execution should use, based on the task's ``execution_policy`` dict.
This is the single place that decides "simulated" vs "omniroute" and, for
"omniroute", the concrete repository target. FastAPI routes and the executor
stay thin: they call ``resolve_execution_policy`` and either use the result
or propagate ``ExecutionPolicyError`` as a 422.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from nexus.agents.adapters.base import ExecutionAdapter
from nexus.agents.adapters.omniroute import OmniRouteAdapter
from nexus.agents.adapters.simulated import SimulatedAdapter
from nexus.router import (
    ProjectAmbiguousError,
    ProjectNotFoundError,
    resolve_project,
)

VALID_EXECUTION_PATHS = {"simulated", "omniroute"}


class ExecutionPolicyError(ValueError):
    """Raised when a task's execution_policy cannot be resolved safely."""


@dataclass(slots=True)
class ResolvedExecutionPolicy:
    """Runtime information needed to execute a Board task."""

    execution_path: str
    adapter: ExecutionAdapter
    workspace_path: Optional[str]
    project_id: Optional[str]


def resolve_execution_policy(execution_policy: Optional[dict]) -> ResolvedExecutionPolicy:
    """Resolve the adapter and workspace target for a task's execution_policy.

    Missing ``execution_path`` (or an explicit ``"simulated"``) safely
    defaults to the ``SimulatedAdapter`` with no workspace target required.
    ``"omniroute"`` requires an explicit, validated Git repository target
    resolved either through ``execution_policy["project_id"]`` (via the
    existing Project Router) or ``execution_policy["workspace_path"]``.
    """
    policy = execution_policy if isinstance(execution_policy, dict) else {}
    execution_path = policy.get("execution_path") or "simulated"

    if execution_path not in VALID_EXECUTION_PATHS:
        raise ExecutionPolicyError(
            f"Invalid execution_path: {execution_path!r}. "
            f"Must be one of {sorted(VALID_EXECUTION_PATHS)}."
        )

    if execution_path == "simulated":
        return ResolvedExecutionPolicy(
            execution_path="simulated",
            adapter=SimulatedAdapter(),
            workspace_path=policy.get("workspace_path"),
            project_id=policy.get("project_id"),
        )

    # execution_path == "omniroute"
    project_id = policy.get("project_id")
    workspace_path = policy.get("workspace_path")

    if project_id:
        try:
            routed = resolve_project(project_id)
        except (ProjectNotFoundError, ProjectAmbiguousError) as exc:
            raise ExecutionPolicyError(str(exc)) from exc
        workspace_path = routed.path

    _validate_git_target(workspace_path)

    return ResolvedExecutionPolicy(
        execution_path="omniroute",
        adapter=OmniRouteAdapter(),
        workspace_path=workspace_path,
        project_id=project_id,
    )


def _validate_git_target(workspace_path: Optional[str]) -> None:
    """Validate that ``workspace_path`` is a usable Git repository/worktree.

    A valid target must be a non-empty path that exists, is a directory, and
    contains a ``.git`` entry (file or directory), which supports both
    normal repositories and Git worktrees. Never falls back to ``"."``.
    """
    if not workspace_path:
        raise ExecutionPolicyError(
            "OmniRoute execution requires an explicit target: set "
            "execution_policy['project_id'] or execution_policy['workspace_path']."
        )

    path = Path(workspace_path)

    if not path.exists():
        raise ExecutionPolicyError(
            f"OmniRoute execution target does not exist: {workspace_path!r}."
        )

    if not path.is_dir():
        raise ExecutionPolicyError(
            f"OmniRoute execution target is not a directory: {workspace_path!r}."
        )

    if not (path / ".git").exists():
        raise ExecutionPolicyError(
            f"OmniRoute execution target is not a Git repository/worktree: "
            f"{workspace_path!r}."
        )
