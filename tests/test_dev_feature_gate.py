"""Tests for nexus.dev.feature_gate and its enforcement in
nexus.dev.executor.run_dev_turn (F2.2 Phase 1).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import nexus.registry.database as database
import nexus.registry.projects as projects
import pytest
from nexus.dev import executor, feature_gate, service


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
    monkeypatch.delenv(feature_gate.ENV_VAR, raising=False)
    try:
        yield tmp_path
    finally:
        database.DATABASE_PATH = original_db_path
        database.STORAGE_DIR = original_storage_dir
        projects.PROJECTS_FILE = original_projects_file


def _run(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _active_session(tmp_path, project_id="demo"):
    import os

    workspace_path = tmp_path / project_id
    workspace_path.mkdir(parents=True, exist_ok=True)
    _run(["init", "-q"], cwd=str(workspace_path))
    _run(["config", "user.email", "gate-test@example.com"], cwd=str(workspace_path))
    _run(["config", "user.name", "gate-test"], cwd=str(workspace_path))
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


def test_execution_enabled_is_false_by_default(monkeypatch):
    monkeypatch.delenv(feature_gate.ENV_VAR, raising=False)
    assert feature_gate.execution_enabled() is False


def test_execution_enabled_requires_the_exact_value_one(monkeypatch):
    for value in ("true", "True", "yes", "on", "2", ""):
        monkeypatch.setenv(feature_gate.ENV_VAR, value)
        assert feature_gate.execution_enabled() is False, f"{value!r} must not be treated as enabled"
    monkeypatch.setenv(feature_gate.ENV_VAR, "1")
    assert feature_gate.execution_enabled() is True


def test_run_dev_turn_is_blocked_by_default_even_for_a_fully_authorized_session(isolated_registry, monkeypatch):
    monkeypatch.delenv(feature_gate.ENV_VAR, raising=False)
    session = _active_session(isolated_registry)

    with pytest.raises(service.DevServiceError) as error:
        executor.run_dev_turn(session["session_id"], "do something")

    assert error.value.code == "DEV_EXECUTION_DISABLED"
    # No turn row was created for a request the gate itself rejected.
    from nexus.registry import dev_sessions

    assert dev_sessions.list_turns(session["session_id"]) == []


def test_run_dev_turn_succeeds_once_the_gate_is_explicitly_enabled(isolated_registry, monkeypatch):
    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

        def thread_start(self, **kwargs):
            return {"thread": {"id": "T"}, "modelProvider": "omniroute"}

        def thread_resume(self, thread_id, **kwargs):
            return {"thread": {"id": thread_id}}

        def send_message_and_wait(self, *a, **k):
            return "ok"

        def stop(self):
            pass

    monkeypatch.setattr(executor, "AppServerClient", _FakeClient)
    session = _active_session(isolated_registry)
    monkeypatch.setenv(feature_gate.ENV_VAR, "1")

    result = executor.run_dev_turn(session["session_id"], "do something")
    assert result["status"] in ("PENDING", "RUNNING")
