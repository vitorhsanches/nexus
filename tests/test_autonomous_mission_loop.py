from __future__ import annotations

import sys
import threading
import time
from unittest.mock import Mock

import pytest

from nexus.dev import integration, mission_runner
from nexus.dev.service import DevServiceError
from nexus.registry import database, dev_missions


@pytest.fixture
def mission_env(tmp_path, monkeypatch):
    original_db = database.DATABASE_PATH
    original_storage = database.STORAGE_DIR
    database.STORAGE_DIR = tmp_path / "storage"
    database.DATABASE_PATH = database.STORAGE_DIR / "nexus.db"
    database.initialize_database()
    monkeypatch.setenv("NEXUS_DEV_EXECUTION_ENABLED", "1")
    monkeypatch.setattr(
        "nexus.dev.models.list_dev_models",
        lambda: {"models": [{"id": "model-a", "supported_efforts": ["low", "high"]}],
                 "default_model": None, "catalog_error": None},
    )
    monkeypatch.setattr(
        mission_runner,
        "resolve_test_profile",
        lambda name, _project_id: None if name == "missing-profile" else ([sys.executable, "-m", "pytest", "-q"], 30),
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    with database.get_connection() as connection:
        connection.execute(
            "INSERT INTO projects (id, name, path, enabled, dev_enabled) VALUES ('demo','Demo',?,1,1)",
            (str(tmp_path),),
        )
        connection.execute(
            """INSERT INTO dev_sessions
               (id, project_id, branch, worktree_path, base_sha, requested_model, status, created_at, updated_at)
               VALUES ('DEV-1','demo','nexus/dev/DEV-1',?,'abc','model-a','ACTIVE',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
            (str(worktree),),
        )
    monkeypatch.setattr(
        mission_runner,
        "_require_session_worktree",
        lambda _session_id: ({"requested_model": "model-a", "project_id": "demo"}, str(worktree)),
    )
    monkeypatch.setattr(mission_runner.executor, "changed_files", lambda _sid: [{"path": "changed.py", "status": "modified"}])
    monkeypatch.setattr(mission_runner.executor, "review_diff", lambda _sid: {"diff": "safe diff"})
    yield tmp_path
    for thread in threading.enumerate():
        if thread.name.startswith("nexus-mission-") and thread is not threading.current_thread():
            thread.join(timeout=5)
    mission_runner._active_missions.clear()
    database.DATABASE_PATH = original_db
    database.STORAGE_DIR = original_storage


def _completed_turns(monkeypatch):
    calls = []

    def run(session_id, prompt, *, model=None):
        turn_id = f"TURN-{len(calls) + 1}"
        calls.append((session_id, prompt, model, turn_id))
        return {"turn_id": turn_id, "status": "PENDING"}

    monkeypatch.setattr(mission_runner.executor, "run_dev_turn", run)
    monkeypatch.setattr(
        mission_runner.executor,
        "get_turn_detail",
        lambda session_id, turn_id: {"turn_id": turn_id, "status": "COMPLETED", "error_message": None},
    )
    return calls


def _start(**overrides):
    args = {
        "model": "model-a",
        "allowed_models": ["model-a"],
        "test_profile": "nexus-f36b",
        "timeout_seconds": 30,
    }
    args.update(overrides)
    return mission_runner.start_mission("DEV-1", "Implement the bounded change", **args)


def _wait(mission_id, expected, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = dev_missions.get_mission(mission_id)
        if row["status"] in expected:
            return mission_runner.get_mission("DEV-1", mission_id)
        time.sleep(0.01)
    raise AssertionError(f"mission did not reach {expected}")


def test_success_reaches_ready_for_human_review(mission_env, monkeypatch):
    calls = _completed_turns(monkeypatch)
    monkeypatch.setattr(mission_runner, "_execute_test_profile", lambda *args: {"status": "PASSED", "stdout": "ok", "stderr": ""})
    started = _start()
    final = _wait(started["mission_id"], {"READY_FOR_REVIEW"})
    assert len(calls) == 1
    assert final["attempt_count"] == 1
    assert final["result"]["human_approval_required"] is True
    assert final["result"]["integration_applied"] is False


def test_failed_test_gets_one_repair_then_passes(mission_env, monkeypatch):
    calls = _completed_turns(monkeypatch)
    outcomes = iter([
        {"status": "FAILED", "stdout": "one failed", "stderr": ""},
        {"status": "PASSED", "stdout": "all passed", "stderr": ""},
    ])
    monkeypatch.setattr(mission_runner, "_execute_test_profile", lambda *args: next(outcomes))
    final = _wait(_start()["mission_id"], {"READY_FOR_REVIEW"})
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0] == "DEV-1"
    assert final["attempt_count"] == 2


def test_persistent_failure_stops_after_single_repair(mission_env, monkeypatch):
    calls = _completed_turns(monkeypatch)
    monkeypatch.setattr(mission_runner, "_execute_test_profile", lambda *args: {"status": "FAILED", "stdout": "failed", "stderr": ""})
    final = _wait(_start()["mission_id"], {"FAILED"})
    assert len(calls) == 2
    assert final["error_code"] == "TESTS_FAILED_AFTER_REPAIR"


def test_unauthorized_session_is_rejected_before_persistence(mission_env, monkeypatch):
    monkeypatch.setattr(
        mission_runner,
        "_require_session_worktree",
        lambda _sid: (_ for _ in ()).throw(DevServiceError("DEV_MODE_DISABLED", "denied", http_status=403)),
    )
    with pytest.raises(DevServiceError) as error:
        _start()
    assert error.value.code == "DEV_MODE_DISABLED"
    assert dev_missions.list_missions("DEV-1") == []


def test_missing_test_profile_is_persisted_blocked(mission_env):
    result = _start(test_profile="missing-profile")
    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "TEST_PROFILE_NOT_FOUND"


def test_concurrent_missions_for_same_session_are_refused(mission_env, monkeypatch):
    _completed_turns(monkeypatch)
    release = threading.Event()

    def blocked_test(*args):
        release.wait(timeout=5)
        return {"status": "PASSED", "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(mission_runner, "_execute_test_profile", blocked_test)
    first = _start()
    with pytest.raises(DevServiceError) as error:
        _start()
    assert error.value.code == "MISSION_BUSY"
    release.set()
    _wait(first["mission_id"], {"READY_FOR_REVIEW"})


def test_interrupt_and_timeout_are_terminal(mission_env, monkeypatch):
    stop_seen = threading.Event()
    monkeypatch.setattr(mission_runner.executor, "run_dev_turn", lambda *args, **kwargs: {"turn_id": "TURN-WAIT", "status": "RUNNING"})
    monkeypatch.setattr(mission_runner.executor, "get_turn_detail", lambda *args: {"turn_id": "TURN-WAIT", "status": "RUNNING", "error_message": None})
    monkeypatch.setattr(mission_runner.executor, "interrupt_dev_turn", lambda *args: stop_seen.set() or {"status": "INTERRUPT_REQUESTED"})
    started = _start()
    mission_runner.interrupt_mission("DEV-1", started["mission_id"])
    final = _wait(started["mission_id"], {"CANCELLED"})
    assert stop_seen.is_set()
    assert final["error_code"] == "MISSION_INTERRUPTED"

    _completed_turns(monkeypatch)
    monkeypatch.setattr(mission_runner, "_wait_for_turn", lambda *args: (_ for _ in ()).throw(TimeoutError("expired")))
    timed = _wait(_start()["mission_id"], {"FAILED"})
    assert timed["error_code"] == "MISSION_TIMEOUT"


def test_restart_marks_uncertain_outcome_without_replay(mission_env):
    mission_id = dev_missions.create_mission("DEV-1", "uncertain", requested_model="model-a", allowed_models=["model-a"], test_profile="nexus-f36b")
    dev_missions.update_status(mission_id, "RUNNING")
    assert dev_missions.recover_orphaned_missions(lambda *_args: True) == [mission_id]
    assert dev_missions.get_mission(mission_id)["status"] == "OUTCOME_UNKNOWN"


def test_interrupt_request_is_durable_when_owner_is_another_process(mission_env):
    mission_id = dev_missions.create_mission("DEV-1", "remote", requested_model="model-a", allowed_models=["model-a"], test_profile="nexus-f36b")
    dev_missions.update_status(mission_id, "RUNNING")
    response = mission_runner.interrupt_mission("DEV-1", mission_id)
    assert response["status"] == "INTERRUPT_REQUESTED"
    assert dev_missions.get_mission(mission_id)["interrupt_requested"] == 1
    assert mission_runner._stop_requested(mission_id, threading.Event()) is True


def test_subprocess_launch_failure_preserves_test_record(mission_env, monkeypatch):
    monkeypatch.setattr(mission_runner.subprocess, "Popen", Mock(side_effect=OSError("launch failed token=secret-value")))
    mission_id = dev_missions.create_mission("DEV-1", "test", requested_model="model-a", allowed_models=["model-a"], test_profile="nexus-f36b")
    with pytest.raises(DevServiceError) as error:
        mission_runner._execute_test_profile(
            mission_id, 1, "nexus-f36b", "demo", str(mission_env),
            threading.Event(), time.monotonic() + 30,
        )
    assert error.value.code == "TEST_PROCESS_FAILED"
    rows = dev_missions.list_test_runs(mission_id)
    assert len(rows) == 1 and rows[0]["status"] == "FAILED"
    assert "secret-value" not in rows[0]["stderr_text"]


def test_ready_result_never_applies_integration(mission_env, monkeypatch):
    _completed_turns(monkeypatch)
    monkeypatch.setattr(mission_runner, "_execute_test_profile", lambda *args: {"status": "PASSED", "stdout": "ok", "stderr": ""})
    apply_spy = Mock()
    monkeypatch.setattr("nexus.dev.integration.apply_review", apply_spy)
    final = _wait(_start()["mission_id"], {"READY_FOR_REVIEW"})
    apply_spy.assert_not_called()
    assert final["result"]["integration_applied"] is False


def test_active_mission_blocks_creation_of_integration_review(mission_env, monkeypatch):
    mission_id = dev_missions.create_mission("DEV-1", "active", requested_model="model-a", allowed_models=["model-a"], test_profile="nexus-f36b")
    dev_missions.update_status(mission_id, "TESTING")
    monkeypatch.setattr(integration, "_authorized_session", lambda _sid: ({"id": "DEV-1"}, "project", mission_env))
    monkeypatch.setattr(integration.dev_sessions, "get_active_turn_for_session", lambda _sid: None)
    snapshot = Mock()
    monkeypatch.setattr(integration, "_snapshot", snapshot)
    with pytest.raises(DevServiceError) as error:
        integration.create_review("DEV-1")
    assert error.value.code == "MISSION_ACTIVE"
    snapshot.assert_not_called()


def test_mission_effort_is_exact_and_survives_restart(mission_env):
    with pytest.raises(DevServiceError) as error:
        _start(effort="ultra", test_profile="missing-profile")
    assert error.value.code == "EFFORT_NOT_SUPPORTED"
    created = _start(effort="high", test_profile="missing-profile")
    assert created["requested_effort"] == "high"
    database.initialize_database()
    assert mission_runner.get_mission("DEV-1", created["mission_id"])["requested_effort"] == "high"
