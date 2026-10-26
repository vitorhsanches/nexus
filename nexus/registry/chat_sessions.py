"""Durable Nexus Chat session/turn metadata.

Codex App-Server's own non-ephemeral thread (see
``nexus.chat.appserver_client``) is the source of truth for conversation
*content* -- this module only persists the minimal mapping needed to
reopen a session after a Nexus restart (which project/workspace, which
Codex thread id, which CODEX_HOME to resume it from) plus durable turn
status, so a client that reconnects long after a turn finished can still
read its final result even if the live in-memory event buffer
(``nexus.chat.service``) has been evicted.
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


# -- sessions ----------------------------------------------------------


def create_session(
    project_id: str,
    workspace_path: str,
    requested_model: str | None = None,
    requested_effort: str | None = None,
    backend: str = "omniroute",
) -> str:
    session_id = f"CHAT-{uuid4().hex[:8].upper()}"
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO chat_sessions (id, project_id, workspace_path, requested_model, requested_effort, backend, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'CREATED', ?, ?)
            """,
            (session_id, project_id, workspace_path, requested_model, requested_effort, backend, _now(), _now()),
        )
    return session_id


def get_session(session_id: str):
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
        ).fetchone()


def list_sessions(project_id: str | None = None):
    with get_connection() as connection:
        if project_id is None:
            return connection.execute(
                "SELECT * FROM chat_sessions ORDER BY updated_at DESC"
            ).fetchall()
        return connection.execute(
            "SELECT * FROM chat_sessions WHERE project_id = ? ORDER BY updated_at DESC",
            (project_id,),
        ).fetchall()


def update_session_thread(
    session_id: str,
    *,
    codex_thread_id: str,
    codex_home: str | None,
    model: str | None,
    provider: str | None,
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE chat_sessions
            SET codex_thread_id = ?, codex_home = ?, model = ?, provider = ?, updated_at = ?
            WHERE id = ?
            """,
            (codex_thread_id, codex_home, model, provider, _now(), session_id),
        )


def update_claude_session(session_id: str, *, claude_session_id: str, model: str | None) -> None:
    """Record the Claude Code conversation a Claude Native session resumes."""
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE chat_sessions
            SET claude_session_id = ?, model = COALESCE(?, model), provider = 'anthropic', updated_at = ?
            WHERE id = ?
            """,
            (claude_session_id, model, _now(), session_id),
        )


def update_session_status(session_id: str, status: str) -> None:
    with get_connection() as connection:
        connection.execute(
            "UPDATE chat_sessions SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), session_id),
        )


# -- turns ---------------------------------------------------------------


class SessionBusyError(RuntimeError):
    def __init__(self, turn_id: str):
        super().__init__(turn_id)
        self.turn_id = turn_id


def create_turn_if_absent(
    session_id: str, idempotency_key: str, request_text: str
) -> tuple[str, bool]:
    """Atomically reserve the session for one active turn across DB connections.

    A retry of the same idempotency key returns its original turn, including
    after completion. A different key cannot insert while a turn is pending
    or running. BEGIN IMMEDIATE makes the lookup and insertion one SQLite
    write transaction, including across Nexus processes.
    """
    turn_id = f"TURN-{uuid4().hex[:8].upper()}"
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT id FROM chat_turns WHERE session_id = ? AND idempotency_key = ?",
            (session_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            return existing["id"], False
        active = connection.execute(
            """SELECT id FROM chat_turns
               WHERE session_id = ? AND status IN ('PENDING', 'RUNNING')
               LIMIT 1""",
            (session_id,),
        ).fetchone()
        if active is not None:
            raise SessionBusyError(active["id"])
        connection.execute(
            """
            INSERT INTO chat_turns
                (id, session_id, idempotency_key, request_text, status,
                 owner_host, owner_pid, owner_acquired_at, created_at)
            VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
            """,
            (
                turn_id, session_id, idempotency_key, request_text,
                socket.gethostname(), os.getpid(), _now(), _now(),
            ),
        )
        return turn_id, True


def recover_orphaned_turns(is_orphaned: Callable[[str | None, int | None, str | None], bool]) -> list[str]:
    """Mark turns from dead Nexus processes outcome-unknown, never replay them.

    Legacy/foreign/ambiguous owners stay reserved. A live process may belong
    to another web worker, so its turn is left untouched.
    """
    recovered: list[str] = []
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            """SELECT id, session_id, owner_host, owner_pid, owner_acquired_at
               FROM chat_turns
               WHERE status IN ('PENDING', 'RUNNING')"""
        ).fetchall()
        for row in active:
            owner_host = row["owner_host"]
            owner_pid = row["owner_pid"]
            if not is_orphaned(owner_host, owner_pid, row["owner_acquired_at"]):
                continue
            changed = connection.execute(
                """UPDATE chat_turns
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
                connection.execute(
                    "UPDATE chat_sessions SET status = 'ERROR', updated_at = ? WHERE id = ?",
                    (_now(), row["session_id"]),
                )
                recovered.append(row["id"])
    return recovered


def get_turn_by_idempotency_key(session_id: str, idempotency_key: str):
    """The turn a caller reserved with this idempotency key, if any."""
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM chat_turns WHERE session_id = ? AND idempotency_key = ?",
            (session_id, idempotency_key),
        ).fetchone()


def get_turn(turn_id: str):
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM chat_turns WHERE id = ?", (turn_id,)
        ).fetchone()


def get_turn_for_session(session_id: str, turn_id: str):
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM chat_turns WHERE session_id = ? AND id = ?",
            (session_id, turn_id),
        ).fetchone()


def get_active_turn_for_session(session_id: str):
    """The most recent PENDING/RUNNING turn for this session, if any."""
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT * FROM chat_turns
            WHERE session_id = ? AND status IN ('PENDING', 'RUNNING')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()


def list_turns(session_id: str, limit: int = 50):
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT * FROM chat_turns
            WHERE session_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()


def update_turn_status(
    turn_id: str,
    status: str,
    *,
    response_text: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> None:
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
    if model is not None:
        fields.append("model = ?")
        params.append(model)
    if provider is not None:
        fields.append("provider = ?")
        params.append(provider)

    params.append(turn_id)

    with get_connection() as connection:
        connection.execute(
            f"UPDATE chat_turns SET {', '.join(fields)} WHERE id = ?",
            params,
        )
