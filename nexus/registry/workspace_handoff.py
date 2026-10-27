"""Durable P2B.2A handoff snapshots and one-way filesystem write barrier."""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from uuid import uuid4

from nexus.registry.database import get_connection


def _now() -> str:
    return datetime.now(UTC).isoformat()


class HandoffBlocked(RuntimeError):
    """A planned session cannot execute until dependency transfer is proven."""


def assert_session_execution_allowed(connection, session_id: str, *, turn: bool = False) -> None:
    """Run inside the mission/turn BEGIN IMMEDIATE transaction.

    This closes direct Development API bypasses and serializes starts with
    handoff.claim() across processes.
    """
    source_transfer = connection.execute(
        """SELECT 1 FROM workspace_handoffs WHERE source_session_id = ?
           AND status = 'TRANSFERRING' LIMIT 1""", (session_id,),
    ).fetchone()
    if source_transfer:
        raise HandoffBlocked("Source handoff is in progress; writing is blocked.")
    uncertain = connection.execute(
        """SELECT 1 FROM workspace_handoffs WHERE target_session_id = ?
           AND status IN ('TRANSFERRING','RESULT_UNCERTAIN') LIMIT 1""", (session_id,),
    ).fetchone()
    if uncertain:
        raise HandoffBlocked("Target handoff is in progress or requires human review.")
    target = connection.execute(
        "SELECT * FROM workspace_panes WHERE dev_session_id = ?", (session_id,),
    ).fetchone()
    if target is not None and target["plan_id"]:
        from nexus.registry import workspace_execution

        execution = connection.execute(
            "SELECT * FROM workspace_plan_executions WHERE plan_id=?", (target["plan_id"],)
        ).fetchone()
        if execution is not None:
            context = workspace_execution.current_launch()
            step = connection.execute(
                """SELECT * FROM workspace_plan_execution_steps
                   WHERE plan_id=? AND agent_key=?""",
                (target["plan_id"], target["plan_agent_key"]),
            ).fetchone()
            allowed = (execution["status"] == "RUNNING" and context is not None
                       and context == (target["plan_id"], target["plan_agent_key"], execution["owner_token"])
                       and execution["lease_renewed_at"] > workspace_execution._cutoff()
                       and step is not None)
            if turn:
                backend_row = connection.execute(
                    "SELECT backend FROM dev_sessions WHERE id=?", (session_id,)
                ).fetchone()
                if backend_row is not None and backend_row["backend"] == "codex_native":
                    # Codex Native has no mission row to prove a legitimate
                    # coordinator launch (mission_runner is never involved),
                    # so the durable proof is instead: this step was marked
                    # STARTING by the coordinator (checked above against the
                    # live owner_token/lease) and this session has never had
                    # a turn before. Native has no repair-turn loop, so
                    # unlike OmniRoute's mission there is exactly one launch
                    # opportunity, ever, per step -- the moment a dev_turns
                    # row exists for this session (whatever its outcome),
                    # this condition is permanently false and a replay is
                    # refused, with no extra column needed to track it.
                    no_prior_turn = connection.execute(
                        "SELECT 1 FROM dev_turns WHERE dev_session_id = ? LIMIT 1", (session_id,)
                    ).fetchone() is None
                    allowed = allowed and step["status"] == "STARTING" and step["mission_id"] is None and no_prior_turn
                else:
                    active_mission = connection.execute(
                        """SELECT 1 FROM dev_autonomous_missions WHERE dev_session_id=?
                           AND status IN ('PENDING','RUNNING','TESTING','REPAIRING') LIMIT 1""",
                        (session_id,),
                    ).fetchone()
                    allowed = allowed and step["status"] in {"STARTING", "RUNNING"} and active_mission is not None
            else:
                allowed = allowed and step["status"] == "STARTING" and step["mission_id"] is None
            if not allowed:
                raise HandoffBlocked("Durable sequence owns this pane; direct execution or replay is blocked.")
    if target is None or not target["plan_id"] or not target["brief_json"]:
        return
    try:
        dependencies = json.loads(target["brief_json"]).get("depends_on") or []
    except (TypeError, ValueError):
        raise HandoffBlocked("Target dependency record is invalid.") from None
    for key in dependencies:
        source = connection.execute(
            "SELECT * FROM workspace_panes WHERE plan_id = ? AND plan_agent_key = ?",
            (target["plan_id"], key),
        ).fetchone()
        if source is None:
            raise HandoffBlocked("Declared dependency agent is missing.")
        source_backend_row = connection.execute(
            "SELECT backend FROM dev_sessions WHERE id=?", (source["dev_session_id"],)
        ).fetchone()
        # P5.3A: a native source has no mission row -- the dependency
        # evidence is a human-created, still-REVIEWED integration review,
        # never "no mission_id exists, so the dependency is satisfied".
        if source_backend_row is not None and source_backend_row["backend"] == "codex_native":
            step = connection.execute(
                """SELECT * FROM workspace_plan_execution_steps WHERE plan_id=? AND agent_key=?""",
                (target["plan_id"], key),
            ).fetchone()
            if (step is None or step["status"] != "READY_FOR_REVIEW" or step["error_code"]
                    or not step["native_review_id"] or step["mission_id"]):
                raise HandoffBlocked("Declared dependency is not ready for review.")
            review = connection.execute(
                "SELECT * FROM dev_integration_reviews WHERE review_id = ?", (step["native_review_id"],)
            ).fetchone()
            if (review is None or review["dev_session_id"] != source["dev_session_id"]
                    or review["status"] != "REVIEWED" or review["error_code"]):
                raise HandoffBlocked("Dependency review is invalid or no longer current.")
            try:
                snapshot = json.loads(review["snapshot_json"] or "{}")
                changed = (snapshot.get("diff") or {}).get("files") or []
            except (TypeError, ValueError):
                raise HandoffBlocked("Dependency review snapshot is invalid.") from None
            if changed:
                transferred = connection.execute(
                    """SELECT 1 FROM workspace_handoffs WHERE source_backend = 'codex_native'
                       AND source_native_review_id = ? AND target_pane_id = ?
                       AND status = 'TRANSFERRED' LIMIT 1""",
                    (step["native_review_id"], target["id"]),
                ).fetchone()
                if transferred is None:
                    raise HandoffBlocked("Dependency worktree changes have not been transferred.")
        else:
            mission = connection.execute(
                """SELECT * FROM dev_autonomous_missions WHERE dev_session_id = ?
                   ORDER BY created_at DESC, id DESC LIMIT 1""", (source["dev_session_id"],),
            ).fetchone()
            if mission is None or mission["status"] != "READY_FOR_REVIEW":
                raise HandoffBlocked("Declared dependency is not ready for review.")
            try:
                changed = json.loads(mission["result_json"] or "{}").get("changed_files") or []
            except (TypeError, ValueError):
                raise HandoffBlocked("Dependency mission result is invalid.") from None
            if changed:
                transferred = connection.execute(
                    """SELECT 1 FROM workspace_handoffs WHERE source_backend = 'omniroute'
                       AND source_mission_id = ? AND target_pane_id = ? AND status = 'TRANSFERRED' LIMIT 1""",
                    (mission["id"], target["id"]),
                ).fetchone()
                if transferred is None:
                    raise HandoffBlocked("Dependency worktree changes have not been transferred.")


def view(row) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    data["handoff_id"] = data.pop("id")
    data["files"] = json.loads(data.pop("files_json"))
    result_json = data.pop("result_json")
    data["result"] = json.loads(result_json) if result_json else None
    data.pop("owner_token")
    data.pop("owner_host")
    data.pop("owner_pid")
    data.pop("owner_acquired_at")
    return data


def get(handoff_id: str):
    with get_connection() as connection:
        return connection.execute("SELECT * FROM workspace_handoffs WHERE id = ?", (handoff_id,)).fetchone()


def list_for_plan(plan_id: str) -> list[dict]:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM workspace_handoffs WHERE plan_id = ? ORDER BY created_at DESC, id DESC",
            (plan_id,),
        ).fetchall()
    return [view(row) for row in rows]


def latest_transfer(source_backend: str, source_evidence_id: str, target_pane_id: str) -> dict | None:
    """source_evidence_id is a mission_id for 'omniroute', a native_review_id
    for 'codex_native' -- never guessed from its shape, always named by the
    caller's own resolved backend."""
    with get_connection() as connection:
        row = connection.execute(
            """SELECT * FROM workspace_handoffs WHERE source_backend = ?
               AND COALESCE(source_mission_id, source_native_review_id) = ?
               AND target_pane_id = ? AND status = 'TRANSFERRED'
               ORDER BY finished_at DESC LIMIT 1""",
            (source_backend, source_evidence_id, target_pane_id),
        ).fetchone()
    return view(row)


def target_has_unresolved_transfer(target_session_id: str) -> bool:
    with get_connection() as connection:
        row = connection.execute(
            """SELECT 1 FROM workspace_handoffs WHERE target_session_id = ?
               AND status IN ('TRANSFERRING','RESULT_UNCERTAIN') LIMIT 1""",
            (target_session_id,),
        ).fetchone()
    return row is not None


def _session_has_active_execution(connection, session_id: str) -> bool:
    """Read durable turn and mission reservations inside the caller's transaction."""
    return connection.execute(
        """SELECT 1 FROM dev_turns WHERE dev_session_id = ?
           AND status IN ('PENDING','RUNNING')
           UNION SELECT 1 FROM dev_autonomous_missions WHERE dev_session_id = ?
           AND status IN ('PENDING','RUNNING','TESTING','REPAIRING') LIMIT 1""",
        (session_id, session_id),
    ).fetchone() is not None


def _claim_identity_valid(connection, row) -> bool:
    """Check durable bindings under the same write transaction as the claim.

    Filesystem content and the review fingerprint are rechecked by the service
    immediately before and after this transaction.
    """
    source = connection.execute(
        """SELECT p.*, s.status AS session_status, s.base_sha AS session_base_sha
           FROM workspace_panes p JOIN dev_sessions s ON s.id = p.dev_session_id
           WHERE p.id = ?""", (row["source_pane_id"],),
    ).fetchone()
    target = connection.execute(
        """SELECT p.*, s.status AS session_status, s.base_sha AS session_base_sha
           FROM workspace_panes p JOIN dev_sessions s ON s.id = p.dev_session_id
           WHERE p.id = ?""", (row["target_pane_id"],),
    ).fetchone()
    if source is None or target is None:
        return False
    if row["source_backend"] == "codex_native":
        if not row["source_native_review_id"] or row["source_mission_id"]:
            return False
        evidence = connection.execute(
            "SELECT * FROM dev_integration_reviews WHERE review_id = ?", (row["source_native_review_id"],),
        ).fetchone()
        if (evidence is None or evidence["dev_session_id"] != row["source_session_id"]
                or evidence["status"] != "REVIEWED" or evidence["error_code"]):
            return False
    else:
        if not row["source_mission_id"] or row["source_native_review_id"]:
            return False
        evidence = connection.execute(
            "SELECT * FROM dev_autonomous_missions WHERE id = ?", (row["source_mission_id"],),
        ).fetchone()
        if (evidence is None or evidence["dev_session_id"] != row["source_session_id"]
                or evidence["status"] != "READY_FOR_REVIEW" or evidence["error_code"]):
            return False
    if (source["project_id"] != row["project_id"] or target["project_id"] != row["project_id"]
            or source["plan_id"] != row["plan_id"] or target["plan_id"] != row["plan_id"]
            or source["plan_agent_key"] != row["source_agent_key"]
            or target["plan_agent_key"] != row["target_agent_key"]
            or source["dev_session_id"] != row["source_session_id"]
            or target["dev_session_id"] != row["target_session_id"]
            or source["session_status"] != "ACTIVE" or target["session_status"] != "ACTIVE"
            or source["session_base_sha"] != row["source_base_sha"]
            or target["session_base_sha"] != row["target_base_sha"]):
        return False
    try:
        return row["source_agent_key"] in (json.loads(target["brief_json"] or "{}").get("depends_on") or [])
    except (TypeError, ValueError):
        return False


def create(snapshot: dict) -> dict:
    handoff_id = f"HANDOFF-{uuid4().hex[:12].upper()}"
    evidence_id = snapshot["source_mission_id"] or snapshot["source_native_review_id"]
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """SELECT * FROM workspace_handoffs WHERE source_backend = ?
               AND COALESCE(source_mission_id, source_native_review_id) = ?
               AND target_pane_id = ? AND snapshot_fingerprint = ?
               AND status IN ('PREVIEWED','APPROVED','TRANSFERRED')
               ORDER BY created_at DESC LIMIT 1""",
            (snapshot["source_backend"], evidence_id, snapshot["target_pane_id"], snapshot["snapshot_fingerprint"]),
        ).fetchone()
        if existing is not None:
            return view(existing)
        connection.execute(
            """INSERT INTO workspace_handoffs
               (id, project_id, plan_id, source_agent_key, target_agent_key,
                source_pane_id, target_pane_id, source_backend, source_mission_id,
                source_native_review_id, source_session_id, target_session_id, source_base_sha,
                source_head_sha, target_base_sha, target_head_sha,
                snapshot_fingerprint, files_json, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PREVIEWED', ?)""",
            (handoff_id, *(snapshot[key] for key in (
                "project_id", "plan_id", "source_agent_key", "target_agent_key",
                "source_pane_id", "target_pane_id", "source_backend", "source_mission_id",
                "source_native_review_id", "source_session_id", "target_session_id", "source_base_sha",
                "source_head_sha", "target_base_sha", "target_head_sha",
                "snapshot_fingerprint",
            )), json.dumps(snapshot["files"], ensure_ascii=False), _now()),
        )
    return view(get(handoff_id))


def approve(handoff_id: str, fingerprint: str) -> bool:
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """UPDATE workspace_handoffs SET status = 'APPROVED', approved_at = ?
               WHERE id = ? AND status = 'PREVIEWED' AND snapshot_fingerprint = ?""",
            (_now(), handoff_id, fingerprint),
        )
    return cursor.rowcount == 1


def claim(handoff_id: str) -> str | None:
    token = uuid4().hex
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM workspace_handoffs WHERE id = ?", (handoff_id,)).fetchone()
        if row is None or row["status"] != "APPROVED":
            return None
        if not _claim_identity_valid(connection, row):
            return None
        busy = connection.execute(
            """SELECT 1 FROM workspace_handoffs
               WHERE (target_session_id = ? OR source_session_id = ?)
               AND status IN ('TRANSFERRING','RESULT_UNCERTAIN') LIMIT 1""",
            (row["target_session_id"], row["source_session_id"]),
        ).fetchone()
        if busy:
            return None
        if (_session_has_active_execution(connection, row["source_session_id"])
                or _session_has_active_execution(connection, row["target_session_id"])):
            return None
        connection.execute(
            """UPDATE workspace_handoffs SET status = 'TRANSFERRING',
               owner_token = ?, owner_host = ?, owner_pid = ?,
               owner_acquired_at = ?, started_at = ?
               WHERE id = ? AND status = 'APPROVED'""",
            (token, socket.gethostname(), os.getpid(), _now(), _now(), handoff_id),
        )
    return token


def owns(handoff_id: str, token: str) -> bool:
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT 1 FROM workspace_handoffs WHERE id = ?
               AND status = 'TRANSFERRING' AND owner_token = ?""",
            (handoff_id, token),
        ).fetchone()
    return row is not None


def finish(handoff_id: str, token: str, status: str, *, result: dict | None = None,
           error_code: str | None = None, error_message: str | None = None) -> bool:
    if status not in {"TRANSFERRED", "BLOCKED", "RESULT_UNCERTAIN"}:
        raise ValueError(status)
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """UPDATE workspace_handoffs SET status = ?, finished_at = ?,
               result_json = ?, error_code = ?, error_message = ?, owner_token = NULL
               WHERE id = ? AND status = 'TRANSFERRING' AND owner_token = ?""",
            (status, _now(), json.dumps(result) if result is not None else None,
             error_code, error_message, handoff_id, token),
        )
    return cursor.rowcount == 1


def recover_orphaned() -> list[str]:
    """Mark dead local owners uncertain; never replay a filesystem write."""
    from nexus.orchestration.ownership import classify_ownership

    recovered = []
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """SELECT id, owner_host, owner_pid, owner_acquired_at
               FROM workspace_handoffs WHERE status = 'TRANSFERRING'"""
        ).fetchall()
        for row in rows:
            if classify_ownership(row["owner_host"], row["owner_pid"], row["owner_acquired_at"]) != "dead_same_host":
                continue
            cursor = connection.execute(
                """UPDATE workspace_handoffs SET status = 'RESULT_UNCERTAIN',
                   error_code = 'HANDOFF_OWNER_EXITED',
                   error_message = 'Owner exited before transfer outcome was recorded; inspect target files.',
                   finished_at = ?, owner_token = NULL
                   WHERE id = ? AND status = 'TRANSFERRING'""",
                (_now(), row["id"]),
            )
            if cursor.rowcount:
                recovered.append(row["id"])
    return recovered


def block(handoff_id: str, status: str, code: str, message: str) -> None:
    if status not in {"PREVIEWED", "APPROVED"}:
        raise ValueError(status)
    with get_connection() as connection:
        connection.execute(
            """UPDATE workspace_handoffs SET status = 'BLOCKED', finished_at = ?,
               error_code = ?, error_message = ? WHERE id = ? AND status = ?""",
            (_now(), code, message, handoff_id, status),
        )
