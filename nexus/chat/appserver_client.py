"""Minimal JSON-RPC transport client for ``codex app-server``.

Scope (Nexus Chat backend, task T1 -- transport only):

* One client owns exactly one ``codex app-server --listen stdio://``
  subprocess, isolated via ``nexus.process_env.codex_subprocess_env``.
  ``AppServerClient`` retains the OmniRoute profile/provider defaults;
  ``CodexNativeAppServerClient`` selects the isolated native profile and
  local CLI authentication. No shared/background daemon or remote control.
* Protocol method/field names below (``initialize``, ``thread/start``,
  ``thread/resume``, ``turn/start``, ``turn/interrupt``, the
  ``item/*/requestApproval`` server requests and their ``decision``
  response shapes) were taken from the installed Codex CLI's own
  ``codex app-server generate-json-schema`` output and verified against a
  live OmniRoute-routed run (two-message continuity in one process, and
  ``thread/resume`` after a real process restart) -- not invented and not
  assumed from documentation alone.
* Session/transcript persistence is Codex App-Server's own job
  (non-ephemeral ``thread/start`` + ``thread/resume``); this client does
  not duplicate a transcript store.

Concurrency / lifecycle (documented here for the next task, not
implemented): the intended architecture is one ``AppServerClient``
process per *active* chat conversation, not a process pool and not a
single shared daemon. A future session registry should (a) cap the
number of concurrently running clients per host, (b) idle-shut-down a
client (``stop()``) after N minutes with no ``turn/start`` and no
attached SSE listener, keeping only the Codex-side non-ephemeral thread
on disk so a later message can ``thread_resume`` a fresh client cheaply,
and (c) never let two clients hold the same ``repo``/worktree open for
writing at once. None of that supervision belongs in this module.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nexus.dispatchers.manager import _find_codex
from nexus.failure_contract import sanitize_error_text
from nexus.process_env import codex_subprocess_env, with_scoped_git_trust
from nexus.process_timeout import kill_process_tree


DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_TURN_TIMEOUT_SECONDS = 180
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5
MAX_BUFFERED_EVENTS = 200
STDERR_TAIL_LIMIT = 4000

# Default retained for every existing caller. Alternative runtimes must opt in
# explicitly through the constructor (see CodexNativeAppServerClient below).
REQUIRED_MODEL_PROVIDER = "omniroute"

# Default approval policy for interactive Chat threads. Deliberately NOT
# "never" (that is the existing Worker/Manager/Reviewer convention for a
# reviewed, non-interactive pipeline): a human is directly driving this
# session, so Codex's own "ask before risky actions" behavior stays on by
# default. The fail-closed default responder below denies any approval
# request that reaches this client before a real UI handler is wired.
DEFAULT_APPROVAL_POLICY = "on-request"
DEFAULT_SANDBOX = "workspace-write"

_SERVER_REQUEST_METHODS_WITH_DECISION = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}
_PERMISSIONS_REQUEST_METHOD = "item/permissions/requestApproval"


class AppServerError(RuntimeError):
    """Raised for known, reportable App-Server client failures.

    Mirrors ``nexus.orchestration.go.GoError``: ``code`` is a short,
    machine-readable reason (e.g. ``OMNIROUTE_NOT_ENFORCED``,
    ``LAUNCH_FAILED``, ``REQUEST_TIMEOUT``). ``detail`` carries the raw
    JSON-RPC error object when one exists, for callers that need the
    structured form; the human-readable ``message`` is always already
    bounded and credential-redacted via
    ``nexus.failure_contract.sanitize_error_text``.
    """

    def __init__(self, code: str, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail


def _default_process_launcher(command: list[str], *, env: dict, cwd: str) -> subprocess.Popen:
    return subprocess.Popen(
        command,
        env=env,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )


class AppServerClient:
    """One isolated, provider-pinned ``codex app-server`` process.

    ``process_launcher`` and ``codex_path`` are injectable for tests
    (mirrors ``nexus.dispatchers.review``'s ``process_launcher``
    convention); production callers omit both.
    """

    def __init__(
        self,
        repo: str | Path,
        *,
        codex_path: str | Path | None = None,
        codex_home: str | Path | None = None,
        sandbox_mode_override: str | None = None,
        approval_policy_override: str | None = None,
        client_name: str = "nexus-chat",
        client_version: str = "0.1.0",
        request_timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        on_notification: Callable[[dict], None] | None = None,
        on_server_request: Callable[[str, dict], dict | None] | None = None,
        process_launcher: Callable[..., subprocess.Popen] | None = None,
        runtime_profile: str = "omniroute",
        required_model_provider: str = REQUIRED_MODEL_PROVIDER,
        include_auth: bool = False,
        process_config_overrides: tuple[str, ...] = (),
    ) -> None:
        """``codex_home``, when given, pins the isolated CODEX_HOME to this
        exact directory instead of a fresh one-shot directory -- required
        for ``thread_resume()`` to find a persisted (non-ephemeral)
        thread's rollout file after a real process restart. Owning *which*
        directory belongs to *which* conversation is the caller's job (the
        future Chat session registry); this client only honors whatever
        it is given.

        ``sandbox_mode_override``, when given, is passed as
        ``-c sandbox_mode="<value>"`` on the ``codex app-server`` command
        line itself. This is NOT redundant with ``thread_start(sandbox=...)``:
        found live, by testing an actual write with a harmless disposable
        file, that the OmniRoute profile's config.toml -- shared with
        Manager/Worker/Reviewer, which hardcodes
        ``sandbox_mode = "workspace-write"`` in
        ``nexus.process_env._safe_omniroute_config`` -- wins over the
        per-thread ``sandbox`` JSON-RPC parameter alone: a thread started
        with ``sandbox="read-only"`` still executed a real
        ``Set-Content`` shell command and wrote a file, with no approval
        prompt. Only this process-level ``-c`` override (the same
        mechanism Manager already uses for ``-c model=...``) actually
        changed that outcome in testing.

        ``approval_policy_override``, when given, is passed as
        ``-c approval_policy="<value>"`` on the command line, exactly
        mirroring ``sandbox_mode_override`` above. This exists because the
        OmniRoute profile's own config.toml (see
        ``nexus.process_env._safe_omniroute_config``) also hardcodes
        ``approval_policy = "never"``, and a live F2.2 diagnostic found
        that the per-thread ``thread_start(approvalPolicy=...)`` JSON-RPC
        parameter alone did not override it (zero approval requests were
        ever observed, even for a provoked, plausibly-escalation-worthy
        action). Whether this process-level override is any more
        effective is exactly what a caller using it must verify live for
        itself -- this parameter only adds the command-line flag; it does
        not by itself prove the App-Server honors it.
        """
        self.repo = str(repo)
        self._codex_path = codex_path
        self._sandbox_mode_override = sandbox_mode_override
        self._approval_policy_override = approval_policy_override
        self._codex_home = str(codex_home) if codex_home is not None else None
        self.codex_home: str | None = None  # resolved once start() runs
        self._client_name = client_name
        self._client_version = client_version
        self._request_timeout_seconds = request_timeout_seconds
        self._on_notification = on_notification
        self._sensitive_values: tuple[str, ...] = ()
        self._on_server_request = on_server_request
        self._launcher = process_launcher or _default_process_launcher
        self._runtime_profile = runtime_profile
        self._required_model_provider = required_model_provider
        self._include_auth = include_auth
        self._process_config_overrides = tuple(process_config_overrides)

        self._process: subprocess.Popen | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None

        self._write_lock = threading.Lock()
        self._next_id_lock = threading.Lock()
        self._next_id = 1

        self._pending_lock = threading.Lock()
        self._pending: dict[int, "queue.Queue[dict]"] = {}

        self._events: "queue.Queue[dict]" = queue.Queue()
        self.recent_events: "deque[dict]" = deque(maxlen=MAX_BUFFERED_EVENTS)
        self._stderr_tail: list[str] = []
        self._stopped = threading.Event()
        self._process_exited = threading.Event()

    # -- lifecycle -----------------------------------------------------

    def start(self) -> dict:
        """Launch the isolated app-server process and send ``initialize``.

        Raises ``AppServerError`` if Codex cannot be located, the explicitly
        selected profile is unavailable or invalid, or the process exits
        before responding. Profiles never fall back to one another.
        """
        if self._process is not None:
            raise AppServerError(
                "ALREADY_STARTED", "AppServerClient.start() called twice."
            )

        try:
            codex_path = self._codex_path or _find_codex()
        except (RuntimeError, FileNotFoundError) as error:
            raise AppServerError(
                "CODEX_NOT_FOUND",
                sanitize_error_text(str(error)) or "Codex executable not found.",
            ) from error

        try:
            env = with_scoped_git_trust(
                codex_subprocess_env(
                    profile=self._runtime_profile,
                    worker_home=self._codex_home,
                    include_auth=self._include_auth,
                ),
                self.repo,
            )
        except (RuntimeError, OSError, ValueError) as error:
            # codex_subprocess_env/with_scoped_git_trust already fail closed
            # for a missing/invalid OmniRoute provider config or an unsafe
            # repo path -- propagate as the one error this client exists to
            # surface explicitly, never retried and never downgraded to a
            # silent default-profile fallback.
            code = (
                "OMNIROUTE_PROFILE_UNAVAILABLE"
                if self._runtime_profile == "omniroute"
                else "CODEX_NATIVE_PROFILE_UNAVAILABLE"
            )
            label = (
                "OmniRoute profile unavailable."
                if self._runtime_profile == "omniroute"
                else "Codex Native profile unavailable."
            )
            raise AppServerError(code, sanitize_error_text(str(error)) or label) from error

        # Record whichever CODEX_HOME was actually used (our own
        # self._codex_home when given, otherwise the fresh directory
        # codex_subprocess_env just generated) so a caller that wants to
        # resume this exact conversation later knows what to pass as
        # `codex_home` to the next AppServerClient.
        self.codex_home = env.get("CODEX_HOME")
        self._sensitive_values = self._collect_sensitive_values(env)

        command = [str(codex_path), "app-server", "--listen", "stdio://"]
        if self._sandbox_mode_override is not None:
            command += ["-c", f'sandbox_mode="{self._sandbox_mode_override}"']
        if self._approval_policy_override is not None:
            command += ["-c", f'approval_policy="{self._approval_policy_override}"']
        for override in self._process_config_overrides:
            command += ["-c", override]

        try:
            process = self._launcher(command, env=env, cwd=self.repo)
        except OSError as error:
            raise AppServerError(
                "LAUNCH_FAILED",
                sanitize_error_text(str(error)) or "Failed to launch app-server.",
            ) from error

        self._process = process
        self._stdout_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        self._stdout_thread.start()
        if process.stderr is not None:
            self._stderr_thread = threading.Thread(target=self._pump_stderr, daemon=True)
            self._stderr_thread.start()

        try:
            result = self._send_request(
                "initialize",
                {
                    "clientInfo": {
                        "name": self._client_name,
                        "title": "Nexus Chat",
                        "version": self._client_version,
                    }
                },
            )
            # Required by the official App-Server handshake. It is a JSON-RPC
            # notification (no id and therefore no response to await).
            self._write_line({"method": "initialized", "params": {}})
            return result
        except AppServerError:
            self.stop()
            raise

    def __enter__(self) -> "AppServerClient":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def stop(self) -> None:
        """Terminate this client's own process tree only.

        ``kill_process_tree`` (shared with Manager/Worker/Reviewer) kills
        by this specific ``process.pid`` (``taskkill /PID <pid> /T /F`` on
        Windows), never by name or by pattern, so an unrelated Codex
        session elsewhere on the machine is never affected.
        """
        self._stopped.set()
        process = self._process
        if process is None:
            return

        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass

        try:
            process.wait(timeout=DEFAULT_SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            kill_process_tree(process)
            try:
                process.wait(timeout=DEFAULT_SHUTDOWN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                pass

        with self._pending_lock:
            pending = list(self._pending.items())
            self._pending.clear()
        for _request_id, pending_queue in pending:
            pending_queue.put(
                {"error": {"code": -32000, "message": "AppServerClient stopped."}}
            )

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # -- protocol: client-initiated requests ----------------------------

    def thread_start(
        self,
        *,
        model: str | None = None,
        cwd: str | None = None,
        sandbox: str = DEFAULT_SANDBOX,
        approval_policy: str = DEFAULT_APPROVAL_POLICY,
        ephemeral: bool = True,
        extra_params: dict | None = None,
    ) -> dict:
        params = self._provider_params(extra_params)
        params.update(
            {
                "cwd": cwd or self.repo,
                "sandbox": sandbox,
                "approvalPolicy": approval_policy,
                "ephemeral": ephemeral,
            }
        )
        if model is not None:
            params["model"] = model

        result = self._send_request("thread/start", params)
        self._assert_provider(result)
        return result

    def thread_resume(
        self,
        thread_id: str,
        *,
        model: str | None = None,
        extra_params: dict | None = None,
    ) -> dict:
        params = self._provider_params(extra_params)
        params["threadId"] = thread_id
        if model is not None:
            params["model"] = model

        result = self._send_request("thread/resume", params)
        self._assert_provider(result)
        return result

    def model_list(self, *, cursor: str | None = None, limit: int = 100) -> dict:
        """Return App-Server's model metadata, including per-model effort support."""
        params: dict[str, Any] = {"limit": limit, "includeHidden": False}
        if cursor is not None:
            params["cursor"] = cursor
        return self._redact_event(self._send_request("model/list", params))

    def account_read(self, *, refresh_token: bool = False) -> dict:
        """Read authentication state without returning it beyond this client.

        Callers should project this to booleans/type only. The raw response can
        contain account identity metadata and must never be sent to the browser.
        """
        return self._send_request("account/read", {"refreshToken": refresh_token})

    def thread_settings_update(self, thread_id: str, *, model: str | None = None, effort: str | None = None) -> dict:
        """Apply per-thread settings through the App-Server's settings protocol."""
        params: dict[str, Any] = {"threadId": thread_id}
        if model is not None:
            params["model"] = model
        if effort is not None:
            params["effort"] = effort
        return self._send_request("thread/settings/update", params)

    def thread_items_list(
        self,
        thread_id: str,
        *,
        cursor: str | dict | None = None,
        limit: int = 100,
        sort_direction: str = "asc",
        turn_id: str | None = None,
    ) -> dict:
        """Read one persisted history page without starting model inference.

        The wire fields and cursor semantics match the installed App-Server
        ``thread/items/list`` protocol. Returned history is passed through
        the same recursive secret redactor as live App-Server notifications.
        """
        if not isinstance(thread_id, str) or not thread_id:
            raise AppServerError("HISTORY_REQUEST_INVALID", "thread_id is required.")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise AppServerError("HISTORY_REQUEST_INVALID", "History page limit must be between 1 and 1000.")
        if sort_direction not in {"asc", "desc"}:
            raise AppServerError("HISTORY_REQUEST_INVALID", "History sort_direction must be asc or desc.")
        if cursor is not None and not isinstance(cursor, (str, dict)):
            raise AppServerError("HISTORY_REQUEST_INVALID", "History cursor has an invalid type.")

        params: dict[str, Any] = {
            "threadId": thread_id,
            "cursor": cursor,
            "limit": limit,
            "sortDirection": sort_direction,
        }
        if turn_id is not None:
            params["turnId"] = turn_id
        return self._redact_event(self._send_request("thread/items/list", params))

    def thread_turns_list(
        self,
        thread_id: str,
        *,
        cursor: str | None = None,
        limit: int = 100,
        sort_direction: str = "desc",
        items_view: str = "notLoaded",
    ) -> dict:
        """Read one persisted turn-metadata page without model inference."""
        if not isinstance(thread_id, str) or not thread_id:
            raise AppServerError("HISTORY_REQUEST_INVALID", "thread_id is required.")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise AppServerError("HISTORY_REQUEST_INVALID", "Turn page limit must be between 1 and 1000.")
        if sort_direction not in {"asc", "desc"}:
            raise AppServerError("HISTORY_REQUEST_INVALID", "Turn sort_direction must be asc or desc.")
        if items_view not in {"notLoaded", "summary", "full"}:
            raise AppServerError("HISTORY_REQUEST_INVALID", "items_view has an invalid value.")
        if cursor is not None and not isinstance(cursor, str):
            raise AppServerError("HISTORY_REQUEST_INVALID", "Turn cursor must be a string.")

        params = {
            "threadId": thread_id,
            "cursor": cursor,
            "limit": limit,
            "sortDirection": sort_direction,
            "itemsView": items_view,
        }
        return self._redact_event(self._send_request("thread/turns/list", params))

    def turn_start(
        self, thread_id: str, text: str, *, extra_params: dict | None = None
    ) -> dict:
        params = dict(extra_params or {})
        params.update(
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
            }
        )
        return self._send_request("turn/start", params)

    def turn_interrupt(self, thread_id: str, turn_id: str) -> dict:
        """Both ids are required by the real protocol (TurnInterruptParams
        requires threadId AND turnId; threadId alone fails with "Invalid
        request: missing field `turnId`" -- found live, not assumed)."""
        return self._send_request(
            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
        )

    def send_message_and_wait(
        self,
        thread_id: str,
        text: str,
        *,
        timeout_seconds: int = DEFAULT_TURN_TIMEOUT_SECONDS,
        on_turn_started: Callable[[str], None] | None = None,
    ) -> str:
        """Convenience: start a turn and block for its completion.

        Intended for simple callers and tests/smoke checks. The future
        Chat API should stream ``item/*``/``turn/*`` notifications live
        (via ``on_notification`` or ``poll_event``) rather than block.

        ``on_turn_started``, if given, is called once with the real Codex
        turn id as soon as ``turn/start`` returns it -- a caller that
        needs to support ``turn_interrupt(thread_id, turn_id)`` while this
        call is still blocking (e.g. from another thread) has no other
        way to learn that id.
        """
        turn = self.turn_start(thread_id, text)
        turn_id = (turn.get("turn") or {}).get("id")
        if not turn_id:
            raise AppServerError(
                "TURN_START_MALFORMED", "turn/start response had no turn id."
            )
        if on_turn_started is not None:
            try:
                on_turn_started(turn_id)
            except Exception:
                pass

        deadline = time.monotonic() + timeout_seconds
        last_text: str | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerError(
                    "TURN_TIMEOUT",
                    f"No turn/completed for turn {turn_id} within "
                    f"{timeout_seconds}s.",
                )
            event = self.poll_event(timeout=remaining)
            if event is None:
                continue
            # queue.get() can return an event after the supplied wait has
            # expired if it races with the deadline. Keep the deadline a
            # hard total execution limit, including terminal notifications;
            # a late completion must not turn a timed-out request into a
            # successful result.
            if time.monotonic() >= deadline:
                raise AppServerError(
                    "TURN_TIMEOUT",
                    f"No turn/completed for turn {turn_id} within "
                    f"{timeout_seconds}s.",
                )
            method = event.get("method")
            params = event.get("params") or {}
            if (
                method == "item/completed"
                and params.get("turnId") == turn_id
                and (params.get("item") or {}).get("type") == "agentMessage"
            ):
                last_text = (params.get("item") or {}).get("text")
            elif method == "turn/completed" and (params.get("turn") or {}).get("id") == turn_id:
                turn_result = params.get("turn") or {}
                turn_status = turn_result.get("status")
                if turn_status != "completed":
                    turn_error = turn_result.get("error")
                    detail = (
                        sanitize_error_text(str(turn_error))
                        if turn_error is not None
                        else f"Turn ended with status {turn_status!r}, not 'completed'."
                    )
                    raise AppServerError("TURN_FAILED", detail or "Turn did not complete.")
                return last_text or ""

    def poll_event(self, timeout: float | None = None) -> dict | None:
        """Return the next buffered notification, or None on timeout.

        Only needed by callers (like ``send_message_and_wait`` above, or a
        test) that consume the queue directly instead of
        ``on_notification``. Events already delivered to
        ``on_notification`` are still queued here too.
        """
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    # -- internals: explicit provider enforcement -------------------------

    def _provider_params(self, extra_params: dict | None) -> dict:
        params = dict(extra_params or {})
        requested = params.get("modelProvider")
        if requested is not None and requested != self._required_model_provider:
            code = (
                "OMNIROUTE_OVERRIDE_REJECTED"
                if self._required_model_provider == REQUIRED_MODEL_PROVIDER
                else "CODEX_NATIVE_OVERRIDE_REJECTED"
            )
            raise AppServerError(
                code,
                f"Refusing modelProvider={requested!r}; this runtime requires "
                f"modelProvider={self._required_model_provider!r}.",
            )
        params["modelProvider"] = self._required_model_provider
        return params

    def _assert_provider(self, result: dict) -> None:
        thread = result.get("thread") if isinstance(result, dict) else None
        provider = thread.get("modelProvider") if isinstance(thread, dict) else None
        if provider is None and isinstance(result, dict):
            provider = result.get("modelProvider")
        if provider != self._required_model_provider:
            code = (
                "OMNIROUTE_NOT_ENFORCED"
                if self._required_model_provider == REQUIRED_MODEL_PROVIDER
                else "CODEX_NATIVE_NOT_ENFORCED"
            )
            raise AppServerError(
                code,
                "App-Server did not confirm "
                f"modelProvider={self._required_model_provider!r} (got {provider!r}); "
                "refusing this thread rather than silently switch runtimes.",
            )

    def _omniroute_params(self, extra_params: dict | None) -> dict:
        """Compatibility wrapper retained for tests/downstream extensions."""
        return self._provider_params(extra_params)

    def _assert_omniroute(self, result: dict) -> None:
        """Compatibility wrapper retained for the OmniRoute adapter contract."""
        self._assert_provider(result)


    # -- internals: JSON-RPC plumbing ------------------------------------

    def _send_request(
        self, method: str, params: dict, *, timeout: float | None = None
    ) -> dict:
        if self._process is None or self._process.poll() is not None:
            raise AppServerError(
                "PROCESS_NOT_RUNNING",
                "AppServerClient has no running app-server process.",
            )

        with self._next_id_lock:
            request_id = self._next_id
            self._next_id += 1

        response_queue: "queue.Queue[dict]" = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = response_queue

        if self._process_exited.is_set():
            # Closes the narrow race where the pump thread already drained
            # and failed every pending request (process died with no/short
            # output) before this request was registered -- fail fast
            # instead of waiting out the full request timeout.
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise AppServerError(
                "PROCESS_NOT_RUNNING", "app-server process already exited."
            )

        self._write_line({"id": request_id, "method": method, "params": params})

        wait_seconds = timeout if timeout is not None else self._request_timeout_seconds
        try:
            message = response_queue.get(timeout=wait_seconds)
        except queue.Empty:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise AppServerError(
                "REQUEST_TIMEOUT",
                f"No response to {method!r} (id={request_id}) within "
                f"{wait_seconds}s.",
            )
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

        error = message.get("error")
        if error:
            raise AppServerError(
                str(error.get("code", "APP_SERVER_ERROR")),
                sanitize_error_text(error.get("message")) or "App-Server error.",
                detail=error,
            )
        return message.get("result") or {}

    def _write_line(self, obj: dict) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise AppServerError(
                "PROCESS_NOT_RUNNING", "No app-server stdin to write to."
            )
        line = json.dumps(obj)
        with self._write_lock:
            try:
                process.stdin.write(line + "\n")
                process.stdin.flush()
            except (OSError, ValueError) as error:
                raise AppServerError(
                    "WRITE_FAILED",
                    sanitize_error_text(str(error)) or "Failed to write to app-server.",
                ) from error

    def _pump_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # Malformed/partial line from the subprocess: never crash
                # the pump thread over it. Surfaced as a bounded diagnostic
                # notification so a caller/test can still observe it.
                self._deliver_notification(
                    {
                        "method": "nexus/malformedLine",
                        "params": {"line": sanitize_error_text(line, limit=500)},
                    }
                )
                continue

            if not isinstance(message, dict):
                continue

            method = message.get("method")
            message_id = message.get("id")

            if method is None:
                # A response to one of our own requests.
                if message_id is not None:
                    self._resolve_pending(message_id, message)
                continue

            if message_id is not None:
                # The server is asking US something (an approval request).
                self._handle_server_request(method, message.get("params") or {}, message_id)
            else:
                self._deliver_notification(message)

        # stdout closed: the process exited. Fail every still-pending
        # request instead of leaving callers blocked until their own
        # timeout, and flag future requests so they fail fast too.
        self._process_exited.set()
        with self._pending_lock:
            pending = list(self._pending.items())
            self._pending.clear()
        for _request_id, pending_queue in pending:
            pending_queue.put(
                {"error": {"code": -32001, "message": "app-server process exited."}}
            )

    def _pump_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        for raw_line in process.stderr:
            line = sanitize_error_text(raw_line.rstrip("\n"), limit=500)
            if line is None:
                continue
            self._stderr_tail.append(line)
            # Bound memory: keep only the tail, never an unbounded log.
            joined_len = sum(len(entry) for entry in self._stderr_tail)
            while joined_len > STDERR_TAIL_LIMIT and len(self._stderr_tail) > 1:
                joined_len -= len(self._stderr_tail.pop(0))

    def stderr_tail(self) -> str:
        """Bounded, redacted recent stderr output, for diagnostics only."""
        return "\n".join(self._stderr_tail)

    def _resolve_pending(self, message_id: Any, message: dict) -> None:
        with self._pending_lock:
            pending_queue = self._pending.get(message_id)
        if pending_queue is not None:
            pending_queue.put(message)

    def _deliver_notification(self, message: dict) -> None:
        message = self._redact_event(message)
        self.recent_events.append(message)
        self._events.put(message)
        if self._on_notification is not None:
            try:
                self._on_notification(message)
            except Exception:
                # A caller's callback must never take down the read loop.
                pass

    @staticmethod
    def _collect_sensitive_values(env: dict[str, str]) -> tuple[str, ...]:
        values = {
            value
            for key, value in env.items()
            if value and len(value) >= 6
            and re.search(r"(?i)(?:api[_-]?key|token|secret|password|credential)", key)
        }
        auth_path = Path(env.get("CODEX_HOME", "")) / "auth.json"
        try:
            auth_data = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            auth_data = None

        def collect_auth_fields(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if re.search(r"(?i)(?:api[_-]?key|token|secret|password|credential)", str(key)):
                        if isinstance(item, str) and len(item) >= 6:
                            values.add(item)
                    collect_auth_fields(item)
            elif isinstance(value, list):
                for item in value:
                    collect_auth_fields(item)

        collect_auth_fields(auth_data)
        return tuple(sorted(values, key=len, reverse=True))

    def _redact_event(self, value):
        if isinstance(value, dict):
            redacted = {}
            for key, item in value.items():
                if re.search(r"(?i)(?:api[_-]?key|token|secret|password|credential)", str(key)):
                    redacted[key] = "<redacted>"
                else:
                    redacted[key] = self._redact_event(item)
            return redacted
        if isinstance(value, list):
            return [self._redact_event(item) for item in value]
        if isinstance(value, str):
            for secret in self._sensitive_values:
                value = value.replace(secret, "<redacted>")
            return value
        return value

    def _handle_server_request(self, method: str, params: dict, request_id: Any) -> None:
        self._deliver_notification({"method": method, "params": params, "id": request_id})

        response_result: dict | None = None
        if self._on_server_request is not None:
            try:
                response_result = self._on_server_request(method, params)
            except Exception:
                response_result = None

        if response_result is None:
            response_result = self._fail_closed_decision(method)

        if response_result is None:
            # Unknown approval-request method: no schema-correct response
            # is known, so reply with a standard JSON-RPC error rather than
            # guess a response shape -- the server is unblocked either way.
            self._write_line(
                {
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"Nexus Chat: unsupported server request {method!r}.",
                    },
                }
            )
            return

        self._write_line({"id": request_id, "result": response_result})

    @staticmethod
    def _fail_closed_decision(method: str) -> dict | None:
        """Real, schema-correct deny responses -- never approves anything.

        Shapes are exactly ``ExecCommandApprovalResponse`` /
        ``FileChangeRequestApprovalResponse`` / ``CommandExecutionRequest
        ApprovalResponse`` (``{"decision": {"denied": {"rejection": ...}}}``)
        and ``PermissionsRequestApprovalResponse`` (``{"permissions": []}``
        = grant none), taken from the installed CLI's own protocol schema.
        """
        rejection = (
            "Nexus Chat: no approval handler is wired yet for this "
            "session; denying by default (fail-closed)."
        )
        if method in _SERVER_REQUEST_METHODS_WITH_DECISION:
            return {"decision": {"denied": {"rejection": rejection}}}
        if method == _PERMISSIONS_REQUEST_METHOD:
            return {"permissions": []}
        return None


class CodexNativeAppServerClient(AppServerClient):
    """Official App-Server transport using the local Codex CLI login.

    The provider is pinned twice: in process configuration and in every
    thread/start or thread/resume request. The response must confirm `openai`;
    there is no fallback to OmniRoute (or any other configured provider).
    """

    def __init__(self, repo: str | Path, **kwargs: Any) -> None:
        forbidden = {
            "runtime_profile", "required_model_provider", "include_auth",
            "process_config_overrides",
        }.intersection(kwargs)
        if forbidden:
            raise TypeError(f"Codex Native runtime options are fixed: {sorted(forbidden)!r}")
        super().__init__(
            repo,
            runtime_profile="native",
            required_model_provider="openai",
            include_auth=True,
            process_config_overrides=(
                'model_provider="openai"',
                'approval_policy="on-request"',
            ),
            **kwargs,
        )
