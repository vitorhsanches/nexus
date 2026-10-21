"""Targeted P2B.1 coordination tests with simulated Development missions."""

from __future__ import annotations

import json
import time

import pytest

from nexus.dev.service import DevServiceError
from nexus.registry import database, dev_missions
from nexus.registry import workspace as store
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
