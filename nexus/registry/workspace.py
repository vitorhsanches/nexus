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


def create_plan(project_id: str, task_text: str, *, policy_version: str, complexity: str, proposal: dict) -> str:
    plan_id = f"WSPLAN-{uuid4().hex[:8].upper()}"
    with get_connection() as connection:
        connection.execute(
            """INSERT INTO workspace_plans
               (id, project_id, task_text, policy_version, complexity, proposal_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (plan_id, project_id, task_text, policy_version, complexity, json.dumps(proposal), _now()),
        )
    return plan_id


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
                position, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pane_id, project_id, dev_session_id, plan_id, origin, role, title, model, row["next"], now, now),
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
