"""Service-level tests for nexus.dev.service (F1.2).

Exercises the full create_dev_session() flow against real, disposable Git
repositories (never mocked) and an isolated SQLite database, with the
dedicated worktree root also pointed at a disposable location (never the
real %LOCALAPPDATA%\\Nexus\\DevWorktrees).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import nexus.registry.database as database
import nexus.registry.projects as projects
import pytest
from nexus.dev import service
from nexus.registry import dev_sessions


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
    try:
        yield tmp_path
    finally:
        database.DATABASE_PATH = original_db_path
        database.STORAGE_DIR = original_storage_dir
        projects.PROJECTS_FILE = original_projects_file


def _run(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _run(["init", "-q"], cwd=str(path))
    _run(["config", "user.email", "dev-service-test@example.com"], cwd=str(path))
    _run(["config", "user.name", "dev-service-test"], cwd=str(path))
    (path / "README.md").write_text("placeholder\n", encoding="utf-8")
    _run(["add", "-A"], cwd=str(path))
    _run(["commit", "-q", "-m", "init"], cwd=str(path))
    return _run(["rev-parse", "HEAD"], cwd=str(path))


def _register_project(tmp_path, project_id="demo", enabled=True):
    workspace_path = tmp_path / project_id
    import os

    head_sha = _init_repo(workspace_path)
    resolved = os.path.realpath(str(workspace_path))
    projects.PROJECTS_FILE.write_text(
        json.dumps(
            {
                "projects": [
                    {"id": project_id, "name": project_id.title(), "path": resolved, "aliases": [], "enabled": enabled}
                ]
            }
        ),
        encoding="utf-8",
    )
    projects.sync_projects()
    return resolved, head_sha


# -- authorization gating ----------------------------------------------------


def test_create_session_is_blocked_for_an_unauthorized_project_by_default(isolated_registry):
    _register_project(isolated_registry)
    with pytest.raises(service.DevServiceError) as error:
        service.create_dev_session("demo")
    assert error.value.code == "DEV_MODE_DISABLED"
    assert dev_sessions.list_sessions("demo") == [], "no session row should be reserved for an unauthorized project"


def test_create_session_is_blocked_for_an_unknown_project(isolated_registry):
    with pytest.raises(service.DevServiceError) as error:
        service.create_dev_session("ghost")
    assert error.value.code == "PROJECT_NOT_FOUND"


def test_create_session_is_blocked_for_a_disabled_project_even_with_dev_enabled(isolated_registry):
    _register_project(isolated_registry, enabled=False)
    projects.set_dev_enabled("demo", True)
    with pytest.raises(service.DevServiceError) as error:
        service.create_dev_session("demo")
    assert error.value.code == "PROJECT_DISABLED"


def test_dev_enabled_on_one_project_never_authorizes_another(isolated_registry):
    _register_project(isolated_registry, "alpha")
    _register_project(isolated_registry, "beta")
    projects.set_dev_enabled("alpha", True)

    with pytest.raises(service.DevServiceError) as error:
        service.create_dev_session("beta")
    assert error.value.code == "DEV_MODE_DISABLED"


# -- authorized creation ------------------------------------------------------


def test_create_session_succeeds_for_an_authorized_project(isolated_registry):
    workspace_path, head_sha = _register_project(isolated_registry)
    projects.set_dev_enabled("demo", True)

    result = service.create_dev_session("demo", requested_model="cc/claude-sonnet-5-low")

    assert result["status"] == dev_sessions.STATUS_ACTIVE
    assert result["project_id"] == "demo"
    assert result["base_sha"] == head_sha
    assert result["branch"].startswith("nexus/dev/")
    assert result["requested_model"] == "cc/claude-sonnet-5-low"
    assert Path(result["worktree_path"]).is_dir()
    # Isolated from the project's own directory.
    assert Path(result["worktree_path"]).resolve() != Path(workspace_path).resolve()


def test_created_worktree_is_on_the_persisted_branch_and_sha(isolated_registry):
    _register_project(isolated_registry)
    projects.set_dev_enabled("demo", True)
    result = service.create_dev_session("demo")

    current_branch = _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=result["worktree_path"])
    current_sha = _run(["rev-parse", "HEAD"], cwd=result["worktree_path"])
    assert current_branch == result["branch"]
    assert current_sha == result["base_sha"]


def test_revoking_authorization_blocks_the_next_session_immediately(isolated_registry):
    _register_project(isolated_registry)
    projects.set_dev_enabled("demo", True)
    service.create_dev_session("demo")  # first session succeeds

    projects.set_dev_enabled("demo", False)

    with pytest.raises(service.DevServiceError) as error:
        service.create_dev_session("demo")
    assert error.value.code == "DEV_MODE_DISABLED"


# -- isolation between sessions -----------------------------------------------


def test_two_sessions_for_the_same_project_get_independent_worktrees(isolated_registry):
    _register_project(isolated_registry)
    projects.set_dev_enabled("demo", True)

    first = service.create_dev_session("demo")
    second = service.create_dev_session("demo")

    assert first["session_id"] != second["session_id"]
    assert first["branch"] != second["branch"]
    assert Path(first["worktree_path"]).resolve() != Path(second["worktree_path"]).resolve()
    (Path(first["worktree_path"]) / "only_in_first.txt").write_text("x", encoding="utf-8")
    assert not (Path(second["worktree_path"]) / "only_in_first.txt").exists()

    listed = {row["session_id"] for row in service.list_sessions("demo")}
    assert listed == {first["session_id"], second["session_id"]}


# -- partial failure preserves resources --------------------------------------


def test_a_worktree_creation_failure_is_recorded_without_losing_the_project(isolated_registry, monkeypatch):
    workspace_path, _ = _register_project(isolated_registry)
    projects.set_dev_enabled("demo", True)
    first = service.create_dev_session("demo")

    # Force create_dev_worktree() to fail with a real
    # nexus.dev.worktree.WorktreeCollisionError: pre-create, in the real
    # project repo, the exact branch name the next reserved session_id
    # will compute, then pin new_session_id() to that same id so
    # create_dev_session() deterministically collides with it.
    from nexus.dev.worktree import branch_name_for_session

    doomed_id = dev_sessions.new_session_id()
    collide_branch = branch_name_for_session(doomed_id)
    subprocess.run(["git", "branch", collide_branch], cwd=workspace_path, check=True)
    monkeypatch.setattr(dev_sessions, "new_session_id", lambda: doomed_id)

    with pytest.raises(service.DevServiceError) as error:
        service.create_dev_session("demo")

    assert error.value.code == "WORKTREE_CREATE_FAILED"
    row = dev_sessions.get_session(doomed_id)
    assert row["status"] == dev_sessions.STATUS_FAILED
    assert row["error_code"] == "WorktreeCollisionError"
    assert row["error_message"]

    # The earlier, unrelated session must be completely unaffected.
    assert dev_sessions.get_session(first["session_id"])["status"] == dev_sessions.STATUS_ACTIVE
    assert Path(first["worktree_path"]).is_dir()


def test_get_session_detail_reports_not_found_for_an_unknown_session(isolated_registry):
    with pytest.raises(service.DevServiceError) as error:
        service.get_session_detail("DEV-NOPE0000")
    assert error.value.code == "SESSION_NOT_FOUND"


# -- recovery integration ------------------------------------------------------


def test_recover_orphaned_sessions_uses_real_process_liveness(isolated_registry):
    _register_project(isolated_registry)
    projects.set_dev_enabled("demo", True)
    session_id = dev_sessions.new_session_id()
    dev_sessions.reserve_session(session_id, "demo", f"nexus/dev/{session_id}")
    with database.get_connection() as connection:
        connection.execute(
            "UPDATE dev_sessions SET owner_pid = 999999999 WHERE id = ?", (session_id,)
        )

    recovered = service.recover_orphaned_sessions()

    assert session_id in recovered
    assert dev_sessions.get_session(session_id)["status"] == dev_sessions.STATUS_INTERRUPTED


def test_recover_orphaned_sessions_leaves_this_own_process_alone(isolated_registry):
    import os
    import socket

    _register_project(isolated_registry)
    session_id = dev_sessions.new_session_id()
    dev_sessions.reserve_session(session_id, "demo", f"nexus/dev/{session_id}")
    with database.get_connection() as connection:
        connection.execute(
            "UPDATE dev_sessions SET owner_host = ?, owner_pid = ? WHERE id = ?",
            (socket.gethostname(), os.getpid(), session_id),
        )

    recovered = service.recover_orphaned_sessions()

    assert session_id not in recovered
    assert dev_sessions.get_session(session_id)["status"] == dev_sessions.STATUS_CREATING
