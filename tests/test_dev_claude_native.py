"""Claude Native Development (P4.1B): permission bridge, approvals and isolation.

Mocks only. A fake CLI process speaks the SDK-host stream-json protocol; the
real Claude Code flow is a separate, explicitly authorized smoke test.
"""

from __future__ import annotations

import json
import queue
import threading
import unittest
from unittest.mock import patch

import pytest

from nexus.chat import claude_native
from nexus.chat.claude_native import ClaudeNativeError, ClaudeTurn, build_command
from nexus.dev import approvals, executor, mission_runner, service
from nexus.dev.service import DevServiceError
from nexus.registry import dev_sessions
from tests.test_dev_executor import (  # noqa: F401 -- fixtures are used by name
    FakeAppServerClient,
    _patch_client,
    _register_project,
    _wait_until,
    isolated_registry,
)

SESSION = "11111111-2222-4333-8444-555555555555"
BASH_INPUT = {"command": "python -m pytest -q", "description": "Run the tests"}
EDIT_INPUT = {"file_path": "calc.py", "old_string": "a - b", "new_string": "a + b"}
EDIT_DIGEST = claude_native.operation_digest("Edit", EDIT_INPUT)
SECRET_CONTENT = "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


# -- adapter: command line ---------------------------------------------------------


def test_development_command_routes_every_prompt_to_nexus_and_never_bypasses():
    command = build_command("claude.exe", session_id=SESSION, resume=False, model="claude-opus-4-8", effort="high",
                            development=True)
    assert command[command.index("--input-format") + 1] == "stream-json"
    assert command[command.index("--tools") + 1] == "Edit,Write"
    assert "Bash" not in command and "PowerShell" not in command, "no shell tool is offered on Windows"
    assert command[command.index("--permission-mode") + 1] == "manual"
    assert command[command.index("--permission-prompt-tool") + 1] == "stdio"
    assert command[command.index("--permission-prompts") + 1] == "host"
    for flag in ("--restricted", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in command
    assert "--allowedTools" not in command, "nothing is pre-approved in Development"
    joined = " ".join(command)
    for forbidden in ("dangerously", "bypassPermissions", "acceptEdits", "dontAsk", "auto"):
        assert forbidden not in joined


def test_model_identifiers_are_validated_before_reaching_the_command_line():
    for bad in ("claude opus", "x;rm", "Opus", ""):
        with pytest.raises(ClaudeNativeError):
            build_command("claude.exe", session_id=SESSION, resume=False, model=bad, effort=None, development=True)


# -- adapter: SDK-host session ------------------------------------------------------


class FakeHostProcess:
    """Replays CLI output; a control_request blocks until Nexus answers it."""

    def __init__(self, script):
        self.script = script
        self.written: list[dict] = []
        self.responses: "queue.Queue[dict]" = queue.Queue()
        self.stdin_closed = threading.Event()
        self.killed = threading.Event()
        self.returncode = None
        self.pid = 1
        self.stderr = iter(())
        outer = self

        class Stdin:
            def write(self, text):
                for line in text.splitlines():
                    if line.strip():
                        message = json.loads(line)
                        outer.written.append(message)
                        if message.get("type") == "control_response":
                            outer.responses.put(message)

            def flush(self):
                pass

            def close(self):
                outer.stdin_closed.set()

        self.stdin = Stdin()

    @property
    def stdout(self):
        for step in self.script:
            if step == "WAIT":
                try:
                    self.responses.get(timeout=10)
                except queue.Empty:
                    return
                continue
            if step == "HANG":
                self.killed.wait(timeout=10)
                return
            yield json.dumps(step) + "\n"
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


def _init(tools=claude_native.DEV_TOOLS):
    return {"type": "system", "subtype": "init", "session_id": SESSION, "tools": list(tools), "model": "claude-x"}


def _can_use(request_id, tool, tool_input):
    return {"type": "control_request", "request_id": request_id,
            "request": {"subtype": "can_use_tool", "tool_name": tool, "input": tool_input, "tool_use_id": "tu_" + request_id}}


RESULT = {"type": "result", "is_error": False, "result": "done", "session_id": SESSION}


@pytest.fixture(autouse=True)
def _fake_cli(monkeypatch):
    monkeypatch.setattr(claude_native, "find_claude", lambda: r"C:\fake\claude.exe")

    def fake_kill(process):
        process.killed.set()
        process.returncode = -9

    monkeypatch.setattr(claude_native, "kill_process_tree", fake_kill)


def _turn(process, tmp_path, on_permission):
    return ClaudeTurn(str(tmp_path), session_id=SESSION, resume=False, development=True,
                      on_permission=on_permission, launcher=lambda command, env, cwd: process)


def _control_responses(process):
    return [m["response"] for m in process.written if m.get("type") == "control_response"]


def test_a_sensitive_edit_waits_for_a_decision_and_only_the_exact_input_is_allowed(tmp_path):
    process = FakeHostProcess([_init(), _can_use("r1", "Edit", EDIT_INPUT), "WAIT", RESULT])
    seen = []
    turn = _turn(process, tmp_path, lambda rid, tool, tool_input: seen.append((rid, tool, tool_input)) or True)
    worker = threading.Thread(target=lambda: turn.run("fix the bug", timeout_seconds=30))
    worker.start()
    assert _wait_until(lambda: seen)
    assert seen == [("r1", "Edit", EDIT_INPUT)]
    assert _control_responses(process) == [], "nothing is answered before a human decides"

    turn.respond_server_request("r1", {"decision": "accept", "operation_sha256": EDIT_DIGEST})
    worker.join(timeout=5)
    assert _control_responses(process) == [{"subtype": "success", "request_id": "r1",
                                             "response": {"behavior": "allow", "updatedInput": EDIT_INPUT}}]
    first, second = process.written[0], process.written[1]
    assert first["request"]["subtype"] == "initialize"
    assert second == {"type": "user", "message": {"role": "user", "content": "fix the bug"},
                      "parent_tool_use_id": None, "session_id": SESSION}
    assert process.stdin_closed.is_set()


@pytest.mark.parametrize("decision", ["decline", "cancel", "acceptForSession", None])
def test_anything_but_accept_denies(tmp_path, decision):
    process = FakeHostProcess([_init(), _can_use("r1", "Edit", EDIT_INPUT), "WAIT", RESULT])
    turn = _turn(process, tmp_path, lambda *_: True)
    worker = threading.Thread(target=lambda: turn.run("x", timeout_seconds=30))
    worker.start()
    assert _wait_until(lambda: "r1" in turn._pending_inputs)
    turn.respond_server_request("r1", {"decision": decision, "operation_sha256": EDIT_DIGEST})
    worker.join(timeout=5)
    assert _control_responses(process)[0]["response"]["behavior"] == "deny"


@pytest.mark.parametrize("approved", [None, "", "0" * 64, claude_native.operation_digest("Edit", {**EDIT_INPUT, "new_string": "evil"})])
def test_an_accept_that_does_not_match_the_requested_operation_is_denied(tmp_path, approved):
    process = FakeHostProcess([_init(), _can_use("r1", "Edit", EDIT_INPUT), "WAIT", RESULT])
    turn = _turn(process, tmp_path, lambda *_: True)
    worker = threading.Thread(target=lambda: turn.run("x", timeout_seconds=30))
    worker.start()
    assert _wait_until(lambda: "r1" in turn._pending_inputs)
    turn.respond_server_request("r1", {"decision": "accept", "operation_sha256": approved})
    worker.join(timeout=5)
    [response] = _control_responses(process)
    assert response["response"]["behavior"] == "deny" and "updatedInput" not in response["response"]


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_arbitrary_commands_are_denied_before_any_approval_exists(tmp_path, tool):
    process = FakeHostProcess([_init(), _can_use("r1", tool, BASH_INPUT), "WAIT", RESULT])
    asked = []
    _turn(process, tmp_path, lambda *args: asked.append(args) or True).run("x", timeout_seconds=30)
    assert asked == []
    [response] = _control_responses(process)
    assert response["response"]["behavior"] == "deny" and "verification profile" in response["response"]["message"]


def test_a_session_that_exposes_a_shell_tool_is_refused(tmp_path):
    process = FakeHostProcess([_init(tools=("Read", "Bash")), RESULT])
    with pytest.raises(ClaudeNativeError) as excinfo:
        _turn(process, tmp_path, lambda *_: True).run("x", timeout_seconds=30)
    assert excinfo.value.code == "UNSAFE_TOOLS"


@pytest.mark.parametrize("tool,tool_input", [
    ("Read", {"file_path": "..\\outside.txt"}),
    ("Read", {"file_path": "C:\\Windows\\win.ini"}),
    ("Read", {"file_path": "\\\\server\\share\\x.txt"}),
    ("Read", {"file_path": "notes.txt:hidden"}),
    ("Read", {"file_path": ".env"}),
    ("Read", {"file_path": "config/.aws/credentials"}),
    ("Read", {}),
    ("Grep", {"pattern": "x", "path": ".."}),
    ("Grep", {"pattern": "AWS_SECRET_ACCESS_KEY"}),
    ("Grep", {"pattern": "AWS_SECRET_ACCESS_KEY", "path": "."}),
    ("Grep", {"pattern": "AWS_SECRET_ACCESS_KEY", "path": "config"}),
    ("Glob", {"pattern": "**/*"}),
    ("Glob", {"pattern": "**/.env*"}),
    ("Glob", {"pattern": "**/*", "path": "."}),
    ("Glob", {"pattern": "../**/*.py"}),
    ("Glob", {"pattern": "C:\\Users\\**\\*"}),
    ("Edit", {"file_path": ".git\\config", "old_string": "a", "new_string": "b"}),
    ("Write", {"file_path": "sub/.GIT/hooks/pre-commit", "content": "x"}),
    ("Write", {"file_path": ".env.local", "content": "x"}),
    ("Write", {"file_path": "", "content": "x"}),
])
def test_access_outside_the_worktree_or_to_secrets_is_denied_before_a_human_is_asked(tmp_path, tool, tool_input):
    process = FakeHostProcess([_init(), _can_use("r1", tool, tool_input), "WAIT", RESULT])
    asked = []
    _turn(process, tmp_path, lambda *args: asked.append(args) or True).run("x", timeout_seconds=30)
    assert asked == []
    assert _control_responses(process)[0]["response"]["behavior"] == "deny"


def test_a_junction_or_symlink_escaping_the_worktree_is_denied(tmp_path):
    import os
    import subprocess

    outside = tmp_path / "outside"
    outside.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    link = worktree / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
    process = FakeHostProcess([_init(), _can_use("r1", "Write", {"file_path": "link/pwn.txt", "content": "x"}), "WAIT", RESULT])
    asked = []
    _turn(process, worktree, lambda *args: asked.append(args) or True).run("x", timeout_seconds=30)
    assert asked == [] and _control_responses(process)[0]["response"]["behavior"] == "deny"


def test_reads_of_sensitive_paths_never_stream_their_content(tmp_path):
    events = []
    process = FakeHostProcess([
        _init(),
        {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": ".env"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": SECRET_CONTENT}]}},
        RESULT,
    ])
    ClaudeTurn(str(tmp_path), session_id=SESSION, resume=False, development=True, on_event=events.append,
               launcher=lambda command, env, cwd: process).run("x", timeout_seconds=30)
    assert SECRET_CONTENT not in json.dumps(events)


def test_writes_outside_the_worktree_are_denied_before_reaching_a_human(tmp_path):
    outside = str(tmp_path.parent / "elsewhere.py")
    process = FakeHostProcess([_init(), _can_use("r1", "Edit", {"file_path": outside, "old_string": "a", "new_string": "b"}),
                               "WAIT", _can_use("r2", "Write", {"file_path": "..\\escape.txt", "content": "x"}), "WAIT", RESULT])
    asked = []
    _turn(process, tmp_path, lambda *args: asked.append(args) or True).run("x", timeout_seconds=30)
    assert asked == []
    responses = _control_responses(process)
    assert [r["response"]["behavior"] for r in responses] == ["deny", "deny"]
    assert "confined to the session worktree" in responses[0]["response"]["message"]


@pytest.mark.parametrize(("tool", "tool_input"), [
    ("Edit", {"file_path": "calc.py", "old_string": "a", "new_string": "b"}),
    ("Edit", {"file_path": "", "old_string": "a", "new_string": "b"}),
    ("Write", {"file_path": "calc.py", "content": "new file"}),
    ("Write", {"file_path": "", "content": "new file"}),
])
def test_supported_cli_edit_and_write_path_forms_go_to_the_human(tmp_path, tool, tool_input):
    tool_input = dict(tool_input)
    if not tool_input["file_path"]:
        tool_input["file_path"] = str(tmp_path / "calc.py")
    process = FakeHostProcess([_init(), _can_use("r1", tool, tool_input),
                               "WAIT", RESULT])
    asked = []
    turn = _turn(process, tmp_path, lambda *args: asked.append(args) or True)
    worker = threading.Thread(target=lambda: turn.run("x", timeout_seconds=30))
    worker.start()
    assert _wait_until(lambda: asked)
    assert asked == [("r1", tool, tool_input)]
    turn.respond_server_request("r1", {"decision": "decline"})
    worker.join(timeout=5)


def test_unknown_tools_and_requests_without_a_channel_are_denied(tmp_path):
    process = FakeHostProcess([_init(), _can_use("r1", "WebFetch", {"url": "http://x"}), "WAIT",
                               _can_use("r2", "Bash", BASH_INPUT), "WAIT",
                               {"type": "control_request", "request_id": "r3", "request": {"subtype": "hook_callback"}}, "WAIT",
                               RESULT])
    _turn(process, tmp_path, lambda *_: False).run("x", timeout_seconds=30)
    responses = _control_responses(process)
    assert responses[0]["response"]["behavior"] == "deny"
    assert responses[1]["response"]["behavior"] == "deny"
    assert responses[2]["subtype"] == "error"


def test_a_session_exposing_extra_tools_is_refused(tmp_path):
    process = FakeHostProcess([_init(tools=("Read", "WebFetch")), RESULT])
    with pytest.raises(ClaudeNativeError) as excinfo:
        _turn(process, tmp_path, lambda *_: True).run("x", timeout_seconds=30)
    assert excinfo.value.code == "UNSAFE_TOOLS"


def test_commands_and_file_edits_stream_as_cockpit_items(tmp_path):
    events = []
    process = FakeHostProcess([
        _init(),
        {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": BASH_INPUT},
            {"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": "calc.py"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "1 passed"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]}},
        RESULT,
    ])
    turn = ClaudeTurn(str(tmp_path), session_id=SESSION, resume=False, development=True, on_event=events.append,
                      launcher=lambda command, env, cwd: process)
    turn.run("x", timeout_seconds=30)
    completed = [e["params"]["item"] for e in events if e["method"] == "item/completed"]
    assert {"type": "commandExecution", "id": "t1", "tool": "Bash", "status": "completed",
            "command": "python -m pytest -q", "aggregatedOutput": "1 passed"} in completed
    assert any(item["type"] == "fileChange" and item["path"] == "calc.py" for item in completed)


def test_interrupt_while_a_decision_is_pending_stops_the_process(tmp_path):
    process = FakeHostProcess([_init(), _can_use("r1", "Edit", EDIT_INPUT), "HANG"])
    turn = _turn(process, tmp_path, lambda *_: True)
    errors = []

    def run():
        try:
            turn.run("x", timeout_seconds=30)
        except ClaudeNativeError as error:
            errors.append(error.code)

    worker = threading.Thread(target=run)
    worker.start()
    assert _wait_until(lambda: "r1" in turn._pending_inputs)
    turn.turn_interrupt()
    worker.join(timeout=5)
    assert errors == ["TURN_INTERRUPTED"] and process.killed.is_set()


class _DiscoveryProcess:
    def __init__(self, lines):
        self._lines = lines
        self.stdin = type("S", (), {"write": lambda self, t: None, "close": lambda self: None})()
        self.returncode = 0

    @property
    def stdout(self):
        return iter(json.dumps(line) + "\n" for line in self._lines)

    def poll(self):
        return 0


def test_model_discovery_reads_the_initialize_response_without_a_prompt():
    response = {"type": "control_response", "response": {"subtype": "success", "request_id": "nexus-init", "response": {
        "models": [{"value": "default", "displayName": "Default", "supportedEffortLevels": ["low", "high"]},
                   {"value": "claude-opus-4-8", "displayName": "Opus 4.8", "supportedEffortLevels": ["max", "bogus"]},
                   {"value": "BAD ID", "displayName": "x"}]}}}
    models = claude_native.discover_models(launcher=lambda *a, **k: _DiscoveryProcess([response]))
    assert models == [{"id": "default", "name": "Default", "efforts": ["low", "high"]},
                      {"id": "claude-opus-4-8", "name": "Opus 4.8", "efforts": ["max"]}]


# -- Development executor -----------------------------------------------------------------


CATALOG = {"backend": "claude_native", "catalog_error": None,
           "account": {"authenticated": True, "installed": True, "type": "claude.ai", "plan": "pro", "version": "2.1.288"},
           "models": [{"id": "default", "supported_efforts": ["low", "high"]}, {"id": "sonnet", "supported_efforts": ["low"]}]}


class ScriptedDevTurn:
    instances: list = []
    request: tuple | None = None

    def __init__(self, cwd, *, session_id, resume, model=None, effort=None, development=False,
                 on_event=None, on_session=None, on_permission=None, launcher=None):
        self.cwd, self.session_id, self.resume, self.model, self.effort = cwd, session_id, resume, model, effort
        self.development = development
        self._on_session, self._on_permission = on_session, on_permission
        self._interrupted = threading.Event()
        self.answers: list = []
        self.answered = threading.Event()
        ScriptedDevTurn.instances.append(self)

    @property
    def interrupted(self):
        return self._interrupted.is_set()

    def respond_server_request(self, request_id, result):
        self.answers.append((request_id, result))
        self.answered.set()

    def turn_interrupt(self, *_):
        self._interrupted.set()
        self.answered.set()

    def stop(self):
        pass

    def run(self, prompt, *, timeout_seconds):
        self.timeout_seconds = timeout_seconds
        self._on_session(self.session_id, "claude-x")
        if ScriptedDevTurn.request is not None:
            tool, tool_input = ScriptedDevTurn.request
            if not self._on_permission("req-1", tool, tool_input):
                return "denied before asking"
            self.answered.wait(timeout=10)
            if self._interrupted.is_set():
                raise ClaudeNativeError("TURN_INTERRUPTED", "interrupted")
            return "decision=" + str(self.answers[-1][1].get("decision"))
        return "fixed"


@pytest.fixture
def claude_dev(monkeypatch, _patch_client):  # noqa: F811
    ScriptedDevTurn.instances = []
    ScriptedDevTurn.request = None
    monkeypatch.setattr(claude_native, "ClaudeTurn", ScriptedDevTurn)
    monkeypatch.setattr("nexus.chat.service._claude_catalog", lambda: json.loads(json.dumps(CATALOG)))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Claude Native must never use Codex or OmniRoute")

    monkeypatch.setattr(executor, "AppServerClient", forbidden)
    monkeypatch.setattr("nexus.workspace.omniroute_oauth.require_connected_route", forbidden)
    monkeypatch.setattr("nexus.workspace.omniroute_oauth.connection_status", forbidden)
    monkeypatch.setattr("nexus.dev.models.list_dev_models", forbidden)
    monkeypatch.setattr("nexus.chat.service._native_model_catalog", forbidden)
    yield
    for pending in list(approvals._pending.values()):
        if pending.timer is not None:
            pending.timer.cancel()
    approvals._pending.clear()


def _claude_session(tmp_path):
    _register_project(tmp_path)
    return service.create_dev_session("demo", backend="claude_native")


def _status(turn_id):
    return dev_sessions.get_turn(turn_id)["status"]


def _pending(session_id):
    rows = [row for row in approvals.list_for_session(session_id) if row["status"] == "PENDING"]
    return rows[0] if rows else None


def test_claude_session_is_persisted_with_its_backend(isolated_registry, claude_dev):
    session = _claude_session(isolated_registry)
    assert (session["backend"], session["requested_model"]) == ("claude_native", "default")
    with pytest.raises(DevServiceError) as excinfo:
        service.create_dev_session("demo", requested_model="gpt-5", backend="claude_native")
    assert excinfo.value.code == "MODEL_NOT_ALLOWED"


def test_claude_turn_runs_in_the_worktree_and_resumes_its_conversation(isolated_registry, claude_dev):
    session = _claude_session(isolated_registry)
    first = executor.run_dev_turn(session["session_id"], "fix the bug")
    assert _wait_until(lambda: _status(first["turn_id"]) == "COMPLETED")
    runner = ScriptedDevTurn.instances[0]
    stored = dev_sessions.get_session(session["session_id"])
    assert runner.development is True and runner.resume is False
    assert runner.cwd == stored["worktree_path"] and runner.session_id == stored["claude_session_id"]
    assert runner.timeout_seconds == executor.NATIVE_DEV_TURN_TIMEOUT_SECONDS

    executor._active_dev_turns.clear()  # simulated restart: only durable state remains
    second = executor.run_dev_turn(session["session_id"], "run the tests", effort="high")
    assert _wait_until(lambda: _status(second["turn_id"]) == "COMPLETED")
    assert ScriptedDevTurn.instances[1].resume is True
    assert ScriptedDevTurn.instances[1].session_id == stored["claude_session_id"]
    assert ScriptedDevTurn.instances[1].effort == "high"


def _start_pending(tmp_path, request=("Edit", EDIT_INPUT)):
    session = _claude_session(tmp_path)
    ScriptedDevTurn.request = request
    turn = executor.run_dev_turn(session["session_id"], "fix and test")
    assert _wait_until(lambda: _pending(session["session_id"]) is not None)
    return session["session_id"], turn["turn_id"], _pending(session["session_id"])


def test_a_claude_edit_is_decided_on_the_exact_operation_and_runs_only_after_accept(isolated_registry, claude_dev):
    session_id, turn_id, pending = _start_pending(isolated_registry)
    live = executor._active_dev_turns[turn_id]
    with live.lock:
        methods = [event.get("method") for _seq, event in live.buffer]
    assert "item/fileChange/requestApproval" in methods, "the cockpit is told live that a decision is needed"
    summary = pending["summary"]
    assert pending["kind"] == "file_change" and summary["tool"] == "Edit" and summary["grantRoot"] == "calc.py"
    assert summary["operation_sha256"] == EDIT_DIGEST and summary["operation_bytes"] > 0
    assert "input" not in summary
    detail = approvals.operation_detail(session_id, pending["approval_id"])
    assert json.loads(detail["operation"]) == EDIT_INPUT and detail["operation_sha256"] == EDIT_DIGEST
    runner = ScriptedDevTurn.instances[-1]
    assert runner.answers == []
    with pytest.raises(DevServiceError) as missing:
        approvals.decide(session_id, pending["approval_id"], "accept")
    assert missing.value.code == "OPERATION_MISMATCH"
    with pytest.raises(DevServiceError):
        approvals.decide(session_id, pending["approval_id"], "accept", operation_sha256="f" * 64)
    assert runner.answers == [] and _pending(session_id) is not None, "a mismatch leaves the request pending"
    approvals.decide(session_id, pending["approval_id"], "accept", operation_sha256=EDIT_DIGEST)
    assert runner.answers == [("req-1", {"decision": "accept", "operation_sha256": EDIT_DIGEST})]
    assert _wait_until(lambda: _status(turn_id) == "COMPLETED")
    assert dev_sessions.get_turn(turn_id)["response_text"] == "decision=accept"
    assert approvals.list_for_session(session_id)[0]["status"] == "APPROVED"
    with pytest.raises(DevServiceError) as gone:
        approvals.operation_detail(session_id, pending["approval_id"])
    assert gone.value.code == "OPERATION_UNAVAILABLE"


def test_claude_tool_input_is_never_persisted_or_listed(isolated_registry, claude_dev):
    content = {"file_path": "notes.txt", "content": SECRET_CONTENT}
    session_id, _turn_id, pending = _start_pending(isolated_registry, ("Write", content))
    from nexus.registry.database import get_connection

    with get_connection() as connection:
        stored = [dict(row) for row in connection.execute("SELECT * FROM dev_approvals")]
        dump = json.dumps(stored) + json.dumps(approvals.list_for_session(session_id))
    assert SECRET_CONTENT not in dump and "AWS_SECRET" not in dump
    assert pending["summary"]["operation_sha256"] == claude_native.operation_digest("Write", content)
    assert "sha256" in dump
    approvals.decide(session_id, pending["approval_id"], "decline")
    with get_connection() as connection:
        assert SECRET_CONTENT not in json.dumps([dict(row) for row in connection.execute("SELECT * FROM dev_approvals")])


def test_legacy_rows_with_a_stored_input_are_scrubbed(isolated_registry, claude_dev):
    from nexus.registry.database import get_connection, initialize_database

    session = _claude_session(isolated_registry)
    with get_connection() as connection:
        connection.execute(
            "INSERT INTO dev_approvals (id, dev_session_id, turn_id, kind, summary_json, status, created_at) "
            "VALUES ('APPROVAL-OLD1', ?, 'T', 'command', ?, 'APPROVED', 'x')",
            (session["session_id"], json.dumps({"kind": "command", "tool": "Bash", "input": SECRET_CONTENT})))
    assert "input" not in approvals.list_for_session(session["session_id"])[0]["summary"]
    initialize_database()
    with get_connection() as connection:
        assert SECRET_CONTENT not in connection.execute("SELECT summary_json FROM dev_approvals").fetchone()[0]


def test_a_claude_command_never_reaches_a_human(isolated_registry, claude_dev):
    session = _claude_session(isolated_registry)
    ScriptedDevTurn.request = ("Bash", BASH_INPUT)
    turn = executor.run_dev_turn(session["session_id"], "run the tests")
    assert _wait_until(lambda: _status(turn["turn_id"]) == "COMPLETED")
    assert dev_sessions.get_turn(turn["turn_id"])["response_text"] == "denied before asking"
    assert approvals.list_for_session(session["session_id"]) == []


def test_a_decline_needs_no_operation_digest(isolated_registry, claude_dev):
    session_id, _turn_id, pending = _start_pending(isolated_registry)
    approvals.decide(session_id, pending["approval_id"], "decline")
    assert ScriptedDevTurn.instances[-1].answers == [("req-1", {"decision": "decline"})]


def test_a_claude_file_edit_is_a_file_change_approval(isolated_registry, claude_dev):
    session_id, _turn_id, pending = _start_pending(isolated_registry)
    assert pending["kind"] == "file_change" and pending["summary"]["grantRoot"] == "calc.py"
    approvals.decide(session_id, pending["approval_id"], "decline")
    assert ScriptedDevTurn.instances[-1].answers == [("req-1", {"decision": "decline"})]


def test_an_operation_too_large_to_show_is_denied_without_an_approval(isolated_registry, claude_dev):
    session = _claude_session(isolated_registry)
    ScriptedDevTurn.request = ("Write", {"file_path": "big.txt", "content": "x" * (executor.CLAUDE_APPROVAL_INPUT_LIMIT + 10)})
    turn = executor.run_dev_turn(session["session_id"], "write it")
    assert _wait_until(lambda: _status(turn["turn_id"]) == "COMPLETED")
    assert dev_sessions.get_turn(turn["turn_id"])["response_text"] == "denied before asking"
    assert approvals.list_for_session(session["session_id"]) == []


def test_interrupt_during_a_pending_claude_approval_cancels_both(isolated_registry, claude_dev):
    session_id, turn_id, pending = _start_pending(isolated_registry)
    assert executor.interrupt_dev_turn(session_id, turn_id)["status"] == "INTERRUPT_REQUESTED"
    assert _wait_until(lambda: _status(turn_id) == "CANCELLED")
    assert approvals.list_for_session(session_id)[0]["status"] == "CANCELLED"
    assert ScriptedDevTurn.instances[-1].answers[0] == ("req-1", {"decision": "cancel"})


def test_an_expired_claude_approval_is_declined(isolated_registry, claude_dev):
    session_id, turn_id, pending = _start_pending(isolated_registry)
    approvals._expire(pending["approval_id"])
    assert ScriptedDevTurn.instances[-1].answers == [("req-1", {"decision": "decline"})]
    assert _wait_until(lambda: _status(turn_id) == "COMPLETED")
    assert approvals.list_for_session(session_id)[0]["status"] == "EXPIRED"
    with pytest.raises(DevServiceError) as late:
        approvals.decide(session_id, pending["approval_id"], "accept", operation_sha256=EDIT_DIGEST)
    assert late.value.code == "APPROVAL_EXPIRED"


def test_a_restart_leaves_no_approvable_operation(isolated_registry, claude_dev):
    session_id, _turn_id, pending = _start_pending(isolated_registry)
    approvals._pending.clear()  # Nexus restarted: the in-memory operation is gone
    assert approvals.list_for_session(session_id)[0]["status"] == "EXPIRED"
    with pytest.raises(DevServiceError):
        approvals.operation_detail(session_id, pending["approval_id"])
    with pytest.raises(DevServiceError):
        approvals.decide(session_id, pending["approval_id"], "accept", operation_sha256=EDIT_DIGEST)


def test_claude_mission_validates_against_the_claude_catalog(isolated_registry, claude_dev, monkeypatch):
    session = _claude_session(isolated_registry)
    monkeypatch.setattr(mission_runner, "resolve_test_profile", lambda *_args: None)
    mission = mission_runner.start_mission(session["session_id"], "fix it", model="sonnet",
                                           allowed_models=["sonnet"], test_profile="p")
    assert mission["requested_model"] == "sonnet"
    with pytest.raises(DevServiceError) as excinfo:
        mission_runner.start_mission(session["session_id"], "fix", model="gpt-5", allowed_models=["gpt-5"], test_profile="p")
    assert excinfo.value.code == "MODEL_NOT_ALLOWED"


def test_codex_native_development_never_runs_through_claude(isolated_registry, monkeypatch, _patch_client):  # noqa: F811
    _register_project(isolated_registry)
    monkeypatch.setattr(claude_native, "ClaudeTurn", lambda *a, **k: (_ for _ in ()).throw(AssertionError("claude used")))
    monkeypatch.setattr("nexus.chat.service._native_model_catalog",
                        lambda *, refresh=False: ([{"id": "gpt-x", "supported_efforts": []}], {"authenticated": True}))
    session = service.create_dev_session("demo", requested_model="gpt-x", backend="codex_native")
    turn = executor.run_dev_turn(session["session_id"], "task")
    assert _wait_until(lambda: _status(turn["turn_id"]) == "COMPLETED")


# -- routes ------------------------------------------------------------------------------


class ClaudeDevRoutesTest(unittest.TestCase):
    def _client(self):
        from starlette.testclient import TestClient
        from nexus.web.app import create_app

        return TestClient(create_app(), base_url="http://127.0.0.1:8000")

    def test_claude_backend_is_forwarded_for_development_sessions(self):
        with patch("nexus.dev.service.create_dev_session", return_value={"session_id": "DEV-1"}) as mock_create:
            response = self._client().post("/api/dev/sessions", json={"project_id": "demo", "backend": "claude_native", "requested_model": "sonnet"})
        self.assertEqual(response.status_code, 200)
        mock_create.assert_called_once_with("demo", requested_model="sonnet", backend="claude_native")

    def test_operation_route_is_uncached_and_the_digest_is_forwarded_to_decide(self):
        client = self._client()
        detail = {"approval_id": "A", "tool": "Edit", "operation_sha256": "a" * 64, "operation": "{}"}
        with patch("nexus.dev.approvals.operation_detail", return_value=detail):
            response = client.get("/api/dev/sessions/DEV-1/approvals/A/operation")
        self.assertEqual(response.json(), detail)
        self.assertEqual(response.headers["cache-control"], "no-store")
        with patch("nexus.dev.approvals.decide", return_value={"status": "APPROVED"}) as mock_decide:
            client.post("/api/dev/sessions/DEV-1/approvals/A", json={"decision": "accept", "operation_sha256": "a" * 64})
        mock_decide.assert_called_once_with("DEV-1", "A", "accept", operation_sha256="a" * 64)
        self.assertEqual(client.post("/api/dev/sessions/DEV-1/approvals/A",
                                     json={"decision": "accept", "operation_sha256": "short"}).status_code, 422)

    def test_claude_development_catalog_route_discovers_the_real_signed_in_account_catalog(self):
        # Development reuses Chat's own catalog (nexus.chat.service.list_models
        # -> _claude_catalog), which discovers the real account catalog from
        # the CLI's own initialize response -- never a second, parallel catalog.
        status = {"installed": True, "logged_in": True, "auth_method": "oauth", "subscription": "max",
                 "version": "1.0.0", "error": None}
        models = [{"id": "claude-opus-4-8", "name": "Opus", "efforts": ["low", "high"]}]
        with patch("nexus.chat.claude_native.cli_status", return_value=status), \
             patch("nexus.chat.claude_native.discover_models", return_value=models) as mock_discover:
            response = self._client().get("/api/dev/models?backend=claude_native")
        mock_discover.assert_called_once_with()
        body = response.json()
        self.assertEqual(body["backend"], "claude_native")
        self.assertEqual([row["id"] for row in body["models"]], ["claude-opus-4-8"])
        self.assertEqual(body["models"][0]["supported_efforts"], ["low", "high"])
        self.assertIsNone(body["catalog_error"])

    def test_claude_development_catalog_route_falls_back_to_aliases_when_discovery_fails(self):
        status = {"installed": True, "logged_in": True, "auth_method": "oauth", "subscription": "max",
                 "version": "1.0.0", "error": None}
        with patch("nexus.chat.claude_native.cli_status", return_value=status), \
             patch("nexus.chat.claude_native.discover_models",
                   side_effect=ClaudeNativeError("MODEL_DISCOVERY_FAILED", "Claude Code did not answer.")):
            response = self._client().get("/api/dev/models?backend=claude_native")
        body = response.json()
        self.assertEqual([row["id"] for row in body["models"]], list(claude_native.MODEL_ALIASES))
        self.assertIsNone(body["catalog_error"])
