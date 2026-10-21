"""Targeted P2B.1 coordination tests with simulated Development missions."""

from __future__ import annotations

import ast
import inspect
import json
import multiprocessing
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from nexus.dev.service import DevServiceError
from nexus.registry import database, dev_missions
from nexus.registry import workspace as store
from nexus.registry import workspace_execution as executions
from nexus.workspace import coordinator

FREE = {"id": "free/model", "cost_status": "free"}


@pytest.fixture
def coord_env(tmp_path, monkeypatch):
    original_db, original_storage = database.DATABASE_PATH, database.STORAGE_DIR
    database.STORAGE_DIR = tmp_path / "storage"
    database.DATABASE_PATH = database.STORAGE_DIR / "nexus.db"
    database.initialize_database()
    monkeypatch.setattr("nexus.dev.test_profiles.list_test_profiles", lambda _project: [{"id": "unit"}])
    monkeypatch.setattr("nexus.dev.models.list_dev_models", lambda: {"models": [FREE], "catalog_error": None})
    with database.get_connection() as connection:
        connection.execute(
            "INSERT INTO projects (id, name, path, enabled, dev_enabled) VALUES ('demo', 'Demo', ?, 1, 1)",
            (str(tmp_path),),
        )
    proposal = {
        "schema_version": "maestro-plan/1", "source": "llm", "complexity": "multi",
        "summary": "two agents", "explanation": "two", "needs_clarification": None,
        "agents": [
            {"key": "api", "role": "backend", "title": "API", "model": "free/model",
             "task": "API", "acceptance_criteria": ["ok"], "depends_on": [], "touches": ["api.py"]},
            {"key": "tests", "role": "tester", "title": "Tests", "model": "free/model",
             "task": "tests", "acceptance_criteria": ["ok"], "depends_on": ["api"], "touches": ["tests.py"]},
        ],
        "execution": {"mode": "sequential", "order": ["api", "tests"]}, "limit": 3,
        "fallback_reason": None,
    }
    plan_id = store.create_plan("demo", "two agents", policy_version="maestro-plan/1", complexity="multi", proposal=proposal, source="llm")
    approval = {"agent_models": {key: {"model": "free/model", "cost_status": "free"} for key in ("api", "tests")},
                "max_turns_per_agent": 1, "mission_timeout_seconds": 60, "cost_confirmed_for": []}
    with database.get_connection() as connection:
        connection.execute("UPDATE workspace_plans SET status='APPROVED', approval_json=? WHERE id=?", (json.dumps(approval), plan_id))
        for index, key in enumerate(("api", "tests")):
            session_id = f"DEV-{key.upper()}"
            pane_id = f"PANE-{key.upper()}"
            connection.execute(
                "INSERT INTO dev_sessions (id, project_id, branch, status) VALUES (?, 'demo', ?, 'ACTIVE')",
                (session_id, f"nexus/{key}"),
            )
            brief = {"task": key, "acceptance_criteria": ["ok"], "depends_on": ["api"] if key == "tests" else [],
                     "authorization": {"model": "free/model", "cost_status": "free", "max_turns": 1, "mission_timeout_seconds": 60}}
            connection.execute(
                """INSERT INTO workspace_panes
                   (id, project_id, dev_session_id, plan_id, origin, role, title, model, position, plan_agent_key, brief_json)
                   VALUES (?, 'demo', ?, ?, 'maestro', ?, ?, 'free/model', ?, ?, ?)""",
                (pane_id, session_id, plan_id, "backend" if key == "api" else "tester", key, index, key, json.dumps(brief)),
            )
    yield {"plan_id": plan_id, "calls": [], "tmp": tmp_path}
    for worker in list(coordinator._workers.values()):
        worker.join(timeout=5)
        assert not worker.is_alive(), "simulated worker leaked past fixture teardown"
    with coordinator._workers_lock:
        coordinator._workers.clear()
    database.DATABASE_PATH, database.STORAGE_DIR = original_db, original_storage


def _wait(plan_id: str, expected: set[str], timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = coordinator.state(plan_id)
        if state["status"] in expected:
            return state
        time.sleep(0.02)
    return coordinator.state(plan_id)


def _authorize(env):
    return coordinator.authorize_execution(
        env["plan_id"], agent_keys=["api", "tests"], test_profiles={"api": "unit", "tests": "unit"},
        max_turns_per_agent=1, mission_timeout_seconds=60,
    )


def _set_models(env, monkeypatch, choices, confirmed=None):
    catalog = [FREE, {"id": "low/model", "cost_status": "low_cost"},
               {"id": "std/model", "cost_status": "standard"},
               {"id": "other/model", "cost_status": "premium"}]
    monkeypatch.setattr("nexus.dev.models.list_dev_models", lambda: {"models": catalog, "catalog_error": None})
    with database.get_connection() as connection:
        plan = connection.execute("SELECT approval_json FROM workspace_plans WHERE id=?", (env["plan_id"],)).fetchone()
        approval = json.loads(plan["approval_json"])
        approval["agent_models"] = {key: {"model": model, "cost_status": next(
            item["cost_status"] for item in catalog if item["id"] == model)} for key, model in choices.items()}
        if confirmed is not None:
            approval["cost_confirmed_for"] = confirmed
        connection.execute("UPDATE workspace_plans SET approval_json=? WHERE id=?", (json.dumps(approval), env["plan_id"]))
        for key, model in choices.items():
            connection.execute("UPDATE workspace_panes SET model=? WHERE plan_id=? AND plan_agent_key=?", (model, env["plan_id"], key))


def test_execution_uses_exact_confirmed_model_ids_across_agents(coord_env, monkeypatch):
    env = coord_env
    _set_models(env, monkeypatch, {"api": "low/model", "tests": "std/model"}, ["low/model"])
    with pytest.raises(DevServiceError) as error:
        _authorize(env)
    assert error.value.code == "COST_CONFIRMATION_REQUIRED"
    assert executions.get_execution(env["plan_id"]) is None
    with database.get_connection() as connection:
        approval = json.loads(connection.execute("SELECT approval_json FROM workspace_plans WHERE id=?", (env["plan_id"],)).fetchone()[0])
    assert approval["cost_confirmed_for"] == ["low/model"]
    result = coordinator.authorize_execution(
        env["plan_id"], agent_keys=["api", "tests"], test_profiles={"api": "unit", "tests": "unit"},
        max_turns_per_agent=1, mission_timeout_seconds=60, confirm_costs=True,
    )
    assert result["status"] == "AWAITING_EXECUTION"
    with database.get_connection() as connection:
        approval = json.loads(connection.execute("SELECT approval_json FROM workspace_plans WHERE id=?", (env["plan_id"],)).fetchone()[0])
    assert approval["cost_confirmed_for"] == ["low/model", "std/model"]
    assert approval["execution_cost_confirmations"][0]["model_ids"] == ["std/model"]
    assert _authorize(env)["status"] == "AWAITING_EXECUTION"


def test_execution_legacy_low_cost_requires_new_recorded_consent(coord_env, monkeypatch):
    env = coord_env
    _set_models(env, monkeypatch, {"api": "low/model", "tests": "low/model"}, [])
    with pytest.raises(DevServiceError) as error:
        _authorize(env)
    assert error.value.code == "COST_CONFIRMATION_REQUIRED"
    coordinator.authorize_execution(
        env["plan_id"], agent_keys=["api", "tests"], test_profiles={"api": "unit", "tests": "unit"},
        max_turns_per_agent=1, mission_timeout_seconds=60, confirm_costs=True,
    )
    with database.get_connection() as connection:
        approval = json.loads(connection.execute("SELECT approval_json FROM workspace_plans WHERE id=?", (env["plan_id"],)).fetchone()[0])
    assert approval["cost_confirmed_for"] == ["low/model"]
    assert approval["execution_cost_confirmations"][0]["model_ids"] == ["low/model"]


def test_execution_rejects_stale_or_unapproved_model_even_with_confirmation(coord_env, monkeypatch):
    env = coord_env
    _set_models(env, monkeypatch, {"api": "std/model", "tests": "free/model"}, ["std/model"])
    monkeypatch.setattr("nexus.dev.models.list_dev_models", lambda: {"models": [FREE], "catalog_error": None})
    with pytest.raises(DevServiceError) as error:
        _authorize(env)
    assert error.value.code == "MODEL_NOT_AVAILABLE"
    assert executions.get_execution(env["plan_id"]) is None


def test_execution_requires_approved_plan_and_is_idempotent(coord_env):
    env = coord_env
    with database.get_connection() as connection:
        connection.execute("UPDATE workspace_plans SET status='PROPOSED' WHERE id=?", (env["plan_id"],))
    with pytest.raises(DevServiceError, match="approved") as error:
        _authorize(env)
    assert error.value.code == "PLAN_NOT_APPROVED"
    with database.get_connection() as connection:
        connection.execute("UPDATE workspace_plans SET status='APPROVED' WHERE id=?", (env["plan_id"],))
    first = _authorize(env)
    second = _authorize(env)
    assert first["status"] == second["status"] == "AWAITING_EXECUTION"
    assert len(first["steps"]) == 2


def test_sequential_execution_reuses_mission_runner_and_completes_for_review(coord_env, monkeypatch):
    env = coord_env
    calls = env["calls"]

    def fake_start(pane_id, *_args, **_kwargs):
        key = "api" if pane_id.endswith("API") else "tests"
        mission_id = dev_missions.create_mission(f"DEV-{key.upper()}", key, requested_model="free/model", allowed_models=["free/model"], test_profile="unit")
        calls.append(key)
        dev_missions.update_status(mission_id, "READY_FOR_REVIEW", result={"changed_files": []})
        return {"pane": {"session": {"session_id": f"DEV-{key.upper()}"}}, "mission": {"mission_id": mission_id}}

    monkeypatch.setattr("nexus.workspace.service.start_pane_mission", fake_start)
    _authorize(env)
    coordinator.start(env["plan_id"])
    state = _wait(env["plan_id"], {"AWAITING_REVIEW"})
    assert state["status"] == "AWAITING_REVIEW", (state["error_code"], state["error_message"], state["steps"])
    assert calls == ["api", "tests"]
    assert all(step["status"] == "READY_FOR_REVIEW" for step in state["steps"])


def test_dependency_changes_block_without_worktree_transfer(coord_env, monkeypatch):
    env = coord_env
    calls = []

    def fake_start(pane_id, *_args, **_kwargs):
        mission_id = dev_missions.create_mission("DEV-API", "api", requested_model="free/model", allowed_models=["free/model"], test_profile="unit")
        calls.append(pane_id)
        dev_missions.update_status(mission_id, "READY_FOR_REVIEW", result={"changed_files": [{"path": "api.py"}]})
        return {"pane": {"session": {"session_id": "DEV-API"}}, "mission": {"mission_id": mission_id}}

    monkeypatch.setattr("nexus.workspace.service.start_pane_mission", fake_start)
    _authorize(env)
    coordinator.start(env["plan_id"])
    state = _wait(env["plan_id"], {"BLOCKED"})
    assert state["status"] == "BLOCKED", (state["error_code"], state["error_message"], state["steps"])
    assert state["error_code"] == "DEPENDENCY_WORKTREE_NOT_TRANSFERRED"
    assert len(calls) == 1


def test_unknown_mission_outcome_is_not_replayed(coord_env, monkeypatch):
    env = coord_env
    calls = []

    def fake_start(pane_id, *_args, **_kwargs):
        mission_id = dev_missions.create_mission("DEV-API", "api", requested_model="free/model", allowed_models=["free/model"], test_profile="unit")
        calls.append(pane_id)
        dev_missions.update_status(mission_id, "OUTCOME_UNKNOWN", error_code="TURN_OUTCOME_UNKNOWN", error_message="uncertain")
        return {"pane": {"session": {"session_id": "DEV-API"}}, "mission": {"mission_id": mission_id}}

    monkeypatch.setattr("nexus.workspace.service.start_pane_mission", fake_start)
    _authorize(env)
    coordinator.start(env["plan_id"])
    state = _wait(env["plan_id"], {"RESULT_UNCERTAIN"})
    coordinator.start(env["plan_id"])
    assert state["status"] == "RESULT_UNCERTAIN" and len(calls) == 1

# P2B.1A: exercise SQLite fences, launch crash boundaries and real processes.


def _stale(plan_id):
    stamp = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    with database.get_connection() as connection:
        connection.execute("UPDATE workspace_plan_executions SET lease_renewed_at=?, owner_acquired_at=? WHERE plan_id=?", (stamp, stamp, plan_id))


def _existing(env, status="READY_FOR_REVIEW", *, starting=False):
    _authorize(env)
    token = executions.claim_execution(env["plan_id"])
    mission_id = dev_missions.create_mission("DEV-API", "api", requested_model="free/model", allowed_models=["free/model"], test_profile="unit")
    dev_missions.update_status(mission_id, status, result={"changed_files": [{"path": "api.py"}]})
    executions.update_step(env["plan_id"], "api", "STARTING" if starting else "RUNNING", mission_id=mission_id, owner_token=token)
    _stale(env["plan_id"])
    return token, mission_id


def _claim_in_process(db_path, plan_id, ready, results):
    from pathlib import Path
    database.DATABASE_PATH = Path(db_path)
    database.STORAGE_DIR = database.DATABASE_PATH.parent
    ready.wait(10)
    token = executions.claim_recovery(plan_id)
    results.put(token)


def _heartbeat_in_process(db_path, plan_id, ready, stop):
    from pathlib import Path
    database.DATABASE_PATH = Path(db_path)
    database.STORAGE_DIR = database.DATABASE_PATH.parent
    token = executions.claim_execution(plan_id)
    ready.put(token)
    while not stop.wait(0.1):
        if not executions.renew_lease(plan_id, token):
            return


def test_state_during_live_starting_is_read_only(coord_env, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def fake_start(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        mission_id = dev_missions.create_mission("DEV-API", "api", requested_model="free/model", allowed_models=["free/model"], test_profile="unit")
        dev_missions.update_status(mission_id, "READY_FOR_REVIEW", result={"changed_files": [{"path": "api.py"}]})
        return {"mission": {"mission_id": mission_id}}

    monkeypatch.setattr("nexus.workspace.service.start_pane_mission", fake_start)
    _authorize(coord_env)
    coordinator.start(coord_env["plan_id"])
    assert entered.wait(3)
    try:
        for _ in range(10):
            snapshot = coordinator.state(coord_env["plan_id"])
            assert snapshot["status"] == "RUNNING"
            assert snapshot["steps"][0]["status"] == "STARTING"
            coordinator.recover(coord_env["plan_id"])
        assert executions.get_execution(coord_env["plan_id"])["error_code"] is None
    finally:
        release.set()
    assert _wait(coord_env["plan_id"], {"BLOCKED"})["status"] == "BLOCKED"


def test_crash_before_mission_id_is_uncertain_without_replay(coord_env, monkeypatch):
    _authorize(coord_env)
    token = executions.claim_execution(coord_env["plan_id"])
    executions.update_step(coord_env["plan_id"], "api", "STARTING", owner_token=token)
    _stale(coord_env["plan_id"])
    monkeypatch.setattr("nexus.workspace.service.start_pane_mission", lambda *_a, **_kw: pytest.fail("replayed uncertain launch"))
    assert coordinator.state(coord_env["plan_id"])["status"] == "RUNNING"
    coordinator.recover(coord_env["plan_id"])
    snapshot = _wait(coord_env["plan_id"], {"RESULT_UNCERTAIN"})
    assert snapshot["status"] == "RESULT_UNCERTAIN"
    assert snapshot["error_code"] == "COORDINATOR_START_INTERRUPTED"
    coordinator.start(coord_env["plan_id"])
    coordinator.recover(coord_env["plan_id"])
    assert executions.get_steps(coord_env["plan_id"])[0]["mission_id"] is None


@pytest.mark.parametrize(("mission_status", "expected"), [
    ("READY_FOR_REVIEW", "BLOCKED"), ("CANCELLED", "INTERRUPTED"),
    ("OUTCOME_UNKNOWN", "RESULT_UNCERTAIN"), ("FAILED", "FAILED"),
])
def test_recovery_reconciles_existing_terminal_mission_without_replay(coord_env, monkeypatch, mission_status, expected):
    _existing(coord_env, mission_status)
    monkeypatch.setattr("nexus.workspace.service.start_pane_mission", lambda *_a, **_kw: pytest.fail("mission replayed"))
    coordinator.recover(coord_env["plan_id"])
    snapshot = _wait(coord_env["plan_id"], {expected})
    assert snapshot["status"] == expected
    assert len(dev_missions.list_missions("DEV-API")) == 1
    coordinator.recover(coord_env["plan_id"])
    coordinator.start(coord_env["plan_id"])
    assert len(dev_missions.list_missions("DEV-API")) == 1


def test_restart_after_mission_id_in_starting_reconciles_existing_record(coord_env, monkeypatch):
    _existing(coord_env, starting=True)
    monkeypatch.setattr("nexus.workspace.service.start_pane_mission", lambda *_a, **_kw: pytest.fail("mission replayed"))
    coordinator.recover(coord_env["plan_id"])
    assert _wait(coord_env["plan_id"], {"BLOCKED"})["status"] == "BLOCKED"
    assert executions.get_steps(coord_env["plan_id"])[0]["status"] == "READY_FOR_REVIEW"


def test_recovery_monitors_running_mission_and_interrupts_same_session(coord_env, monkeypatch):
    _, mission_id = _existing(coord_env, "RUNNING")
    monkeypatch.setattr("nexus.workspace.service.start_pane_mission", lambda *_a, **_kw: pytest.fail("mission replayed"))
    calls = []

    def interrupt(session_id, ident):
        calls.append((session_id, ident))
        dev_missions.update_status(ident, "CANCELLED")

    monkeypatch.setattr("nexus.dev.mission_runner.interrupt_mission", interrupt)
    coordinator.recover(coord_env["plan_id"])
    assert coordinator.state(coord_env["plan_id"])["status"] == "RUNNING"
    coordinator.interrupt(coord_env["plan_id"])
    assert _wait(coord_env["plan_id"], {"INTERRUPTED"})["status"] == "INTERRUPTED"
    assert calls == [("DEV-API", mission_id)]


def test_lost_owner_cannot_renew_write_or_launch(coord_env, monkeypatch):
    old, _ = _existing(coord_env)
    new = executions.claim_recovery(coord_env["plan_id"])
    assert new and new != old
    assert not executions.renew_lease(coord_env["plan_id"], old)
    with pytest.raises(executions.OwnershipLost):
        executions.update_step(coord_env["plan_id"], "api", "STARTING", owner_token=old)
    with pytest.raises(executions.OwnershipLost):
        executions.update_execution(coord_env["plan_id"], "FAILED", owner_token=old)
    monkeypatch.setattr("nexus.workspace.service.start_pane_mission", lambda *_a, **_kw: pytest.fail("old owner launched"))
    coordinator._run(coord_env["plan_id"], old)
    assert executions.get_execution(coord_env["plan_id"])["status"] == "RUNNING"


def test_two_processes_cannot_claim_same_stale_execution(coord_env):
    old, _ = _existing(coord_env)
    context = multiprocessing.get_context("spawn")
    ready, results = context.Event(), context.Queue()
    processes = [context.Process(target=_claim_in_process, args=(str(database.DATABASE_PATH), coord_env["plan_id"], ready, results)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        ready.set()
        claims = [results.get(timeout=15) for _ in processes]
        assert sum(bool(token) for token in claims) == 1
        assert not executions.renew_lease(coord_env["plan_id"], old)
    finally:
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join()
        results.close()
    assert all(process.exitcode == 0 for process in processes)


def test_live_process_renews_beyond_thirty_seconds_and_dead_owner_is_recoverable(coord_env):
    _authorize(coord_env)
    context = multiprocessing.get_context("spawn")
    ready, stop = context.Queue(), context.Event()
    process = context.Process(target=_heartbeat_in_process, args=(str(database.DATABASE_PATH), coord_env["plan_id"], ready, stop))
    process.start()
    try:
        token = ready.get(timeout=15)
        assert token
        initial = executions.get_execution(coord_env["plan_id"])["lease_renewed_at"]
        # Keep the real process and SQLite heartbeat active past the lease TTL.
        deadline = time.monotonic() + 31
        while time.monotonic() < deadline:
            assert not executions.claim_recovery(coord_env["plan_id"])
            time.sleep(0.25)
        assert executions.get_execution(coord_env["plan_id"])["lease_renewed_at"] > initial
        assert not executions.claim_recovery(coord_env["plan_id"])
    finally:
        stop.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
        ready.close()
    assert process.exitcode == 0
    _stale(coord_env["plan_id"])
    assert executions.claim_recovery(coord_env["plan_id"])
    assert not executions.renew_lease(coord_env["plan_id"], token)


def test_heartbeat_protects_slow_launcher_and_old_worker_stops_after_takeover(coord_env, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow_start(*_a, **_kw):
        calls.append("launch")
        entered.set()
        assert release.wait(8)
        mission_id = dev_missions.create_mission("DEV-API", "api", requested_model="free/model", allowed_models=["free/model"], test_profile="unit")
        dev_missions.update_status(mission_id, "READY_FOR_REVIEW")
        return {"mission": {"mission_id": mission_id}}

    monkeypatch.setattr("nexus.workspace.service.start_pane_mission", slow_start)
    _authorize(coord_env)
    coordinator.start(coord_env["plan_id"])
    assert entered.wait(3)
    original = executions.get_execution(coord_env["plan_id"])
    try:
        time.sleep(2.3)
        assert executions.get_execution(coord_env["plan_id"])["lease_renewed_at"] > original["lease_renewed_at"]
        _stale(coord_env["plan_id"])
        coordinator.recover(coord_env["plan_id"])
        assert _wait(coord_env["plan_id"], {"RESULT_UNCERTAIN"})["status"] == "RESULT_UNCERTAIN"
    finally:
        release.set()
    time.sleep(0.3)
    assert calls == ["launch"]
    assert coordinator.state(coord_env["plan_id"])["status"] == "RESULT_UNCERTAIN"


def test_review_result_preserves_human_integration_approval(coord_env, monkeypatch):
    _existing(coord_env)
    with database.get_connection() as connection:
        connection.execute("DELETE FROM workspace_plan_execution_steps WHERE plan_id=? AND agent_key='tests'", (coord_env["plan_id"],))
    monkeypatch.setattr("nexus.workspace.service.start_pane_mission", lambda *_a, **_kw: pytest.fail("mission replayed"))
    coordinator.recover(coord_env["plan_id"])
    snapshot = _wait(coord_env["plan_id"], {"AWAITING_REVIEW"})
    assert snapshot["status"] == "AWAITING_REVIEW"
    assert snapshot["result"] == {"human_approval_required": True, "integration_applied": False}
    tree = ast.parse(inspect.getsource(coordinator))
    forbidden = {"apply", "apply_changes", "merge", "push", "run_dev_turn", "start_mission"}
    calls = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not calls.intersection(forbidden)


def test_recovery_never_invokes_launcher_for_a_persisted_mission(coord_env, monkeypatch):
    _authorize(coord_env)
    executions.claim_execution(coord_env["plan_id"])
    mission_id = dev_missions.create_mission("DEV-API", "api", requested_model="free/model", allowed_models=["free/model"], test_profile="unit")
    dev_missions.update_status(mission_id, "READY_FOR_REVIEW", result={"changed_files": [{"path": "api.py"}]})
    with database.get_connection() as connection:
        connection.execute("UPDATE workspace_plan_execution_steps SET status='RUNNING', mission_id=? WHERE plan_id=? AND agent_key='api'", (mission_id, coord_env["plan_id"]))
        connection.execute("UPDATE workspace_plan_executions SET owner_pid=999999 WHERE plan_id=?", (coord_env["plan_id"],))
    _stale(coord_env["plan_id"])
    launches = []

    def record_launch(*_a, **_kw):
        launches.append(mission_id)
        return {"mission": {"mission_id": mission_id}}

    monkeypatch.setattr("nexus.workspace.service.start_pane_mission", record_launch)
    coordinator.recover(coord_env["plan_id"])
    snapshot = _wait(coord_env["plan_id"], {"BLOCKED"})
    assert launches == []
    assert snapshot["status"] == "BLOCKED"


def test_recent_renewal_blocks_takeover_of_old_acquisition(coord_env):
    _authorize(coord_env)
    executions.claim_execution(coord_env["plan_id"])
    old = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    with database.get_connection() as connection:
        connection.execute("UPDATE workspace_plan_executions SET owner_pid=999999, owner_acquired_at=? WHERE plan_id=?", (old, coord_env["plan_id"]))
    assert not executions.claim_recovery(coord_env["plan_id"])


def test_api_uses_exact_persisted_approval_limits(coord_env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from nexus.web.workspace_routes import router

    plan_id = coord_env["plan_id"]
    with database.get_connection() as connection:
        approval = json.loads(connection.execute("SELECT approval_json FROM workspace_plans WHERE id=?", (plan_id,)).fetchone()[0])
        approval.update(max_turns_per_agent=2, mission_timeout_seconds=1800)
        connection.execute("UPDATE workspace_plans SET approval_json=? WHERE id=?", (json.dumps(approval), plan_id))
    # Authorize through the real API/SQLite path without launching a mission.
    monkeypatch.setattr(coordinator, "start", lambda ident: executions.view(ident))
    app = FastAPI()
    app.include_router(router)
    payload = {"agent_keys": ["api", "tests"], "test_profiles": {"api": "unit", "tests": "unit"},
               "max_turns_per_agent": 2, "mission_timeout_seconds": 1800}
    with TestClient(app) as client:
        rejected = client.post(f"/api/workspace/plans/{plan_id}/execute", json={**payload, "max_turns_per_agent": 6})
        assert rejected.status_code == 400
        assert rejected.json()["detail"]["code"] == "LIMITS_NOT_CONFIRMED"
        accepted = client.post(f"/api/workspace/plans/{plan_id}/execute", json=payload)
        assert accepted.status_code == 200
        assert accepted.json()["max_turns_per_agent"] == 2
        assert accepted.json()["mission_timeout_seconds"] == 1800
        assert accepted.json()["status"] == "AWAITING_EXECUTION"
