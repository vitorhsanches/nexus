"""Durable records for the multi-pane workspace (P1).

Separation of concepts, stored explicitly:

* plan    -- one auditable Maestro proposal for one task (workspace_plans)
* pane    -- one agent identity (role, title, model for future work, layout)
             bound to exactly one Development session (workspace_panes)
* session -- the existing dev_sessions row: one isolated Git worktree
* mission -- the existing dev_autonomous_missions rows of that session
* event   -- append-only pane history (model changes, missions started,
             visibility changes) in workspace_pane_events
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from nexus.registry.database import get_connection

LAYOUT_FIELDS = ("position", "collapsed", "expanded", "hidden")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def create_plan(
    project_id: str,
    task_text: str,
    *,
    policy_version: str,
    complexity: str,
    proposal: dict,
    source: str = "deterministic",
    conversation_id: str | None = None,
    message_id: str | None = None,
) -> str:
    plan_id = f"WSPLAN-{uuid4().hex[:8].upper()}"
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if conversation_id is not None:
            # A newer plan in the same conversation supersedes older proposals.
            connection.execute(
                "UPDATE workspace_plans SET status = 'SUPERSEDED' WHERE conversation_id = ? AND status = 'PROPOSED'",
                (conversation_id,),
            )
        connection.execute(
            """INSERT INTO workspace_plans
               (id, project_id, task_text, policy_version, complexity, proposal_json, created_at,
                source, conversation_id, message_id, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PROPOSED')""",
            (plan_id, project_id, task_text, policy_version, complexity, json.dumps(proposal), _now(),
             source, conversation_id, message_id),
        )
    return plan_id


def claim_plan_for_approval(plan_id: str, approval: dict) -> bool:
    """Atomically move a PROPOSED plan to APPROVING; False if already taken."""
    with get_connection() as connection:
        cursor = connection.execute(
            """UPDATE workspace_plans SET status = 'APPROVING', approval_json = ?, approved_at = ?
               WHERE id = ? AND status = 'PROPOSED'""",
            (json.dumps(approval), _now(), plan_id),
        )
        return cursor.rowcount == 1


def finish_plan_approval(plan_id: str, status: str, approval: dict) -> None:
    with get_connection() as connection:
        connection.execute(
            "UPDATE workspace_plans SET status = ?, approval_json = ? WHERE id = ?",
            (status, json.dumps(approval), plan_id),
        )


def get_plan(plan_id: str):
    with get_connection() as connection:
        return connection.execute("SELECT * FROM workspace_plans WHERE id = ?", (plan_id,)).fetchone()


def list_plans(project_id: str, limit: int = 50) -> list:
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM workspace_plans WHERE project_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()


def count_open_panes(project_id: str) -> int:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM workspace_panes WHERE project_id = ? AND hidden = 0",
            (project_id,),
        ).fetchone()
    return int(row["n"])


def create_pane(
    *,
    project_id: str,
    dev_session_id: str,
    origin: str,
    role: str,
    title: str,
    model: str | None,
    plan_id: str | None = None,
    plan_agent_key: str | None = None,
    brief: dict | None = None,
) -> str:
    pane_id = f"PANE-{uuid4().hex[:8].upper()}"
    now = _now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM workspace_panes WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        connection.execute(
            """INSERT INTO workspace_panes
               (id, project_id, dev_session_id, plan_id, origin, role, title, model,
                position, created_at, updated_at, plan_agent_key, brief_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pane_id, project_id, dev_session_id, plan_id, origin, role, title, model, row["next"], now, now,
             plan_agent_key, json.dumps(brief) if brief is not None else None),
        )
        _insert_event(connection, pane_id, "CREATED", {"origin": origin, "role": role, "model": model, "plan_id": plan_id})
    return pane_id


def get_pane(pane_id: str):
    with get_connection() as connection:
        return connection.execute("SELECT * FROM workspace_panes WHERE id = ?", (pane_id,)).fetchone()


def list_panes(project_id: str | None = None, *, include_hidden: bool = False) -> list:
    clauses, params = [], []
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if not include_hidden:
        clauses.append("hidden = 0")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_connection() as connection:
        return connection.execute(
            f"SELECT * FROM workspace_panes {where} ORDER BY position, created_at",
            params,
        ).fetchall()


def update_pane(pane_id: str, *, event_kind: str, event_detail: dict, **fields) -> None:
    allowed = {"title", "model", *LAYOUT_FIELDS}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown pane fields: {sorted(unknown)}")
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    values = [int(value) if isinstance(value, bool) else value for value in fields.values()]
    with get_connection() as connection:
        connection.execute(
            f"UPDATE workspace_panes SET {assignments}, updated_at = ? WHERE id = ?",
            (*values, _now(), pane_id),
        )
        _insert_event(connection, pane_id, event_kind, event_detail)


def record_event(pane_id: str, kind: str, detail: dict) -> None:
    with get_connection() as connection:
        _insert_event(connection, pane_id, kind, detail)


def list_events(pane_id: str, limit: int = 200) -> list:
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM workspace_pane_events WHERE pane_id = ? ORDER BY id DESC LIMIT ?",
            (pane_id, limit),
        ).fetchall()


def _insert_event(connection, pane_id: str, kind: str, detail: dict) -> None:
    connection.execute(
        "INSERT INTO workspace_pane_events (pane_id, kind, detail_json, created_at) VALUES (?, ?, ?, ?)",
        (pane_id, kind, json.dumps(detail), _now()),
    )


def panes_for_plan(plan_id: str) -> list:
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM workspace_panes WHERE plan_id = ? ORDER BY position", (plan_id,)
        ).fetchall()


# -- Maestro conversation -------------------------------------------------


def get_conversation_for_project(project_id: str):
    with get_connection() as connection:
        return connection.execute(
            """SELECT * FROM workspace_maestro_conversations WHERE project_id = ?
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (project_id,),
        ).fetchone()


def create_conversation(project_id: str) -> str:
    conversation_id = f"MAESTRO-{uuid4().hex[:8].upper()}"
    now = _now()
    with get_connection() as connection:
        connection.execute(
            "INSERT INTO workspace_maestro_conversations (id, project_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (conversation_id, project_id, now, now),
        )
    return conversation_id


def get_conversation(conversation_id: str):
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM workspace_maestro_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()


def authorize_conversation(conversation_id: str, *, chat_session_id: str, model: str) -> None:
    now = _now()
    with get_connection() as connection:
        connection.execute(
            """UPDATE workspace_maestro_conversations
               SET chat_session_id = ?, model = ?, planning_authorized_at = ?, updated_at = ?
               WHERE id = ?""",
            (chat_session_id, model, now, now, conversation_id),
        )


def add_message(
    conversation_id: str,
    *,
    author: str,
    text: str,
    status: str,
    chat_turn_id: str | None = None,
    plan_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> str:
    message_id = f"MMSG-{uuid4().hex[:10].upper()}"
    now = _now()
    with get_connection() as connection:
        connection.execute(
            """INSERT INTO workspace_maestro_messages
               (id, conversation_id, author, text, chat_turn_id, status, plan_id, error_code, error_message,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (message_id, conversation_id, author, text, chat_turn_id, status, plan_id, error_code, error_message,
             now, now),
        )
    return message_id


def set_message_turn(message_id: str, chat_turn_id: str) -> None:
    with get_connection() as connection:
        connection.execute(
            "UPDATE workspace_maestro_messages SET chat_turn_id = ?, updated_at = ? WHERE id = ?",
            (chat_turn_id, _now(), message_id),
        )


def record_maestro_answer(
    user_message_id: str,
    *,
    project_id: str,
    task_text: str,
    plan: dict,
    policy_version: str,
    reply: str,
    user_status: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> str | None:
    """Atomically store the plan, the Maestro reply and the user message's
    final state -- all or nothing, exactly once per user message.

    Idempotent: a message that is already final is left untouched. A plan
    already stored for this message (left by an interrupted older
    transition sequence) is reused, and its missing reply is repaired,
    instead of creating a duplicate. Returns the plan id, or None when the
    message no longer exists.
    """
    now = _now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        message = connection.execute(
            "SELECT * FROM workspace_maestro_messages WHERE id = ?", (user_message_id,)
        ).fetchone()
        if message is None:
            return None
        if message["status"] not in {"PENDING", "PROCESSING"}:
            return message["plan_id"]
        existing = connection.execute(
            "SELECT id FROM workspace_plans WHERE message_id = ?", (user_message_id,)
        ).fetchone()
        if existing is not None:
            plan_id = existing["id"]
        else:
            plan_id = f"WSPLAN-{uuid4().hex[:8].upper()}"
            connection.execute(
                "UPDATE workspace_plans SET status = 'SUPERSEDED' WHERE conversation_id = ? AND status = 'PROPOSED'",
                (message["conversation_id"],),
            )
            connection.execute(
                """INSERT INTO workspace_plans
                   (id, project_id, task_text, policy_version, complexity, proposal_json, created_at,
                    source, conversation_id, message_id, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PROPOSED')""",
                (plan_id, project_id, task_text, policy_version, plan["complexity"], json.dumps(plan), now,
                 plan["source"], message["conversation_id"], user_message_id),
            )
        has_reply = connection.execute(
            "SELECT 1 FROM workspace_maestro_messages WHERE author = 'maestro' AND plan_id = ?", (plan_id,)
        ).fetchone()
        if has_reply is None:
            connection.execute(
                """INSERT INTO workspace_maestro_messages
                   (id, conversation_id, author, text, status, plan_id, created_at, updated_at)
                   VALUES (?, ?, 'maestro', ?, 'COMPLETED', ?, ?, ?)""",
                (f"MMSG-{uuid4().hex[:10].upper()}", message["conversation_id"], reply, plan_id, now, now),
            )
        connection.execute(
            """UPDATE workspace_maestro_messages
               SET status = ?, plan_id = ?, error_code = ?, error_message = ?, updated_at = ?
               WHERE id = ?""",
            (user_status, plan_id, error_code, error_message, now, user_message_id),
        )
    return plan_id


def list_messages(conversation_id: str, limit: int = 200) -> list:
    with get_connection() as connection:
        rows = connection.execute(
            """SELECT * FROM workspace_maestro_messages WHERE conversation_id = ?
               ORDER BY created_at DESC, rowid DESC LIMIT ?""",
            (conversation_id, limit),
        ).fetchall()
    return list(reversed(rows))


# -- Direct pane messages -------------------------------------------------


def add_pane_message(pane_id: str, *, text: str, model: str | None, changes_before: list) -> str:
    message_id = f"PMSG-{uuid4().hex[:10].upper()}"
    now = _now()
    with get_connection() as connection:
        connection.execute(
            """INSERT INTO workspace_pane_messages
               (id, pane_id, text, model, changes_before_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (message_id, pane_id, text, model, json.dumps(changes_before), now, now),
        )
    return message_id


def set_pane_message_turn(message_id: str, dev_turn_id: str | None) -> None:
    with get_connection() as connection:
        connection.execute(
            "UPDATE workspace_pane_messages SET dev_turn_id = ?, updated_at = ? WHERE id = ?",
            (dev_turn_id, _now(), message_id),
        )


def finish_pane_message(message_id: str, *, outcome: str, changes_after: list | None) -> bool:
    with get_connection() as connection:
        cursor = connection.execute(
            """UPDATE workspace_pane_messages SET outcome = ?, changes_after_json = ?, updated_at = ?
               WHERE id = ? AND outcome IS NULL""",
            (outcome, json.dumps(changes_after) if changes_after is not None else None, _now(), message_id),
        )
        return cursor.rowcount == 1


def list_pane_messages(pane_id: str, limit: int = 100) -> list:
    with get_connection() as connection:
        rows = connection.execute(
            """SELECT * FROM workspace_pane_messages WHERE pane_id = ?
               ORDER BY created_at DESC, rowid DESC LIMIT ?""",
            (pane_id, limit),
        ).fetchall()
    return list(reversed(rows))
