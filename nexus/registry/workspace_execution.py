"""Durable records for P2B.1 sequential Workspace plan coordination."""

from __future__ import annotations

import json
import os
import socket
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from nexus.registry.database import get_connection

LEASE_SECONDS = 30
_launch: ContextVar[tuple[str, str, str] | None] = ContextVar("workspace_launch", default=None)


@contextmanager
def coordinated_launch(plan_id: str, agent_key: str, owner_token: str):
    """Carry a private credential; the SQLite owner fence remains authoritative."""
    reset = _launch.set((plan_id, agent_key, owner_token))
    try:
        yield
    finally:
        _launch.reset(reset)


def current_launch() -> tuple[str, str, str] | None:
    return _launch.get()


def assert_manual_execution_allowed(pane) -> None:
    """Friendly service-level gate; registry repeats it transactionally."""
    if not pane["plan_id"]:
        return
    row = get_execution(pane["plan_id"])
    if row is None:
        return
    context = current_launch()
    if (row["status"] != "RUNNING" or context is None
            or context != (pane["plan_id"], pane["plan_agent_key"], row["owner_token"])):
        from nexus.dev.service import DevServiceError

        raise DevServiceError(
            "COORDINATED_EXECUTION_REQUIRED",
            "This agent belongs to a durable sequence; use its coordinator and explicit resume.",
            http_status=409,
        )


class OwnershipLost(RuntimeError):
    """The worker no longer holds an unexpired fenced lease."""


def _cutoff(seconds: int = LEASE_SECONDS) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def _require_owner(connection, plan_id: str, owner_token: str) -> None:
    # Serialize the fence check and the following mutation with takeover.
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute(
        """SELECT 1 FROM workspace_plan_executions
           WHERE plan_id = ? AND owner_token = ? AND lease_renewed_at > ?
             AND status = 'RUNNING'""", (plan_id, owner_token, _cutoff()),
    ).fetchone()
    if row is None:
        raise OwnershipLost(plan_id)

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
    "PENDING", "STARTING", "RUNNING", "AWAITING_NATIVE_REVIEW", "READY_FOR_REVIEW", "BLOCKED",
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
    confirmed_model_ids: list[str] | None = None,
    expected_agent_models: dict | None = None,
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
        if confirmed_model_ids:
            plan = connection.execute(
                "SELECT status, approval_json FROM workspace_plans WHERE id = ?", (plan_id,)
            ).fetchone()
            approval = _json(plan["approval_json"], {}) if plan is not None else {}
            if plan is None or plan["status"] != "APPROVED" or approval.get("agent_models") != expected_agent_models:
                raise ValueError("plan approval changed before cost confirmation")
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
            if confirmed_model_ids:
                _record_cost_confirmation(connection, plan_id, approval, confirmed_model_ids, now)
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
        if confirmed_model_ids:
            _record_cost_confirmation(connection, plan_id, approval, confirmed_model_ids, now)
        row = connection.execute(
            "SELECT * FROM workspace_plan_executions WHERE plan_id = ?", (plan_id,)
        ).fetchone()
    return row, True


def _record_cost_confirmation(connection, plan_id: str, approval: dict, model_ids: list[str], now: str) -> None:
    confirmed = approval.get("cost_confirmed_for")
    approval["cost_confirmed_for"] = sorted(set(confirmed if isinstance(confirmed, list) else []) | set(model_ids))
    history = approval.get("execution_cost_confirmations")
    if not isinstance(history, list):
        history = []
    history.append({"model_ids": model_ids, "confirmed_at": now})
    approval["execution_cost_confirmations"] = history
    connection.execute(
        "UPDATE workspace_plans SET approval_json = ? WHERE id = ?", (json.dumps(approval), plan_id)
    )


def claim_execution(plan_id: str) -> str | None:
    """Atomically claim an awaiting plan for this process."""
    now = _now()
    token = uuid4().hex
    with get_connection() as connection:
        cursor = connection.execute(
            """UPDATE workspace_plan_executions
               SET status = 'RUNNING', owner_host = ?, owner_pid = ?, owner_acquired_at = ?,
                   started_at = COALESCE(started_at, ?), updated_at = ?,
                   owner_token = ?, lease_renewed_at = ?
               WHERE plan_id = ? AND status = 'AWAITING_EXECUTION'""",
            (socket.gethostname(), os.getpid(), now, now, now, token, now, plan_id),
        )
        return token if cursor.rowcount == 1 else None


def claim_recovery(plan_id: str, stale_after_seconds: int = LEASE_SECONDS) -> str | None:
    """Take over a durable worker only after its lease is stale."""
    now = datetime.now(UTC)
    cutoff = (now - timedelta(seconds=stale_after_seconds)).isoformat()
    stamp = now.isoformat()
    token = uuid4().hex
    with get_connection() as connection:
        cursor = connection.execute(
            """UPDATE workspace_plan_executions
               SET owner_host = ?, owner_pid = ?, owner_acquired_at = ?, updated_at = ?,
                   owner_token = ?, lease_renewed_at = ?
               WHERE plan_id = ? AND status = 'RUNNING'
                 AND (COALESCE(lease_renewed_at, owner_acquired_at) IS NULL
                      OR COALESCE(lease_renewed_at, owner_acquired_at) <= ?)""",
            (socket.gethostname(), os.getpid(), stamp, stamp, token, stamp, plan_id, cutoff),
        )
        return token if cursor.rowcount == 1 else None


def claim_resume(plan_id: str, index: int, handoff_id: str, approval_json: str) -> str | None:
    """Fence one explicitly approved BLOCKED -> RUNNING transition.

    All durable identity and state predicates are checked under one SQLite
    write reservation. Filesystem hashes are checked by the coordinator both
    before this claim and immediately before launch.
    """
    now = _now()
    token = uuid4().hex
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT e.*, p.status AS plan_status, p.approval_json
               FROM workspace_plan_executions e JOIN workspace_plans p ON p.id=e.plan_id
               WHERE e.plan_id=?""", (plan_id,),
        ).fetchone()
        if (row is None or row["status"] != "BLOCKED"
                or row["error_code"] != "DEPENDENCY_WORKTREE_NOT_TRANSFERRED"
                or row["current_index"] != index or row["interrupt_requested"]
                or row["plan_status"] != "APPROVED" or row["approval_json"] != approval_json):
            return None
        steps = connection.execute(
            "SELECT * FROM workspace_plan_execution_steps WHERE plan_id=? ORDER BY ordinal", (plan_id,)
        ).fetchall()
        keys = _json(row["agent_keys_json"], [])
        profiles = _json(row["test_profiles_json"], {})
        approval = _json(approval_json, {})
        if (not isinstance(keys, list) or not isinstance(profiles, dict)
                or not isinstance(approval, dict)
                or len(steps) != len(keys) or index < 1 or index >= len(steps)
                or [step["agent_key"] for step in steps] != keys
                or set(profiles) != set(keys)
                or approval.get("max_turns_per_agent") != row["max_turns_per_agent"]
                or approval.get("mission_timeout_seconds") != row["mission_timeout_seconds"]):
            return None
        pending = steps[index]
        if (pending["status"] != "BLOCKED" or pending["error_code"] != row["error_code"]
                or pending["mission_id"] or pending["started_at"] or pending["result_json"]
                or any(step["status"] != "READY_FOR_REVIEW" or not step["mission_id"] or step["error_code"]
                       for step in steps[:index])
                or any(step["status"] != "PENDING" or step["mission_id"] for step in steps[index + 1:])):
            return None
        previous = steps[index - 1]
        source = connection.execute("SELECT * FROM workspace_panes WHERE id=?", (previous["pane_id"],)).fetchone()
        target = connection.execute("SELECT * FROM workspace_panes WHERE id=?", (pending["pane_id"],)).fetchone()
        selected = approval.get("agent_models") or {}
        if not isinstance(selected, dict) or any(not isinstance(selected.get(key), dict) for key in keys):
            return None
        if (source is None or target is None or source["plan_id"] != plan_id
                or target["plan_id"] != plan_id or source["project_id"] != row["project_id"]
                or target["project_id"] != row["project_id"]
                or source["plan_agent_key"] != previous["agent_key"]
                or target["plan_agent_key"] != pending["agent_key"]
                or any((connection.execute(
                    "SELECT model FROM workspace_panes WHERE id=?", (step["pane_id"],)
                ).fetchone() or {"model": None})["model"] != (selected.get(step["agent_key"]) or {}).get("model")
                    or step["test_profile"] != profiles[step["agent_key"]] for step in steps)):
            return None
        try:
            dependencies = json.loads(target["brief_json"] or "{}").get("depends_on") or []
        except (TypeError, ValueError):
            return None
        if dependencies != [previous["agent_key"]]:
            return None
        handoff = connection.execute("SELECT * FROM workspace_handoffs WHERE id=?", (handoff_id,)).fetchone()
        if (handoff is None or handoff["status"] != "TRANSFERRED"
                or handoff["plan_id"] != plan_id or handoff["project_id"] != row["project_id"]
                or handoff["source_pane_id"] != source["id"] or handoff["target_pane_id"] != target["id"]
                or handoff["source_mission_id"] != previous["mission_id"]
                or connection.execute(
                    """SELECT 1 FROM workspace_handoffs WHERE target_session_id=?
                       AND status IN ('TRANSFERRING','RESULT_UNCERTAIN') LIMIT 1""",
                    (target["dev_session_id"],),
                ).fetchone()):
            return None
        try:
            history = json.loads(row["resume_history_json"]) if row["resume_history_json"] else []
        except (TypeError, ValueError):
            return None
        if not isinstance(history, list):
            return None
        history.append({"at": now, "step": pending["agent_key"], "handoff_id": handoff_id,
                        "previous_status": row["status"], "previous_error_code": row["error_code"],
                        "previous_error_message": row["error_message"]})
        connection.execute(
            """UPDATE workspace_plan_execution_steps SET status='PENDING', error_code=NULL,
               error_message=NULL, finished_at=NULL, updated_at=?
               WHERE plan_id=? AND agent_key=? AND status='BLOCKED' AND mission_id IS NULL""",
            (now, plan_id, pending["agent_key"]),
        )
        connection.execute(
            """UPDATE workspace_plan_executions SET status='RUNNING', owner_host=?, owner_pid=?,
               owner_acquired_at=?, owner_token=?, lease_renewed_at=?, updated_at=?,
               error_code=NULL, error_message=NULL, finished_at=NULL, resume_history_json=?
               WHERE plan_id=? AND status='BLOCKED'""",
            (socket.gethostname(), os.getpid(), now, token, now, now,
             json.dumps(history, ensure_ascii=False), plan_id),
        )
    return token


def claim_native_review(plan_id: str, agent_key: str, review_id: str) -> str | None:
    """Fence one explicit, human-confirmed AWAITING_NATIVE_REVIEW -> RUNNING
    transition, mirroring claim_resume()'s shape for the handoff case.

    Purely mechanical: the caller (coordinator.bind_native_review) has
    already validated the review's identity, freshness and approval_ready
    state against the exact session and turn before calling this. This
    function only re-checks the durable preconditions that must hold at
    the moment of the claim, under one write reservation, and never
    inspects the review itself.
    """
    now = _now()
    token = uuid4().hex
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        execution = connection.execute(
            "SELECT * FROM workspace_plan_executions WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if (execution is None or execution["status"] != "BLOCKED"
                or execution["error_code"] != "AWAITING_NATIVE_REVIEW"):
            return None
        steps = connection.execute(
            "SELECT * FROM workspace_plan_execution_steps WHERE plan_id=? ORDER BY ordinal", (plan_id,)
        ).fetchall()
        index = execution["current_index"]
        if not isinstance(index, int) or index < 0 or index >= len(steps):
            return None
        step = steps[index]
        if (step["agent_key"] != agent_key or step["status"] != "AWAITING_NATIVE_REVIEW"
                or not step["native_turn_id"] or step["native_review_id"] or step["mission_id"]):
            return None
        connection.execute(
            """UPDATE workspace_plan_execution_steps
               SET status='READY_FOR_REVIEW', native_review_id=?,
                   finished_at=COALESCE(finished_at, ?), updated_at=?
               WHERE plan_id=? AND agent_key=? AND status='AWAITING_NATIVE_REVIEW'
                 AND native_review_id IS NULL""",
            (review_id, now, now, plan_id, agent_key),
        )
        connection.execute(
            """UPDATE workspace_plan_executions SET status='RUNNING', owner_host=?, owner_pid=?,
               owner_acquired_at=?, owner_token=?, lease_renewed_at=?, updated_at=?,
               error_code=NULL, error_message=NULL, finished_at=NULL
               WHERE plan_id=? AND status='BLOCKED'""",
            (socket.gethostname(), os.getpid(), now, token, now, now, plan_id),
        )
    return token


def renew_lease(plan_id: str, owner_token: str) -> bool:
    now = _now()
    with get_connection() as connection:
        cursor = connection.execute(
            """UPDATE workspace_plan_executions SET lease_renewed_at = ?
               WHERE plan_id = ? AND owner_token = ? AND status = 'RUNNING'
                 AND lease_renewed_at > ?""", (now, plan_id, owner_token, _cutoff()),
        )
        return cursor.rowcount == 1


def update_execution(plan_id: str, status: str, *, current_index: int | None = None,
                     interrupt_requested: bool | None = None, error_code: str | None = None,
                     error_message: str | None = None, result: dict | None = None,
                     owner_token: str) -> None:
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
        _require_owner(connection, plan_id, owner_token)
        connection.execute(f"UPDATE workspace_plan_executions SET {', '.join(fields)} WHERE plan_id = ?", params)


def update_step(plan_id: str, agent_key: str, status: str, *, mission_id: str | None = None,
                native_turn_id: str | None = None, native_review_id: str | None = None,
                result: dict | None = None, error_code: str | None = None,
                error_message: str | None = None, owner_token: str) -> None:
    if status not in STEP_STATUSES:
        raise ValueError(f"unknown step status: {status!r}")
    fields = ["status = ?", "updated_at = ?"]
    params: list = [status, _now()]
    if mission_id is not None:
        fields.append("mission_id = ?"); params.append(mission_id)
    if native_turn_id is not None:
        fields.append("native_turn_id = ?"); params.append(native_turn_id)
    if native_review_id is not None:
        fields.append("native_review_id = ?"); params.append(native_review_id)
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
        _require_owner(connection, plan_id, owner_token)
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
    from nexus.registry import workspace_handoff

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
        "resume_history": _json(row["resume_history_json"], []),
        "result": _json(row["result_json"], None),
        "owner": {"host": row["owner_host"], "pid": row["owner_pid"]} if row["owner_pid"] else None,
        "lease_renewed_at": row["lease_renewed_at"],
        "created_at": row["created_at"], "started_at": row["started_at"],
        "finished_at": row["finished_at"], "updated_at": row["updated_at"],
        "handoffs": workspace_handoff.list_for_plan(plan_id),
        "steps": [{
            "agent_key": step["agent_key"], "ordinal": step["ordinal"], "pane_id": step["pane_id"],
            "test_profile": step["test_profile"], "status": step["status"], "mission_id": step["mission_id"],
            "native_turn_id": step["native_turn_id"], "native_review_id": step["native_review_id"],
            "result": _json(step["result_json"], None), "error_code": step["error_code"],
            "error_message": step["error_message"], "started_at": step["started_at"],
            "finished_at": step["finished_at"], "updated_at": step["updated_at"],
        } for step in steps],
    }
