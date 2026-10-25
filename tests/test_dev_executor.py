"""Service-layer tests for nexus.dev.executor (F2.1).

Uses a fake AppServerClient (never a real subprocess, never real
inference) so these are deterministic and fast -- mirrors
tests/test_chat_service.py's own FakeAppServerClient pattern closely.
Real, disposable Git repositories are still used for session/worktree
creation (nexus.dev.service.create_dev_session, F1.1/F1.2), since the
point of several of these tests is to prove the executor points a
write-capable client at the correct, isolated directory -- only the
Codex subprocess itself is faked.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from pathlib import Path

import nexus.registry.database as database
import nexus.registry.projects as projects
import pytest
from nexus.chat.appserver_client import AppServerError
from nexus.dev import executor, feature_gate, integration, integration_gate, service
from nexus.registry import dev_sessions
from nexus.registry import dev_integrations


# -- fake App-Server client --------------------------------------------


class FakeAppServerClient:
    """Stands in for nexus.chat.appserver_client.AppServerClient."""

    start_error: Exception | None = None
    send_error: Exception | None = None
    block_until_interrupted = False
    response_text = "fake dev reply"
    instances: list["FakeAppServerClient"] = []

    def __init__(self, repo, *, codex_home=None, sandbox_mode_override=None, approval_policy_override=None, on_notification=None, on_server_request=None, **kwargs):
        self.repo = repo
        self.codex_home = codex_home or f"fake-dev-codex-home-{uuid.uuid4().hex[:8]}"
        self.sandbox_mode_override = sandbox_mode_override
        self.approval_policy_override = approval_policy_override
        self.on_server_request = on_server_request
        self.started = False
        self.stopped = False
        self.thread_start_kwargs: dict | None = None
        self.resumed_thread_id = None
        self.reasoning_effort = None
        self.interrupt_calls: list[tuple[str, str]] = []
        self._interrupt_event = threading.Event()
        FakeAppServerClient.instances.append(self)

    def start(self):
        if FakeAppServerClient.start_error is not None:
            raise FakeAppServerClient.start_error
        self.started = True

    def thread_start(self, **kwargs):
        self.thread_start_kwargs = kwargs
        return {
            "thread": {"id": "FAKE-DEV-THREAD-1", "modelProvider": "omniroute"},
            "model": kwargs.get("model"),
            "modelProvider": "omniroute",
        }

    def thread_resume(self, thread_id, **kwargs):
        self.resumed_thread_id = thread_id
        return {"thread": {"id": thread_id, "modelProvider": "omniroute"},
                "modelProvider": "omniroute", "model": kwargs.get("model"),
                "reasoningEffort": self.reasoning_effort}

    def thread_settings_update(self, thread_id, *, model=None, effort=None):
        self.reasoning_effort = effort
        return {}

    def send_message_and_wait(self, thread_id, text, *, timeout_seconds=None, on_turn_started=None):
        if on_turn_started:
            on_turn_started("FAKE-DEV-CODEX-TURN-1")
        if FakeAppServerClient.send_error is not None:
            raise FakeAppServerClient.send_error
        if FakeAppServerClient.block_until_interrupted:
            self._interrupt_event.wait(timeout=10)
            raise AppServerError("TURN_FAILED", "Turn ended with status 'interrupted'.")
        return FakeAppServerClient.response_text

    def turn_interrupt(self, thread_id, turn_id):
        self.interrupt_calls.append((thread_id, turn_id))
        self._interrupt_event.set()

    def stop(self):
        self.stopped = True


@pytest.fixture(autouse=True)
def _patch_client(monkeypatch):
    FakeAppServerClient.start_error = None
    FakeAppServerClient.send_error = None
    FakeAppServerClient.block_until_interrupted = False
    FakeAppServerClient.response_text = "fake dev reply"
    FakeAppServerClient.instances = []
    monkeypatch.setattr(executor, "AppServerClient", FakeAppServerClient)
    yield


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
    # This file's tests are about the executor's own behavior, not the
    # feature gate itself (see test_dev_feature_gate.py for that) -- opt
    # in explicitly here, the same way an operator would have to.
    monkeypatch.setenv(feature_gate.ENV_VAR, "1")
    try:
        yield tmp_path
    finally:
        # Join every nexus-dev-* background thread before swapping the DB
        # path back / moving to the next test -- a turn's own cleanup
        # (client.stop(), popping _active_dev_turns) runs asynchronously
        # on that thread, after this test's own assertions already
        # observed the turn's terminal DB status, so without this a
        # still-finishing thread from one test can occupy a
        # MAX_CONCURRENT_DEV_TURNS slot when the next test starts,
        # producing a flaky TOO_MANY_CONCURRENT_DEV_TURNS failure that
        # has nothing to do with what that next test is actually checking.
        for thread in threading.enumerate():
            if thread.name.startswith("nexus-dev-") and thread is not threading.current_thread():
                thread.join(timeout=10)
        database.DATABASE_PATH = original_db_path
        database.STORAGE_DIR = original_storage_dir
        projects.PROJECTS_FILE = original_projects_file
        executor._active_dev_turns.clear()


def _run(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run(["init", "-q"], cwd=str(path))
    _run(["config", "user.email", "dev-executor-test@example.com"], cwd=str(path))
    _run(["config", "user.name", "dev-executor-test"], cwd=str(path))
    (path / "README.md").write_text("placeholder\n", encoding="utf-8")
    _run(["add", "-A"], cwd=str(path))
    _run(["commit", "-q", "-m", "init"], cwd=str(path))


def _register_project(tmp_path, project_id="demo", enabled=True, dev_enabled=True):
    import os

    workspace_path = tmp_path / project_id
    _init_repo(workspace_path)
    resolved = os.path.realpath(str(workspace_path))

    # sync_projects() disables (enabled=0) any project id NOT present in
    # the file it reads -- accumulate every project registered so far in
    # this test, rather than overwriting with just this one, so
    # registering a second project never silently disables the first.
    existing = []
    if projects.PROJECTS_FILE.exists():
        existing = json.loads(projects.PROJECTS_FILE.read_text(encoding="utf-8")).get("projects", [])
    existing = [entry for entry in existing if entry["id"] != project_id]
    existing.append({"id": project_id, "name": project_id.title(), "path": resolved, "aliases": [], "enabled": enabled})
    projects.PROJECTS_FILE.write_text(json.dumps({"projects": existing}), encoding="utf-8")
    projects.sync_projects()
    if dev_enabled:
        projects.set_dev_enabled(project_id, True)
    return resolved


def _active_session(tmp_path, project_id="demo"):
    _register_project(tmp_path, project_id)
    return service.create_dev_session(project_id)


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# -- 1: valid dev session -----------------------------------------------


def test_run_dev_turn_succeeds_for_a_valid_active_session(isolated_registry):
    session = _active_session(isolated_registry)

    result = executor.run_dev_turn(session["session_id"], "do something")
    # The background thread's own first action is to flip PENDING ->
    # RUNNING; run_dev_turn() reads the row back right after starting
    # that thread, so either value is a legitimate outcome of this
    # inherent, harmless race -- the real contract is just "not yet
    # terminal", not one specific in-flight status.
    assert result["status"] in ("PENDING", "RUNNING")
    assert _wait_until(lambda: executor.get_turn_detail(session["session_id"], result["turn_id"])["status"] == "COMPLETED")

    turn = executor.get_turn_detail(session["session_id"], result["turn_id"])
    assert turn["response_text"] == "fake dev reply"
    assert turn["started_at"] is not None
    assert turn["finished_at"] is not None


# -- 2 & 3: project authorization, including revocation ---------------------


def test_run_dev_turn_is_blocked_when_the_project_itself_is_disabled(isolated_registry):
    session = _active_session(isolated_registry)
    with database.get_connection() as connection:
        connection.execute("UPDATE projects SET enabled = 0 WHERE id = 'demo'")

    with pytest.raises(service.DevServiceError) as error:
        executor.run_dev_turn(session["session_id"], "do something")
    assert error.value.code == "PROJECT_DISABLED"


def test_revoking_dev_mode_blocks_the_next_turn_immediately(isolated_registry):
    session = _active_session(isolated_registry)

    projects.set_dev_enabled("demo", False)

    with pytest.raises(service.DevServiceError) as error:
        executor.run_dev_turn(session["session_id"], "do something")
    assert error.value.code == "DEV_MODE_DISABLED"
    assert dev_sessions.list_turns(session["session_id"]) == [], "no turn row should be created for a rejected request"


# -- 4: missing/invalid worktree ------------------------------------------


def test_run_dev_turn_is_blocked_when_the_worktree_directory_is_gone(isolated_registry):
    import shutil

    session = _active_session(isolated_registry)
    shutil.rmtree(session["worktree_path"])

    with pytest.raises(service.DevServiceError) as error:
        executor.run_dev_turn(session["session_id"], "do something")
    assert error.value.code == "WORKTREE_INVALID"


def test_run_dev_turn_is_blocked_for_a_session_with_no_worktree_recorded(isolated_registry):
    _register_project(isolated_registry)
    session_id = dev_sessions.new_session_id()
    dev_sessions.reserve_session(session_id, "demo", f"nexus/dev/{session_id}")
    # Never transitioned to ACTIVE -- still CREATING, with no worktree_path.
    with pytest.raises(service.DevServiceError) as error:
        executor.run_dev_turn(session_id, "do something")
    assert error.value.code == "SESSION_NOT_ACTIVE"


# -- 5 & 6: correct working directory and explicit sandbox config -----------


def test_turn_runs_with_cwd_set_to_the_worktree_and_explicit_workspace_write_sandbox(isolated_registry, monkeypatch):
    session = _active_session(isolated_registry)
    monkeypatch.setattr("nexus.dev.models.list_dev_models", lambda refresh=False: {"models": [
        {"id": "cc/claude-sonnet-5-low", "supported_efforts": ["low"], "confirmed_efforts": ["low"]}]})

    result = executor.run_dev_turn(session["session_id"], "do something", model="cc/claude-sonnet-5-low", effort="low")
    assert _wait_until(lambda: executor.get_turn_detail(session["session_id"], result["turn_id"])["status"] == "COMPLETED")

    assert len(FakeAppServerClient.instances) == 1
    client = FakeAppServerClient.instances[0]
    assert client.repo == session["worktree_path"]
    assert client.sandbox_mode_override == "workspace-write"
    assert client.approval_policy_override == "on-request"
    assert client.thread_start_kwargs["sandbox"] == "workspace-write"
    assert client.thread_start_kwargs["approval_policy"] == "on-request"
    assert client.thread_start_kwargs["cwd"] == session["worktree_path"]
    assert client.thread_start_kwargs["ephemeral"] is False
    assert client.thread_start_kwargs["model"] == "cc/claude-sonnet-5-low"
    assert client.reasoning_effort == "low"


def test_cc_route_requires_confirmed_effort_before_a_turn_exists(isolated_registry, monkeypatch):
    session = _active_session(isolated_registry)
    monkeypatch.setattr("nexus.dev.models.list_dev_models", lambda refresh=False: {"models": [
        {"id": "cc/claude-sonnet-5-low", "supported_efforts": ["low"], "confirmed_efforts": []}]})
    with pytest.raises(service.DevServiceError) as missing:
        executor.run_dev_turn(session["session_id"], "do something", model="cc/claude-sonnet-5-low")
    assert missing.value.code == "AI_EFFORT_REQUIRED"
    with pytest.raises(service.DevServiceError) as unconfirmed:
        executor.run_dev_turn(session["session_id"], "do something", model="cc/claude-sonnet-5-low", effort="low")
    assert unconfirmed.value.code == "EFFORT_NOT_SUPPORTED"
    assert dev_sessions.list_turns(session["session_id"]) == []


def test_appserver_effort_mismatch_never_starts_inference(isolated_registry, monkeypatch):
    session = _active_session(isolated_registry)
    model = "cc/claude-sonnet-5-low"
    monkeypatch.setattr("nexus.dev.models.list_dev_models", lambda refresh=False: {"models": [
        {"id": model, "supported_efforts": ["low"], "confirmed_efforts": ["low"]}]})
    sent = []
    monkeypatch.setattr(FakeAppServerClient, "thread_resume", lambda self, thread_id, **kwargs: {
        "thread": {"id": thread_id}, "model": model, "reasoningEffort": "high"})
    monkeypatch.setattr(FakeAppServerClient, "send_message_and_wait",
                        lambda self, *args, **kwargs: sent.append(True) or "wrong")
    turn = executor.run_dev_turn(session["session_id"], "do something", model=model, effort="low")
    assert _wait_until(lambda: executor.get_turn_detail(session["session_id"], turn["turn_id"])["status"] == "FAILED")
    detail = executor.get_turn_detail(session["session_id"], turn["turn_id"])
    assert detail["error_code"] == "EFFORT_ACTIVATION_FAILED"
    assert sent == []


# -- 7: isolated CODEX_HOME, reused across turns in the same session --------


def test_codex_home_is_isolated_and_reused_across_turns_in_the_same_session(isolated_registry):
    session = _active_session(isolated_registry)

    first = executor.run_dev_turn(session["session_id"], "first")
    assert _wait_until(lambda: executor.get_turn_detail(session["session_id"], first["turn_id"])["status"] == "COMPLETED")
    first_client = FakeAppServerClient.instances[0]
    first_codex_home = first_client.codex_home
    assert dev_sessions.get_session(session["session_id"])["codex_home"] == first_codex_home

    second = executor.run_dev_turn(session["session_id"], "second")
    assert _wait_until(lambda: executor.get_turn_detail(session["session_id"], second["turn_id"])["status"] == "COMPLETED")
    second_client = FakeAppServerClient.instances[1]
    # The second turn resumes the first turn's own thread/CODEX_HOME --
    # never a fresh one -- because it was passed explicitly as codex_home=.
    assert second_client.codex_home == first_codex_home
    assert second_client.resumed_thread_id == "FAKE-DEV-THREAD-1"


# -- 8 & 9: normal completion, App-Server failure ----------------------------


def test_app_server_failure_marks_the_turn_failed_without_corrupting_the_session(isolated_registry):
    session = _active_session(isolated_registry)
    FakeAppServerClient.send_error = AppServerError("TURN_FAILED", "boom")

    result = executor.run_dev_turn(session["session_id"], "will fail")
    assert _wait_until(lambda: executor.get_turn_detail(session["session_id"], result["turn_id"])["status"] == "FAILED")

    turn = executor.get_turn_detail(session["session_id"], result["turn_id"])
    assert turn["error_code"] == "TURN_FAILED"
    assert turn["error_message"]
    assert dev_sessions.get_session(session["session_id"])["status"] == dev_sessions.STATUS_ACTIVE


# -- 10: interruption --------------------------------------------------------


def test_interrupt_dev_turn_stops_a_running_turn(isolated_registry):
    session = _active_session(isolated_registry)
    FakeAppServerClient.block_until_interrupted = True

    result = executor.run_dev_turn(session["session_id"], "block me")
    assert _wait_until(lambda: len(FakeAppServerClient.instances) == 1 and FakeAppServerClient.instances[0].started)
    assert _wait_until(lambda: result["turn_id"] in executor._active_dev_turns and executor._active_dev_turns[result["turn_id"]].codex_turn_id is not None)

    response = executor.interrupt_dev_turn(session["session_id"])
    assert response["status"] == "INTERRUPT_REQUESTED"
    assert _wait_until(lambda: executor.get_turn_detail(session["session_id"], result["turn_id"])["status"] == "CANCELLED")

    assert FakeAppServerClient.instances[0].interrupt_calls == [("FAKE-DEV-THREAD-1", "FAKE-DEV-CODEX-TURN-1")]


def test_interrupt_with_no_active_turn_reports_not_active(isolated_registry):
    session = _active_session(isolated_registry)
    response = executor.interrupt_dev_turn(session["session_id"])
    assert response["status"] == "NOT_ACTIVE"


# -- 11: concurrency ----------------------------------------------------------


def test_a_second_turn_for_the_same_session_is_rejected_while_one_is_active(isolated_registry):
    session = _active_session(isolated_registry)
    FakeAppServerClient.block_until_interrupted = True

    first = executor.run_dev_turn(session["session_id"], "first")
    assert _wait_until(lambda: len(FakeAppServerClient.instances) == 1)

    with pytest.raises(service.DevServiceError) as error:
        executor.run_dev_turn(session["session_id"], "second, should be rejected")
    assert error.value.code == "SESSION_BUSY"

    # Clean up: interrupt the blocked turn so the background thread exits.
    executor.interrupt_dev_turn(session["session_id"], first["turn_id"])
    assert _wait_until(lambda: executor.get_turn_detail(session["session_id"], first["turn_id"])["status"] == "CANCELLED")


def test_concurrent_turn_cap_is_enforced_across_sessions(isolated_registry):
    FakeAppServerClient.block_until_interrupted = True
    session_a = _active_session(isolated_registry, "alpha")
    session_b = _active_session(isolated_registry, "beta")

    started = []
    for session in (session_a, session_b):
        result = executor.run_dev_turn(session["session_id"], "block")
        started.append((session, result))
    assert _wait_until(lambda: len(FakeAppServerClient.instances) == 2)

    session_c = _active_session(isolated_registry, "gamma")
    with pytest.raises(service.DevServiceError) as error:
        executor.run_dev_turn(session_c["session_id"], "over the cap")
    assert error.value.code == "TOO_MANY_CONCURRENT_DEV_TURNS"

    for session, result in started:
        executor.interrupt_dev_turn(session["session_id"], result["turn_id"])
    for session, result in started:
        assert _wait_until(lambda s=session, r=result: executor.get_turn_detail(s["session_id"], r["turn_id"])["status"] == "CANCELLED")


# -- 12: persistence across reopening SQLite ---------------------------------


def test_turn_persists_after_reopening_the_database(isolated_registry):
    session = _active_session(isolated_registry)
    result = executor.run_dev_turn(session["session_id"], "do something")
    assert _wait_until(lambda: executor.get_turn_detail(session["session_id"], result["turn_id"])["status"] == "COMPLETED")

    database.initialize_database()  # simulate reopening/restart

    turn = executor.get_turn_detail(session["session_id"], result["turn_id"])
    assert turn["status"] == "COMPLETED"
    assert turn["response_text"] == "fake dev reply"


# -- 13: no approval handler is ever wired (fail-closed by construction) ----


def test_no_approval_handler_is_ever_passed_to_the_app_server_client(isolated_registry):
    session = _active_session(isolated_registry)
    result = executor.run_dev_turn(session["session_id"], "do something")
    assert _wait_until(lambda: executor.get_turn_detail(session["session_id"], result["turn_id"])["status"] == "COMPLETED")

    assert FakeAppServerClient.instances[0].on_server_request is None, (
        "the executor must never register an approval handler -- "
        "AppServerClient's own fail-closed default is the entire approval story for this MVP"
    )


# -- 14: terminal state is never overwritten by a later/duplicate event -----


def test_recovery_never_overwrites_an_already_terminal_turn(isolated_registry):
    session = _active_session(isolated_registry)
    result = executor.run_dev_turn(session["session_id"], "do something")
    assert _wait_until(lambda: executor.get_turn_detail(session["session_id"], result["turn_id"])["status"] == "COMPLETED")

    # A recovery sweep that would call everything orphaned must not touch
    # an already-COMPLETED turn -- its registry-level query only ever
    # selects PENDING/RUNNING rows.
    recovered = executor.recover_orphaned_dev_turns()

    assert result["turn_id"] not in recovered
    turn = executor.get_turn_detail(session["session_id"], result["turn_id"])
    assert turn["status"] == "COMPLETED"
    assert turn["response_text"] == "fake dev reply"


# -- 15: orphaned process recovery -------------------------------------------


def test_recover_orphaned_dev_turns_marks_a_dead_owner_outcome_unknown(isolated_registry):
    session = _active_session(isolated_registry)
    turn_id = dev_sessions.create_turn(session["session_id"], "stuck")
    dev_sessions.update_turn_status(turn_id, "RUNNING")
    with database.get_connection() as connection:
        connection.execute("UPDATE dev_turns SET owner_pid = 999999999 WHERE id = ?", (turn_id,))

    recovered = executor.recover_orphaned_dev_turns()

    assert turn_id in recovered
    turn = dev_sessions.get_turn(turn_id)
    assert turn["status"] == "FAILED"
    assert turn["error_code"] == "TURN_OUTCOME_UNKNOWN"
    # The session itself (and its worktree) must be untouched by recovery.
    assert dev_sessions.get_session(session["session_id"])["status"] == dev_sessions.STATUS_ACTIVE
    assert Path(session["worktree_path"]).is_dir()


# -- 16: no changes to the original project ----------------------------------


def test_running_a_turn_never_modifies_the_original_project_working_tree(isolated_registry):
    project_path = _register_project(isolated_registry)
    session = service.create_dev_session("demo")

    result = executor.run_dev_turn(session["session_id"], "do something")
    assert _wait_until(lambda: executor.get_turn_detail(session["session_id"], result["turn_id"])["status"] == "COMPLETED")

    status = _run(["status", "--porcelain"], cwd=project_path)
    assert status == "", f"the project's own working tree must stay clean, got: {status!r}"


# -- changed_files (F2.4A): read-only Git status summary --------------------


def test_changed_files_is_empty_for_a_brand_new_session(isolated_registry):
    session = _active_session(isolated_registry)
    assert executor.changed_files(session["session_id"]) == []


def test_changed_files_reflects_writes_the_executor_itself_made(isolated_registry):
    session = _active_session(isolated_registry)
    worktree = session["worktree_path"]
    (Path(worktree) / "generated_by_turn.py").write_text("def added(): pass\n", encoding="utf-8")

    files = executor.changed_files(session["session_id"])
    assert files == [{"path": "generated_by_turn.py", "status": "added"}]


def test_changed_files_is_rejected_for_an_unknown_session(isolated_registry):
    with pytest.raises(service.DevServiceError) as error:
        executor.changed_files("DEV-DOES-NOT-EXIST")
    assert error.value.code == "SESSION_NOT_FOUND"


def test_changed_files_is_rejected_once_project_authorization_is_revoked(isolated_registry):
    session = _active_session(isolated_registry)
    projects.set_dev_enabled("demo", False)
    with pytest.raises(service.DevServiceError) as error:
        executor.changed_files(session["session_id"])
    assert error.value.code == "DEV_MODE_DISABLED"


def test_changed_files_never_requires_the_execution_feature_gate(isolated_registry, monkeypatch):
    """Inspecting a worktree is read-only, not an execution action -- it
    must work with the gate off, unlike run_dev_turn."""
    monkeypatch.delenv(feature_gate.ENV_VAR, raising=False)
    session = _active_session(isolated_registry)
    assert feature_gate.execution_enabled() is False
    assert executor.changed_files(session["session_id"]) == []


def test_review_diff_returns_base_sha_diff_without_requiring_execution_gate(isolated_registry, monkeypatch):
    monkeypatch.delenv(feature_gate.ENV_VAR, raising=False)
    session = _active_session(isolated_registry)
    (Path(session["worktree_path"]) / "reviewed.txt").write_text("review content\n", encoding="utf-8")
    review = executor.review_diff(session["session_id"])
    assert feature_gate.execution_enabled() is False
    assert review["base_sha"] == session["base_sha"]
    assert review["files"][0]["path"] == "reviewed.txt"
    assert "+review content" in review["files"][0]["patch"]


def test_review_diff_is_rejected_for_unknown_or_revoked_sessions(isolated_registry):
    with pytest.raises(service.DevServiceError) as unknown:
        executor.review_diff("DEV-DOES-NOT-EXIST")
    assert unknown.value.code == "SESSION_NOT_FOUND"
    session = _active_session(isolated_registry)
    projects.set_dev_enabled("demo", False)
    with pytest.raises(service.DevServiceError) as revoked:
        executor.review_diff(session["session_id"])
    assert revoked.value.code == "DEV_MODE_DISABLED"


def test_integration_gate_defaults_off_and_apply_requires_explicit_confirmation(isolated_registry, monkeypatch):
    monkeypatch.delenv(integration_gate.ENV_VAR, raising=False)
    assert integration_gate.integration_enabled() is False
    session = _active_session(isolated_registry)
    (Path(session["worktree_path"]) / "change.txt").write_text("approved\n", encoding="utf-8")
    review = integration.create_review(session["session_id"])
    with pytest.raises(service.DevServiceError) as error:
        integration.apply_review(session["session_id"], review["review_id"], confirmed=True)
    assert error.value.code == "INTEGRATION_DISABLED"
    monkeypatch.setenv(integration_gate.ENV_VAR, "1")
    with pytest.raises(service.DevServiceError) as error:
        integration.apply_review(session["session_id"], review["review_id"], confirmed=False)
    assert error.value.code == "CONFIRMATION_REQUIRED"


def test_integration_review_detects_snapshot_and_target_changes(isolated_registry, monkeypatch):
    monkeypatch.setenv(integration_gate.ENV_VAR, "1")
    session = _active_session(isolated_registry)
    candidate = Path(session["worktree_path"]) / "reviewed.txt"
    candidate.write_text("before\n", encoding="utf-8")
    review = integration.create_review(session["session_id"])
    candidate.write_text("after\n", encoding="utf-8")
    preflight = integration.integration_preflight(session["session_id"], review["review_id"])
    assert preflight["ready"] is False
    assert any(issue["code"] == "SNAPSHOT_STALE" for issue in preflight["issues"])


def test_integration_snapshot_hash_detects_binary_content_changes(isolated_registry, monkeypatch):
    monkeypatch.setenv(integration_gate.ENV_VAR, "1")
    session = _active_session(isolated_registry)
    blob = Path(session["worktree_path"]) / "payload.bin"
    blob.write_bytes(b"\x00first")
    review = integration.create_review(session["session_id"])
    original_hash = review["snapshot"]["snapshot_hash"]
    blob.write_bytes(b"\x00second")
    preflight = integration.integration_preflight(session["session_id"], review["review_id"])
    assert review["snapshot"]["diff"]["files"][0]["binary"] is True
    assert original_hash != integration.create_review(session["session_id"])["snapshot"]["snapshot_hash"]
    assert any(issue["code"] == "SNAPSHOT_STALE" for issue in preflight["issues"])


def test_approved_snapshot_applies_exact_contents_and_persists_history(isolated_registry, monkeypatch):
    monkeypatch.setenv(integration_gate.ENV_VAR, "1")
    session = _active_session(isolated_registry)
    worktree = Path(session["worktree_path"])
    (worktree / "README.md").write_text("exact approved contents\n", encoding="utf-8")
    (worktree / "added.txt").write_text("new file\n", encoding="utf-8")
    review = integration.create_review(session["session_id"])
    preflight = integration.integration_preflight(session["session_id"], review["review_id"])
    assert preflight["ready"] is True
    applied = integration.apply_review(session["session_id"], review["review_id"], confirmed=True)
    assert applied["status"] == "APPLIED"
    project_path = projects.get_project("demo")["path"]
    assert (Path(project_path) / "README.md").read_text(encoding="utf-8") == "exact approved contents\n"
    assert (Path(project_path) / "added.txt").read_text(encoding="utf-8") == "new file\n"
    assert _run(["rev-parse", "HEAD"], cwd=project_path) == applied["commit_sha"]
    integrated_paths = set(_run(["diff-tree", "--no-commit-id", "--name-only", "-r", applied["commit_sha"]], cwd=project_path).splitlines())
    assert integrated_paths == {"README.md", "added.txt"}
    history = integration.list_history(session["session_id"])
    assert history[0]["status"] == "APPLIED"
    assert history[0]["result_sha"] == applied["commit_sha"]
    with pytest.raises(service.DevServiceError) as repeated:
        integration.apply_review(session["session_id"], review["review_id"], confirmed=True)
    assert repeated.value.code == "APPLY_ALREADY_ATTEMPTED"


def test_apply_normalizes_crlf_worktree_like_git_add_so_commit_matches_review(isolated_registry, monkeypatch):
    # core.autocrlf=true checks the worktree out with CRLF while the repo
    # stores LF; `git diff` (the review) hides that, so Apply must too.
    monkeypatch.setenv(integration_gate.ENV_VAR, "1")
    session = _active_session(isolated_registry)
    project_path = projects.get_project("demo")["path"]
    _run(["config", "core.autocrlf", "true"], cwd=project_path)
    worktree = Path(session["worktree_path"])
    (worktree / "README.md").write_bytes(b"placeholder\r\nsecond line\r\n")
    review = integration.create_review(session["session_id"])
    [reviewed] = review["snapshot"]["diff"]["files"]
    assert (reviewed["additions"], reviewed["deletions"]) == (1, 0)
    assert integration.integration_preflight(session["session_id"], review["review_id"])["ready"] is True
    applied = integration.apply_review(session["session_id"], review["review_id"], confirmed=True)
    assert applied["status"] == "APPLIED"
    commit = applied["commit_sha"]
    blob = subprocess.run(["git", "cat-file", "blob", f"{commit}:README.md"], cwd=project_path,
                          capture_output=True, check=True).stdout
    assert blob == b"placeholder\nsecond line\n"
    assert _run(["diff", "--numstat", f"{commit}^", commit], cwd=project_path) == "1\t0\tREADME.md"
    integrated_patch = _run(["diff", "--no-ext-diff", "--no-textconv", f"{commit}^", commit, "--", "README.md"], cwd=project_path)
    reviewed_patch = _run(["diff", "--no-ext-diff", "--no-textconv", session["base_sha"], "--", "README.md"], cwd=str(worktree))
    assert integrated_patch == reviewed_patch
    assert reviewed["patch"].strip() in integrated_patch


def test_rejection_preserves_original_repository_and_worktree(isolated_registry):
    session = _active_session(isolated_registry)
    worktree = Path(session["worktree_path"])
    (worktree / "keep-me.txt").write_text("not applied\n", encoding="utf-8")
    review = integration.create_review(session["session_id"])
    project_path = projects.get_project("demo")["path"]
    original_head = _run(["rev-parse", "HEAD"], cwd=project_path)
    assert integration.reject_review(session["session_id"], review["review_id"])["status"] == "REJECTED"
    assert _run(["rev-parse", "HEAD"], cwd=project_path) == original_head
    assert not (Path(project_path) / "keep-me.txt").exists()
    assert (worktree / "keep-me.txt").exists()


def test_preflight_blocks_dirty_or_moved_destination_without_touching_it(isolated_registry, monkeypatch):
    monkeypatch.setenv(integration_gate.ENV_VAR, "1")
    session = _active_session(isolated_registry)
    worktree = Path(session["worktree_path"])
    (worktree / "new.txt").write_text("candidate\n", encoding="utf-8")
    review = integration.create_review(session["session_id"])
    project_path = projects.get_project("demo")["path"]
    (Path(project_path) / "local.txt").write_text("keep local\n", encoding="utf-8")
    dirty = integration.integration_preflight(session["session_id"], review["review_id"])
    assert any(item["code"] == "TARGET_DIRTY" for item in dirty["issues"])
    assert not (Path(project_path) / "new.txt").exists()
    (Path(project_path) / "local.txt").unlink()
    (Path(project_path) / "README.md").write_text("changed locally\n", encoding="utf-8")
    _run(["add", "README.md"], cwd=project_path)
    _run(["commit", "-q", "-m", "move target ahead"], cwd=project_path)
    moved = integration.integration_preflight(session["session_id"], review["review_id"])
    assert any(item["code"] == "TARGET_MOVED" for item in moved["issues"])
    assert not (Path(project_path) / "new.txt").exists()


def test_active_turn_and_cross_process_project_lock_block_apply(isolated_registry, monkeypatch):
    monkeypatch.setenv(integration_gate.ENV_VAR, "1")
    session = _active_session(isolated_registry)
    Path(session["worktree_path"], "new.txt").write_text("candidate\n", encoding="utf-8")
    review = integration.create_review(session["session_id"])
    lock = dev_integrations.acquire_project_lock(session["project_id"], review["review_id"])
    assert lock is True
    assert dev_integrations.acquire_project_lock(session["project_id"], review["review_id"]) is False
    with pytest.raises(dev_sessions.DevTurnBusyError):
        dev_sessions.create_turn(session["session_id"], "must wait for integration")
    dev_integrations.release_project_lock(session["project_id"], review["review_id"])
    dev_sessions.create_turn(session["session_id"], "still running")
    preflight = integration.integration_preflight(session["session_id"], review["review_id"])
    assert any(issue["code"] == "TURN_ACTIVE" for issue in preflight["issues"])


def test_fast_forward_failure_before_destination_update_preserves_original(isolated_registry, monkeypatch):
    monkeypatch.setenv(integration_gate.ENV_VAR, "1")
    session = _active_session(isolated_registry)
    Path(session["worktree_path"], "new.txt").write_text("candidate\n", encoding="utf-8")
    review = integration.create_review(session["session_id"])
    project_path = projects.get_project("demo")["path"]
    original_head = _run(["rev-parse", "HEAD"], cwd=project_path)
    real_git = integration._git

    def fail_merge(args, **kwargs):
        if "merge" in args:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="simulated pre-update failure")
        return real_git(args, **kwargs)

    monkeypatch.setattr(integration, "_git", fail_merge)
    with pytest.raises(service.DevServiceError) as error:
        integration.apply_review(session["session_id"], review["review_id"], confirmed=True)
    assert error.value.code == "GIT_APPLY_FAILED"
    assert _run(["rev-parse", "HEAD"], cwd=project_path) == original_head
    assert _run(["status", "--porcelain"], cwd=project_path) == ""
    assert not (Path(project_path) / "new.txt").exists()


class _SimulatedWinError5(PermissionError):
    winerror = 5


def _temporary_directory_with_cleanup_error(monkeypatch, *, failing_prefix):
    real_temporary_directory = integration.tempfile.TemporaryDirectory

    class CleanupFailure:
        def __init__(self, *args, **kwargs):
            self.prefix = kwargs.get("prefix", "")
            self.inner = real_temporary_directory(*args, **kwargs)

        def __enter__(self):
            return self.inner.__enter__()

        def __exit__(self, exc_type, exc, tb):
            result = self.inner.__exit__(exc_type, exc, tb)
            if self.prefix == failing_prefix and exc_type is None:
                raise _SimulatedWinError5(5, "Access is denied.", self.prefix)
            return result

    monkeypatch.setattr(integration.tempfile, "TemporaryDirectory", CleanupFailure)


def test_winerror5_candidate_index_cleanup_keeps_destination_unchanged_and_blocks_retry(isolated_registry, monkeypatch):
    monkeypatch.setenv(integration_gate.ENV_VAR, "1")
    session = _active_session(isolated_registry)
    Path(session["worktree_path"], "candidate.txt").write_text("approved\n", encoding="utf-8")
    review = integration.create_review(session["session_id"])
    project_path = projects.get_project("demo")["path"]
    original_head = _run(["rev-parse", "HEAD"], cwd=project_path)
    _temporary_directory_with_cleanup_error(monkeypatch, failing_prefix="nexus-dev-integration-")

    with pytest.raises(service.DevServiceError) as error:
        integration.apply_review(session["session_id"], review["review_id"], confirmed=True)

    assert error.value.code == "INTEGRATION_ERROR"
    assert _run(["rev-parse", "HEAD"], cwd=project_path) == original_head
    assert _run(["status", "--porcelain"], cwd=project_path) == ""
    history = integration.list_history(session["session_id"])
    assert history[0]["status"] == "FAILED"
    assert "WinError 5" in history[0]["error_message"]
    with pytest.raises(service.DevServiceError) as repeated:
        integration.apply_review(session["session_id"], review["review_id"], confirmed=True)
    assert repeated.value.code == "APPLY_ALREADY_ATTEMPTED"


def test_winerror5_hooks_cleanup_reconciles_successful_git_update_as_applied(isolated_registry, monkeypatch):
    monkeypatch.setenv(integration_gate.ENV_VAR, "1")
    session = _active_session(isolated_registry)
    worktree = Path(session["worktree_path"])
    (worktree / "integrated.txt").write_text("exact approved content\n", encoding="utf-8")
    review = integration.create_review(session["session_id"])
    project_path = projects.get_project("demo")["path"]
    _temporary_directory_with_cleanup_error(monkeypatch, failing_prefix="nexus-dev-hooks-")

    applied = integration.apply_review(session["session_id"], review["review_id"], confirmed=True)

    assert applied["status"] == "APPLIED"
    assert (Path(project_path) / "integrated.txt").read_text(encoding="utf-8") == "exact approved content\n"
    assert _run(["rev-parse", "HEAD"], cwd=project_path) == applied["commit_sha"]
    assert _run(["status", "--porcelain"], cwd=project_path) == ""
    history = integration.list_history(session["session_id"])
    assert history[0]["status"] == "APPLIED"
    assert history[0]["result_sha"] == applied["commit_sha"]
    with pytest.raises(service.DevServiceError) as repeated:
        integration.apply_review(session["session_id"], review["review_id"], confirmed=True)
    assert repeated.value.code == "APPLY_ALREADY_ATTEMPTED"
