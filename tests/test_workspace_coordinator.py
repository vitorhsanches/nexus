"""Targeted P2B.1 coordination tests with simulated Development missions."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import multiprocessing
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.dev.service import DevServiceError
from nexus.registry import database, dev_missions, dev_sessions, workspace_handoff
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


def test_durable_token_gates_mission_and_internal_turn_in_one_transaction(coord_env):
    _authorize(coord_env)
    plan_id = coord_env["plan_id"]
    token = executions.claim_execution(plan_id)
    executions.update_step(plan_id, "api", "STARTING", owner_token=token)
    with pytest.raises(workspace_handoff.HandoffBlocked):
        dev_missions.create_mission("DEV-API", "manual", requested_model="free/model",
                                    allowed_models=["free/model"], test_profile="unit")
    with executions.coordinated_launch(plan_id, "api", token):
        mission_id = dev_missions.create_mission("DEV-API", "coordinated", requested_model="free/model",
                                                allowed_models=["free/model"], test_profile="unit")
        turn_id = dev_sessions.create_turn("DEV-API", "internal", requested_model="free/model")
    assert mission_id and turn_id
    with pytest.raises(workspace_handoff.HandoffBlocked):
        dev_sessions.create_turn("DEV-API", "manual", requested_model="free/model")
    _stale(plan_id)
    new = executions.claim_recovery(plan_id)
    assert new and new != token
    with executions.coordinated_launch(plan_id, "api", token), pytest.raises(workspace_handoff.HandoffBlocked):
        dev_missions.create_mission("DEV-API", "old owner", requested_model="free/model",
                                    allowed_models=["free/model"], test_profile="unit")


# P5.2A: Codex Native has no mission row -- the gate must prove a legitimate
# coordinated launch a different way, without a generic bypass and without
# weakening the OmniRoute path above.


def _set_backend(session_id: str, backend: str) -> None:
    with database.get_connection() as connection:
        connection.execute("UPDATE dev_sessions SET backend=? WHERE id=?", (backend, session_id))


def _authorize_with_native_api(env):
    """Like _authorize(), but "api" is codex_native: it gets no test profile."""
    return coordinator.authorize_execution(
        env["plan_id"], agent_keys=["api", "tests"], test_profiles={"tests": "unit"},
        max_turns_per_agent=1, mission_timeout_seconds=60,
    )


def test_native_turn_launch_is_gated_without_any_mission_row(coord_env):
    _set_backend("DEV-API", "codex_native")
    _authorize_with_native_api(coord_env)
    plan_id = coord_env["plan_id"]
    token = executions.claim_execution(plan_id)
    executions.update_step(plan_id, "api", "STARTING", owner_token=token)

    # Manual, uncoordinated attempt: still blocked, exactly like OmniRoute.
    with pytest.raises(workspace_handoff.HandoffBlocked):
        dev_sessions.create_turn("DEV-API", "manual", requested_model="free/model")

    # No mission row was ever created for this native session -- the old
    # gate required `active_mission is not None`, which is always False for
    # Codex Native. The fix must allow the coordinated launch anyway.
    with database.get_connection() as connection:
        assert connection.execute(
            "SELECT 1 FROM dev_autonomous_missions WHERE dev_session_id='DEV-API'"
        ).fetchone() is None
    with executions.coordinated_launch(plan_id, "api", token):
        turn_id = dev_sessions.create_turn("DEV-API", "coordinated", requested_model="free/model")
    assert turn_id


def test_native_turn_launch_refuses_replay_after_the_first_turn(coord_env):
    """Codex Native has no repair-turn loop: exactly one launch per step, ever."""
    _set_backend("DEV-API", "codex_native")
    _authorize_with_native_api(coord_env)
    plan_id = coord_env["plan_id"]
    token = executions.claim_execution(plan_id)
    executions.update_step(plan_id, "api", "STARTING", owner_token=token)
    with executions.coordinated_launch(plan_id, "api", token):
        turn_id = dev_sessions.create_turn("DEV-API", "coordinated", requested_model="free/model")
    # The turn reaches a terminal state; nothing has advanced the step yet
    # (that is P5.2C's job), so it is still durably STARTING with no mission_id.
    dev_sessions.update_turn_status(turn_id, "COMPLETED")
    with database.get_connection() as connection:
        step = connection.execute(
            "SELECT status, mission_id FROM workspace_plan_execution_steps WHERE plan_id=? AND agent_key='api'",
            (plan_id,),
        ).fetchone()
    assert step["status"] == "STARTING" and step["mission_id"] is None
    # A second attempt, even with a live, correctly fenced coordinated
    # launch for the exact same step, must still be refused: this session
    # already had a turn, and a native step is a one-time launch.
    with executions.coordinated_launch(plan_id, "api", token):
        with pytest.raises(workspace_handoff.HandoffBlocked):
            dev_sessions.create_turn("DEV-API", "replay", requested_model="free/model")


def test_native_turn_launch_requires_a_live_owner_token(coord_env):
    _set_backend("DEV-API", "codex_native")
    _authorize_with_native_api(coord_env)
    plan_id = coord_env["plan_id"]
    token = executions.claim_execution(plan_id)
    executions.update_step(plan_id, "api", "STARTING", owner_token=token)
    _stale(plan_id)
    new = executions.claim_recovery(plan_id)
    assert new and new != token
    with executions.coordinated_launch(plan_id, "api", token), pytest.raises(workspace_handoff.HandoffBlocked):
        dev_sessions.create_turn("DEV-API", "old owner", requested_model="free/model")
    with executions.coordinated_launch(plan_id, "api", new):
        turn_id = dev_sessions.create_turn("DEV-API", "new owner", requested_model="free/model")
    assert turn_id


def test_native_and_omniroute_panes_are_gated_independently_in_one_plan(coord_env):
    """Backend selection is read per session, never a blanket flag.

    "tests" is freed of its fixture dependency on "api" here on purpose:
    the dependency-readiness check elsewhere in this module is itself
    mission-coupled (a separate, known P5.2C gap -- see the P5.2A report),
    and exercising it is not this test's concern.
    """
    _set_backend("DEV-API", "codex_native")
    # "tests" keeps the fixture's default backend, 'omniroute'.
    with database.get_connection() as connection:
        connection.execute(
            "UPDATE workspace_panes SET brief_json=? WHERE plan_id=? AND plan_agent_key='tests'",
            (json.dumps({"task": "tests", "acceptance_criteria": ["ok"], "depends_on": [],
                        "authorization": {"model": "free/model", "cost_status": "free",
                                          "max_turns": 1, "mission_timeout_seconds": 60}}),
             coord_env["plan_id"]),
        )
    _authorize_with_native_api(coord_env)
    plan_id = coord_env["plan_id"]
    token = executions.claim_execution(plan_id)
    executions.update_step(plan_id, "api", "STARTING", owner_token=token)
    executions.update_step(plan_id, "tests", "STARTING", owner_token=token)

    with executions.coordinated_launch(plan_id, "api", token):
        turn_id = dev_sessions.create_turn("DEV-API", "native step", requested_model="free/model")
    assert turn_id

    # The OmniRoute step is unaffected: it still needs an active mission
    # row, not a turn -- the native fix must not have loosened that.
    with executions.coordinated_launch(plan_id, "tests", token):
        with pytest.raises(workspace_handoff.HandoffBlocked):
            dev_sessions.create_turn("DEV-TESTS", "no mission yet", requested_model="free/model")
        mission_id = dev_missions.create_mission("DEV-TESTS", "tests", requested_model="free/model",
                                                 allowed_models=["free/model"], test_profile="unit")
    assert mission_id


# P5.2B: test_profile is backend-aware -- required for OmniRoute, never
# fabricated for Codex Native, never silently accepted for it either.


def test_authorize_execution_omits_test_profile_for_native_agent(coord_env):
    _set_backend("DEV-API", "codex_native")
    result = coordinator.authorize_execution(
        coord_env["plan_id"], agent_keys=["api", "tests"], test_profiles={"tests": "unit"},
        max_turns_per_agent=1, mission_timeout_seconds=60,
    )
    assert result["status"] == "AWAITING_EXECUTION"
    steps = {step["agent_key"]: step for step in result["steps"]}
    assert steps["api"]["test_profile"] == ""
    assert steps["tests"]["test_profile"] == "unit"
    assert steps["api"]["native_turn_id"] is None and steps["api"]["native_review_id"] is None


def test_authorize_execution_rejects_test_profile_supplied_for_native_agent(coord_env):
    _set_backend("DEV-API", "codex_native")
    with pytest.raises(DevServiceError) as error:
        coordinator.authorize_execution(
            coord_env["plan_id"], agent_keys=["api", "tests"],
            test_profiles={"api": "unit", "tests": "unit"},
            max_turns_per_agent=1, mission_timeout_seconds=60,
        )
    assert error.value.code == "TEST_PROFILE_NOT_APPLICABLE"
    assert executions.get_execution(coord_env["plan_id"]) is None


def test_authorize_execution_still_requires_test_profile_for_omniroute_agent(coord_env):
    with pytest.raises(DevServiceError) as error:
        coordinator.authorize_execution(
            coord_env["plan_id"], agent_keys=["api", "tests"], test_profiles={"tests": "unit"},
            max_turns_per_agent=1, mission_timeout_seconds=60,
        )
    assert error.value.code == "TEST_PROFILES_NOT_CONFIRMED"
    assert executions.get_execution(coord_env["plan_id"]) is None


def test_authorize_execution_all_native_plan_needs_no_test_profiles(coord_env):
    _set_backend("DEV-API", "codex_native")
    _set_backend("DEV-TESTS", "codex_native")
    result = coordinator.authorize_execution(
        coord_env["plan_id"], agent_keys=["api", "tests"], test_profiles={},
        max_turns_per_agent=1, mission_timeout_seconds=60,
    )
    assert result["status"] == "AWAITING_EXECUTION"
    assert all(step["test_profile"] == "" for step in result["steps"])


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
    executions.update_step(env["plan_id"], "api", "STARTING", owner_token=token)
    with executions.coordinated_launch(env["plan_id"], "api", token):
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
    token = executions.claim_execution(coord_env["plan_id"])
    executions.update_step(coord_env["plan_id"], "api", "STARTING", owner_token=token)
    with executions.coordinated_launch(coord_env["plan_id"], "api", token):
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


# P5.2C: a simulated Codex Native agent through the coordinator -- launch
# via the P5.2A gate, poll a turn instead of a mission, park at
# AWAITING_NATIVE_REVIEW, and resume only once a confirmed human review is
# explicitly bound. No real Codex/App-Server call is ever made: the turn
# itself is created for real (through dev_sessions.create_turn, so the
# gate and fencing are exercised exactly as in production), but nothing
# drives it to completion except the test itself.


def _review_id(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _save_review(session_id, review_id, *, approval_ready=True, status="REVIEWED"):
    from nexus.registry import dev_integrations

    record = dev_integrations.save_review(
        review_id=review_id, dev_session_id=session_id, project_id="demo",
        base_sha="base", target_head="base", target_branch="main",
        snapshot={"diff": {"files": [], "truncated": False, "review_id": "fingerprint"},
                 "approval_ready": approval_ready},
    )
    if status != "REVIEWED":
        dev_integrations.update_review(review_id, status=status)
        record = dev_integrations.get_review(review_id)
    return record


def _fake_run_dev_turn(calls):
    def run(dev_session_id, prompt, *, model=None, effort=None):
        turn_id = dev_sessions.create_turn(dev_session_id, prompt, requested_model=model)
        calls.append(turn_id)
        return {"turn_id": turn_id, "dev_session_id": dev_session_id, "status": "PENDING"}
    return run


def _allow_review_lookup(monkeypatch, session_id: str) -> None:
    """nexus.dev.integration._authorized_session() requires a real, Git-
    verified worktree. These tests fabricate review rows directly (see
    _save_review) and only need that check to pass, never a real diff."""
    with database.get_connection() as connection:
        connection.execute(
            "UPDATE dev_sessions SET worktree_path=?, base_sha='base' WHERE id=?",
            (f"C:/fake-worktree/{session_id}", session_id),
        )
    monkeypatch.setattr("nexus.dev.integration.verify_dev_worktree", lambda *args, **kwargs: Path(args[1]))


def _single_native_plan(env) -> str:
    """An independent, genuinely one-agent plan (coord_env's own plan always
    has a second, dependent OmniRoute agent) -- for the true single-native-
    agent happy path the P5.2C scope describes."""
    proposal = {
        "schema_version": "maestro-plan/1", "source": "llm", "complexity": "simple",
        "summary": "one agent", "explanation": "one", "needs_clarification": None,
        "agents": [{"key": "solo", "role": "implementer", "title": "Solo", "model": "free/model",
                    "task": "solo", "acceptance_criteria": ["ok"], "depends_on": [], "touches": ["solo.py"]}],
        "execution": {"mode": "sequential", "order": ["solo"]}, "limit": 3, "fallback_reason": None,
    }
    plan_id = store.create_plan("demo", "one agent", policy_version="maestro-plan/1", complexity="simple",
                                proposal=proposal, source="llm")
    approval = {"agent_models": {"solo": {"model": "free/model", "cost_status": "free"}},
               "max_turns_per_agent": 1, "mission_timeout_seconds": 60, "cost_confirmed_for": []}
    brief = {"task": "solo", "acceptance_criteria": ["ok"], "depends_on": [],
             "authorization": {"model": "free/model", "cost_status": "free", "max_turns": 1, "mission_timeout_seconds": 60}}
    with database.get_connection() as connection:
        connection.execute("UPDATE workspace_plans SET status='APPROVED', approval_json=? WHERE id=?",
                          (json.dumps(approval), plan_id))
        connection.execute(
            "INSERT INTO dev_sessions (id, project_id, branch, status, backend) VALUES "
            "('DEV-SOLO', 'demo', 'nexus/solo', 'ACTIVE', 'codex_native')",
        )
        connection.execute(
            """INSERT INTO workspace_panes
               (id, project_id, dev_session_id, plan_id, origin, role, title, model, position, plan_agent_key, brief_json)
               VALUES ('PANE-SOLO', 'demo', 'DEV-SOLO', ?, 'maestro', 'implementer', 'Solo', 'free/model', 0, 'solo', ?)""",
            (plan_id, json.dumps(brief)),
        )
    return plan_id


def _launch_native_step(env, monkeypatch):
    _set_backend("DEV-API", "codex_native")
    _allow_review_lookup(monkeypatch, "DEV-API")
    turns = []
    monkeypatch.setattr("nexus.dev.executor.run_dev_turn", _fake_run_dev_turn(turns))
    _authorize_with_native_api(env)
    plan_id = env["plan_id"]
    coordinator.start(plan_id)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not turns:
        time.sleep(0.02)
    assert turns, "the native turn was never launched"
    return plan_id, turns[0]


def test_native_step_launches_a_real_turn_through_the_gate(coord_env, monkeypatch):
    plan_id, turn_id = _launch_native_step(coord_env, monkeypatch)
    turn = dev_sessions.get_turn(turn_id)
    assert turn["dev_session_id"] == "DEV-API"
    state = _wait(plan_id, {"RUNNING"})
    assert state["steps"][0]["status"] == "RUNNING"
    assert state["steps"][0]["native_turn_id"] == turn_id
    assert state["steps"][0]["mission_id"] is None
    # Let the still-polling worker finish so it does not outlive this test.
    dev_sessions.update_turn_status(turn_id, "COMPLETED", response_text="done")
    _wait(plan_id, {"BLOCKED"})


def test_native_turn_completion_parks_at_awaiting_native_review(coord_env, monkeypatch):
    plan_id, turn_id = _launch_native_step(coord_env, monkeypatch)
    dev_sessions.update_turn_status(turn_id, "COMPLETED", response_text="done")
    state = _wait(plan_id, {"BLOCKED"})
    assert state["status"] == "BLOCKED"
    assert state["error_code"] == "AWAITING_NATIVE_REVIEW"
    assert state["steps"][0]["status"] == "AWAITING_NATIVE_REVIEW"
    assert state["steps"][0]["native_turn_id"] == turn_id
    assert state["steps"][0]["native_review_id"] is None


def test_native_turn_failure_is_a_plain_failed_step(coord_env, monkeypatch):
    plan_id, turn_id = _launch_native_step(coord_env, monkeypatch)
    dev_sessions.update_turn_status(turn_id, "FAILED", error_code="APP_SERVER_ERROR", error_message="boom")
    state = _wait(plan_id, {"FAILED"})
    assert state["status"] == "FAILED"
    assert state["error_code"] == "APP_SERVER_ERROR"
    assert state["steps"][0]["status"] == "FAILED"


def test_native_turn_outcome_unknown_becomes_result_uncertain(coord_env, monkeypatch):
    plan_id, turn_id = _launch_native_step(coord_env, monkeypatch)
    dev_sessions.update_turn_status(turn_id, "FAILED", error_code="TURN_OUTCOME_UNKNOWN", error_message="orphaned")
    state = _wait(plan_id, {"RESULT_UNCERTAIN"})
    assert state["status"] == "RESULT_UNCERTAIN"
    assert state["steps"][0]["status"] == "RESULT_UNCERTAIN"


def test_interrupting_a_running_native_step_cancels_its_turn(coord_env, monkeypatch):
    plan_id, turn_id = _launch_native_step(coord_env, monkeypatch)
    calls = []
    monkeypatch.setattr("nexus.dev.executor.interrupt_dev_turn", lambda session_id, tid: calls.append((session_id, tid)))
    coordinator.interrupt(plan_id)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not calls:
        time.sleep(0.02)
    assert calls == [("DEV-API", turn_id)]
    dev_sessions.update_turn_status(turn_id, "CANCELLED", error_code="INTERRUPTED")
    state = _wait(plan_id, {"INTERRUPTED"})
    assert state["status"] == "INTERRUPTED"
    assert state["steps"][0]["status"] == "INTERRUPTED"


def test_bind_native_review_requires_a_matching_pending_step(coord_env, monkeypatch):
    plan_id, turn_id = _launch_native_step(coord_env, monkeypatch)
    dev_sessions.update_turn_status(turn_id, "COMPLETED", response_text="done")
    _wait(plan_id, {"BLOCKED"})
    review = _save_review("DEV-API", _review_id("ok-1"))
    with pytest.raises(DevServiceError) as error:
        coordinator.bind_native_review(plan_id, "tests", review["review_id"])
    assert error.value.code == "NATIVE_REVIEW_NOT_AWAITED"


def test_bind_native_review_rejects_a_review_from_another_session(coord_env, monkeypatch):
    plan_id, turn_id = _launch_native_step(coord_env, monkeypatch)
    dev_sessions.update_turn_status(turn_id, "COMPLETED", response_text="done")
    _wait(plan_id, {"BLOCKED"})
    review = _save_review("DEV-TESTS", _review_id("wrong-session"))
    with pytest.raises(DevServiceError) as error:
        coordinator.bind_native_review(plan_id, "api", review["review_id"])
    assert error.value.code == "REVIEW_NOT_FOUND"


def test_bind_native_review_rejects_incomplete_snapshot(coord_env, monkeypatch):
    plan_id, turn_id = _launch_native_step(coord_env, monkeypatch)
    dev_sessions.update_turn_status(turn_id, "COMPLETED", response_text="done")
    _wait(plan_id, {"BLOCKED"})
    review = _save_review("DEV-API", _review_id("not-ready"), approval_ready=False)
    with pytest.raises(DevServiceError) as error:
        coordinator.bind_native_review(plan_id, "api", review["review_id"])
    assert error.value.code == "NATIVE_REVIEW_INCOMPLETE"


def test_bind_native_review_rejects_a_superseded_review(coord_env, monkeypatch):
    plan_id, turn_id = _launch_native_step(coord_env, monkeypatch)
    dev_sessions.update_turn_status(turn_id, "COMPLETED", response_text="done")
    _wait(plan_id, {"BLOCKED"})
    stale = _save_review("DEV-API", _review_id("first"))
    _save_review("DEV-API", _review_id("second"))
    with pytest.raises(DevServiceError) as error:
        coordinator.bind_native_review(plan_id, "api", stale["review_id"])
    assert error.value.code == "NATIVE_REVIEW_STALE"


def test_bind_native_review_rejects_a_rejected_review(coord_env, monkeypatch):
    plan_id, turn_id = _launch_native_step(coord_env, monkeypatch)
    dev_sessions.update_turn_status(turn_id, "COMPLETED", response_text="done")
    _wait(plan_id, {"BLOCKED"})
    review = _save_review("DEV-API", _review_id("rejected"), status="REJECTED")
    with pytest.raises(DevServiceError) as error:
        coordinator.bind_native_review(plan_id, "api", review["review_id"])
    assert error.value.code == "NATIVE_REVIEW_NOT_READY"


def test_bind_native_review_rejects_when_the_turn_is_not_durably_completed(coord_env, monkeypatch):
    """Defense in depth: even if a step were ever marked AWAITING_NATIVE_REVIEW
    without its turn genuinely COMPLETED (a corrupted or raced state), binding
    must still refuse rather than trust the step status alone."""
    _set_backend("DEV-API", "codex_native")
    _authorize_with_native_api(coord_env)
    plan_id = coord_env["plan_id"]
    token = executions.claim_execution(plan_id)
    with executions.coordinated_launch(plan_id, "api", token):
        executions.update_step(plan_id, "api", "STARTING", owner_token=token)
        turn_id = dev_sessions.create_turn("DEV-API", "task", requested_model="free/model")
    executions.update_step(plan_id, "api", "AWAITING_NATIVE_REVIEW", native_turn_id=turn_id, owner_token=token)
    executions.update_execution(plan_id, "BLOCKED", current_index=0, error_code="AWAITING_NATIVE_REVIEW", owner_token=token)
    review = _save_review("DEV-API", _review_id("premature"))
    with pytest.raises(DevServiceError) as error:
        coordinator.bind_native_review(plan_id, "api", review["review_id"])
    assert error.value.code == "NATIVE_REVIEW_TURN_NOT_READY"


def test_bind_native_review_resumes_and_still_refuses_to_hand_off_into_omniroute(coord_env, monkeypatch):
    """coord_env's plan has a second, OmniRoute "tests" step depending on
    "api". Binding must still advance the sequence (not just mark the step
    done and stop) -- but the second step must stay refused by the same
    NATIVE_HANDOFF_NOT_SUPPORTED guard, never silently proceed as if "api"
    had produced no changes to hand off."""
    plan_id, turn_id = _launch_native_step(coord_env, monkeypatch)
    dev_sessions.update_turn_status(turn_id, "COMPLETED", response_text="done")
    _wait(plan_id, {"BLOCKED"})
    review = _save_review("DEV-API", _review_id("final"))
    result = coordinator.bind_native_review(plan_id, "api", review["review_id"])
    assert result["steps"][0]["status"] == "READY_FOR_REVIEW"
    assert result["steps"][0]["native_review_id"] == review["review_id"]
    final = _wait(plan_id, {"BLOCKED"})
    assert final["status"] == "BLOCKED"
    assert final["error_code"] == "NATIVE_HANDOFF_NOT_SUPPORTED"
    assert final["steps"][1]["status"] == "BLOCKED"


def test_single_native_agent_plan_reaches_awaiting_review_after_a_bound_review(coord_env, monkeypatch):
    plan_id = _single_native_plan(coord_env)
    _allow_review_lookup(monkeypatch, "DEV-SOLO")
    turns = []
    monkeypatch.setattr("nexus.dev.executor.run_dev_turn", _fake_run_dev_turn(turns))
    coordinator.authorize_execution(plan_id, agent_keys=["solo"], test_profiles={},
                                    max_turns_per_agent=1, mission_timeout_seconds=60)
    coordinator.start(plan_id)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not turns:
        time.sleep(0.02)
    assert turns, "the native turn was never launched"
    turn_id = turns[0]
    dev_sessions.update_turn_status(turn_id, "COMPLETED", response_text="done")
    _wait(plan_id, {"BLOCKED"})
    review = _save_review("DEV-SOLO", _review_id("solo-final"))
    result = coordinator.bind_native_review(plan_id, "solo", review["review_id"])
    assert result["steps"][0]["status"] == "READY_FOR_REVIEW"
    final = _wait(plan_id, {"AWAITING_REVIEW"})
    assert final["status"] == "AWAITING_REVIEW"
    assert final["result"]["human_approval_required"] is True
    assert final["result"]["integration_applied"] is False


def test_bind_native_review_cannot_be_replayed(coord_env, monkeypatch):
    plan_id, turn_id = _launch_native_step(coord_env, monkeypatch)
    dev_sessions.update_turn_status(turn_id, "COMPLETED", response_text="done")
    _wait(plan_id, {"BLOCKED"})
    first = _save_review("DEV-API", _review_id("bound"))
    coordinator.bind_native_review(plan_id, "api", first["review_id"])
    _wait(plan_id, {"AWAITING_REVIEW"})
    second = _save_review("DEV-API", _review_id("replay-attempt"))
    with pytest.raises(DevServiceError) as error:
        coordinator.bind_native_review(plan_id, "api", second["review_id"])
    assert error.value.code == "NATIVE_REVIEW_NOT_AWAITED"


def test_bind_native_review_is_fenced_against_a_concurrent_recovery(coord_env, monkeypatch):
    plan_id, turn_id = _launch_native_step(coord_env, monkeypatch)
    dev_sessions.update_turn_status(turn_id, "COMPLETED", response_text="done")
    _wait(plan_id, {"BLOCKED"})
    review = _save_review("DEV-API", _review_id("raced"))
    # Simulate another process having already reclaimed this plan between
    # the human's read of its state and this call.
    with database.get_connection() as connection:
        connection.execute("UPDATE workspace_plan_executions SET status='RUNNING' WHERE plan_id=?", (plan_id,))
    with pytest.raises(DevServiceError) as error:
        coordinator.bind_native_review(plan_id, "api", review["review_id"])
    assert error.value.code == "NATIVE_REVIEW_NOT_AWAITED"


def test_crash_before_native_turn_id_is_recorded_is_uncertain_not_replayed(coord_env):
    _set_backend("DEV-API", "codex_native")
    _authorize_with_native_api(coord_env)
    plan_id = coord_env["plan_id"]
    token = executions.claim_execution(plan_id)
    # The launch barrier was written, but the process died before the
    # turn id came back -- exactly like the OmniRoute mission case.
    executions.update_step(plan_id, "api", "STARTING", owner_token=token)
    coordinator._run(plan_id, token)
    state = coordinator.state(plan_id)
    assert state["status"] == "RESULT_UNCERTAIN"
    assert state["error_code"] == "COORDINATOR_START_INTERRUPTED"
    assert state["steps"][0]["status"] == "RESULT_UNCERTAIN"
    assert dev_sessions.get_active_turn_for_session("DEV-API") is None, "a crash must never be replayed as a new turn"


def test_native_predecessor_blocks_the_next_step_instead_of_skipping_handoff(coord_env):
    """_changed_files(_mission_for(previous)) returns [] for a native
    predecessor (it has no mission_id to look up) -- which would otherwise
    silently skip handoff verification entirely. This must be refused."""
    _set_backend("DEV-API", "codex_native")
    _authorize_with_native_api(coord_env)
    plan_id = coord_env["plan_id"]
    token = executions.claim_execution(plan_id)
    now = datetime.now(UTC).isoformat()
    with database.get_connection() as connection:
        connection.execute(
            """UPDATE workspace_plan_execution_steps
               SET status='READY_FOR_REVIEW', native_turn_id=?, native_review_id=?, finished_at=?
               WHERE plan_id=? AND agent_key='api'""",
            ("DEVTURN-SIM", _review_id("simulated-bound"), now, plan_id),
        )
    coordinator._run(plan_id, token)
    state = coordinator.state(plan_id)
    assert state["status"] == "BLOCKED"
    assert state["error_code"] == "NATIVE_HANDOFF_NOT_SUPPORTED"
    assert state["steps"][1]["status"] == "BLOCKED"
    assert state["steps"][1]["error_code"] == "NATIVE_HANDOFF_NOT_SUPPORTED"
