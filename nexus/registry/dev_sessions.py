"""Durable Development Mode session and turn metadata (F1.2 + F2.1).

Scope: session bookkeeping (project, isolated worktree -- see
nexus/dev/worktree.py --, branch, base commit, explicit lifecycle status)
plus, as of F2.1, the per-turn record of each execution inside a session
(prompt, model, status, result, timestamps). No branch integration into
the project's main branch lives here; that is a later phase built on top.

Mirrors nexus/registry/chat_sessions.py's proven patterns deliberately
(BEGIN IMMEDIATE for atomic, concurrency-safe reservation; owner_host/
owner_pid/owner_acquired_at for dead-process recovery after a restart)
but is a fully separate set of tables -- see nexus/registry/database.py's
migration comment for why the schemas are kept independent rather than
threading a "mode" flag through chat_sessions/chat_turns.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from nexus.registry.database import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DevSessionError(RuntimeError):
    """Base error for nexus.registry.dev_sessions failures."""


class DevSessionBranchBusyError(DevSessionError):
    """Another session already reserved this project's branch name."""

    def __init__(self, project_id: str, branch: str):
        super().__init__(f"{branch!r} is already reserved for project {project_id!r}")
        self.project_id = project_id
        self.branch = branch


class InvalidDevSessionTransitionError(DevSessionError):
    """Refused: this status change is not a valid transition."""

    def __init__(self, session_id: str, current: str, requested: str):
        super().__init__(
            f"session {session_id!r} cannot move from {current!r} to {requested!r}"
        )
        self.session_id = session_id
        self.current = current
        self.requested = requested


# -- explicit session lifecycle --------------------------------------------
#
# CREATING    -- row reserved, worktree creation in progress (owner_* set).
# ACTIVE      -- worktree created and verified; session ready. No agent is
#                running yet in F1.2's scope -- later phases add RUNNING
#                states on top of this one, never replacing it.
# FAILED      -- worktree creation failed; error_code/error_message set.
#                The session is NOT deleted -- it stays queryable for
#                inspection, and whatever Git resources may have been
#                created are left exactly as nexus/dev/worktree.py left
#                them (see its own no-auto-cleanup-on-failure contract).
# INTERRUPTED -- the owning Nexus process died while this session was
#                CREATING; the true outcome is unknown (mirrors
#                chat_turns' TURN_OUTCOME_UNKNOWN treatment) -- never
#                silently reported as FAILED or ACTIVE.
# CLOSING     -- removal requested/in progress (reserved for a later
#                phase; no code path sets this yet).
# CLOSED      -- worktree removed; session archived (reserved for a later
#                phase; no code path sets this yet).
STATUS_CREATING = "CREATING"
STATUS_ACTIVE = "ACTIVE"
STATUS_FAILED = "FAILED"
STATUS_INTERRUPTED = "INTERRUPTED"
STATUS_CLOSING = "CLOSING"
STATUS_CLOSED = "CLOSED"

ALL_STATUSES = frozenset(
    {STATUS_CREATING, STATUS_ACTIVE, STATUS_FAILED, STATUS_INTERRUPTED, STATUS_CLOSING, STATUS_CLOSED}
)

# Explicit, closed transition table -- anything not listed here is refused.
# Terminal states (FAILED, INTERRUPTED, CLOSED) have no outgoing edges: once
# a session is known-dead, it stays dead rather than being resurrected.
_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_CREATING: frozenset({STATUS_ACTIVE, STATUS_FAILED, STATUS_INTERRUPTED}),
    STATUS_ACTIVE: frozenset({STATUS_CLOSING, STATUS_FAILED, STATUS_INTERRUPTED}),
    STATUS_CLOSING: frozenset({STATUS_CLOSED, STATUS_FAILED}),
    STATUS_FAILED: frozenset(),
    STATUS_INTERRUPTED: frozenset(),
    STATUS_CLOSED: frozenset(),
}


def new_session_id() -> str:
    """Generate a fresh session id, independent of any DB state.

    Exposed so a caller can derive the id-dependent branch name (see
    nexus.dev.worktree.branch_name_for_session) BEFORE reserving the row,
    without this module needing to know anything about branch-naming
    conventions itself.
    """
    return f"DEV-{uuid4().hex[:8].upper()}"


def reserve_session(
    session_id: str,
    project_id: str,
    branch: str,
    *,
    requested_model: str | None = None,
    backend: str = "omniroute",
) -> str:
    """Atomically reserve a new, CREATING development session row.

    BEGIN IMMEDIATE makes the branch-uniqueness check and the insertion one
    SQLite write transaction (including across separate Nexus processes),
    so two concurrent requests for the same project/branch can never both
    succeed -- the loser gets DevSessionBranchBusyError before any
    filesystem/Git work starts. The (project_id, branch) unique index is
    the second, DB-level backstop behind this. Returns session_id
    unchanged, for a convenient assignment at the call site.
    """
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT id FROM dev_sessions WHERE project_id = ? AND branch = ?",
            (project_id, branch),
        ).fetchone()
        if existing is not None:
            raise DevSessionBranchBusyError(project_id, branch)
        connection.execute(
            """
            INSERT INTO dev_sessions
                (id, project_id, branch, requested_model, backend, status,
                 owner_host, owner_pid, owner_acquired_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id, project_id, branch, requested_model, backend, STATUS_CREATING,
                socket.gethostname(), os.getpid(), _now(), _now(), _now(),
            ),
        )
    return session_id


def update_thread(session_id: str, *, codex_thread_id: str, codex_home: str | None) -> None:
    """Record the Codex App-Server thread/CODEX_HOME a session's turns run
    on, so a later turn in the same session can thread_resume() the same
    conversation (see nexus/dev/executor.py). Mirrors
    chat_sessions.update_session_thread() exactly; does not itself change
    `status` or require any particular transition."""
    with get_connection() as connection:
        connection.execute(
            "UPDATE dev_sessions SET codex_thread_id = ?, codex_home = ?, updated_at = ? WHERE id = ?",
            (codex_thread_id, codex_home, _now(), session_id),
        )


def mark_active(session_id: str, *, worktree_path: str, base_sha: str) -> None:
    _transition(session_id, STATUS_ACTIVE, worktree_path=worktree_path, base_sha=base_sha)


def mark_failed(session_id: str, *, error_code: str, error_message: str) -> None:
    _transition(session_id, STATUS_FAILED, error_code=error_code, error_message=error_message)


def _transition(
    session_id: str,
    new_status: str,
    *,
    worktree_path: str | None = None,
    base_sha: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    if new_status not in ALL_STATUSES:
        raise DevSessionError(f"unknown status: {new_status!r}")
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status FROM dev_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise DevSessionError(f"unknown dev session: {session_id!r}")
        current = row["status"]
        if new_status not in _VALID_TRANSITIONS.get(current, frozenset()):
            raise InvalidDevSessionTransitionError(session_id, current, new_status)

        fields = ["status = ?", "updated_at = ?"]
        params: list = [new_status, _now()]
        if worktree_path is not None:
            fields.append("worktree_path = ?")
            params.append(worktree_path)
        if base_sha is not None:
            fields.append("base_sha = ?")
            params.append(base_sha)
        if error_code is not None:
            fields.append("error_code = ?")
            params.append(error_code)
        if error_message is not None:
            fields.append("error_message = ?")
            params.append(error_message)
        params.append(session_id)
        connection.execute(
            f"UPDATE dev_sessions SET {', '.join(fields)} WHERE id = ?", params
        )


def get_session(session_id: str):
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM dev_sessions WHERE id = ?", (session_id,)
        ).fetchone()


def list_sessions(project_id: str | None = None):
    with get_connection() as connection:
        if project_id is None:
            return connection.execute(
                "SELECT * FROM dev_sessions ORDER BY updated_at DESC"
            ).fetchall()
        return connection.execute(
            "SELECT * FROM dev_sessions WHERE project_id = ? ORDER BY updated_at DESC",
            (project_id,),
        ).fetchall()


def recover_orphaned_sessions(
    is_orphaned: Callable[[str | None, int | None, str | None], bool],
) -> list[str]:
    """Mark CREATING sessions from dead Nexus processes INTERRUPTED.

    Never replays or retries anything, and never touches the filesystem or
    Git -- this is pure metadata bookkeeping, mirroring
    chat_sessions.recover_orphaned_turns() exactly. A session whose owner
    process is still alive (even if it belongs to another web worker) is
    left untouched: liveness, not origin, is what is being checked.
    """
    recovered: list[str] = []
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        creating = connection.execute(
            """SELECT id, owner_host, owner_pid, owner_acquired_at
               FROM dev_sessions WHERE status = ?""",
            (STATUS_CREATING,),
        ).fetchall()
        for row in creating:
            owner_host = row["owner_host"]
            owner_pid = row["owner_pid"]
            if not is_orphaned(owner_host, owner_pid, row["owner_acquired_at"]):
                continue
            changed = connection.execute(
                """UPDATE dev_sessions
                   SET status = ?, error_code = 'SESSION_OUTCOME_UNKNOWN',
                       error_message = ?, updated_at = ?
                   WHERE id = ? AND status = ?
                     AND owner_host IS ? AND owner_pid IS ?""",
                (
                    STATUS_INTERRUPTED,
                    "The owning process exited before this session's worktree "
                    "creation was confirmed. Its outcome is unknown; nothing "
                    "was automatically created or removed.",
                    _now(),
                    row["id"],
                    STATUS_CREATING,
                    owner_host,
                    owner_pid,
                ),
            )
            if changed.rowcount:
                recovered.append(row["id"])
    return recovered


# -- turns (F2.1) ------------------------------------------------------
#
# PENDING -> RUNNING -> COMPLETED | FAILED | CANCELLED (interrupted).
# Unlike dev_sessions, turn status is not guarded by an explicit
# transition table: update_turn_status() mirrors
# chat_sessions.update_turn_status() exactly (same four terminal/
# non-terminal state handling for started_at/finished_at), and the one
# invariant that actually matters here -- never completing an
# already-terminal turn twice, never running two turns at once for the
# same session -- is enforced at reservation/recovery time instead (see
# create_turn() and recover_orphaned_turns() below).


class DevTurnBusyError(DevSessionError):
    """Another turn is already PENDING/RUNNING for this development session."""

    def __init__(self, dev_session_id: str, turn_id: str):
        super().__init__(f"session {dev_session_id!r} already has an active turn ({turn_id!r})")
        self.dev_session_id = dev_session_id
        self.turn_id = turn_id


def create_turn(dev_session_id: str, prompt: str, *, requested_model: str | None = None) -> str:
    """Atomically reserve one new PENDING turn for a development session.

    Refuses (DevTurnBusyError) if this session already has a PENDING or
    RUNNING turn -- exactly one active turn per development session, ever
    (F2.1's explicit concurrency requirement), enforced the same way
    chat_sessions.create_turn_if_absent() enforces one active turn per
    Chat session: BEGIN IMMEDIATE makes the check-then-insert one SQLite
    write transaction, including across separate Nexus processes.
    """
    turn_id = f"DEVTURN-{uuid4().hex[:8].upper()}"
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        from nexus.registry.workspace_handoff import assert_session_execution_allowed

        assert_session_execution_allowed(connection, dev_session_id, turn=True)
        active = connection.execute(
            """SELECT id FROM dev_turns
               WHERE dev_session_id = ? AND status IN ('PENDING', 'RUNNING')
               LIMIT 1""",
            (dev_session_id,),
        ).fetchone()
        if active is not None:
            raise DevTurnBusyError(dev_session_id, active["id"])
        integration = connection.execute(
            """SELECT 1 FROM dev_sessions s JOIN dev_integration_locks l
               ON l.project_id = s.project_id WHERE s.id = ? LIMIT 1""",
            (dev_session_id,),
        ).fetchone()
        if integration is not None:
            raise DevTurnBusyError(dev_session_id, "INTEGRATION_IN_PROGRESS")
        connection.execute(
            """
            INSERT INTO dev_turns
                (id, dev_session_id, prompt, requested_model, status,
                 owner_host, owner_pid, owner_acquired_at, created_at)
            VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
            """,
            (
                turn_id, dev_session_id, prompt, requested_model,
                socket.gethostname(), os.getpid(), _now(), _now(),
            ),
        )
    return turn_id


def get_turn(turn_id: str):
    with get_connection() as connection:
        return connection.execute("SELECT * FROM dev_turns WHERE id = ?", (turn_id,)).fetchone()


def get_turn_for_session(dev_session_id: str, turn_id: str):
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM dev_turns WHERE dev_session_id = ? AND id = ?",
            (dev_session_id, turn_id),
        ).fetchone()


def get_active_turn_for_session(dev_session_id: str):
    """The most recent PENDING/RUNNING turn for this session, if any."""
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT * FROM dev_turns
            WHERE dev_session_id = ? AND status IN ('PENDING', 'RUNNING')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (dev_session_id,),
        ).fetchone()


def list_turns(dev_session_id: str, limit: int = 50):
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT * FROM dev_turns
            WHERE dev_session_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (dev_session_id, limit),
        ).fetchall()


def update_turn_status(
    turn_id: str,
    status: str,
    *,
    response_text: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    """Mirrors chat_sessions.update_turn_status() exactly. A write against
    a turn_id that is no longer PENDING/RUNNING (e.g. a late event after
    this turn was already recovered as orphaned) still applies -- the
    caller (nexus/dev/executor.py) is responsible for not calling this
    after it has already observed its own terminal state; see
    recover_orphaned_turns()'s docstring for why a *second*, independent
    writer (recovery) racing a live one is the scenario actually guarded
    against, not this."""
    fields = ["status = ?"]
    params: list = [status]

    if status == "RUNNING":
        fields.append("started_at = COALESCE(started_at, ?)")
        params.append(_now())

    if status in {"COMPLETED", "FAILED", "CANCELLED"}:
        fields.append("finished_at = ?")
        params.append(_now())

    if response_text is not None:
        fields.append("response_text = ?")
        params.append(response_text)
    if error_code is not None:
        fields.append("error_code = ?")
        params.append(error_code)
    if error_message is not None:
        fields.append("error_message = ?")
        params.append(error_message)

    params.append(turn_id)

    with get_connection() as connection:
        connection.execute(
            f"UPDATE dev_turns SET {', '.join(fields)} WHERE id = ?",
            params,
        )


def recover_orphaned_turns(
    is_orphaned: Callable[[str | None, int | None, str | None], bool],
) -> list[str]:
    """Mark turns from dead Nexus processes outcome-unknown, never replay them.

    Mirrors chat_sessions.recover_orphaned_turns() exactly. The
    development *session* itself (its worktree, its ACTIVE status) is
    deliberately left untouched here: an interrupted turn's outcome is
    unknown, but the worktree it ran in is not corrupted by that alone,
    and the session remains usable for a new turn once the uncertain one
    is accounted for.
    """
    recovered: list[str] = []
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            """SELECT id, owner_host, owner_pid, owner_acquired_at
               FROM dev_turns
               WHERE status IN ('PENDING', 'RUNNING')"""
        ).fetchall()
        for row in active:
            owner_host = row["owner_host"]
            owner_pid = row["owner_pid"]
            if not is_orphaned(owner_host, owner_pid, row["owner_acquired_at"]):
                continue
            changed = connection.execute(
                """UPDATE dev_turns
                   SET status = 'FAILED', error_code = 'TURN_OUTCOME_UNKNOWN',
                       error_message = ?, finished_at = ?
                   WHERE id = ? AND status IN ('PENDING', 'RUNNING')
                     AND owner_host IS ? AND owner_pid IS ?""",
                (
                    "The owning process exited before recording the result. "
                    "The provider may have completed work; this turn was not replayed.",
                    _now(),
                    row["id"],
                    owner_host,
                    owner_pid,
                ),
            )
            if changed.rowcount:
                recovered.append(row["id"])
    return recovered
