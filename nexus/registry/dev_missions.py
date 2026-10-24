"""Durable records for the bounded Development Mode mission loop."""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from nexus.registry.database import get_connection

ACTIVE_STATUSES = frozenset({"PENDING", "RUNNING", "TESTING", "REPAIRING"})
TERMINAL_STATUSES = frozenset({"READY_FOR_REVIEW", "FAILED", "BLOCKED", "CANCELLED", "OUTCOME_UNKNOWN"})
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MissionBusyError(RuntimeError):
    def __init__(self, mission_id: str):
        super().__init__(f"development session already has an active autonomous mission ({mission_id!r})")
        self.mission_id = mission_id


def create_mission(
    dev_session_id: str,
    description: str,
    *,
    requested_model: str | None,
    requested_effort: str | None = None,
    allowed_models: list[str],
    test_profile: str,
) -> str:
    mission_id = f"DEVMISSION-{uuid4().hex[:8].upper()}"
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        from nexus.registry.workspace_handoff import assert_session_execution_allowed

        assert_session_execution_allowed(connection, dev_session_id)
        active = connection.execute(
            """SELECT id FROM dev_autonomous_missions
               WHERE dev_session_id = ? AND status IN ('PENDING','RUNNING','TESTING','REPAIRING')
               LIMIT 1""",
            (dev_session_id,),
        ).fetchone()
        if active is not None:
            raise MissionBusyError(active["id"])
        connection.execute(
            """INSERT INTO dev_autonomous_missions
               (id, dev_session_id, description, requested_model, requested_effort,
                allowed_models_json, test_profile, status, owner_host,
                owner_pid, owner_acquired_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?)""",
            (
                mission_id,
                dev_session_id,
                description,
                requested_model,
                requested_effort,
                json.dumps(allowed_models),
                test_profile,
                socket.gethostname(),
                os.getpid(),
                _now(),
                _now(),
                _now(),
            ),
        )
    return mission_id


def get_mission(mission_id: str):
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM dev_autonomous_missions WHERE id = ?", (mission_id,)
        ).fetchone()


def get_mission_for_session(dev_session_id: str, mission_id: str):
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM dev_autonomous_missions WHERE dev_session_id = ? AND id = ?",
            (dev_session_id, mission_id),
        ).fetchone()


def list_missions(dev_session_id: str, limit: int = 50):
    with get_connection() as connection:
        return connection.execute(
            """SELECT * FROM dev_autonomous_missions WHERE dev_session_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (dev_session_id, limit),
        ).fetchall()


def get_active_mission_for_session(dev_session_id: str):
    with get_connection() as connection:
        return connection.execute(
            """SELECT * FROM dev_autonomous_missions
               WHERE dev_session_id = ? AND status IN ('PENDING','RUNNING','TESTING','REPAIRING')
               ORDER BY created_at DESC LIMIT 1""",
            (dev_session_id,),
        ).fetchone()


def update_status(
    mission_id: str,
    status: str,
    *,
    current_turn_id: str | None = None,
    attempt_count: int | None = None,
    result: dict | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    if status not in ALL_STATUSES:
        raise ValueError(f"unknown autonomous mission status: {status!r}")
    fields = ["status = ?", "updated_at = ?"]
    params: list = [status, _now()]
    if status == "RUNNING":
        fields.append("started_at = COALESCE(started_at, ?)")
        params.append(_now())
    if status in TERMINAL_STATUSES:
        fields.append("finished_at = ?")
        params.append(_now())
    if current_turn_id is not None:
        fields.append("current_turn_id = ?")
        params.append(current_turn_id)
    if attempt_count is not None:
        fields.append("attempt_count = ?")
        params.append(attempt_count)
    if result is not None:
        fields.append("result_json = ?")
        params.append(json.dumps(result, ensure_ascii=False))
    if error_code is not None:
        fields.append("error_code = ?")
        params.append(error_code)
    if error_message is not None:
        fields.append("error_message = ?")
        params.append(error_message)
    params.append(mission_id)
    with get_connection() as connection:
        connection.execute(
            f"UPDATE dev_autonomous_missions SET {', '.join(fields)} WHERE id = ?", params
        )


def request_interrupt(mission_id: str) -> None:
    with get_connection() as connection:
        connection.execute(
            "UPDATE dev_autonomous_missions SET interrupt_requested = 1, updated_at = ? WHERE id = ?",
            (_now(), mission_id),
        )


def create_test_run(mission_id: str, attempt_number: int, profile: str, argv: list[str]) -> str:
    run_id = f"DEVTEST-{uuid4().hex[:8].upper()}"
    with get_connection() as connection:
        connection.execute(
            """INSERT INTO dev_mission_test_runs
               (id, mission_id, attempt_number, profile, argv_json, status, started_at)
               VALUES (?, ?, ?, ?, ?, 'RUNNING', ?)""",
            (run_id, mission_id, attempt_number, profile, json.dumps(argv), _now()),
        )
    return run_id


def finish_test_run(
    run_id: str,
    status: str,
    *,
    exit_code: int | None = None,
    stdout_text: str = "",
    stderr_text: str = "",
    error_code: str | None = None,
) -> None:
    with get_connection() as connection:
        connection.execute(
            """UPDATE dev_mission_test_runs
               SET status = ?, exit_code = ?, stdout_text = ?, stderr_text = ?,
                   error_code = ?, finished_at = ? WHERE id = ?""",
            (status, exit_code, stdout_text, stderr_text, error_code, _now(), run_id),
        )


def list_test_runs(mission_id: str):
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM dev_mission_test_runs WHERE mission_id = ? ORDER BY started_at",
            (mission_id,),
        ).fetchall()


def recover_orphaned_missions(
    is_orphaned: Callable[[str | None, int | None, str | None], bool],
) -> list[str]:
    recovered: list[str] = []
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """SELECT id, owner_host, owner_pid, owner_acquired_at
               FROM dev_autonomous_missions
               WHERE status IN ('PENDING','RUNNING','TESTING','REPAIRING')"""
        ).fetchall()
        for row in rows:
            if not is_orphaned(row["owner_host"], row["owner_pid"], row["owner_acquired_at"]):
                continue
            changed = connection.execute(
                """UPDATE dev_autonomous_missions
                   SET status = 'OUTCOME_UNKNOWN', error_code = 'MISSION_OUTCOME_UNKNOWN',
                       error_message = ?, finished_at = ?, updated_at = ?
                   WHERE id = ? AND status IN ('PENDING','RUNNING','TESTING','REPAIRING')""",
                (
                    (
                        "The owning process exited before the mission result was recorded. "
                        "The worktree was preserved and the mission was not replayed."
                    ),
                    _now(), _now(), row["id"],
                ),
            )
            if changed.rowcount:
                recovered.append(row["id"])
    return recovered
