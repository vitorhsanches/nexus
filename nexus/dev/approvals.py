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


_STATUS_DETAILS = {
    "PENDING": "Waiting for an explicit human decision.",
    "APPROVED": "Approved once by a human; no standing permission was granted.",
    "DECLINED": "Declined by a human; the action was not executed.",
    "EXPIRED": (
        "Expired before a human decision; the action was denied and "
        "no requested action was executed."
    ),
    "CANCELLED": (
        "Cancelled because the turn ended before a decision; no requested action was executed."
    ),
}


class _Pending:
    def __init__(self, approval_id: str, dev_session_id: str, turn_id: str, client, request_id: Any) -> None:
        self.approval_id = approval_id
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


def _text(value) -> str | None:
    if isinstance(value, list):
        value = " ".join(str(part) for part in value)
    if not isinstance(value, str) or not value:
        return None
    return value[:SUMMARY_TEXT_LIMIT]


def _summary(kind: str, params: dict) -> dict:
    summary = {"kind": kind}
    for key in ("command", "cwd", "reason", "grantRoot", "itemId"):
        text = _text(params.get(key))
        if text is not None:
            summary[key] = text
    return summary


def _row_dict(row) -> dict:
    status = row["status"]
    return {
        "approval_id": row["id"],
        "dev_session_id": row["dev_session_id"],
        "turn_id": row["turn_id"],
        "kind": row["kind"],
        "summary": json.loads(row["summary_json"]),
        "status": status,
        "status_detail": _STATUS_DETAILS.get(
            status, "No additional status detail is available."
        ),
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
    try:
        pending.client.respond_server_request(pending.request_id, {"decision": decision})
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
        with _lock:
            # A reader treats a durable PENDING row without a live request as
            # orphaned. Publish both under the same lock so it cannot expire
            # an approval between the SQLite commit and in-memory registration.
            with get_connection() as connection:
                connection.execute(
                    """INSERT INTO dev_approvals (id, dev_session_id, turn_id, kind, summary_json, status, created_at)
                       VALUES (?, ?, ?, ?, ?, 'PENDING', ?)""",
                    (approval_id, dev_session_id, turn_id, kind, json.dumps(_summary(kind, params)), _now()),
                )
            pending = _Pending(approval_id, dev_session_id, turn_id, client_ref[0], request_id)
            pending.timer = threading.Timer(timeout_seconds(), _expire, args=(approval_id,))
            pending.timer.daemon = True
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


def decide(dev_session_id: str, approval_id: str, decision: str) -> dict:
    if decision not in {"accept", "decline"}:
        raise DevServiceError("INVALID_DECISION", "decision must be 'accept' or 'decline'.", http_status=422)
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM dev_approvals WHERE id = ? AND dev_session_id = ?", (approval_id, dev_session_id)
        ).fetchone()
    if row is None:
        raise DevServiceError("APPROVAL_NOT_FOUND", "Approval request not found for this session.", http_status=404)
    with _lock:
        pending = _pending.pop(approval_id, None)
    if pending is None:
        status = row["status"]
        if status == "EXPIRED":
            raise DevServiceError(
                "APPROVAL_EXPIRED",
                (
                    "Approval expired before a human decision; the action was denied "
                    "and no requested action was executed."
                ),
                http_status=409,
            )
        if status == "CANCELLED":
            raise DevServiceError(
                "APPROVAL_CANCELLED",
                (
                    "Approval was cancelled because the turn ended; "
                    "no requested action was executed."
                ),
                http_status=409,
            )
        if status in {"APPROVED", "DECLINED"}:
            raise DevServiceError(
                "APPROVAL_ALREADY_DECIDED",
                f"Approval was already recorded as {status.lower()}.",
                http_status=409,
            )
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
