"""Nexus Chat session orchestration: the HTTP layer's only entry point
into nexus.chat.appserver_client.AppServerClient.

Process lifecycle (deliberately simple, bounded, and consistent with how
Manager/Worker/Reviewer already work): a session does NOT keep a live
App-Server process running between messages. Each turn starts (or
resumes, via the session's persisted codex_home + codex_thread_id) a
single AppServerClient, runs exactly one turn, then stops it. This means
every second-or-later message in a session already exercises a real
"stop the process, start a fresh one, thread/resume" cycle -- there is no
separate idle-process supervisor to build, and at most
MAX_CONCURRENT_TURNS processes are ever alive at once (bounded, checked
before launching another).

Security default for this first Chat MVP: every thread is started/resumed
with sandbox="read-only" and the client's own default approvalPolicy
("on-request", never Worker's "never") -- enforced here, not left to a
caller-supplied parameter, and not merely a frontend convention.
"""

from __future__ import annotations

import queue
import threading
import uuid
import os
import time
from collections import deque
from pathlib import Path
from typing import Optional

from nexus.chat.appserver_client import (
    BACKEND_CODEX_NATIVE,
    BACKEND_OMNIROUTE,
    AppServerClient,
    AppServerError,
)
from nexus.failure_contract import sanitize_error_text
from nexus.registry import chat_sessions
from nexus.registry.projects import get_project

MAX_CONCURRENT_TURNS = 4
EVENT_BUFFER_LIMIT = 500
SUBSCRIBER_QUEUE_LIMIT = 200
DEFAULT_CHAT_TURN_TIMEOUT_SECONDS = 900
MIN_CHAT_TURN_TIMEOUT_SECONDS = 30
MAX_CHAT_TURN_TIMEOUT_SECONDS = 3600
CHAT_TURN_TIMEOUT_ENV = "NEXUS_CHAT_TURN_TIMEOUT_SECONDS"
# Deliberately read-only for the first Chat MVP -- never Worker's
# "workspace-write" default, and never silently upgraded by a caller.
CHAT_SANDBOX = "read-only"
CHAT_APPROVAL_POLICY = "on-request"
HISTORY_MAX_PAGE_SIZE = 100
HISTORY_CURSOR_MAX_LENGTH = 4096
HISTORY_MAX_TURN_PAGES = 100
DEFAULT_CHAT_MODEL = "cx/gpt-5.6-sol"
_model_catalog_lock = threading.Lock()
_model_catalog_cache: tuple[float, list[dict]] | None = None
_native_catalog_cache: tuple[float, list[dict], dict] | None = None


def _chat_turn_timeout_seconds() -> int:
    """Return the bounded, Chat-only App-Server turn deadline.

    Invalid configuration fails the requested turn before launching the
    App-Server or starting inference. The global AppServerClient default is
    intentionally unchanged for its other consumers.
    """
    raw = os.environ.get(CHAT_TURN_TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_CHAT_TURN_TIMEOUT_SECONDS
    try:
        value = int(raw.strip())
    except (AttributeError, ValueError):
        raise ChatError(
            "INVALID_TURN_TIMEOUT_CONFIG",
            f"{CHAT_TURN_TIMEOUT_ENV} must be an integer between "
            f"{MIN_CHAT_TURN_TIMEOUT_SECONDS} and {MAX_CHAT_TURN_TIMEOUT_SECONDS} seconds.",
            http_status=500,
        ) from None
    if not MIN_CHAT_TURN_TIMEOUT_SECONDS <= value <= MAX_CHAT_TURN_TIMEOUT_SECONDS:
        raise ChatError(
            "INVALID_TURN_TIMEOUT_CONFIG",
            f"{CHAT_TURN_TIMEOUT_ENV} must be between "
            f"{MIN_CHAT_TURN_TIMEOUT_SECONDS} and {MAX_CHAT_TURN_TIMEOUT_SECONDS} seconds.",
            http_status=500,
        )
    return value


def _model_catalog(*, refresh: bool = False) -> list[dict]:
    """Return OmniRoute's exact selectable route IDs with optional metadata.

    App-Server model metadata is attached only for an exact ID match. The
    catalogs have different naming conventions, so model/list must never
    filter the OmniRoute routes or rewrite their IDs. Discovery does not
    verify inference; activation is checked on the actual session before a
    turn starts. Cost remains unknown without trusted price metadata.
    """
    global _model_catalog_cache
    with _model_catalog_lock:
        if not refresh and _model_catalog_cache and _model_catalog_cache[0] > time.monotonic():
            return [dict(item) for item in _model_catalog_cache[1]]
        from nexus.routing.telemetry import OmniRouteTelemetryClient

        telemetry = OmniRouteTelemetryClient()
        discovered = telemetry.fetch_models()
        omni_models = {item.model_id: item for item in discovered}
        metadata: dict[str, dict] = {}
        # model/list is optional enrichment. An unavailable or differently
        # named App-Server catalog must not hide routes advertised by OmniRoute.
        client = None
        started = False
        try:
            client = AppServerClient(Path.cwd(), sandbox_mode_override=CHAT_SANDBOX)
            client.start()
            started = True
            cursor = None
            seen_cursors: set[str] = set()
            for _page in range(10):
                try:
                    response = client.model_list(cursor=cursor, limit=100)
                except Exception:
                    break
                entries = response.get("data") if isinstance(response, dict) else None
                if not isinstance(entries, list):
                    break
                for entry in entries:
                    if not isinstance(entry, dict) or entry.get("hidden") is True:
                        continue
                    model_id = entry.get("id") or entry.get("model")
                    if isinstance(model_id, str) and model_id in omni_models and model_id not in metadata:
                        metadata[model_id] = entry
                cursor = response.get("nextCursor")
                if not cursor:
                    break
                if not isinstance(cursor, str) or cursor in seen_cursors:
                    break
                seen_cursors.add(cursor)
        except Exception:
            # OmniRoute remains the source of selectable route IDs.
            metadata = {}
        finally:
            if started and client is not None:
                client.stop()

        rows: list[dict] = []
        for model_id, route in omni_models.items():
            entry = metadata.get(model_id, {})
            effort_rows = entry.get("supportedReasoningEfforts") or []
            efforts = []
            if isinstance(effort_rows, list):
                for effort_row in effort_rows:
                    value = effort_row.get("reasoningEffort") if isinstance(effort_row, dict) else effort_row
                    if isinstance(value, str) and value not in efforts:
                        efforts.append(value)
            default_effort = entry.get("defaultReasoningEffort")
            if not isinstance(default_effort, str) or default_effort not in efforts:
                default_effort = None
            rows.append({
                "id": model_id,
                "name": str(entry.get("displayName") or model_id),
                "provider": route.provider,
                "selectable": True,
                "availability": "omniroute-listed-unverified",
                "reason": "Advertised by OmniRoute; successful inference has not been verified.",
                "cost_status": "unknown",
                "supported_efforts": efforts,
                "default_effort": default_effort,
                "appserver_compatible": bool(entry and efforts),
            })
        rows.sort(key=lambda item: (item["id"] != DEFAULT_CHAT_MODEL, item["id"].casefold()))
        _model_catalog_cache = (time.monotonic() + 30, rows)
        return [dict(item) for item in rows]


def _native_model_catalog(*, refresh: bool = False) -> tuple[list[dict], dict]:
    """Models and login state straight from the local Codex App-Server.

    No OmniRoute involvement. The catalog is whatever the signed-in Codex
    account is offered; ``account`` never carries tokens or the e-mail.
    """
    global _native_catalog_cache
    from nexus.process_env import native_catalog_home

    with _model_catalog_lock:
        if not refresh and _native_catalog_cache and _native_catalog_cache[0] > time.monotonic():
            return [dict(item) for item in _native_catalog_cache[1]], dict(_native_catalog_cache[2])
        client = AppServerClient(
            Path.cwd(), sandbox_mode_override=CHAT_SANDBOX,
            codex_home=native_catalog_home(), backend=BACKEND_CODEX_NATIVE,
        )
        client.start()
        try:
            raw_account = client.account_read()
            account_row = raw_account.get("account") if isinstance(raw_account, dict) else None
            account = {
                "authenticated": isinstance(account_row, dict),
                "type": account_row.get("type") if isinstance(account_row, dict) else None,
                "plan": account_row.get("planType") if isinstance(account_row, dict) else None,
            }
            rows: list[dict] = []
            if account["authenticated"]:
                cursor = None
                seen_cursors: set[str] = set()
                for _page in range(10):
                    response = client.model_list(cursor=cursor, limit=100)
                    entries = response.get("data") if isinstance(response, dict) else None
                    if not isinstance(entries, list):
                        break
                    for entry in entries:
                        if not isinstance(entry, dict) or entry.get("hidden") is True:
                            continue
                        model_id = entry.get("model") or entry.get("id")
                        if not isinstance(model_id, str) or any(row["id"] == model_id for row in rows):
                            continue
                        efforts = []
                        for effort_row in entry.get("supportedReasoningEfforts") or []:
                            value = effort_row.get("reasoningEffort") if isinstance(effort_row, dict) else effort_row
                            if isinstance(value, str) and value not in efforts:
                                efforts.append(value)
                        default_effort = entry.get("defaultReasoningEffort")
                        rows.append({
                            "id": model_id,
                            "name": str(entry.get("displayName") or model_id),
                            "provider": "codex",
                            "selectable": True,
                            "availability": "codex-native",
                            "reason": "Offered by the local Codex App-Server for the signed-in account.",
                            "cost_status": "known",
                            "supported_efforts": efforts,
                            "default_effort": default_effort if default_effort in efforts else None,
                            "is_default": bool(entry.get("isDefault")),
                            "appserver_compatible": True,
                        })
                    cursor = response.get("nextCursor")
                    if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                        break
                    seen_cursors.add(cursor)
        finally:
            client.stop()
        rows.sort(key=lambda item: (not item["is_default"], item["id"].casefold()))
        _native_catalog_cache = (time.monotonic() + 30, rows, account)
        return [dict(item) for item in rows], dict(account)


def list_models(*, refresh: bool = False, backend: str = BACKEND_OMNIROUTE) -> dict:
    if backend == BACKEND_CODEX_NATIVE:
        try:
            models, account = _native_model_catalog(refresh=refresh)
            error = None if account["authenticated"] else (
                "Codex is not signed in. Run 'codex login' for this Windows user, then refresh."
            )
        except Exception as exc:
            models, account = [], {"authenticated": False, "type": None, "plan": None}
            error = sanitize_error_text(str(exc)) or "Codex App-Server is unavailable."
        default = next((item["id"] for item in models if item["is_default"]), models[0]["id"] if models else None)
        return {"backend": backend, "models": models, "default_model": default,
                "catalog_error": error, "account": account}
    try:
        models = _model_catalog(refresh=refresh)
        error = None
    except Exception as exc:
        models = []
        error = "Model discovery is unavailable. Existing sessions remain accessible; new sessions are disabled."
    return {
        "models": models,
        "default_model": DEFAULT_CHAT_MODEL if any(item["id"] == DEFAULT_CHAT_MODEL for item in models) else None,
        "catalog_error": error,
    }


def recover_orphaned_turns() -> list[str]:
    """Reconcile only turns whose owning Nexus process is definitely gone.

    Failed turns carry an explicit uncertain-outcome error and are never
    automatically executed again. Rows without an owner PID remain reserved.
    """
    from nexus.orchestration.ownership import classify_ownership

    return chat_sessions.recover_orphaned_turns(
        lambda host, pid, acquired_at: classify_ownership(host, pid, acquired_at)
        == "dead_same_host"
    )


class ChatError(RuntimeError):
    """Raised for known, reportable Chat API failures.

    Mirrors nexus.chat.appserver_client.AppServerError / nexus.orchestration
    .go.GoError: ``code`` is a short machine-readable reason, ``http_status``
    is the HTTP status the route layer should return for it.
    """

    def __init__(self, code: str, message: str, *, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class _ActiveTurn:
    def __init__(self) -> None:
        self.client: Optional[AppServerClient] = None
        self.thread_id: Optional[str] = None
        # The Codex-native turn id (distinct from the Nexus TURN-xxxx row
        # id used everywhere else in this module) -- turn/interrupt
        # requires both threadId AND this, per the real protocol.
        self.codex_turn_id: Optional[str] = None
        self.lock = threading.Lock()
        self.buffer: "deque[tuple[int, dict]]" = deque(maxlen=EVENT_BUFFER_LIMIT)
        self.subscribers: list["queue.Queue"] = []
        self.next_seq = 1
        self.interrupted = threading.Event()
        self.done = threading.Event()


class _SubscriberQueue(queue.Queue):
    """Bounded subscriber queue that records the highest silently evicted ID."""
    def __init__(self) -> None:
        super().__init__(maxsize=SUBSCRIBER_QUEUE_LIMIT)
        self.dropped_through = 0


_active_lock = threading.Lock()
_active_turns: dict[str, _ActiveTurn] = {}


# -- row serialization ----------------------------------------------------


def _session_dict(row) -> dict:
    return {
        "session_id": row["id"],
        "project_id": row["project_id"],
        "workspace_path": row["workspace_path"],
        "codex_thread_id": row["codex_thread_id"],
        "model": row["model"],
        "requested_model": row["requested_model"] if "requested_model" in row.keys() else None,
        "requested_effort": row["requested_effort"] if "requested_effort" in row.keys() else None,
        "provider": row["provider"],
        "backend": row["backend"] if "backend" in row.keys() and row["backend"] else BACKEND_OMNIROUTE,
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_backend(row) -> str:
    """The session's persisted backend. Legacy rows predate the column and are OmniRoute.

    Any other unknown value fails closed: a session is never silently run on
    a different backend than the one it was created for.
    """
    backend = row["backend"] if "backend" in row.keys() and row["backend"] else BACKEND_OMNIROUTE
    if backend not in {BACKEND_OMNIROUTE, BACKEND_CODEX_NATIVE}:
        raise ChatError("UNSUPPORTED_BACKEND", f"Session has an unsupported backend: {backend!r}", http_status=409)
    return backend


def _turn_dict(row) -> dict:
    return {
        "turn_id": row["id"],
        "session_id": row["session_id"],
        "idempotency_key": row["idempotency_key"],
        "request_text": row["request_text"],
        "status": row["status"],
        # Renderable content: always plain text. Never HTML/markup -- a
        # caller must escape it before inserting into a DOM, same as any
        # other untrusted text.
        "response_text": row["response_text"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "model": row["model"],
        "provider": row["provider"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


# -- sessions --------------------------------------------------------------


def _validate_project(project_id: str) -> str:
    """Return the project's registered workspace path, or fail closed.

    The workspace is ALWAYS the project's own registered path -- a caller
    never supplies a path directly, so there is no way to point a Chat
    session at an unapproved location.
    """
    project = get_project(project_id)
    if project is None:
        raise ChatError("PROJECT_NOT_FOUND", f"Unknown project: {project_id!r}", http_status=404)
    if not project["enabled"]:
        raise ChatError("PROJECT_DISABLED", f"Project is disabled: {project_id!r}", http_status=400)
    workspace_path = project["path"]
    if not Path(workspace_path).is_dir():
        raise ChatError(
            "WORKSPACE_UNAVAILABLE",
            f"Project workspace does not exist on disk: {workspace_path!r}",
            http_status=409,
        )
    return workspace_path


def create_session(
    project_id: str,
    model: str | None = DEFAULT_CHAT_MODEL,
    effort: str | None = None,
    allow_unknown_cost: bool = False,
    backend: str = BACKEND_OMNIROUTE,
) -> dict:
    if backend == BACKEND_CODEX_NATIVE:
        return _create_native_session(project_id, model, effort)
    if backend != BACKEND_OMNIROUTE:
        raise ChatError("UNSUPPORTED_BACKEND", f"Unsupported Chat backend: {backend!r}", http_status=422)
    _require_chat_provider(model)
    catalog = {item["id"]: item for item in _model_catalog()}
    selected = catalog.get(model)
    if selected is None:
        raise ChatError("MODEL_NOT_ALLOWED", "The selected Chat model is not currently advertised by OmniRoute.", http_status=422)
    if selected["cost_status"] == "unknown" and not allow_unknown_cost:
        raise ChatError("MODEL_COST_CONFIRMATION_REQUIRED", "Route cost is unknown. Explicitly confirm before creating a session.", http_status=422)
    if effort is not None and effort not in selected["supported_efforts"]:
        raise ChatError("EFFORT_NOT_SUPPORTED", "The selected reasoning effort is not supported by this model.", http_status=422)
    workspace_path = _validate_project(project_id)
    session_id = chat_sessions.create_session(project_id, workspace_path, model, effort)
    return _session_dict(chat_sessions.get_session(session_id))


def _create_native_session(project_id: str, model: str | None, effort: str | None) -> dict:
    """Codex Native: no OmniRoute catalog, OAuth gate or qualification.

    A model, when supplied, must be one the local Codex account is actually
    offered. Without one, Codex's own configured default is used.
    """
    workspace_path = _validate_project(project_id)
    if model is None and effort is not None:
        raise ChatError("EFFORT_REQUIRES_MODEL", "Choose a model before choosing a reasoning effort.", http_status=422)
    if model is not None:
        try:
            models, account = _native_model_catalog()
        except Exception as error:
            raise ChatError("NATIVE_CATALOG_UNAVAILABLE", sanitize_error_text(str(error)) or "Codex App-Server is unavailable.", http_status=503) from error
        if not account["authenticated"]:
            raise ChatError("CODEX_NOT_SIGNED_IN", "Codex is not signed in. Run 'codex login' first.", http_status=409)
        selected = next((item for item in models if item["id"] == model), None)
        if selected is None:
            raise ChatError("MODEL_NOT_ALLOWED", "The selected model is not offered by the local Codex account.", http_status=422)
        if effort is not None and effort not in selected["supported_efforts"]:
            raise ChatError("EFFORT_NOT_SUPPORTED", "The selected reasoning effort is not supported by this model.", http_status=422)
    session_id = chat_sessions.create_session(project_id, workspace_path, model, effort, BACKEND_CODEX_NATIVE)
    return _session_dict(chat_sessions.get_session(session_id))


def _require_chat_provider(model: str, backend: str = BACKEND_OMNIROUTE) -> None:
    if backend == BACKEND_CODEX_NATIVE:
        return
    from nexus.dev.service import DevServiceError
    from nexus.workspace.omniroute_oauth import require_connected_route
    try:
        require_connected_route(model)
    except DevServiceError as error:
        raise ChatError(error.code, str(error), http_status=error.http_status) from error


def _get_session_row(session_id: str):
    row = chat_sessions.get_session(session_id)
    if row is None:
        raise ChatError("SESSION_NOT_FOUND", f"Unknown session: {session_id!r}", http_status=404)
    return row


def _validated_session_workspace(row) -> str:
    """Bind a stored session to the project's current registered directory."""
    registered_path = _validate_project(row["project_id"])
    try:
        registered = Path(registered_path).resolve(strict=True)
        stored = Path(row["workspace_path"]).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ChatError(
            "WORKSPACE_BINDING_CHANGED", "Session workspace no longer matches its project.",
            http_status=409,
        ) from None
    if os.path.normcase(str(stored)) != os.path.normcase(str(registered)):
        raise ChatError(
            "WORKSPACE_BINDING_CHANGED", "Session workspace no longer matches its project.",
            http_status=409,
        )
    return str(registered)


def get_session_detail(session_id: str) -> dict:
    row = _get_session_row(session_id)
    # Re-validate on every operation, per spec: a project can be disabled
    # or its workspace removed after the session was created.
    _validated_session_workspace(row)
    detail = _session_dict(row)
    detail["turns"] = [_turn_dict(turn) for turn in chat_sessions.list_turns(session_id, limit=20)]
    return detail


def _history_item(client: AppServerClient, entry: dict) -> dict | None:
    """Project a redacted App-Server item onto the small public history shape."""
    safe_entry = client._redact_event(entry)
    item = safe_entry.get("item") if isinstance(safe_entry, dict) else None
    if not isinstance(item, dict):
        return None
    item_id = item.get("id")
    turn_id = safe_entry.get("turnId")
    item_type = item.get("type")
    if not all(isinstance(value, str) and value for value in (item_id, turn_id, item_type)):
        return None

    result = {"turn_id": turn_id, "item_id": item_id, "type": item_type}
    text: str | None = None
    if item_type == "userMessage":
        parts = [
            part.get("text")
            for part in item.get("content", [])
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        ] if isinstance(item.get("content"), list) else []
        text = "\n".join(parts)
    elif item_type in {"agentMessage", "plan"}:
        if isinstance(item.get("text"), str):
            text = item["text"]
    elif item_type == "commandExecution":
        if isinstance(item.get("command"), str):
            result["command"] = item["command"]
        if isinstance(item.get("exitCode"), int):
            result["exit_code"] = item["exitCode"]
        if isinstance(item.get("aggregatedOutput"), str):
            result["output"] = item["aggregatedOutput"]
            text = item["aggregatedOutput"]
        if isinstance(item.get("status"), str):
            result["status"] = item["status"]
    elif item_type == "functionCallOutput":
        if isinstance(item.get("name"), str):
            result["name"] = item["name"]
        output = item.get("output")
        if isinstance(output, str):
            text = output
        elif isinstance(output, list):
            text = "\n".join(
                part["text"] for part in output
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
    elif item_type == "dynamicToolCall":
        for key in ("tool", "status"):
            if isinstance(item.get(key), str):
                result[key] = item[key]
        content_items = item.get("contentItems")
        if isinstance(content_items, list):
            text = "\n".join(
                part["text"] for part in content_items
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
    elif item_type == "mcpToolCall":
        for key in ("server", "tool", "status"):
            if isinstance(item.get(key), str):
                result[key] = item[key]
        tool_result = item.get("result")
        content = tool_result.get("content") if isinstance(tool_result, dict) else None
        if isinstance(content, list):
            text = "\n".join(
                part["text"] for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        tool_error = item.get("error")
        if isinstance(tool_error, dict) and isinstance(tool_error.get("message"), str):
            result["error"] = tool_error["message"]
    elif item_type in {"fileChange", "webSearch", "functionCall"}:
        for key in ("name", "status"):
            if isinstance(item.get(key), str):
                result[key] = item[key]
        # Unknown details remain omitted; the stable item ID/type still
        # represents the historical event without exposing arbitrary data.
    if text is not None:
        result["text"] = text
    for source, target in (("startedAtMs", "started_at_ms"), ("completedAtMs", "completed_at_ms")):
        stamp = safe_entry.get(source)
        if isinstance(stamp, int) and not isinstance(stamp, bool):
            result[target] = stamp
    return result


def _validate_history_cursor(cursor: str | None) -> None:
    if cursor is not None and (not cursor or len(cursor) > HISTORY_CURSOR_MAX_LENGTH):
        raise ChatError("INVALID_HISTORY_CURSOR", "History cursor is invalid.", http_status=422)


def _verify_codex_turn(client: AppServerClient, thread_id: str, turn_id: str) -> None:
    cursor = None
    seen_cursors: set[str] = set()
    for _ in range(HISTORY_MAX_TURN_PAGES):
        page = client.thread_turns_list(thread_id, cursor=cursor, limit=100, sort_direction="desc")
        turns = page.get("data") if isinstance(page, dict) else None
        if not isinstance(turns, list):
            raise ChatError("HISTORY_UNAVAILABLE", "Codex thread history is unavailable.", http_status=503)
        for turn in turns:
            if isinstance(turn, dict) and turn.get("id") == turn_id:
                return
        next_cursor = page.get("nextCursor")
        if next_cursor is None:
            break
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
            raise ChatError("HISTORY_UNAVAILABLE", "Codex returned an invalid history cursor.", http_status=503)
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise ChatError("HISTORY_LIMIT_EXCEEDED", "Turn lookup exceeded the history scan limit.", http_status=413)
    raise ChatError("TURN_NOT_FOUND", "Turn not found in this session's Codex thread.", http_status=404)


def _history_turns(client: AppServerClient, thread_id: str, turn_ids: list[str]) -> list[dict]:
    """Return safe lifecycle metadata for turns represented on this page."""
    if not turn_ids:
        return []
    turn_ids = list(dict.fromkeys(turn_ids))
    requested_turn_ids = set(turn_ids)
    cursor = None
    seen_cursors: set[str] = set()
    found: dict[str, dict] = {}
    for _ in range(HISTORY_MAX_TURN_PAGES):
        page = client.thread_turns_list(
            thread_id, cursor=cursor, limit=100, sort_direction="desc", items_view="notLoaded"
        )
        turns = page.get("data") if isinstance(page, dict) else None
        if not isinstance(turns, list):
            raise ChatError("HISTORY_UNAVAILABLE", "Codex turn history is unavailable.", http_status=503)
        for turn in turns:
            if not isinstance(turn, dict) or turn.get("id") not in requested_turn_ids:
                continue
            turn_id = turn["id"]
            safe = {"turn_id": turn_id}
            if isinstance(turn.get("status"), str):
                safe["status"] = turn["status"]
            for source, target in (("startedAt", "started_at"), ("completedAt", "completed_at")):
                stamp = turn.get(source)
                if isinstance(stamp, int) and not isinstance(stamp, bool):
                    safe[target] = stamp
            error = turn.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                safe["error"] = error["message"]
            found[turn_id] = safe
        if found.keys() >= requested_turn_ids:
            break
        next_cursor = page.get("nextCursor")
        if next_cursor is None:
            break
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
            raise ChatError("HISTORY_UNAVAILABLE", "Codex returned an invalid turn cursor.", http_status=503)
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise ChatError("HISTORY_LIMIT_EXCEEDED", "Turn lookup exceeded the history scan limit.", http_status=413)
    return [found[turn_id] for turn_id in turn_ids if turn_id in found]


def get_history(
    session_id: str,
    *,
    turn_id: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict:
    """Read persisted Codex history for an authorized session, without inference."""
    session_row = _get_session_row(session_id)
    workspace_path = _validated_session_workspace(session_row)
    _validate_history_cursor(cursor)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= HISTORY_MAX_PAGE_SIZE:
        raise ChatError("INVALID_HISTORY_LIMIT", "History limit must be between 1 and 100.", http_status=422)
    if turn_id is not None and (not isinstance(turn_id, str) or not turn_id or len(turn_id) > 128):
        raise ChatError("INVALID_HISTORY_TURN", "History turn_id is invalid.", http_status=422)

    codex_thread_id = session_row["codex_thread_id"]
    if not codex_thread_id:
        if chat_sessions.list_turns(session_id, limit=1):
            raise ChatError("HISTORY_UNAVAILABLE", "This session has turns but no readable Codex thread.", http_status=409)
        return {"session_id": session_id, "items": [], "turns": [], "next_cursor": None, "source": "codex_thread"}

    client = AppServerClient(
        workspace_path,
        codex_home=session_row["codex_home"],
        sandbox_mode_override=CHAT_SANDBOX,
        backend=_row_backend(session_row),
    )
    try:
        client.start()
        _validated_session_workspace(_get_session_row(session_id))
        try:
            client.thread_resume(codex_thread_id, model=session_row["requested_model"])
        except AppServerError as error:
            raise ChatError(
                "HISTORY_UNAVAILABLE",
                "The persisted Codex thread could not be resumed for history retrieval.",
                http_status=409,
            ) from error
        if turn_id is not None:
            _verify_codex_turn(client, codex_thread_id, turn_id)

        page = client.thread_items_list(
            codex_thread_id,
            cursor=cursor,
            limit=limit,
            sort_direction="asc",
            turn_id=turn_id,
        )
        entries = page.get("data") if isinstance(page, dict) else None
        if not isinstance(entries, list):
            raise ChatError("HISTORY_UNAVAILABLE", "Codex returned an unreadable history page.", http_status=503)
        items = [item for entry in entries if isinstance(entry, dict) if (item := _history_item(client, entry)) is not None]
        next_cursor = page.get("nextCursor")
        if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > HISTORY_CURSOR_MAX_LENGTH):
            raise ChatError("HISTORY_UNAVAILABLE", "Codex returned an invalid history cursor.", http_status=503)
        if cursor is not None and next_cursor == cursor:
            raise ChatError("HISTORY_UNAVAILABLE", "Codex history cursor did not advance.", http_status=503)
        history_turns = _history_turns(client, codex_thread_id, [item["turn_id"] for item in items])
        return {
            "session_id": session_id,
            "items": items,
            "turns": history_turns,
            "next_cursor": next_cursor,
            "source": "codex_thread",
        }
    except AppServerError as error:
        detail = str(error.detail or "").lower()
        if cursor is not None and ("cursor" in detail or str(error.code) == "-32602"):
            raise ChatError(
                "INVALID_HISTORY_CURSOR",
                "History cursor is not valid for this Codex thread.",
                http_status=422,
            ) from error
        raise ChatError(
            "HISTORY_UNAVAILABLE",
            "Codex history could not be read.",
            http_status=503,
        ) from error
    finally:
        try:
            client.stop()
        except Exception:
            pass


def list_sessions(project_id: str | None = None) -> list[dict]:
    return [_session_dict(row) for row in chat_sessions.list_sessions(project_id)]


# -- turns -----------------------------------------------------------------


def send_turn(session_id: str, text: str, *, idempotency_key: str | None = None) -> dict:
    session_row = _get_session_row(session_id)
    _validated_session_workspace(session_row)
    _require_chat_provider(session_row["requested_model"] or "", _row_backend(session_row))

    if idempotency_key is None:
        idempotency_key = uuid.uuid4().hex

    try:
        turn_id, created = chat_sessions.create_turn_if_absent(session_id, idempotency_key, text)
    except chat_sessions.SessionBusyError as error:
        raise ChatError(
            "SESSION_BUSY",
            f"Session {session_id!r} already has an active turn "
            f"({error.turn_id!r}); wait for it to finish or "
            "interrupt it first.",
            http_status=409,
        ) from error
    if not created:
        # Exact same idempotency key as a previous call: never execute
        # twice, just report whatever that turn's current state is.
        return _turn_dict(chat_sessions.get_turn(turn_id))

    with _active_lock:
        if len(_active_turns) >= MAX_CONCURRENT_TURNS:
            chat_sessions.update_turn_status(
                turn_id,
                "FAILED",
                error_code="TOO_MANY_CONCURRENT_TURNS",
                error_message=f"At most {MAX_CONCURRENT_TURNS} Chat turns may run at once.",
            )
            raise ChatError(
                "TOO_MANY_CONCURRENT_TURNS",
                f"At most {MAX_CONCURRENT_TURNS} Chat turns may run at once; try again shortly.",
                http_status=429,
            )
        active = _ActiveTurn()
        _active_turns[turn_id] = active

    thread = threading.Thread(
        target=_run_turn,
        args=(session_id, turn_id, text, active),
        daemon=True,
        name=f"nexus-chat-{turn_id}",
    )
    try:
        thread.start()
    except RuntimeError as error:
        with _active_lock:
            _active_turns.pop(turn_id, None)
        chat_sessions.update_turn_status(
            turn_id,
            "FAILED",
            error_code="TURN_START_FAILED",
            error_message=sanitize_error_text(str(error)) or "Chat turn could not be started.",
        )
        raise ChatError(
            "TURN_START_FAILED", "Chat turn could not be started; retry with the same idempotency key.",
            http_status=503,
        ) from error

    return _turn_dict(chat_sessions.get_turn(turn_id))


def _on_event(active: _ActiveTurn, event: dict) -> None:
    with active.lock:
        # The turn's durable terminal state wins over notifications that
        # race in while the client is shutting down after timeout/failure.
        if active.done.is_set():
            return
        seq = active.next_seq
        active.next_seq += 1
        active.buffer.append((seq, event))
        subscribers = list(active.subscribers)
    for subscriber_queue in subscribers:
        try:
            subscriber_queue.put_nowait((seq, event))
        except Exception:
            # Bounded, best-effort fan-out: a full/broken subscriber queue
            # never blocks turn execution or other subscribers.
            try:
                dropped_seq, _dropped_event = subscriber_queue.get_nowait()
                if isinstance(subscriber_queue, _SubscriberQueue):
                    subscriber_queue.dropped_through = max(subscriber_queue.dropped_through, dropped_seq)
                subscriber_queue.put_nowait((seq, event))
            except Exception:
                pass


def _mark_active_done(active: _ActiveTurn) -> None:
    with active.lock:
        active.done.set()


def _run_turn(session_id: str, turn_id: str, text: str, active: _ActiveTurn) -> None:
    chat_sessions.update_turn_status(turn_id, "RUNNING")
    client = None
    try:
        turn_timeout_seconds = _chat_turn_timeout_seconds()
        session_row = _get_session_row(session_id)
        workspace_path = _validated_session_workspace(session_row)
        backend = _row_backend(session_row)
        _require_chat_provider(session_row["requested_model"] or "", backend)
        client = AppServerClient(
            workspace_path,
            codex_home=session_row["codex_home"],
            # The profile is shared with writable orchestration clients;
            # enforce Chat's read-only sandbox at the process level.
            sandbox_mode_override=CHAT_SANDBOX,
            on_notification=lambda event: _on_event(active, event),
            backend=backend,
        )
        active.client = client
        client.start()
        _validated_session_workspace(_get_session_row(session_id))

        if session_row["codex_thread_id"]:
            thread_result = client.thread_resume(session_row["codex_thread_id"], model=session_row["requested_model"])
        else:
            thread_result = client.thread_start(
                model=session_row["requested_model"], sandbox=CHAT_SANDBOX, approval_policy=CHAT_APPROVAL_POLICY, ephemeral=False
            )
        thread_info = thread_result.get("thread") or {}
        thread_id = thread_info.get("id") or session_row["codex_thread_id"]
        active.thread_id = thread_id

        requested_effort = session_row["requested_effort"] if "requested_effort" in session_row.keys() else None
        if requested_effort and backend != BACKEND_CODEX_NATIVE:
            client.thread_settings_update(thread_id, model=session_row["requested_model"], effort=requested_effort)
            # Confirmation uses the runtime's persisted thread snapshot. If
            # this App-Server build cannot confirm the applied value, do not
            # start inference and do not substitute another effort/model.
            confirmed = client.thread_resume(thread_id, model=session_row["requested_model"])
            if confirmed.get("reasoningEffort") != requested_effort:
                raise ChatError("EFFORT_ACTIVATION_FAILED", "App-Server did not confirm the selected reasoning effort.", http_status=503)
            thread_result = confirmed

        model = thread_result.get("model")
        provider = thread_result.get("modelProvider")
        if session_row["requested_model"] and model != session_row["requested_model"]:
            raise ChatError("MODEL_ACTIVATION_FAILED", "App-Server did not activate the selected model.", http_status=503)
        chat_sessions.update_session_thread(
            session_id,
            codex_thread_id=thread_id,
            codex_home=client.codex_home,
            model=model,
            provider=provider,
        )

        turn_options = {"effort": requested_effort} if requested_effort and backend == BACKEND_CODEX_NATIVE else {}
        response_text = client.send_message_and_wait(
            thread_id,
            text,
            timeout_seconds=turn_timeout_seconds,
            on_turn_started=lambda codex_turn_id: setattr(active, "codex_turn_id", codex_turn_id),
            **turn_options,
        )

        chat_sessions.update_turn_status(
            turn_id, "COMPLETED", response_text=response_text, model=model, provider=provider
        )
        chat_sessions.update_session_status(session_id, "ACTIVE")
        _mark_active_done(active)

    except (AppServerError, ChatError) as error:
        status = "CANCELLED" if active.interrupted.is_set() else "FAILED"
        chat_sessions.update_turn_status(
            turn_id,
            status,
            error_code=error.code,
            error_message=str(error),  # AppServerError messages are already sanitized/bounded
        )
        chat_sessions.update_session_status(session_id, "ERROR" if status == "FAILED" else "ACTIVE")
        _mark_active_done(active)
    except Exception as error:  # pragma: no cover -- defensive, unexpected failure
        chat_sessions.update_turn_status(
            turn_id,
            "FAILED",
            error_code="UNEXPECTED_ERROR",
            error_message=sanitize_error_text(str(error)) or "Unexpected error.",
        )
        chat_sessions.update_session_status(session_id, "ERROR")
        _mark_active_done(active)
    finally:
        if client is not None:
            try:
                client.stop()
            except Exception:
                pass
        _mark_active_done(active)
        with _active_lock:
            _active_turns.pop(turn_id, None)


def interrupt_turn(session_id: str, turn_id: str | None = None) -> dict:
    _get_session_row(session_id)

    if turn_id is None:
        active_row = chat_sessions.get_active_turn_for_session(session_id)
        if active_row is None:
            return {"status": "NOT_ACTIVE", "turn": None}
        turn_id = active_row["id"]

    turn_row = require_session_turn(session_id, turn_id)

    with _active_lock:
        active = _active_turns.get(turn_id)

    if active is None or active.client is None or active.thread_id is None:
        return {"status": "NOT_ACTIVE", "turn": _turn_dict(turn_row)}

    if active.codex_turn_id is None:
        # turn/start hasn't returned yet -- too early to have a Codex
        # turn id to interrupt. Genuinely transient (the window is a
        # fraction of a second); the caller can simply retry.
        return {"status": "STARTING", "turn_id": turn_id}

    active.interrupted.set()
    try:
        active.client.turn_interrupt(active.thread_id, active.codex_turn_id)
    except AppServerError as error:
        # The turn may have finished a moment before the interrupt reached
        # it -- that's a benign race, not a failure to report.
        return {"status": "INTERRUPT_FAILED", "error": str(error)}

    return {"status": "INTERRUPT_REQUESTED", "turn_id": turn_id}


# -- SSE ---------------------------------------------------------------


def require_session_turn(session_id: str, turn_id: str):
    turn_row = chat_sessions.get_turn_for_session(session_id, turn_id)
    if turn_row is None:
        raise ChatError("TURN_NOT_FOUND", "Turn not found in this session.", http_status=404)
    return turn_row


def subscribe_events(turn_id: str, after_seq: int | None = None):
    """Atomically attach a bounded subscriber and replay retained newer events.

    Returns ``(queue, catch_up, active_done, replay_gap, oldest, latest)``.
    A gap is explicit when a supplied cursor predates retained events.
    """
    # The durable status is updated just before the worker is removed from
    # _active_turns. A subscriber arriving in that small completion window
    # must use the snapshot route instead of attaching to a finished turn.
    turn = chat_sessions.get_turn(turn_id)
    if turn is not None and turn["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
        return None

    with _active_lock:
        active = _active_turns.get(turn_id)
    if active is None:
        return None

    subscriber_queue: "queue.Queue" = _SubscriberQueue()
    with active.lock:
        retained = list(active.buffer)
        oldest = retained[0][0] if retained else active.next_seq
        latest = active.next_seq - 1
        replay_gap = (
            after_seq < oldest - 1
            if after_seq is not None
            else oldest > 1
        )
        catch_up = [(seq, event) for seq, event in retained if after_seq is None or seq > after_seq]
        active.subscribers.append(subscriber_queue)
    return subscriber_queue, catch_up, active.done, replay_gap, oldest, latest


def active_event_latest_seq(turn_id: str) -> int | None:
    """Return the current per-turn sequence high-water mark, if still active."""
    with _active_lock:
        active = _active_turns.get(turn_id)
    if active is None:
        return None
    with active.lock:
        return active.next_seq - 1


def unsubscribe_events(turn_id: str, subscriber_queue) -> None:
    with _active_lock:
        active = _active_turns.get(turn_id)
    if active is None:
        return
    with active.lock:
        try:
            active.subscribers.remove(subscriber_queue)
        except ValueError:
            pass


def latest_turn_id_for_session(session_id: str) -> str | None:
    turns = chat_sessions.list_turns(session_id, limit=1)
    return turns[0]["id"] if turns else None


def turn_snapshot(turn_id: str) -> dict | None:
    row = chat_sessions.get_turn(turn_id)
    return _turn_dict(row) if row else None
