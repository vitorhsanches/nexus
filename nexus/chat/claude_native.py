"""Claude Code Native adapter for Nexus Chat (read-only).

Uses the Claude Code CLI's documented headless mode
(``claude -p --output-format stream-json``) with the CLI's own local login.
No OmniRoute, no Codex, no Messages API calls and no credential copies.

One CLI process runs exactly one turn, mirroring the Codex Chat design: the
CLI persists the conversation itself (``--session-id`` on the first turn,
``--resume`` afterwards), so a later turn -- including after a Nexus restart --
continues the same context. Interrupting a turn terminates that process.

Read-only by construction, using only official CLI controls:
``--restricted`` (drops every tool that runs commands or code, ignores user /
project / local settings, confines file tools to the working directory),
an explicit ``--tools`` allowlist of read-only tools, ``--permission-mode
dontAsk`` plus ``--permission-prompts none`` (anything that would prompt is
denied automatically), no MCP servers and no slash commands. Bypass flags are
never passed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from nexus.failure_contract import sanitize_error_text
from nexus.process_timeout import kill_process_tree

BACKEND_CLAUDE_NATIVE = "claude_native"
READ_ONLY_TOOLS = ("Read", "Grep", "Glob")
MODEL_ALIASES = ("default", "sonnet", "opus", "haiku")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
STATUS_TIMEOUT_SECONDS = 20
TOOL_TEXT_LIMIT = 2000

_SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SECRET_KEY_RE = re.compile(r"(?i)(?:api[_-]?key|token|secret|password|credential)")
# Variables that would change who answers or where requests go: an enclosing
# Claude Code / Agent SDK host session, a custom gateway, or OmniRoute.
_STRIPPED_ENV_PREFIXES = ("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_AGENT_SDK_", "OMNIROUTE_")
_STRIPPED_ENV_NAMES = frozenset({"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_PID", "CLAUDE_EFFORT"})


class ClaudeNativeError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def find_claude() -> str:
    """Locate the real Claude Code executable, never a shell shim.

    ``NEXUS_CLAUDE_PATH`` overrides discovery. The npm ``claude.cmd`` shim is
    resolved to the native ``claude.exe`` it launches; running the ``.cmd``
    itself would route arguments through ``cmd.exe``.
    """
    override = os.environ.get("NEXUS_CLAUDE_PATH")
    candidates = [override] if override else []
    found = shutil.which("claude")
    if found:
        path = Path(found)
        if path.suffix.lower() in {".cmd", ".bat", ".ps1", ""}:
            candidates.append(str(path.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"))
        else:
            candidates.append(found)
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    if home:
        candidates.append(os.path.join(home, ".local", "bin", "claude.exe"))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and Path(candidate).suffix.lower() not in {".cmd", ".bat", ".ps1"}:
            return candidate
    raise ClaudeNativeError(
        "CLAUDE_NOT_FOUND",
        "Claude Code CLI was not found. Install it or set NEXUS_CLAUDE_PATH to claude.exe.",
    )


def claude_env() -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if key not in _STRIPPED_ENV_NAMES and not key.upper().startswith(_STRIPPED_ENV_PREFIXES)
    }
    if not env.get("HOME") and env.get("USERPROFILE"):
        env["HOME"] = env["USERPROFILE"]
    return env


def _secrets(env: dict[str, str]) -> tuple[str, ...]:
    values = {value for key, value in env.items() if value and len(value) >= 6 and _SECRET_KEY_RE.search(key)}
    return tuple(sorted(values, key=len, reverse=True))


def _redact(value, secrets: tuple[str, ...]):
    if isinstance(value, dict):
        return {key: "<redacted>" if _SECRET_KEY_RE.search(str(key)) else _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "<redacted>")
        return value
    return value


def cli_status(*, runner=subprocess.run) -> dict:
    """Installed version and local login state; never returns credentials."""
    status = {"installed": False, "version": None, "logged_in": False, "auth_method": None,
              "api_provider": None, "subscription": None, "error": None}
    try:
        executable = find_claude()
    except ClaudeNativeError as error:
        status["error"] = str(error)
        return status
    status["installed"] = True
    env = claude_env()
    try:
        version = runner([executable, "--version"], capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=STATUS_TIMEOUT_SECONDS, env=env, check=False)
        status["version"] = (version.stdout or "").strip().split(" ")[0] or None
        auth = runner([executable, "auth", "status", "--json"], capture_output=True, text=True, encoding="utf-8",
                      errors="replace", timeout=STATUS_TIMEOUT_SECONDS, env=env, check=False)
        data = json.loads(auth.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        status["error"] = sanitize_error_text(str(error)) or "Claude Code status is unavailable."
        return status
    if not isinstance(data, dict):
        status["error"] = "Claude Code returned an unreadable auth status."
        return status
    status["logged_in"] = data.get("loggedIn") is True
    for source, target in (("authMethod", "auth_method"), ("apiProvider", "api_provider"), ("subscriptionType", "subscription")):
        if isinstance(data.get(source), str):
            status[target] = data[source]
    return status


def build_command(executable: str, *, session_id: str, resume: bool, model: str | None, effort: str | None) -> list[str]:
    """The only command line Nexus ever uses for Claude Native Chat."""
    if not _SESSION_ID_RE.match(session_id or ""):
        raise ClaudeNativeError("INVALID_SESSION_ID", "Claude session id must be a lowercase UUID.")
    if model is not None and model not in MODEL_ALIASES:
        raise ClaudeNativeError("MODEL_NOT_ALLOWED", "Unsupported Claude model alias.")
    if effort is not None and effort not in EFFORTS:
        raise ClaudeNativeError("EFFORT_NOT_SUPPORTED", "Unsupported Claude effort level.")
    tools = ",".join(READ_ONLY_TOOLS)
    command = [
        executable, "-p",
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        "--restricted", "--tools", tools, "--allowedTools", tools,
        "--permission-mode", "dontAsk", "--permission-prompts", "none",
        "--strict-mcp-config", "--disable-slash-commands",
    ]
    command += ["--resume", session_id] if resume else ["--session-id", session_id]
    if model and model != "default":
        command += ["--model", model]
    if effort:
        command += ["--effort", effort]
    return command


def _default_launcher(command, *, env, cwd):
    return subprocess.Popen(
        command, env=env, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )


class ClaudeTurn:
    """Runs one read-only Claude Code turn and streams Chat-shaped events.

    Emitted envelopes reuse the shapes Nexus Chat already renders:
    ``turn/started``, ``item/agentMessage/delta`` (same item id as the final
    message, so the bubble is replaced, never duplicated), ``item/started`` /
    ``item/completed`` for tool use, and ``turn/completed``.
    """

    def __init__(self, cwd: str, *, session_id: str, resume: bool, model: str | None = None,
                 effort: str | None = None, on_event: Callable[[dict], None] | None = None,
                 on_session: Callable[[str, str | None], None] | None = None, launcher=None) -> None:
        self.cwd = cwd
        self.session_id = session_id
        self.resume = resume
        self.model = model
        self.effort = effort
        self._on_event = on_event
        self._on_session = on_session
        self._launcher = launcher or _default_launcher
        self._process = None
        self._interrupted = threading.Event()
        self._secrets: tuple[str, ...] = ()
        self.active_model: str | None = None
        self.session_confirmed = False
        self._stderr: list[str] = []

    # -- public ---------------------------------------------------------

    def turn_interrupt(self, _thread_id=None, _turn_id=None) -> dict:
        """Stop the running turn (the CLI keeps what it already persisted)."""
        self._interrupted.set()
        process = self._process
        if process is not None and process.poll() is None:
            kill_process_tree(process)
        return {}

    def stop(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            kill_process_tree(process)

    @property
    def interrupted(self) -> bool:
        return self._interrupted.is_set()

    def run(self, prompt: str, *, timeout_seconds: int) -> str:
        executable = find_claude()
        command = build_command(executable, session_id=self.session_id, resume=self.resume,
                                model=self.model, effort=self.effort)
        env = claude_env()
        self._secrets = _secrets(env)
        try:
            process = self._launcher(command, env=env, cwd=self.cwd)
        except OSError as error:
            raise ClaudeNativeError("LAUNCH_FAILED", sanitize_error_text(str(error)) or "Claude Code could not start.") from error
        self._process = process
        stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        stderr_thread.start()
        self._timed_out = False
        timer = threading.Timer(timeout_seconds, self._timeout)
        timer.daemon = True
        timer.start()
        try:
            try:
                # The prompt goes through stdin, never through the command line.
                process.stdin.write(prompt)
                process.stdin.close()
            except (OSError, ValueError):
                pass
            result = self._consume(process)
        finally:
            timer.cancel()
            if process.poll() is None:
                kill_process_tree(process)
        if self._interrupted.is_set():
            raise ClaudeNativeError("TURN_INTERRUPTED", "The Claude turn was interrupted.")
        if self._timed_out:
            raise ClaudeNativeError("TURN_TIMEOUT", f"Claude turn exceeded {timeout_seconds}s.")
        if result is None:
            detail = sanitize_error_text(" ".join(self._stderr)[-500:])
            raise ClaudeNativeError("CLAUDE_EXITED", detail or "Claude Code exited without a result.")
        if result.get("is_error"):
            code = "CLAUDE_NOT_SIGNED_IN" if result.get("_error") == "authentication_failed" else "CLAUDE_TURN_FAILED"
            message = sanitize_error_text(str(result.get("result") or "")) or "Claude Code reported an error."
            raise ClaudeNativeError(code, message)
        return str(result.get("result") or "")

    # -- internals ----------------------------------------------------------

    def _timeout(self) -> None:
        self._timed_out = True
        self.stop()

    def _drain_stderr(self) -> None:
        stream = getattr(self._process, "stderr", None)
        if stream is None:
            return
        for line in stream:
            text = sanitize_error_text(_redact(line.rstrip("\n"), self._secrets), limit=500)
            if text:
                self._stderr.append(text)
                del self._stderr[:-20]

    def _emit(self, event: dict) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(_redact(event, self._secrets))
        except Exception:
            pass

    def _consume(self, process) -> dict | None:
        result = None
        last_error = None
        for raw in process.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                self._on_init(event)
            elif kind == "stream_event":
                self._on_stream(event.get("event") or {})
            elif kind == "assistant":
                last_error = event.get("error") or last_error
                self._on_assistant(event.get("message") or {})
            elif kind == "user":
                self._on_tool_results(event.get("message") or {})
            elif kind == "result":
                result = dict(event)
                result["_error"] = last_error
                self._on_result(result)
        return result

    def _on_init(self, event: dict) -> None:
        if event.get("session_id") != self.session_id:
            # The CLI must run exactly the conversation Nexus asked for.
            self.stop()
            raise ClaudeNativeError("SESSION_MISMATCH", "Claude Code started a different session than requested.")
        tools = event.get("tools") or []
        if any(tool not in READ_ONLY_TOOLS for tool in tools):
            self.stop()
            raise ClaudeNativeError("UNSAFE_TOOLS", "Claude Code exposed tools beyond the read-only allowlist.")
        self.session_confirmed = True
        self.active_model = event.get("model") if isinstance(event.get("model"), str) else None
        if self._on_session is not None:
            self._on_session(self.session_id, self.active_model)
        self._emit({"method": "turn/started", "params": {"turn": {"id": self.session_id}, "model": self.active_model}})

    def _on_stream(self, event: dict) -> None:
        if event.get("type") == "message_start":
            self._message_id = ((event.get("message") or {}).get("id"))
        elif event.get("type") == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                self._emit({"method": "item/agentMessage/delta",
                            "params": {"itemId": getattr(self, "_message_id", None), "delta": delta["text"]}})

    def _on_assistant(self, message: dict) -> None:
        texts = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block.get("type") == "tool_use":
                arguments = json.dumps(block.get("input") or {}, ensure_ascii=False)[:TOOL_TEXT_LIMIT]
                self._emit({"method": "item/started", "params": {"item": {
                    "type": "dynamicToolCall", "id": block.get("id"), "tool": block.get("name"),
                    "status": "inProgress", "arguments": arguments}}})
        if texts:
            self._emit({"method": "item/completed", "params": {"item": {
                "type": "agentMessage", "id": message.get("id"), "text": "".join(texts)}}})

    def _on_tool_results(self, message: dict) -> None:
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            raw = block.get("content")
            if isinstance(raw, list):
                raw = "\n".join(part.get("text", "") for part in raw if isinstance(part, dict))
            text = str(raw or "")[:TOOL_TEXT_LIMIT]
            self._emit({"method": "item/completed", "params": {"item": {
                "type": "dynamicToolCall", "id": block.get("tool_use_id"),
                "status": "failed" if block.get("is_error") else "completed",
                "contentItems": [{"type": "inputText", "text": text}]}}})

    def _on_result(self, result: dict) -> None:
        for denial in result.get("permission_denials") or []:
            if isinstance(denial, dict):
                self._emit({"method": "item/completed", "params": {"item": {
                    "type": "dynamicToolCall", "id": denial.get("tool_use_id"), "tool": denial.get("tool_name"),
                    "status": "declined", "contentItems": [{"type": "inputText",
                    "text": "Denied: Claude Native Chat is read-only."}]}}})
        failed = bool(result.get("is_error"))
        self._emit({"method": "turn/completed", "params": {"turn": {
            "id": self.session_id, "status": "failed" if failed else "completed"}}})
