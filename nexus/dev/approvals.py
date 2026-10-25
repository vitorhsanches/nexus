"""Human approval of sensitive Codex Native actions in a Development turn.

Codex asks before a command or file change it may not perform inside its
sandbox (``item/commandExecution/requestApproval`` /
``item/fileChange/requestApproval``). The request is held open, shown in the
cockpit, and answered only by an explicit human decision. Anything else --
no decision in time, an interrupted or finished turn, revoked project
authorization, a restarted Nexus -- declines. Only the one-time ``accept``
is ever granted; session-wide or policy-amending grants are never sent.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from nexus.dev.service import DevServiceError
from nexus.registry.database import get_connection

TIMEOUT_ENV = "NEXUS_DEV_APPROVAL_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS = 300
MIN_TIMEOUT_SECONDS = 30
# Must stay below nexus.dev.executor.NATIVE_DEV_TURN_TIMEOUT_SECONDS.
MAX_TIMEOUT_SECONDS = 600
SUMMARY_TEXT_LIMIT = 2000

_KINDS = {
    "item/commandExecution/requestApproval": "command",
    "item/fileChange/requestApproval": "file_change",
}

_lock = threading.Lock()
_pending: dict[str, "_Pending"] = {}


class _Pending:
    def __init__(self, approval_id: str, dev_session_id: str, turn_id: str, client, request_id: Any,
                 operation: dict | None = None) -> None:
        self.approval_id = approval_id
        # Claude Native only: the exact operation the human decides on. It
        # lives in memory for the life of the request and is never persisted.
        self.operation = operation
        self.dev_session_id = dev_session_id
        self.turn_id = turn_id
        self.client = client
        self.request_id = request_id
        self.timer: threading.Timer | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def timeout_seconds() -> int:
    raw = os.environ.get(TIMEOUT_ENV)
    try:
        value = int(raw) if raw is not None else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return min(max(value, MIN_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)


def _text(value, limit: int = SUMMARY_TEXT_LIMIT) -> str | None:
    if isinstance(value, list):
        value = " ".join(str(part) for part in value)
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def _summary(kind: str, params: dict) -> dict:
    summary = {"kind": kind}
    for key in ("command", "cwd", "reason", "grantRoot", "itemId", "tool"):
        text = _text(params.get(key))
        if text is not None:
            summary[key] = text
    # Claude Native: the tool input (file contents, edits) is deliberately NOT
    # part of the persisted/listed summary. Only its identity and size are.
    if isinstance(params.get("operation_sha256"), str):
        summary["operation_sha256"] = params["operation_sha256"]
        summary["operation_bytes"] = int(params.get("operation_bytes") or 0)
    return summary


def _row_dict(row) -> dict:
    summary = json.loads(row["summary_json"])
    summary.pop("input", None)  # rows written before inputs stopped being persisted
    return {
        "approval_id": row["id"],
        "dev_session_id": row["dev_session_id"],
        "turn_id": row["turn_id"],
        "kind": row["kind"],
        "summary": summary,
        "status": row["status"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
    }


def _set_status(approval_id: str, status: str) -> bool:
    """Move a PENDING row to a final status exactly once."""
    with get_connection() as connection:
        cursor = connection.execute(
            "UPDATE dev_approvals SET status = ?, decided_at = ? WHERE id = ? AND status = 'PENDING'",
            (status, _now(), approval_id),
        )
        return cursor.rowcount == 1


def _answer(pending: _Pending, decision: str) -> None:
    if pending.timer is not None:
        pending.timer.cancel()
    result = {"decision": decision}
    if decision == "accept" and pending.operation is not None:
        result["operation_sha256"] = pending.operation["sha256"]
    try:
        pending.client.respond_server_request(pending.request_id, result)
    except Exception:
        # The App-Server already exited; the turn's own failure handling wins.
        pass


def deferred_handler(dev_session_id: str, turn_id: str, client_ref: list):
    """AppServerClient ``on_deferred_server_request`` for one Development turn.

    ``client_ref`` holds the client once it exists (it is created with this
    handler). Requests other than command/file-change approvals are left to
    the client's fail-closed default.
    """

    def handle(method: str, params: dict, request_id: Any) -> bool:
        kind = _KINDS.get(method)
        if kind is None or not client_ref:
            return False
        approval_id = f"APPROVAL-{uuid.uuid4().hex[:12].upper()}"
        params = dict(params)
        operation = None
        if "input" in params:
            # The full input stays in memory; only its digest and size are persisted.
            text = params.pop("input")
            digest = params.get("operation_sha256")
            if not isinstance(text, str) or not isinstance(digest, str):
                return False
            params["operation_bytes"] = len(text.encode("utf-8"))
            operation = {"text": text, "sha256": digest, "tool": params.get("tool")}
        with get_connection() as connection:
            connection.execute(
                """INSERT INTO dev_approvals (id, dev_session_id, turn_id, kind, summary_json, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'PENDING', ?)""",
                (approval_id, dev_session_id, turn_id, kind, json.dumps(_summary(kind, params)), _now()),
            )
        pending = _Pending(approval_id, dev_session_id, turn_id, client_ref[0], request_id, operation)
        pending.timer = threading.Timer(timeout_seconds(), _expire, args=(approval_id,))
        pending.timer.daemon = True
        with _lock:
            _pending[approval_id] = pending
        pending.timer.start()
        return True

    return handle


def _expire(approval_id: str) -> None:
    with _lock:
        pending = _pending.pop(approval_id, None)
    if pending is not None and _set_status(approval_id, "EXPIRED"):
        _answer(pending, "decline")


def close_turn(turn_id: str, *, interrupt: bool = False) -> None:
    """Resolve every open request of a turn that is being interrupted or has ended."""
    with _lock:
        closing = [item for item in _pending.values() if item.turn_id == turn_id]
        for item in closing:
            _pending.pop(item.approval_id, None)
    for item in closing:
        if item.timer is not None:
            item.timer.cancel()
        if _set_status(item.approval_id, "CANCELLED") and interrupt:
            _answer(item, "cancel")


def list_for_session(dev_session_id: str) -> list[dict]:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM dev_approvals WHERE dev_session_id = ? ORDER BY created_at DESC",
            (dev_session_id,),
        ).fetchall()
    with _lock:
        live = set(_pending)
    for row in rows:
        if row["status"] == "PENDING" and row["id"] not in live:
            # Its App-Server is gone (e.g. Nexus restarted): it can never be answered.
            _set_status(row["id"], "EXPIRED")
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM dev_approvals WHERE dev_session_id = ? ORDER BY created_at DESC",
            (dev_session_id,),
        ).fetchall()
    return [_row_dict(row) for row in rows]


def operation_detail(dev_session_id: str, approval_id: str) -> dict:
    """The exact operation behind one still-pending Claude approval, for the human deciding on it.

    Served from memory only while the request is live; once decided, expired,
    cancelled or after a restart it is gone and can never be shown or approved.
    """
    with get_connection() as connection:
        row = connection.execute(
            "SELECT status FROM dev_approvals WHERE id = ? AND dev_session_id = ?", (approval_id, dev_session_id)
        ).fetchone()
    if row is None:
        raise DevServiceError("APPROVAL_NOT_FOUND", "Approval request not found for this session.", http_status=404)
    with _lock:
        pending = _pending.get(approval_id)
        operation = pending.operation if pending is not None and pending.dev_session_id == dev_session_id else None
    if row["status"] != "PENDING" or operation is None:
        raise DevServiceError("OPERATION_UNAVAILABLE", "The operation is no longer available for this approval.", http_status=409)
    return {"approval_id": approval_id, "tool": operation["tool"], "operation_sha256": operation["sha256"],
            "operation": operation["text"]}


def decide(dev_session_id: str, approval_id: str, decision: str, *, operation_sha256: str | None = None) -> dict:
    if decision not in {"accept", "decline"}:
        raise DevServiceError("INVALID_DECISION", "decision must be 'accept' or 'decline'.", http_status=422)
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM dev_approvals WHERE id = ? AND dev_session_id = ?", (approval_id, dev_session_id)
        ).fetchone()
    if row is None:
        raise DevServiceError("APPROVAL_NOT_FOUND", "Approval request not found for this session.", http_status=404)
    with _lock:
        candidate = _pending.get(approval_id)
        if decision == "accept" and candidate is not None and candidate.operation is not None:
            # Accept only the operation that was presented: the caller must
            # echo its digest. A mismatch leaves the request pending.
            presented = operation_sha256 if isinstance(operation_sha256, str) else ""
            if not hmac.compare_digest(presented, candidate.operation["sha256"]):
                raise DevServiceError("OPERATION_MISMATCH",
                                      "The decision does not reference the operation that was presented.", http_status=409)
        pending = _pending.pop(approval_id, None)
    if pending is None:
        _set_status(approval_id, "EXPIRED")
        raise DevServiceError("APPROVAL_NOT_PENDING", "This approval request is no longer pending.", http_status=409)

    if decision == "accept":
        # Re-validate, not re-trust: authorization may have been revoked
        # while the request was waiting for a human.
        from nexus.dev.executor import _require_active_session

        try:
            _require_active_session(dev_session_id)
        except DevServiceError:
            _set_status(approval_id, "DECLINED")
            _answer(pending, "decline")
            raise
    status = "APPROVED" if decision == "accept" else "DECLINED"
    _set_status(approval_id, status)
    _answer(pending, decision)
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM dev_approvals WHERE id = ?", (approval_id,)).fetchone()
    return _row_dict(row)
