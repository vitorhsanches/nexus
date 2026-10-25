"""Claude Native Chat (P4.1A): adapter contract, service integration and isolation.

Mocks only: a fake CLI process replays recorded ``stream-json`` events. The
real Claude Code flow is a separate, explicitly authorized smoke test.
"""

from __future__ import annotations

import io
import json
import threading
import unittest
from unittest.mock import patch

import pytest

from nexus.chat import claude_native, service
from nexus.chat.claude_native import ClaudeNativeError, ClaudeTurn, build_command
from nexus.registry import chat_sessions
from tests.test_chat_service import (  # noqa: F401 -- fixtures are used by name
    FakeAppServerClient,
    _patch_client,
    _register_project,
    _wait_until,
    isolated_registry,
)

SESSION = "11111111-2222-4333-8444-555555555555"


# -- fakes ------------------------------------------------------------------


def _init(session_id=SESSION, tools=("Glob", "Grep", "Read"), model="claude-sonnet-x"):
    return {"type": "system", "subtype": "init", "session_id": session_id, "tools": list(tools), "model": model,
            "permissionMode": "dontAsk", "mcp_servers": []}


def _success_events(text="The README describes Nexus.", session_id=SESSION):
    return [
        _init(session_id),
        {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "msg_1"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "The README "}}},
        {"type": "assistant", "message": {"id": "msg_1", "content": [
            {"type": "tool_use", "id": "tool_1", "name": "Read", "input": {"file_path": "README.md"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tool_1", "content": "# Nexus"}]}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "describes Nexus."}}},
        {"type": "assistant", "message": {"id": "msg_1", "content": [{"type": "text", "text": text}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": session_id,
         "permission_denials": [{"tool_name": "Bash", "tool_use_id": "tool_2", "tool_input": {"command": "rm -rf ."}}]},
    ]


class FakeProcess:
    def __init__(self, lines, *, block=False, stderr=""):
        self._lines = [json.dumps(line) if isinstance(line, dict) else line for line in lines]
        self._block = block
        self.killed = threading.Event()
        self.stdin = io.StringIO()
        self.stdin_value = None
        original_close = self.stdin.close

        def close():
            self.stdin_value = self.stdin.getvalue()
            original_close()

        self.stdin.close = close
        self.stderr = io.StringIO(stderr)
        self.returncode = None
        self.pid = 4242

    @property
    def stdout(self):
        for line in self._lines:
            yield line + "\n"
        if self._block:
            self.killed.wait(timeout=10)
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture(autouse=True)
def _fake_cli(monkeypatch):
    monkeypatch.setattr(claude_native, "find_claude", lambda: r"C:\fake\claude.exe")

    def fake_kill(process):
        process.killed.set()
        process.returncode = -9

    monkeypatch.setattr(claude_native, "kill_process_tree", fake_kill)


def _launcher(process, captured=None):
    def launch(command, *, env, cwd):
        if captured is not None:
            captured.update(command=command, env=env, cwd=cwd)
        return process
    return launch


# -- command line and environment ------------------------------------------------


def test_command_line_is_read_only_and_never_bypasses_permissions():
    command = build_command(r"C:\c\claude.exe", session_id=SESSION, resume=False, model="sonnet", effort="high")
    assert command[:2] == [r"C:\c\claude.exe", "-p"]
    joined = " ".join(command)
    for flag in ("--restricted", "--strict-mcp-config", "--disable-slash-commands", "--include-partial-messages"):
        assert flag in command
    assert command[command.index("--tools") + 1] == "Read,Grep,Glob"
    assert command[command.index("--allowedTools") + 1] == "Read,Grep,Glob"
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--permission-prompts") + 1] == "none"
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert command[command.index("--session-id") + 1] == SESSION and "--resume" not in command
    assert command[command.index("--model") + 1] == "sonnet"
    assert command[command.index("--effort") + 1] == "high"
    for forbidden in ("dangerously", "bypassPermissions", "acceptEdits", "Bash", "Edit", "Write", "auto"):
        assert forbidden not in joined


def test_resume_reuses_the_conversation_and_default_model_is_left_to_the_cli():
    command = build_command("claude.exe", session_id=SESSION, resume=True, model="default", effort=None)
    assert command[command.index("--resume") + 1] == SESSION
    assert "--session-id" not in command and "--model" not in command and "--effort" not in command


@pytest.mark.parametrize("kwargs, code", [
    ({"session_id": "not-a-uuid"}, "INVALID_SESSION_ID"),
    ({"model": "claude-anything; rm"}, "MODEL_NOT_ALLOWED"),
    ({"effort": "ultra"}, "EFFORT_NOT_SUPPORTED"),
])
def test_unvalidated_values_never_reach_the_command_line(kwargs, code):
    values = {"session_id": SESSION, "resume": False, "model": None, "effort": None, **kwargs}
    with pytest.raises(ClaudeNativeError) as excinfo:
        build_command("claude.exe", **values)
    assert excinfo.value.code == code


def test_environment_drops_host_sessions_gateways_and_omniroute(monkeypatch):
    for key in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_AGENT_SDK_VERSION",
                "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "OMNIROUTE_API_KEY", "CLAUDE_PID", "CLAUDE_EFFORT"):
        monkeypatch.setenv(key, "value-123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-native-key")
    env = claude_native.claude_env()
    for key in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_AGENT_SDK_VERSION",
                "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "OMNIROUTE_API_KEY", "CLAUDE_PID", "CLAUDE_EFFORT"):
        assert key not in env
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-native-key", "a key the CLI itself supports stays usable"


def test_discovery_resolves_the_npm_shim_to_the_native_executable(tmp_path, monkeypatch):
    monkeypatch.undo()
    monkeypatch.delenv("NEXUS_CLAUDE_PATH", raising=False)
    npm = tmp_path / "npm"
    exe = npm / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    (npm / "claude.cmd").write_text("", encoding="utf-8")
    monkeypatch.setattr(claude_native.shutil, "which", lambda name: str(npm / "claude.cmd"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "nobody"))
    assert claude_native.find_claude() == str(exe)


def test_discovery_refuses_a_shell_shim_without_a_native_executable(tmp_path, monkeypatch):
    monkeypatch.undo()
    monkeypatch.delenv("NEXUS_CLAUDE_PATH", raising=False)
    (tmp_path / "claude.cmd").write_text("", encoding="utf-8")
    monkeypatch.setattr(claude_native.shutil, "which", lambda name: str(tmp_path / "claude.cmd"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "nobody"))
    with pytest.raises(ClaudeNativeError) as excinfo:
        claude_native.find_claude()
    assert excinfo.value.code == "CLAUDE_NOT_FOUND"


class _Completed:
    def __init__(self, stdout):
        self.stdout = stdout


def test_cli_status_reports_login_without_credentials():
    def runner(command, **_kwargs):
        if command[1] == "--version":
            return _Completed("2.1.288 (Claude Code)")
        return _Completed(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                                      "subscriptionType": "max", "oauthAccessToken": "secret"}))

    status = claude_native.cli_status(runner=runner)
    assert status == {"installed": True, "version": "2.1.288", "logged_in": True, "auth_method": "claude.ai",
                      "api_provider": "firstParty", "subscription": "max", "error": None}


def test_cli_status_reports_logged_out_and_unreadable_states():
    out = claude_native.cli_status(runner=lambda command, **_: _Completed(
        "2.1.288" if command[1] == "--version" else json.dumps({"loggedIn": False, "authMethod": "none"})))
    assert out["installed"] and out["logged_in"] is False
    broken = claude_native.cli_status(runner=lambda command, **_: _Completed("2.1" if command[1] == "--version" else "<html>"))
    assert broken["logged_in"] is False and broken["error"]


# -- one turn ----------------------------------------------------------------------


def test_a_turn_streams_without_duplicating_text_and_reports_tools_and_denials():
    events, sessions, captured = [], [], {}
    process = FakeProcess(_success_events())
    turn = ClaudeTurn("C:/repo", session_id=SESSION, resume=False, on_event=events.append,
                      on_session=lambda sid, model: sessions.append((sid, model)), launcher=_launcher(process, captured))

    text = turn.run("Summarize the README.", timeout_seconds=30)

    assert text == "The README describes Nexus."
    assert process.stdin_value == "Summarize the README." and "Summarize" not in " ".join(captured["command"])
    assert captured["cwd"] == "C:/repo"
    assert sessions == [(SESSION, "claude-sonnet-x")] and turn.active_model == "claude-sonnet-x"
    methods = [event["method"] for event in events]
    assert methods[0] == "turn/started" and methods[-1] == "turn/completed"
    deltas = [e["params"] for e in events if e["method"] == "item/agentMessage/delta"]
    assert [d["delta"] for d in deltas] == ["The README ", "describes Nexus."]
    finals = [e["params"]["item"] for e in events if e["method"] == "item/completed" and e["params"]["item"]["type"] == "agentMessage"]
    assert len(finals) == 1 and finals[0]["id"] == deltas[0]["itemId"] == "msg_1", "final message replaces the streamed bubble"
    tools = [e["params"]["item"] for e in events if e["params"].get("item", {}).get("type") == "dynamicToolCall"]
    assert tools[0] == {"type": "dynamicToolCall", "id": "tool_1", "tool": "Read", "status": "inProgress",
                        "arguments": '{"file_path": "README.md"}'}
    assert tools[1]["status"] == "completed" and tools[1]["tool"] == "Read"
    assert tools[1]["contentItems"][0]["text"] == "# Nexus"
    assert tools[2]["status"] == "declined" and tools[2]["tool"] == "Bash"


def test_not_signed_in_is_a_clear_error():
    process = FakeProcess([
        _init(),
        {"type": "assistant", "error": "authentication_failed", "message": {"id": "m", "content": [{"type": "text", "text": "Not logged in · Please run /login"}]}},
        {"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login", "session_id": SESSION},
    ])
    with pytest.raises(ClaudeNativeError) as excinfo:
        ClaudeTurn("C:/repo", session_id=SESSION, resume=False, launcher=_launcher(process)).run("hi", timeout_seconds=30)
    assert excinfo.value.code == "CLAUDE_NOT_SIGNED_IN"


@pytest.mark.parametrize("init, code", [
    (_init(session_id="99999999-2222-4333-8444-555555555555"), "SESSION_MISMATCH"),
    (_init(tools=("Read", "Bash")), "UNSAFE_TOOLS"),
])
def test_an_unexpected_session_or_tool_set_is_refused(init, code):
    process = FakeProcess([init, {"type": "result", "is_error": False, "result": "x", "session_id": SESSION}])
    with pytest.raises(ClaudeNativeError) as excinfo:
        ClaudeTurn("C:/repo", session_id=SESSION, resume=False, launcher=_launcher(process)).run("hi", timeout_seconds=30)
    assert excinfo.value.code == code
    assert process.killed.is_set()


def test_a_process_that_exits_without_a_result_fails_and_redacts_stderr(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-very-secret-value")
    process = FakeProcess([_init(), "not json"], stderr="crash with sk-ant-very-secret-value\n")
    with pytest.raises(ClaudeNativeError) as excinfo:
        ClaudeTurn("C:/repo", session_id=SESSION, resume=False, launcher=_launcher(process)).run("hi", timeout_seconds=30)
    assert excinfo.value.code == "CLAUDE_EXITED"
    assert "sk-ant-very-secret-value" not in str(excinfo.value)


def test_events_never_carry_secret_values(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leak-check-123")
    events = []
    process = FakeProcess(_success_events(text="key is sk-ant-leak-check-123"))
    ClaudeTurn("C:/repo", session_id=SESSION, resume=False, on_event=events.append, launcher=_launcher(process)).run("hi", timeout_seconds=30)
    assert "sk-ant-leak-check-123" not in json.dumps(events)


def test_interrupt_terminates_the_process():
    process = FakeProcess([_init()], block=True)
    turn = ClaudeTurn("C:/repo", session_id=SESSION, resume=False, launcher=_launcher(process))
    errors = []
    worker = threading.Thread(target=lambda: errors.append(_capture(turn)))
    worker.start()
    assert _wait_until(lambda: turn.session_confirmed)
    turn.turn_interrupt()
    worker.join(timeout=5)
    assert process.killed.is_set() and errors[0] == "TURN_INTERRUPTED"


def test_a_turn_that_exceeds_its_budget_is_stopped():
    process = FakeProcess([_init()], block=True)
    turn = ClaudeTurn("C:/repo", session_id=SESSION, resume=False, launcher=_launcher(process))
    assert _capture(turn, timeout_seconds=0.2) == "TURN_TIMEOUT"
    assert process.killed.is_set()


def _capture(turn, timeout_seconds=30):
    try:
        turn.run("hi", timeout_seconds=timeout_seconds)
    except ClaudeNativeError as error:
        return error.code
    return None


# -- Chat service -------------------------------------------------------------------------


LOGGED_IN = {"installed": True, "version": "2.1.288", "logged_in": True, "auth_method": "claude.ai",
             "api_provider": "firstParty", "subscription": "max", "error": None}


class ScriptedTurn:
    """Stands in for ClaudeTurn inside the Chat service."""

    instances: list = []
    script = "success"
    release = None

    def __init__(self, cwd, *, session_id, resume, model=None, effort=None, on_event=None, on_session=None):
        self.cwd, self.session_id, self.resume, self.model, self.effort = cwd, session_id, resume, model, effort
        self._on_event, self._on_session = on_event, on_session
        self.active_model = "claude-sonnet-x"
        self._interrupted = threading.Event()
        ScriptedTurn.instances.append(self)

    @property
    def interrupted(self):
        return self._interrupted.is_set()

    def turn_interrupt(self, *_args):
        self._interrupted.set()

    def run(self, prompt, *, timeout_seconds):
        self._on_session(self.session_id, self.active_model)
        self._on_event({"method": "item/agentMessage/delta", "params": {"itemId": "m", "delta": "ok"}})
        if ScriptedTurn.script == "block":
            self._interrupted.wait(timeout=10)
            raise ClaudeNativeError("TURN_INTERRUPTED", "interrupted")
        if ScriptedTurn.script == "fail":
            raise ClaudeNativeError("CLAUDE_TURN_FAILED", "API overloaded")
        return f"answer to: {prompt}"


@pytest.fixture
def claude_service(monkeypatch):
    ScriptedTurn.instances = []
    ScriptedTurn.script = "success"
    monkeypatch.setattr(claude_native, "cli_status", lambda: dict(LOGGED_IN))
    monkeypatch.setattr(claude_native, "ClaudeTurn", ScriptedTurn)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Claude Native must never use OmniRoute or Codex")

    monkeypatch.setattr(service, "_model_catalog", forbidden)
    monkeypatch.setattr(service, "_native_model_catalog", forbidden)
    monkeypatch.setattr(service, "AppServerClient", forbidden)
    monkeypatch.setattr("nexus.workspace.omniroute_oauth.require_connected_route", forbidden)
    monkeypatch.setattr("nexus.workspace.omniroute_oauth.connection_status", forbidden)
    monkeypatch.setattr("nexus.routing.telemetry.OmniRouteTelemetryClient", forbidden)


def _done(turn_id):
    return chat_sessions.get_turn(turn_id)["status"] in {"COMPLETED", "FAILED", "CANCELLED"}


def test_catalog_reports_aliases_and_login_state(claude_service):
    result = service.list_models(backend="claude_native")
    assert [m["id"] for m in result["models"]] == ["default", "sonnet", "opus", "haiku"]
    assert result["default_model"] == "default" and result["catalog_error"] is None
    assert result["account"] == {"authenticated": True, "type": "claude.ai", "plan": "max", "version": "2.1.288", "installed": True}


@pytest.mark.parametrize("status, needle", [
    ({**LOGGED_IN, "logged_in": False, "auth_method": "none"}, "claude auth login"),
    ({**LOGGED_IN, "installed": False, "logged_in": False, "error": "Claude Code CLI was not found."}, "not found"),
])
def test_catalog_explains_missing_login_or_cli(claude_service, monkeypatch, status, needle):
    monkeypatch.setattr(claude_native, "cli_status", lambda: dict(status))
    result = service.list_models(backend="claude_native")
    assert result["models"] == [] and needle in result["catalog_error"]


def test_session_is_bound_to_the_project_and_persists_its_backend(isolated_registry, claude_service):
    _register_project(isolated_registry)
    session = service.create_session("demo", "sonnet", "high", backend="claude_native")
    assert (session["backend"], session["requested_model"], session["requested_effort"]) == ("claude_native", "sonnet", "high")
    row = chat_sessions.get_session(session["session_id"])
    assert row["backend"] == "claude_native" and row["claude_session_id"] is None


@pytest.mark.parametrize("status, code", [
    ({**LOGGED_IN, "logged_in": False}, "CLAUDE_NOT_SIGNED_IN"),
    ({**LOGGED_IN, "installed": False, "logged_in": False, "error": "missing"}, "CLAUDE_NOT_FOUND"),
])
def test_session_creation_fails_closed_without_cli_or_login(isolated_registry, claude_service, monkeypatch, status, code):
    _register_project(isolated_registry)
    monkeypatch.setattr(claude_native, "cli_status", lambda: dict(status))
    with pytest.raises(service.ChatError) as excinfo:
        service.create_session("demo", "default", backend="claude_native")
    assert excinfo.value.code == code
    assert chat_sessions.list_sessions("demo") == []


def test_invalid_alias_or_effort_is_rejected(isolated_registry, claude_service):
    _register_project(isolated_registry)
    for model, effort, code in (("gpt-5", None, "MODEL_NOT_ALLOWED"), ("opus", "ultra", "EFFORT_NOT_SUPPORTED")):
        with pytest.raises(service.ChatError) as excinfo:
            service.create_session("demo", model, effort, backend="claude_native")
        assert excinfo.value.code == code


def test_turns_stream_persist_and_resume_the_same_conversation(isolated_registry, claude_service):
    _register_project(isolated_registry)
    sid = service.create_session("demo", "default", backend="claude_native")["session_id"]

    first = service.send_turn(sid, "Read the README")
    assert _wait_until(lambda: _done(first["turn_id"]))
    turn_row = chat_sessions.get_turn(first["turn_id"])
    assert turn_row["status"] == "COMPLETED" and turn_row["response_text"] == "answer to: Read the README"
    assert turn_row["provider"] == "anthropic" and turn_row["model"] == "claude-sonnet-x"
    conversation = chat_sessions.get_session(sid)["claude_session_id"]
    assert service.get_session_detail(sid)["claude_session_id"] == conversation, "the cockpit uses it to load history"
    assert ScriptedTurn.instances[0].resume is False and ScriptedTurn.instances[0].session_id == conversation
    assert ScriptedTurn.instances[0].model == "default" and ScriptedTurn.instances[0].cwd.endswith("demo")

    # Simulated Nexus restart: only durable state remains.
    service._active_turns.clear()
    second = service.send_turn(sid, "And the license?")
    assert _wait_until(lambda: _done(second["turn_id"]))
    assert ScriptedTurn.instances[1].resume is True and ScriptedTurn.instances[1].session_id == conversation

    history = service.get_history(sid)
    assert history["source"] == "nexus_turns"
    assert [(item["type"], item["text"]) for item in history["items"]] == [
        ("userMessage", "Read the README"), ("agentMessage", "answer to: Read the README"),
        ("userMessage", "And the license?"), ("agentMessage", "answer to: And the license?")]


def test_interrupt_cancels_the_turn_and_keeps_the_session_usable(isolated_registry, claude_service):
    _register_project(isolated_registry)
    sid = service.create_session("demo", "default", backend="claude_native")["session_id"]
    ScriptedTurn.script = "block"
    turn = service.send_turn(sid, "long question")
    assert _wait_until(lambda: service._active_turns.get(turn["turn_id"]) is not None
                       and service._active_turns[turn["turn_id"]].codex_turn_id is not None)
    assert service.interrupt_turn(sid, turn["turn_id"])["status"] == "INTERRUPT_REQUESTED"
    assert _wait_until(lambda: _done(turn["turn_id"]))
    assert chat_sessions.get_turn(turn["turn_id"])["status"] == "CANCELLED"
    assert chat_sessions.get_session(sid)["status"] == "ACTIVE"


def test_a_failure_is_reported_and_never_falls_back_to_codex_or_omniroute(isolated_registry, claude_service):
    _register_project(isolated_registry)
    sid = service.create_session("demo", "default", backend="claude_native")["session_id"]
    ScriptedTurn.script = "fail"
    turn = service.send_turn(sid, "hi")
    assert _wait_until(lambda: _done(turn["turn_id"]))
    row = chat_sessions.get_turn(turn["turn_id"])
    assert (row["status"], row["error_code"]) == ("FAILED", "CLAUDE_TURN_FAILED")
    assert FakeAppServerClient.instances == []


def test_codex_sessions_never_run_through_claude(isolated_registry, monkeypatch):
    _register_project(isolated_registry)
    monkeypatch.setattr(claude_native, "ClaudeTurn", lambda *a, **k: (_ for _ in ()).throw(AssertionError("claude used")))
    session = service.create_session("demo")
    turn = service.send_turn(session["session_id"], "hello")
    assert _wait_until(lambda: _done(turn["turn_id"]))
    assert chat_sessions.get_turn(turn["turn_id"])["status"] == "COMPLETED"
    assert FakeAppServerClient.instances


# -- routes ----------------------------------------------------------------------------


class ClaudeNativeRoutesTest(unittest.TestCase):
    def _client(self):
        from starlette.testclient import TestClient
        from nexus.web.app import create_app

        return TestClient(create_app(), base_url="http://127.0.0.1:8000")

    def test_claude_backend_is_forwarded(self):
        with patch("nexus.chat.service.create_session", return_value={"session_id": "CHAT-1"}) as mock_create:
            response = self._client().post("/api/chat/sessions", json={"project_id": "demo", "backend": "claude_native", "model": "opus"})
        self.assertEqual(response.status_code, 200)
        mock_create.assert_called_once_with("demo", "opus", None, backend="claude_native")

    def test_claude_catalog_route(self):
        with patch("nexus.chat.service.list_models", return_value={"models": []}) as mock_list:
            self._client().get("/api/chat/models?backend=claude_native")
        mock_list.assert_called_once_with(refresh=False, backend="claude_native")
