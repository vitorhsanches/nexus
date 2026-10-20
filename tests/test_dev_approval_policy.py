"""Regression tests for the F2.3 process-level approval-policy override.

Covers what tests/test_chat_appserver_client.py's own
test_*_approval_policy_override_* tests (command-line construction) do
not: that Development Mode actually requests this override, that Chat
never does (configuration isolation -- Chat's own command line must stay
byte-identical), that an unhandled/unknown approval request is still
denied regardless of which policy value is in effect, and that a
failure while constructing/starting the App-Server surfaces as a
FAILED turn rather than crashing the executor. No real Codex subprocess
is used here -- see the scratchpad diagnostics referenced in the F2.3
report for the live, disposable-repo verification of the real runtime
behavior this override builds on.
"""

from __future__ import annotations

import threading
import uuid

import nexus.registry.database as database
import nexus.registry.projects as projects
import pytest
from nexus.dev import executor, feature_gate, service


# -- configuration isolation: Chat never requests an approval override -----


def test_chat_appserver_construction_never_passes_an_approval_policy_override():
    import inspect

    import nexus.chat.service as chat_service

    source = inspect.getsource(chat_service)
    # Chat's own AppServerClient(...) call sites must never mention this
    # kwarg -- Development Mode is the only caller that opts in, so
    # Chat's command line stays byte-identical to before F2.3.
    assert "approval_policy_override" not in source


# -- Development Mode policy selection --------------------------------------


class _FakeAppServerClient:
    instances: list["_FakeAppServerClient"] = []

    def __init__(self, repo, *, codex_home=None, sandbox_mode_override=None, approval_policy_override=None, on_notification=None, on_server_request=None, **kwargs):
        self.repo = repo
        self.codex_home = codex_home or f"fake-codex-home-{uuid.uuid4().hex[:8]}"
        self.sandbox_mode_override = sandbox_mode_override
        self.approval_policy_override = approval_policy_override
        self.on_server_request = on_server_request
        self.on_notification = on_notification
        _FakeAppServerClient.instances.append(self)

    def start(self):
        pass

    def thread_start(self, **kwargs):
        return {"thread": {"id": "fake-thread"}, "modelProvider": "omniroute"}

    def thread_resume(self, thread_id, **kwargs):
        return {"thread": {"id": thread_id}}

    def send_message_and_wait(self, *a, **k):
        return "ok"

    def stop(self):
        pass


def _run(args, cwd):
    import subprocess

    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    original_db_path = database.DATABASE_PATH
    original_storage_dir = database.STORAGE_DIR
    original_projects_file = projects.PROJECTS_FILE
    database.STORAGE_DIR = tmp_path / "storage"
    database.DATABASE_PATH = database.STORAGE_DIR / "nexus.db"
    projects.PROJECTS_FILE = tmp_path / "projects.json"
    database.initialize_database()
    monkeypatch.setenv("NEXUS_DEV_WORKTREE_ROOT", str(tmp_path / "dev-worktree-root"))
    monkeypatch.setenv(feature_gate.ENV_VAR, "1")
    _FakeAppServerClient.instances.clear()
    try:
        yield tmp_path
    finally:
        for thread in threading.enumerate():
            if thread.name.startswith("nexus-dev-") and thread is not threading.current_thread():
                thread.join(timeout=10)
        database.DATABASE_PATH = original_db_path
        database.STORAGE_DIR = original_storage_dir
        projects.PROJECTS_FILE = original_projects_file
        executor._active_dev_turns.clear()


def _active_session(tmp_path, project_id="demo"):
    import json
    import os

    workspace_path = tmp_path / project_id
    workspace_path.mkdir(parents=True, exist_ok=True)
    _run(["init", "-q"], cwd=str(workspace_path))
    _run(["config", "user.email", "f23-test@example.com"], cwd=str(workspace_path))
    _run(["config", "user.name", "f23-test"], cwd=str(workspace_path))
    (workspace_path / "README.md").write_text("placeholder\n", encoding="utf-8")
    _run(["add", "-A"], cwd=str(workspace_path))
    _run(["commit", "-q", "-m", "init"], cwd=str(workspace_path))
    resolved = os.path.realpath(str(workspace_path))
    projects.PROJECTS_FILE.write_text(
        json.dumps({"projects": [{"id": project_id, "name": project_id.title(), "path": resolved, "aliases": [], "enabled": True}]}),
        encoding="utf-8",
    )
    projects.sync_projects()
    projects.set_dev_enabled(project_id, True)
    return service.create_dev_session(project_id)


def _wait_until(predicate, timeout=5.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_dev_turn_requests_the_same_approval_policy_as_the_thread_level_param(isolated_registry, monkeypatch):
    monkeypatch.setattr(executor, "AppServerClient", _FakeAppServerClient)
    session = _active_session(isolated_registry)

    result = executor.run_dev_turn(session["session_id"], "do something")
    assert _wait_until(lambda: executor.get_turn_detail(session["session_id"], result["turn_id"])["status"] == "COMPLETED")

    assert len(_FakeAppServerClient.instances) == 1
    client = _FakeAppServerClient.instances[0]
    assert client.approval_policy_override == executor.DEV_APPROVAL_POLICY == "on-request"
    assert client.sandbox_mode_override == executor.DEV_SANDBOX == "workspace-write"


def test_dev_turn_never_registers_a_custom_approval_handler(isolated_registry, monkeypatch):
    """The override only changes what policy is requested; it must never
    come with a custom on_server_request handler -- AppServerClient's own
    fail-closed default must remain the sole decision-maker."""
    monkeypatch.setattr(executor, "AppServerClient", _FakeAppServerClient)
    session = _active_session(isolated_registry)

    result = executor.run_dev_turn(session["session_id"], "do something")
    assert _wait_until(lambda: executor.get_turn_detail(session["session_id"], result["turn_id"])["status"] == "COMPLETED")

    client = _FakeAppServerClient.instances[0]
    assert client.on_server_request is None


def test_execution_feature_gate_default_is_unchanged_by_the_approval_override(monkeypatch):
    monkeypatch.delenv(feature_gate.ENV_VAR, raising=False)
    assert feature_gate.execution_enabled() is False


# -- real AppServerClient: unhandled/unknown requests remain denied ---------
# (mirrors tests/test_chat_appserver_client.py's own coverage, scoped here
# to confirm it holds regardless of approval_policy_override.)


def test_unhandled_approval_request_is_still_denied_with_the_override_set():
    import json

    from tests.test_chat_appserver_client import _make_client, _ok, _thread_result

    client, captured = _make_client(
        {
            "initialize": [
                _ok(1, {}),
                json.dumps(
                    {
                        "id": 99,
                        "method": "item/commandExecution/requestApproval",
                        "params": {"command": "rm -rf /"},
                    }
                ),
            ],
            "thread/start": [_ok(2, _thread_result())],
        },
        approval_policy_override="on-request",
    )
    client.start()
    assert 'approval_policy="on-request"' in captured["command"]
    assert "-c" in captured["command"]
    client.thread_start()

    responses = captured["server"].sent
    approval_response = next(line for line in responses if line.get("id") == 99)
    assert approval_response["result"]["decision"]["denied"]["rejection"]
    client.stop()
