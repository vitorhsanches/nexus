"""Development session management service (F1.2).

Scope, strictly: validate the project and its explicit Development Mode
authorization, reserve a unique session, create its isolated worktree
(nexus/dev/worktree.py), persist the result, and expose query/list/
recovery operations. This module never starts an agent, never performs
inference, never runs a project's own tests, and never integrates a
branch back into the project -- those are later phases built on top.
"""

from __future__ import annotations

from pathlib import Path

from nexus.dev.worktree import branch_name_for_session, create_dev_worktree, read_worktree_head_sha
from nexus.registry import dev_sessions
from nexus.registry.projects import get_dev_enabled, get_project


class DevServiceError(RuntimeError):
    """Raised for known, reportable Development Mode service failures.

    Mirrors nexus.chat.service.ChatError's shape: ``code`` is a short
    machine-readable reason, ``http_status`` is the HTTP status a future
    route layer should return for it (no route exists yet in F1.2).
    """

    def __init__(self, code: str, message: str, *, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def validate_dev_project(project_id: str) -> str:
    """Return the project's registered workspace path, or fail closed.

    Mirrors nexus.chat.service._validate_project's re-validate-every-call
    discipline exactly, plus the additional, independent Development Mode
    authorization check. Both the project's own `enabled` flag and its
    `dev_enabled` flag are re-read fresh here -- never cached, never
    inherited from another project -- so disabling either one takes
    effect on the very next call, including for an already-open session.
    """
    project = get_project(project_id)
    if project is None:
        raise DevServiceError("PROJECT_NOT_FOUND", f"Unknown project: {project_id!r}", http_status=404)
    if not project["enabled"]:
        raise DevServiceError("PROJECT_DISABLED", f"Project is disabled: {project_id!r}", http_status=400)
    if not get_dev_enabled(project_id):
        raise DevServiceError(
            "DEV_MODE_DISABLED",
            f"Development Mode is not authorized for project: {project_id!r}",
            http_status=403,
        )
    workspace_path = project["path"]
    if not Path(workspace_path).is_dir():
        raise DevServiceError(
            "WORKSPACE_UNAVAILABLE",
            f"Project workspace does not exist on disk: {workspace_path!r}",
            http_status=409,
        )
    return workspace_path


def _session_dict(row) -> dict:
    return {
        "session_id": row["id"],
        "project_id": row["project_id"],
        "worktree_path": row["worktree_path"],
        "branch": row["branch"],
        "base_sha": row["base_sha"],
        "requested_model": row["requested_model"],
        "backend": row["backend"] if "backend" in row.keys() and row["backend"] else "omniroute",
        "status": row["status"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_dev_session(project_id: str, *, requested_model: str | None = None, backend: str = "omniroute") -> dict:
    """Create and persist one new development session with its own worktree.

    The session row is reserved (status=CREATING, owner_host/pid recorded)
    BEFORE any filesystem/Git work starts, so a failure at any later point
    always has a durable, queryable record to attach the failure to --
    nothing about a failed attempt is ever silently lost. If worktree
    creation itself fails, whatever (if anything) nexus.dev.worktree left
    behind is NOT touched or removed here -- the session is marked FAILED
    with the error preserved, for inspection or future, explicitly
    authorized cleanup.
    """
    workspace_path = validate_dev_project(project_id)
    if backend not in {"omniroute", "codex_native"}:
        raise DevServiceError("UNSUPPORTED_BACKEND", f"Unsupported Development backend: {backend!r}", http_status=422)
    if backend == "codex_native":
        from nexus.dev.executor import validate_native_choice

        validate_native_choice(requested_model, None)

    session_id = dev_sessions.new_session_id()
    branch = branch_name_for_session(session_id)
    try:
        dev_sessions.reserve_session(session_id, project_id, branch, requested_model=requested_model, backend=backend)
    except dev_sessions.DevSessionBranchBusyError as error:
        raise DevServiceError("SESSION_BRANCH_BUSY", str(error), http_status=409) from error

    try:
        worktree_path = create_dev_worktree(workspace_path, session_id)
    except Exception as error:
        dev_sessions.mark_failed(
            session_id,
            error_code=type(error).__name__,
            error_message=str(error)[:2000],
        )
        raise DevServiceError(
            "WORKTREE_CREATE_FAILED",
            f"Could not create the development worktree: {error}",
            http_status=500,
        ) from error

    base_sha = read_worktree_head_sha(worktree_path)
    dev_sessions.mark_active(session_id, worktree_path=worktree_path, base_sha=base_sha)
    return _session_dict(dev_sessions.get_session(session_id))


def get_session_detail(session_id: str) -> dict:
    row = dev_sessions.get_session(session_id)
    if row is None:
        raise DevServiceError("SESSION_NOT_FOUND", f"Unknown development session: {session_id!r}", http_status=404)
    return _session_dict(row)


def list_sessions(project_id: str | None = None) -> list[dict]:
    return [_session_dict(row) for row in dev_sessions.list_sessions(project_id)]


def recover_orphaned_sessions() -> list[str]:
    """Mark CREATING sessions whose owning Nexus process has died
    INTERRUPTED. Pure metadata bookkeeping -- never touches the
    filesystem or Git, and never replays or retries anything.

    Reuses nexus.orchestration.ownership.classify_ownership (the same
    liveness check nexus.chat.service.recover_orphaned_turns() uses) for
    this, rather than a bespoke os.kill()-based check: on Windows,
    os.kill(pid, 0) does not raise ProcessLookupError for a dead PID (it
    raises a generic OSError indistinguishable from "permission denied"),
    so a naive POSIX-style check silently never detects a dead process on
    this platform. classify_ownership() already encodes the correct,
    platform-specific liveness probe (including Windows PID-reuse
    detection via process creation time) and is already relied on
    elsewhere in Nexus -- only "dead_same_host" is ever treated as
    orphaned; a foreign host, an unowned row, or a live process (even one
    this call cannot signal) is left untouched.
    """
    from nexus.orchestration.ownership import classify_ownership

    return dev_sessions.recover_orphaned_sessions(
        lambda host, pid, acquired_at: classify_ownership(host, pid, acquired_at) == "dead_same_host"
    )
