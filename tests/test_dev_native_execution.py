"""Codex Native execution in Development sessions (P4.0B).

Mocked App-Server only; the real Codex flow is validated separately. Reuses
the executor tests' isolated registry, Git fixtures and fake client.
"""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

import nexus.registry.database as database
from nexus.chat.appserver_client import AppServerError
from nexus.dev import approvals, executor, mission_runner, service
from nexus.dev.service import DevServiceError
from nexus.registry import dev_sessions, projects
from tests.test_dev_executor import (  # noqa: F401 -- fixtures are used by name
    FakeAppServerClient,
    _patch_client,
    _register_project,
    _wait_until,
    isolated_registry,
)

NATIVE_MODELS = [{"id": "gpt-native", "supported_efforts": ["low", "high"]}]


class NativeFake(FakeAppServerClient):
    """Fake client that records native wiring and can raise an approval request."""

    approval_request: tuple[str, dict] | None = None
    responses: list = []
    turn_efforts: list = []

    def __init__(self, repo, **kwargs):
        super().__init__(repo, **kwargs)
        self.kwargs = kwargs
        self.backend = kwargs.get("backend", "omniroute")
        self.deferred = kwargs.get("on_deferred_server_request")
        self.answered = threading.Event()

    def respond_server_request(self, request_id, result):
        NativeFake.responses.append((request_id, result))
        self.answered.set()

    def thread_settings_update(self, thread_id, *, model=None, effort=None):
        raise AssertionError("Codex Native must not use the experimental settings API")

    def send_message_and_wait(self, thread_id, text, *, timeout_seconds=None, on_turn_started=None, effort=None):
        self.timeout_seconds = timeout_seconds
        if effort is not None:
            NativeFake.turn_efforts.append(effort)
        if on_turn_started:
            on_turn_started("FAKE-DEV-CODEX-TURN-1")
        if NativeFake.approval_request is not None and self.deferred is not None:
            method, params = NativeFake.approval_request
            assert self.deferred(method, params, 7) is True
            self.answered.wait(timeout=10)
            decision = NativeFake.responses[-1][1]["decision"] if NativeFake.responses else None
            if decision == "cancel":
                raise AppServerError("TURN_FAILED", "Turn ended with status 'interrupted'.")
            return f"decision={decision}"
        return FakeAppServerClient.response_text


@pytest.fixture(autouse=True)
def _native(_patch_client, monkeypatch):  # noqa: F811 -- runs after the base fake is installed
    NativeFake.approval_request = None
    NativeFake.responses = []
    NativeFake.turn_efforts = []
    monkeypatch.setattr(executor, "AppServerClient", NativeFake)
    monkeypatch.setattr(
        "nexus.chat.service._native_model_catalog",
        lambda *, refresh=False: ([dict(item) for item in NATIVE_MODELS], {"authenticated": True, "type": "chatgpt", "plan": "plus"}),
    )
    yield
    for pending in list(approvals._pending.values()):
        if pending.timer is not None:
            pending.timer.cancel()
    approvals._pending.clear()


@pytest.fixture
def no_omniroute(monkeypatch):
    """Codex Native may not touch any OmniRoute catalog, OAuth or telemetry path."""

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Codex Native must not consult OmniRoute")

    monkeypatch.setattr("nexus.workspace.omniroute_oauth.require_connected_route", forbidden)
    monkeypatch.setattr("nexus.workspace.omniroute_oauth.connection_status", forbidden)
    monkeypatch.setattr("nexus.dev.models.list_dev_models", forbidden)
    monkeypatch.setattr("nexus.routing.telemetry.OmniRouteTelemetryClient", forbidden)


def _native_session(tmp_path, model=None):
    _register_project(tmp_path)
    return service.create_dev_session("demo", requested_model=model, backend="codex_native")


def _turn_status(turn_id):
    return dev_sessions.get_turn(turn_id)["status"]


def _pending(session_id):
    rows = [row for row in approvals.list_for_session(session_id) if row["status"] == "PENDING"]
    return rows[0] if rows else None


COMMAND_REQUEST = ("item/commandExecution/requestApproval", {
    "command": ["python", "-m", "pytest", "-q"], "cwd": "C:/wt", "reason": "Sandbox blocked the test cache.",
    "itemId": "exec-1", "threadId": "T", "turnId": "U", "startedAtMs": 1,
})


# -- sessions -------------------------------------------------------------


def test_native_session_is_persisted_and_needs_no_omniroute(isolated_registry, no_omniroute):
    session = _native_session(isolated_registry, model="gpt-native")
    assert session["backend"] == "codex_native"
    assert session["requested_model"] == "gpt-native"
    assert dev_sessions.get_session(session["session_id"])["backend"] == "codex_native"


def test_native_session_rejects_a_model_the_local_account_does_not_offer(isolated_registry, no_omniroute):
    _register_project(isolated_registry)
    with pytest.raises(DevServiceError) as excinfo:
        service.create_dev_session("demo", requested_model="cx/not-offered", backend="codex_native")
    assert excinfo.value.code == "MODEL_NOT_ALLOWED"
    assert dev_sessions.list_sessions("demo") == []


def test_unknown_backend_is_rejected_before_any_worktree_exists(isolated_registry):
    _register_project(isolated_registry)
    with pytest.raises(DevServiceError) as excinfo:
        service.create_dev_session("demo", backend="bogus")
    assert excinfo.value.code == "UNSUPPORTED_BACKEND"
    assert dev_sessions.list_sessions("demo") == []


def test_existing_sessions_default_to_omniroute(isolated_registry):
    _register_project(isolated_registry)
    assert service.create_dev_session("demo")["backend"] == "omniroute"


def test_old_dev_sessions_table_migrates_to_the_omniroute_backend(isolated_registry):
    _register_project(isolated_registry)
    session_id = service.create_dev_session("demo")["session_id"]
    with sqlite3.connect(database.DATABASE_PATH) as connection:
        connection.execute("ALTER TABLE dev_sessions DROP COLUMN backend")
    database.initialize_database()
    database.initialize_database()
    assert dev_sessions.get_session(session_id)["backend"] == "omniroute"


# -- turns ------------------------------------------------------------------


def test_native_turn_runs_in_the_worktree_without_omniroute_and_wires_human_approvals(isolated_registry, no_omniroute):
    session = _native_session(isolated_registry, model="gpt-native")
    turn = executor.run_dev_turn(session["session_id"], "fix the bug")
    assert _wait_until(lambda: _turn_status(turn["turn_id"]) == "COMPLETED")

    client = NativeFake.instances[-1]
    assert client.backend == "codex_native"
    assert callable(client.deferred)
    assert client.sandbox_mode_override == executor.DEV_SANDBOX == "workspace-write"
    assert client.approval_policy_override == executor.DEV_APPROVAL_POLICY == "on-request"
    assert client.repo == dev_sessions.get_session(session["session_id"])["worktree_path"]
    assert client.thread_start_kwargs["model"] == "gpt-native"
    assert client.timeout_seconds == executor.NATIVE_DEV_TURN_TIMEOUT_SECONDS
    assert executor.NATIVE_DEV_TURN_TIMEOUT_SECONDS > approvals.MAX_TIMEOUT_SECONDS


def test_native_effort_uses_the_per_turn_override(isolated_registry, no_omniroute):
    session = _native_session(isolated_registry, model="gpt-native")
    turn = executor.run_dev_turn(session["session_id"], "fix", effort="high")
    assert _wait_until(lambda: _turn_status(turn["turn_id"]) == "COMPLETED")
    assert NativeFake.turn_efforts == ["high"]
    with pytest.raises(DevServiceError) as excinfo:
        executor.run_dev_turn(session["session_id"], "fix", effort="ultra")
    assert excinfo.value.code == "EFFORT_NOT_SUPPORTED"


def test_omniroute_sessions_keep_the_oauth_gate_and_fail_closed_approvals(isolated_registry, monkeypatch):
    _register_project(isolated_registry)
    session = service.create_dev_session("demo")
    gate = []
    monkeypatch.setattr("nexus.workspace.omniroute_oauth.require_connected_route", lambda model: gate.append(model))
    turn = executor.run_dev_turn(session["session_id"], "task")
    assert _wait_until(lambda: _turn_status(turn["turn_id"]) == "COMPLETED")
    client = NativeFake.instances[-1]
    assert client.backend == "omniroute"
    assert client.deferred is None and "on_deferred_server_request" not in client.kwargs
    assert gate


def test_a_session_with_an_unknown_backend_fails_closed(isolated_registry):
    session = _native_session(isolated_registry)
    with sqlite3.connect(database.DATABASE_PATH) as connection:
        connection.execute("UPDATE dev_sessions SET backend = 'mystery' WHERE id = ?", (session["session_id"],))
    with pytest.raises(DevServiceError) as excinfo:
        executor.run_dev_turn(session["session_id"], "task")
    assert excinfo.value.code == "UNSUPPORTED_BACKEND"
    assert NativeFake.instances == []


# -- human approvals -------------------------------------------------------------


def _start_turn_waiting_for_approval(tmp_path):
    session = _native_session(tmp_path, model="gpt-native")
    NativeFake.approval_request = COMMAND_REQUEST
    turn = executor.run_dev_turn(session["session_id"], "fix and run pytest")
    assert _wait_until(lambda: _pending(session["session_id"]) is not None)
    return session["session_id"], turn["turn_id"], _pending(session["session_id"])


def test_a_sensitive_command_waits_for_a_human_and_runs_only_after_accept(isolated_registry, no_omniroute):
    session_id, turn_id, pending = _start_turn_waiting_for_approval(isolated_registry)
    assert pending["kind"] == "command"
    assert pending["summary"]["command"] == "python -m pytest -q"
    assert pending["summary"]["reason"] == "Sandbox blocked the test cache."
    assert NativeFake.responses == [], "nothing may be answered before a human decides"
    assert _turn_status(turn_id) == "RUNNING"

    decided = approvals.decide(session_id, pending["approval_id"], "accept")

    assert decided["status"] == "APPROVED" and decided["decided_at"]
    assert NativeFake.responses == [(7, {"decision": "accept"})]
    assert _wait_until(lambda: _turn_status(turn_id) == "COMPLETED")
    assert dev_sessions.get_turn(turn_id)["response_text"] == "decision=accept"


def test_decline_is_sent_and_recorded(isolated_registry):
    session_id, turn_id, pending = _start_turn_waiting_for_approval(isolated_registry)
    assert approvals.decide(session_id, pending["approval_id"], "decline")["status"] == "DECLINED"
    assert NativeFake.responses == [(7, {"decision": "decline"})]
    assert _wait_until(lambda: _turn_status(turn_id) == "COMPLETED")


def test_session_wide_or_policy_grants_are_never_accepted(isolated_registry):
    session_id, _turn_id, pending = _start_turn_waiting_for_approval(isolated_registry)
    for decision in ("acceptForSession", "cancel", "", "ACCEPT"):
        with pytest.raises(DevServiceError) as excinfo:
            approvals.decide(session_id, pending["approval_id"], decision)
        assert excinfo.value.code == "INVALID_DECISION"
    assert NativeFake.responses == []
    approvals.decide(session_id, pending["approval_id"], "decline")


def test_interrupting_a_turn_cancels_its_pending_approval(isolated_registry):
    session_id, turn_id, pending = _start_turn_waiting_for_approval(isolated_registry)
    result = executor.interrupt_dev_turn(session_id, turn_id)
    assert result["status"] == "INTERRUPT_REQUESTED"
    assert NativeFake.responses == [(7, {"decision": "cancel"})]
    assert _wait_until(lambda: _turn_status(turn_id) == "CANCELLED")
    cancelled = approvals.list_for_session(session_id)[0]
    assert cancelled["status"] == "CANCELLED"
    assert "turn ended" in cancelled["status_detail"]
    with pytest.raises(DevServiceError) as excinfo:
        approvals.decide(session_id, pending["approval_id"], "accept")
    assert excinfo.value.code == "APPROVAL_CANCELLED"
    assert "turn ended" in str(excinfo.value)


def test_revoked_authorization_turns_an_accept_into_a_decline(isolated_registry):
    session_id, turn_id, pending = _start_turn_waiting_for_approval(isolated_registry)
    projects.set_dev_enabled("demo", False)
    with pytest.raises(DevServiceError):
        approvals.decide(session_id, pending["approval_id"], "accept")
    assert NativeFake.responses == [(7, {"decision": "decline"})]
    assert approvals.list_for_session(session_id)[0]["status"] == "DECLINED"
    assert _wait_until(lambda: _turn_status(turn_id) in {"COMPLETED", "FAILED"})


def test_an_unanswered_request_expires_as_a_decline(isolated_registry):
    session_id, turn_id, pending = _start_turn_waiting_for_approval(isolated_registry)
    approvals._expire(pending["approval_id"])
    assert NativeFake.responses == [(7, {"decision": "decline"})]
    expired = approvals.list_for_session(session_id)[0]
    assert expired["status"] == "EXPIRED"
    assert "no human decision" in expired["status_detail"]
    assert _wait_until(lambda: _turn_status(turn_id) == "COMPLETED")


def test_a_pending_row_without_a_live_request_is_expired_after_a_restart(isolated_registry):
    session = _native_session(isolated_registry)
    with sqlite3.connect(database.DATABASE_PATH) as connection:
        connection.execute(
            "INSERT INTO dev_approvals (id, dev_session_id, turn_id, kind, summary_json, status, created_at) "
            "VALUES ('APPROVAL-OLD', ?, 'DEVTURN-X', 'command', ?, 'PENDING', 'then')",
            (session["session_id"], json.dumps({"kind": "command"})),
        )
    expired = approvals.list_for_session(session["session_id"])[0]
    assert expired["status"] == "EXPIRED"
    assert "no requested action was executed" in expired["status_detail"]
    with pytest.raises(DevServiceError) as excinfo:
        approvals.decide(session["session_id"], "APPROVAL-OLD", "accept")
    assert excinfo.value.code == "APPROVAL_EXPIRED"
    assert "no requested action was executed" in str(excinfo.value)


def test_a_decision_must_target_the_owning_session(isolated_registry):
    session_id, _turn_id, pending = _start_turn_waiting_for_approval(isolated_registry)
    with pytest.raises(DevServiceError) as excinfo:
        approvals.decide("DEV-OTHER", pending["approval_id"], "accept")
    assert excinfo.value.code == "APPROVAL_NOT_FOUND"
    assert NativeFake.responses == []
    approvals.decide(session_id, pending["approval_id"], "decline")


def test_pending_approval_cannot_expire_during_registration(isolated_registry, monkeypatch):
    """A polling reader must not mistake a newly inserted row for an orphan."""
    inserted = threading.Event()
    release_registration = threading.Event()
    original_pending = approvals._Pending

    class PausedPending(original_pending):
        def __init__(self, *args, **kwargs):
            inserted.set()  # The SQLite PENDING row has already committed.
            assert release_registration.wait(5)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(approvals, "_Pending", PausedPending)
    session = _native_session(isolated_registry, model="gpt-native")
    NativeFake.approval_request = COMMAND_REQUEST
    turn = executor.run_dev_turn(session["session_id"], "fix and run pytest")
    assert inserted.wait(5)

    reader_started = threading.Event()
    reader_finished = threading.Event()
    observed = []

    def read_approvals():
        reader_started.set()
        try:
            observed.extend(approvals.list_for_session(session["session_id"]))
        finally:
            reader_finished.set()

    reader = threading.Thread(target=read_approvals, daemon=True)
    reader.start()
    try:
        assert reader_started.wait(5)
        assert not reader_finished.wait(1), "reader observed a row before its live request was registered"
    finally:
        release_registration.set()
        reader.join(timeout=5)

    assert reader_finished.is_set()
    assert len(observed) == 1 and observed[0]["status"] == "PENDING"
    approvals.decide(session["session_id"], observed[0]["approval_id"], "decline")
    assert _wait_until(lambda: _turn_status(turn["turn_id"]) == "COMPLETED")


def test_only_command_and_file_change_requests_are_deferred():
    handler = approvals.deferred_handler("DEV-1", "TURN-1", [object()])
    assert handler("item/permissions/requestApproval", {}, 1) is False
    assert handler("item/tool/requestUserInput", {}, 2) is False


def test_approval_timeout_is_bounded(monkeypatch):
    monkeypatch.setenv(approvals.TIMEOUT_ENV, "1")
    assert approvals.timeout_seconds() == approvals.MIN_TIMEOUT_SECONDS
    monkeypatch.setenv(approvals.TIMEOUT_ENV, "999999")
    assert approvals.timeout_seconds() == approvals.MAX_TIMEOUT_SECONDS
    monkeypatch.setenv(approvals.TIMEOUT_ENV, "nope")
    assert approvals.timeout_seconds() == approvals.DEFAULT_TIMEOUT_SECONDS


# -- missions ---------------------------------------------------------------------


def test_native_mission_validates_against_the_local_catalog_not_omniroute(isolated_registry, no_omniroute, monkeypatch):
    session = _native_session(isolated_registry, model="gpt-native")
    monkeypatch.setattr(mission_runner, "resolve_test_profile", lambda *_args: None)
    mission = mission_runner.start_mission(
        session["session_id"], "fix it", model="gpt-native", allowed_models=["gpt-native"], test_profile="p",
    )
    assert mission["requested_model"] == "gpt-native"
    assert mission["allowed_models"] == ["gpt-native"]
    with pytest.raises(DevServiceError) as excinfo:
        mission_runner.start_mission(
            session["session_id"], "fix", model="gpt-native", allowed_models=["other"], test_profile="p",
        )
    assert excinfo.value.code == "MODEL_NOT_CONFIRMED"
