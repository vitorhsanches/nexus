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

import hashlib
import hmac
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
# Development (P4.1B): edits are gated by a human decision through the
# SDK-host permission channel. Native reads/searches are intentionally not
# offered: Claude Code can satisfy Read/Grep/Glob from its own permissions
# without asking the SDK host, so Nexus cannot reliably inspect their paths
# before file contents reach the model. Shell tools are also NOT offered:
# Bash/PowerShell have no filesystem confinement on Windows (neither the
# worktree cwd nor a human approval is a sandbox), so arbitrary commands are
# blocked outright. Tests run through Nexus' operator-owned verification
# profiles (nexus.dev.test_profiles), never through a command the model composes.
WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
COMMAND_TOOLS = frozenset({"Bash", "PowerShell"})
PATH_READ_TOOLS = frozenset({"Read", "Grep", "Glob"})
# Development must not expose native readers/searchers: the SDK permission
# callback is not guaranteed to run for every read, so path validation there
# cannot protect file contents before Claude sees them.
DEV_TOOLS = ("Edit", "Write")
MODEL_ALIASES = ("default", "sonnet", "opus", "haiku")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
STATUS_TIMEOUT_SECONDS = 20
TOOL_TEXT_LIMIT = 2000

_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,63}$")
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


def operation_digest(tool: str, tool_input: dict) -> str:
    """Stable identity of one exact operation (tool + complete input).

    The same digest is stored with the approval, shown to the human, echoed in
    the decision and re-checked just before the input is released to Claude.
    """
    canonical = json.dumps({"tool": tool, "input": tool_input}, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _absolute_like(value: str) -> bool:
    return value.startswith(("/", "\\", "~")) or bool(re.match(r"^[A-Za-z]:", value))


def _sensitive_path(relative: str) -> bool:
    from nexus.dev.worktree import _sensitive_review_path  # noqa: PLC0415 -- shared credential-path rules

    return _sensitive_review_path(relative)


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


def _control_session_command(executable: str) -> list[str]:
    return [
        executable, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--restricted", "--tools", ",".join(READ_ONLY_TOOLS), "--permission-prompts", "none",
        "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
    ]


def discover_models(*, timeout_seconds: int = STATUS_TIMEOUT_SECONDS, launcher=None) -> list[dict]:
    """Models offered to the signed-in account, from the CLI's own ``initialize``
    control response. No user message is sent, so no inference happens."""
    try:
        process = (launcher or _default_launcher)(_control_session_command(find_claude()), env=claude_env(),
                                                  cwd=os.environ.get("TEMP") or os.getcwd())
    except OSError as error:
        raise ClaudeNativeError("MODEL_DISCOVERY_FAILED", sanitize_error_text(str(error)) or "Claude Code could not start.") from error
    timer = threading.Timer(timeout_seconds, lambda: kill_process_tree(process))
    timer.daemon = True
    timer.start()
    try:
        process.stdin.write(json.dumps({"type": "control_request", "request_id": "nexus-init",
                                        "request": {"subtype": "initialize", "hooks": None}}) + "\n")
        process.stdin.close()
        for raw in process.stdout:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            response = (event.get("response") or {}) if isinstance(event, dict) and event.get("type") == "control_response" else {}
            if response.get("request_id") != "nexus-init":
                continue
            if response.get("subtype") != "success":
                raise ClaudeNativeError("MODEL_DISCOVERY_FAILED", "Claude Code refused the initialize request.")
            models = []
            for row in (response.get("response") or {}).get("models") or []:
                value = row.get("value") if isinstance(row, dict) else None
                if isinstance(value, str) and _MODEL_ID_RE.match(value):
                    efforts = row.get("supportedEffortLevels") if isinstance(row.get("supportedEffortLevels"), list) else []
                    models.append({"id": value, "name": str(row.get("displayName") or value),
                                   "efforts": [item for item in efforts if item in EFFORTS]})
            return models
    except (OSError, ValueError) as error:
        raise ClaudeNativeError("MODEL_DISCOVERY_FAILED", sanitize_error_text(str(error)) or "Model discovery failed.") from error
    finally:
        timer.cancel()
        if process.poll() is None:
            kill_process_tree(process)
    raise ClaudeNativeError("MODEL_DISCOVERY_FAILED", "Claude Code did not answer the initialize request.")


def build_command(executable: str, *, session_id: str, resume: bool, model: str | None, effort: str | None,
                  development: bool = False) -> list[str]:
    """The only command lines Nexus ever uses for Claude Native.

    Chat is read-only (prompts denied automatically). Development exposes
    only editing tools and routes permission prompts to Nexus (the SDK host)
    for a human decision. Native read/search tools are excluded because their
    executions are not guaranteed to prompt the host.
    """
    if not _SESSION_ID_RE.match(session_id or ""):
        raise ClaudeNativeError("INVALID_SESSION_ID", "Claude session id must be a lowercase UUID.")
    if model is not None and not _MODEL_ID_RE.match(model):
        raise ClaudeNativeError("MODEL_NOT_ALLOWED", "Unsupported Claude model identifier.")
    if effort is not None and effort not in EFFORTS:
        raise ClaudeNativeError("EFFORT_NOT_SUPPORTED", "Unsupported Claude effort level.")
    if development:
        command = [
            executable, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--restricted", "--tools", ",".join(DEV_TOOLS),
            "--permission-mode", "manual",
            "--permission-prompt-tool", "stdio", "--permission-prompts", "host",
            "--strict-mcp-config", "--disable-slash-commands",
        ]
    else:
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
                 on_session: Callable[[str, str | None], None] | None = None, launcher=None,
                 development: bool = False,
                 on_permission: Callable[[str, str, dict], bool] | None = None) -> None:
        self.cwd = cwd
        self.development = development
        # Development only: returns True when it took ownership of answering
        # the request later (respond_server_request), False to deny it now.
        self._on_permission = on_permission
        self._pending_inputs: dict[str, tuple[str, dict]] = {}
        self._write_lock = threading.Lock()
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
        self._tool_names: dict[str, str] = {}
        self._tool_inputs: dict[str, dict] = {}

    # -- public ---------------------------------------------------------

    def turn_interrupt(self, _thread_id=None, _turn_id=None) -> dict:
        """Stop the running turn (the CLI keeps what it already persisted)."""
        self._interrupted.set()
        process = self._process
        if process is not None and process.poll() is None:
            kill_process_tree(process)
        return {}

    def respond_server_request(self, request_id, result: dict) -> None:
        """Answer one permission request with the human decision.

        ``accept`` allows exactly the input that was presented (sent back as
        ``updatedInput``); anything else denies it.
        """
        pending = self._pending_inputs.pop(request_id, None)
        if pending is None:
            return
        tool, tool_input, digest = pending
        decision = result or {}
        if decision.get("decision") == "accept":
            # Execute only what was presented and approved: the approved digest
            # must match the exact input held for this request.
            approved = decision.get("operation_sha256")
            if not isinstance(approved, str) or not hmac.compare_digest(approved, digest) \
                    or operation_digest(tool, tool_input) != digest:
                self._deny(request_id, "Denied by Nexus: the approval does not match the requested operation.")
                return
            self._answer(request_id, {"behavior": "allow", "updatedInput": tool_input})
        else:
            self._answer(request_id, {"behavior": "deny", "message": "Declined by the Nexus operator."})

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
                                model=self.model, effort=self.effort, development=self.development)
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
                if self.development:
                    # SDK-host session: stdin stays open to answer permission
                    # requests and is closed once the result arrives.
                    self._send({"type": "control_request", "request_id": "nexus-init",
                                "request": {"subtype": "initialize", "hooks": None}})
                    self._send({"type": "user", "message": {"role": "user", "content": prompt},
                                "parent_tool_use_id": None, "session_id": self.session_id})
                else:
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

    def _send(self, message: dict) -> None:
        process = self._process
        if process is None or process.stdin is None:
            return
        with self._write_lock:
            try:
                process.stdin.write(json.dumps(message) + "\n")
                process.stdin.flush()
            except (OSError, ValueError):
                pass

    def _answer(self, request_id, response: dict) -> None:
        self._send({"type": "control_response",
                    "response": {"subtype": "success", "request_id": request_id, "response": response}})

    def _deny(self, request_id, message: str) -> None:
        self._answer(request_id, {"behavior": "deny", "message": message})

    def _unsafe_path(self, tool: str, tool_input: dict) -> str | None:
        """Why a tool's target is refused, or None when it stays inside the worktree.

        Fails closed: anything unresolvable, remote/device/stream-style, inside
        ``.git`` or credential-like is refused before a human is asked.
        """
        if tool == "Glob":
            pattern = tool_input.get("pattern")
            if not isinstance(pattern, str) or _absolute_like(pattern) or ".." in re.split(r"[\\/]", pattern):
                return "glob patterns must be relative to the session worktree"
        target = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
        if target is None and tool in {"Grep", "Glob"}:
            return None  # searches default to the worktree
        if not isinstance(target, str) or not target or "\0" in target:
            return "a worktree path is required"
        normalized = target.replace("\\", "/")
        if normalized.startswith("//") or ":" in normalized[2:]:
            return "network, device and stream paths are not allowed"
        try:
            root = os.path.realpath(self.cwd)
            resolved = os.path.realpath(os.path.join(root, target))
            inside = os.path.normcase(os.path.commonpath([root, resolved])) == os.path.normcase(root)
        except (ValueError, OSError):
            return "paths outside the session worktree are not allowed"
        if not inside:
            return "paths outside the session worktree are not allowed"
        relative = os.path.relpath(resolved, root).replace("\\", "/")
        if ".git" in {part.lower() for part in relative.split("/")}:
            return "the Git metadata of the worktree is off limits"
        if _sensitive_path(relative):
            return "credential and secret files are off limits"
        return None

    def _on_control_request(self, request_id, request: dict) -> None:
        if request.get("subtype") != "can_use_tool":
            self._send({"type": "control_response", "response": {
                "subtype": "error", "request_id": request_id, "error": "Unsupported request from Claude Code."}})
            return
        tool = request.get("tool_name")
        tool_input = request.get("input") if isinstance(request.get("input"), dict) else {}
        if tool in COMMAND_TOOLS:
            self._deny(request_id, "Denied by Nexus: arbitrary commands are not available in Claude Native "
                                   "Development; run tests through a Nexus verification profile.")
            return
        if not self.development or tool not in DEV_TOOLS:
            self._deny(request_id, "Denied by Nexus: tool not allowed in this session.")
            return
        reason = self._unsafe_path(tool, tool_input)
        if reason:
            self._deny(request_id, f"Denied by Nexus: {reason}; access is confined to the session worktree.")
            return
        self._pending_inputs[request_id] = (tool, tool_input, operation_digest(tool, tool_input))
        accepted = False
        if self._on_permission is not None:
            try:
                accepted = bool(self._on_permission(request_id, tool, tool_input))
            except Exception:
                accepted = False
        if not accepted:
            self._pending_inputs.pop(request_id, None)
            self._deny(request_id, "Denied by Nexus: no human approval channel.")

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
            if kind == "control_request":
                self._on_control_request(event.get("request_id"), event.get("request") or {})
                continue
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
                if self.development:
                    try:
                        process.stdin.close()
                    except (OSError, ValueError):
                        pass
        return result

    def _on_init(self, event: dict) -> None:
        if event.get("session_id") != self.session_id:
            # The CLI must run exactly the conversation Nexus asked for.
            self.stop()
            raise ClaudeNativeError("SESSION_MISMATCH", "Claude Code started a different session than requested.")
        tools = event.get("tools") or []
        allowed = DEV_TOOLS if self.development else READ_ONLY_TOOLS
        if any(tool not in allowed for tool in tools):
            self.stop()
            raise ClaudeNativeError("UNSAFE_TOOLS", "Claude Code exposed tools beyond the allowlist.")
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
                if isinstance(block.get("id"), str) and isinstance(block.get("name"), str):
                    self._tool_names[block["id"]] = block["name"]
                    self._tool_inputs[block["id"]] = block.get("input") if isinstance(block.get("input"), dict) else {}
                self._emit({"method": "item/started", "params": {"item": self._tool_item(block.get("id"), "inProgress")}})
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
            if self._tool_is_sensitive(block.get("tool_use_id")):
                text = "<content withheld: sensitive path>"
            item = self._tool_item(block.get("tool_use_id"), "failed" if block.get("is_error") else "completed")
            if item["type"] == "commandExecution":
                item["aggregatedOutput"] = text
            else:
                item["contentItems"] = [{"type": "inputText", "text": text}]
            self._emit({"method": "item/completed", "params": {"item": item}})

    def _tool_is_sensitive(self, tool_use_id) -> bool:
        tool_input = self._tool_inputs.get(tool_use_id, {})
        target = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
        return isinstance(target, str) and bool(target) and _sensitive_path(target.replace("\\", "/").lstrip("/"))

    def _tool_item(self, tool_use_id, status: str) -> dict:
        """Command and file tools use the item shapes the cockpit already renders."""
        name = self._tool_names.get(tool_use_id)
        tool_input = self._tool_inputs.get(tool_use_id, {})
        if name in COMMAND_TOOLS:
            return {"type": "commandExecution", "id": tool_use_id, "tool": name, "status": status,
                    "command": str(tool_input.get("command", ""))[:TOOL_TEXT_LIMIT]}
        if name in WRITE_TOOLS:
            return {"type": "fileChange", "id": tool_use_id, "tool": name, "status": status,
                    "path": str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")}
        return {"type": "dynamicToolCall", "id": tool_use_id, "tool": name, "status": status,
                "arguments": json.dumps(tool_input, ensure_ascii=False)[:TOOL_TEXT_LIMIT]}

    def _on_result(self, result: dict) -> None:
        for denial in result.get("permission_denials") or []:
            if isinstance(denial, dict):
                self._emit({"method": "item/completed", "params": {"item": {
                    "type": "dynamicToolCall", "id": denial.get("tool_use_id"), "tool": denial.get("tool_name"),
                    "status": "declined", "contentItems": [{"type": "inputText",
                    "text": "Denied by Nexus." if self.development else "Denied: Claude Native Chat is read-only."}]}}})
        failed = bool(result.get("is_error"))
        self._emit({"method": "turn/completed", "params": {"turn": {
            "id": self.session_id, "status": "failed" if failed else "completed"}}})
