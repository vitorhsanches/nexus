"""Durable records for P2B.1 sequential Workspace plan coordination."""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime, timedelta

from nexus.registry.database import get_connection

EXECUTION_STATUSES = frozenset({
    "AWAITING_EXECUTION",
    "RUNNING",
    "WAITING_DEPENDENCY",
    "AWAITING_REVIEW",
    "BLOCKED",
    "FAILED",
    "INTERRUPTED",
    "RESULT_UNCERTAIN",
})
STEP_STATUSES = frozenset({
    "PENDING", "STARTING", "RUNNING", "READY_FOR_REVIEW", "BLOCKED",
    "FAILED", "INTERRUPTED", "RESULT_UNCERTAIN",
})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def get_execution(plan_id: str):
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM workspace_plan_executions WHERE plan_id = ?", (plan_id,)
        ).fetchone()


def get_steps(plan_id: str) -> list:
    with get_connection() as connection:
        return list(connection.execute(
            "SELECT * FROM workspace_plan_execution_steps WHERE plan_id = ? ORDER BY ordinal",
            (plan_id,),
        ).fetchall())


def list_executions(project_id: str, limit: int = 50) -> list:
    with get_connection() as connection:
        return list(connection.execute(
            "SELECT * FROM workspace_plan_executions WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
        ).fetchall())


def create_execution(
    plan_id: str,
    project_id: str,
    steps: list[dict],
    *,
    max_turns_per_agent: int,
    mission_timeout_seconds: int,
) -> tuple[object, bool]:
    """Create the one execution authorization, or return the existing one.

    The conditional insert is the cross-process idempotency gate. A second
    identical request gets the existing authorization and cannot create a
    second controller or duplicate missions.
    """
    now = _now()
    keys = [step["agent_key"] for step in steps]
    profiles = {step["agent_key"]: step["test_profile"] for step in steps}
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM workspace_plan_executions WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if existing is not None:
            if (
                _json(existing["agent_keys_json"], []) != keys
                or _json(existing["test_profiles_json"], {}) != profiles
                or existing["max_turns_per_agent"] != max_turns_per_agent
                or existing["mission_timeout_seconds"] != mission_timeout_seconds
            ):
                raise ValueError("plan execution already has a different authorization")
            return existing, False
        connection.execute(
            """INSERT INTO workspace_plan_executions
               (plan_id, project_id, status, agent_keys_json, test_profiles_json,
                max_turns_per_agent, mission_timeout_seconds, created_at, updated_at)
               VALUES (?, ?, 'AWAITING_EXECUTION', ?, ?, ?, ?, ?, ?)""",
            (plan_id, project_id, json.dumps(keys), json.dumps(profiles),
             max_turns_per_agent, mission_timeout_seconds, now, now),
        )
        for step in steps:
            connection.execute(
                """INSERT INTO workspace_plan_execution_steps
                   (plan_id, agent_key, ordinal, pane_id, test_profile, status, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'PENDING', ?)""",
                (plan_id, step["agent_key"], step["ordinal"], step["pane_id"], step["test_profile"], now),
            )
        row = connection.execute(
            "SELECT * FROM workspace_plan_executions WHERE plan_id = ?", (plan_id,)
        ).fetchone()
    return row, True


def claim_execution(plan_id: str) -> bool:
    """Atomically claim an awaiting plan for this process."""
    now = _now()
    with get_connection() as connection:
        cursor = connection.execute(
            """UPDATE workspace_plan_executions
               SET status = 'RUNNING', owner_host = ?, owner_pid = ?, owner_acquired_at = ?,
                   started_at = COALESCE(started_at, ?), updated_at = ?
               WHERE plan_id = ? AND status = 'AWAITING_EXECUTION'""",
            (socket.gethostname(), os.getpid(), now, now, now, plan_id),
        )
        return cursor.rowcount == 1


def claim_recovery(plan_id: str, stale_after_seconds: int = 30) -> bool:
    """Take over a durable worker only after its lease is stale."""
    now = datetime.now(UTC)
    cutoff = (now - timedelta(seconds=stale_after_seconds)).isoformat()
    stamp = now.isoformat()
    with get_connection() as connection:
        cursor = connection.execute(
            """UPDATE workspace_plan_executions
               SET owner_host = ?, owner_pid = ?, owner_acquired_at = ?, updated_at = ?
               WHERE plan_id = ? AND status = 'RUNNING'
                 AND (owner_acquired_at IS NULL OR owner_acquired_at < ?)
                 AND NOT (owner_host = ? AND owner_pid = ?)""",
            (socket.gethostname(), os.getpid(), stamp, stamp, plan_id, cutoff,
             socket.gethostname(), os.getpid()),
        )
        return cursor.rowcount == 1


def update_execution(plan_id: str, status: str, *, current_index: int | None = None,
                     interrupt_requested: bool | None = None, error_code: str | None = None,
                     error_message: str | None = None, result: dict | None = None) -> None:
    if status not in EXECUTION_STATUSES:
        raise ValueError(f"unknown execution status: {status!r}")
    fields = ["status = ?", "updated_at = ?"]
    params: list = [status, _now()]
    if current_index is not None:
        fields.append("current_index = ?"); params.append(current_index)
    if interrupt_requested is not None:
        fields.append("interrupt_requested = ?"); params.append(int(interrupt_requested))
    if error_code is not None:
        fields.append("error_code = ?"); params.append(error_code)
    if error_message is not None:
        fields.append("error_message = ?"); params.append(error_message)
    if result is not None:
        fields.append("result_json = ?"); params.append(json.dumps(result, ensure_ascii=False))
    if status in {"AWAITING_REVIEW", "BLOCKED", "FAILED", "INTERRUPTED", "RESULT_UNCERTAIN"}:
        fields.append("finished_at = COALESCE(finished_at, ?)"); params.append(_now())
    params.append(plan_id)
    with get_connection() as connection:
        connection.execute(f"UPDATE workspace_plan_executions SET {', '.join(fields)} WHERE plan_id = ?", params)


def update_step(plan_id: str, agent_key: str, status: str, *, mission_id: str | None = None,
                result: dict | None = None, error_code: str | None = None,
                error_message: str | None = None) -> None:
    if status not in STEP_STATUSES:
        raise ValueError(f"unknown step status: {status!r}")
    fields = ["status = ?", "updated_at = ?"]
    params: list = [status, _now()]
    if mission_id is not None:
        fields.append("mission_id = ?"); params.append(mission_id)
    if result is not None:
        fields.append("result_json = ?"); params.append(json.dumps(result, ensure_ascii=False))
    if error_code is not None:
        fields.append("error_code = ?"); params.append(error_code)
    if error_message is not None:
        fields.append("error_message = ?"); params.append(error_message)
    if status in {"RUNNING", "STARTING"}:
        fields.append("started_at = COALESCE(started_at, ?)"); params.append(_now())
    if status in {"READY_FOR_REVIEW", "BLOCKED", "FAILED", "INTERRUPTED", "RESULT_UNCERTAIN"}:
        fields.append("finished_at = COALESCE(finished_at, ?)"); params.append(_now())
    params.extend([plan_id, agent_key])
    with get_connection() as connection:
        connection.execute(
            f"UPDATE workspace_plan_execution_steps SET {', '.join(fields)} WHERE plan_id = ? AND agent_key = ?",
            params,
        )


def request_interrupt(plan_id: str) -> None:
    with get_connection() as connection:
        connection.execute(
            "UPDATE workspace_plan_executions SET interrupt_requested = 1, updated_at = ? WHERE plan_id = ?",
            (_now(), plan_id),
        )


def view(plan_id: str) -> dict | None:
    row = get_execution(plan_id)
    if row is None:
        return None
    steps = get_steps(plan_id)
    return {
        "plan_id": row["plan_id"], "project_id": row["project_id"], "status": row["status"],
        "agent_keys": _json(row["agent_keys_json"], []),
        "test_profiles": _json(row["test_profiles_json"], {}),
        "max_turns_per_agent": row["max_turns_per_agent"],
        "mission_timeout_seconds": row["mission_timeout_seconds"],
        "current_index": row["current_index"],
        "interrupt_requested": bool(row["interrupt_requested"]),
        "error_code": row["error_code"], "error_message": row["error_message"],
        "result": _json(row["result_json"], None),
        "owner": {"host": row["owner_host"], "pid": row["owner_pid"]} if row["owner_pid"] else None,
        "created_at": row["created_at"], "started_at": row["started_at"],
        "finished_at": row["finished_at"], "updated_at": row["updated_at"],
        "steps": [{
            "agent_key": step["agent_key"], "ordinal": step["ordinal"], "pane_id": step["pane_id"],
            "test_profile": step["test_profile"], "status": step["status"], "mission_id": step["mission_id"],
            "result": _json(step["result_json"], None), "error_code": step["error_code"],
            "error_message": step["error_message"], "started_at": step["started_at"],
            "finished_at": step["finished_at"], "updated_at": step["updated_at"],
        } for step in steps],
    }
