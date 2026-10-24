"""Registry-level tests for nexus.registry.dev_sessions and the F1.2
database migration (new dev_sessions table + projects.dev_enabled column).

Isolated from the production nexus.db using the same
STORAGE_DIR/DATABASE_PATH/PROJECTS_FILE monkeypatch pattern as
tests/test_chat_sessions_registry.py, so these never touch real Nexus
state.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import nexus.registry.database as database
import nexus.registry.projects as projects
import pytest
from nexus.registry import chat_sessions, dev_sessions


@pytest.fixture
def isolated_registry(tmp_path):
    original_db_path = database.DATABASE_PATH
    original_storage_dir = database.STORAGE_DIR
    original_projects_file = projects.PROJECTS_FILE
    database.STORAGE_DIR = tmp_path / "storage"
    database.DATABASE_PATH = database.STORAGE_DIR / "nexus.db"
    projects.PROJECTS_FILE = tmp_path / "projects.json"
    database.initialize_database()
    try:
        yield tmp_path
    finally:
        database.DATABASE_PATH = original_db_path
        database.STORAGE_DIR = original_storage_dir
        projects.PROJECTS_FILE = original_projects_file


def _register_project(tmp_path, project_id="demo", workspace_path=None, enabled=True):
    workspace_path = workspace_path or str(tmp_path / "workspace")
    Path(workspace_path).mkdir(parents=True, exist_ok=True)
    projects.PROJECTS_FILE.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "id": project_id,
                        "name": project_id.title(),
                        "path": workspace_path,
                        "aliases": [project_id],
                        "enabled": enabled,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    projects.sync_projects()
    return workspace_path


# -- migration ----------------------------------------------------------


def test_migration_creates_dev_sessions_table_and_dev_enabled_column(isolated_registry):
    with database.get_connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        project_columns = {row["name"] for row in connection.execute("PRAGMA table_info(projects)")}
    assert "dev_sessions" in tables
    assert "dev_enabled" in project_columns


def test_migration_is_idempotent_and_safe_to_run_twice(isolated_registry):
    # Running it again (e.g. on every Nexus startup) must never raise and
    # must never reset existing data.
    _register_project(isolated_registry)
    projects.set_dev_enabled("demo", True)
    database.initialize_database()
    assert projects.get_dev_enabled("demo") is True


def test_legacy_projects_table_without_dev_enabled_is_migrated(isolated_registry):
    # Simulate a database created before F1.2: drop the column by
    # rebuilding the table without it, then confirm initialize_database()
    # adds it back with every existing row defaulting to disabled.
    _register_project(isolated_registry)
    with database.get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE projects_legacy (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,
                aliases_json TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO projects_legacy (id, name, path, aliases_json, enabled)
                SELECT id, name, path, aliases_json, enabled FROM projects;
            DROP TABLE projects;
            ALTER TABLE projects_legacy RENAME TO projects;
            """
        )
    columns_before = {row["name"] for row in database.get_connection().execute("PRAGMA table_info(projects)")}
    assert "dev_enabled" not in columns_before

    database.initialize_database()

    assert projects.get_dev_enabled("demo") is False


# -- default authorization ------------------------------------------------


def test_dev_enabled_defaults_false_for_a_freshly_registered_project(isolated_registry):
    _register_project(isolated_registry)
    assert projects.get_dev_enabled("demo") is False


def test_dev_enabled_defaults_false_for_an_unknown_project(isolated_registry):
    assert projects.get_dev_enabled("does-not-exist") is False


def test_resyncing_projects_json_never_changes_dev_enabled(isolated_registry):
    _register_project(isolated_registry)
    projects.set_dev_enabled("demo", True)
    # Re-registering (re-syncing) the same project from config must not
    # touch the Development Mode flag either way.
    _register_project(isolated_registry)
    assert projects.get_dev_enabled("demo") is True


# -- reservation / persistence --------------------------------------------


def test_reserve_and_mark_active_persists_worktree_branch_and_sha(isolated_registry):
    _register_project(isolated_registry)
    session_id = dev_sessions.new_session_id()
    branch = f"nexus/dev/{session_id.lower()}"
    dev_sessions.reserve_session(session_id, "demo", branch, requested_model="cc/claude-sonnet-5-low")

    row = dev_sessions.get_session(session_id)
    assert row["status"] == dev_sessions.STATUS_CREATING
    assert row["project_id"] == "demo"
    assert row["branch"] == branch
    assert row["requested_model"] == "cc/claude-sonnet-5-low"
    assert row["worktree_path"] is None
    assert row["base_sha"] is None

    dev_sessions.mark_active(session_id, worktree_path="/tmp/wt", base_sha="deadbeef")
    row = dev_sessions.get_session(session_id)
    assert row["status"] == dev_sessions.STATUS_ACTIVE
    assert row["worktree_path"] == "/tmp/wt"
    assert row["base_sha"] == "deadbeef"


def test_mark_failed_preserves_the_session_row_for_inspection(isolated_registry):
    _register_project(isolated_registry)
    session_id = dev_sessions.new_session_id()
    dev_sessions.reserve_session(session_id, "demo", f"nexus/dev/{session_id}")

    dev_sessions.mark_failed(session_id, error_code="WorktreeCollisionError", error_message="boom")

    row = dev_sessions.get_session(session_id)
    assert row["status"] == dev_sessions.STATUS_FAILED
    assert row["error_code"] == "WorktreeCollisionError"
    assert row["error_message"] == "boom"


# -- isolation between sessions and projects -------------------------------


def test_sessions_are_isolated_across_projects(isolated_registry):
    _register_project(isolated_registry, "alpha", str(isolated_registry / "alpha"))
    _register_project(isolated_registry, "beta", str(isolated_registry / "beta"))
    id_alpha = dev_sessions.new_session_id()
    dev_sessions.reserve_session(id_alpha, "alpha", f"nexus/dev/{id_alpha}")
    id_beta = dev_sessions.new_session_id()
    dev_sessions.reserve_session(id_beta, "beta", f"nexus/dev/{id_beta}")

    assert [row["id"] for row in dev_sessions.list_sessions("alpha")] == [id_alpha]
    assert [row["id"] for row in dev_sessions.list_sessions("beta")] == [id_beta]
    assert {row["id"] for row in dev_sessions.list_sessions()} == {id_alpha, id_beta}


def test_same_branch_name_is_independent_per_project(isolated_registry):
    _register_project(isolated_registry, "alpha", str(isolated_registry / "alpha"))
    _register_project(isolated_registry, "beta", str(isolated_registry / "beta"))
    # Same branch string, two different projects -- the unique index is
    # scoped to (project_id, branch), not branch alone.
    id_alpha = dev_sessions.new_session_id()
    dev_sessions.reserve_session(id_alpha, "alpha", "nexus/dev/shared-name")
    id_beta = dev_sessions.new_session_id()
    dev_sessions.reserve_session(id_beta, "beta", "nexus/dev/shared-name")
    assert dev_sessions.get_session(id_alpha)["project_id"] == "alpha"
    assert dev_sessions.get_session(id_beta)["project_id"] == "beta"


def test_duplicate_branch_within_the_same_project_is_refused(isolated_registry):
    _register_project(isolated_registry)
    first = dev_sessions.new_session_id()
    dev_sessions.reserve_session(first, "demo", "nexus/dev/dup")
    with pytest.raises(dev_sessions.DevSessionBranchBusyError):
        dev_sessions.reserve_session(dev_sessions.new_session_id(), "demo", "nexus/dev/dup")


# -- state transitions ------------------------------------------------------


def test_valid_transition_creating_to_active_succeeds(isolated_registry):
    _register_project(isolated_registry)
    session_id = dev_sessions.new_session_id()
    dev_sessions.reserve_session(session_id, "demo", f"nexus/dev/{session_id}")
    dev_sessions.mark_active(session_id, worktree_path="/tmp/x", base_sha="abc123")
    assert dev_sessions.get_session(session_id)["status"] == dev_sessions.STATUS_ACTIVE


def test_invalid_transition_is_refused_and_leaves_status_unchanged(isolated_registry):
    _register_project(isolated_registry)
    session_id = dev_sessions.new_session_id()
    dev_sessions.reserve_session(session_id, "demo", f"nexus/dev/{session_id}")
    dev_sessions.mark_active(session_id, worktree_path="/tmp/x", base_sha="abc123")

    # ACTIVE -> ACTIVE (re-activating) is not a listed transition.
    with pytest.raises(dev_sessions.InvalidDevSessionTransitionError):
        dev_sessions.mark_active(session_id, worktree_path="/tmp/y", base_sha="def456")
    row = dev_sessions.get_session(session_id)
    assert row["status"] == dev_sessions.STATUS_ACTIVE
    assert row["worktree_path"] == "/tmp/x", "a refused transition must not partially apply its fields"


def test_terminal_states_have_no_outgoing_transitions(isolated_registry):
    _register_project(isolated_registry)
    session_id = dev_sessions.new_session_id()
    dev_sessions.reserve_session(session_id, "demo", f"nexus/dev/{session_id}")
    dev_sessions.mark_failed(session_id, error_code="X", error_message="y")

    with pytest.raises(dev_sessions.InvalidDevSessionTransitionError):
        dev_sessions.mark_active(session_id, worktree_path="/tmp/z", base_sha="123")


# -- concurrency --------------------------------------------------------


def test_concurrent_reservation_of_the_same_project_branch_allows_exactly_one(isolated_registry):
    _register_project(isolated_registry)
    start = threading.Barrier(2)

    def attempt(session_id):
        start.wait(timeout=5)
        try:
            return dev_sessions.reserve_session(session_id, "demo", "nexus/dev/race")
        except dev_sessions.DevSessionBranchBusyError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(attempt, "DEV-AAAAAAAA")
        second = pool.submit(attempt, "DEV-BBBBBBBB")
        results = [first.result(timeout=10), second.result(timeout=10)]

    successes = [r for r in results if isinstance(r, str)]
    failures = [r for r in results if isinstance(r, dev_sessions.DevSessionBranchBusyError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert len(dev_sessions.list_sessions("demo")) == 1


# -- recovery after restart -------------------------------------------------


def test_recovery_marks_dead_owner_interrupted_without_touching_filesystem(isolated_registry):
    _register_project(isolated_registry)
    session_id = dev_sessions.new_session_id()
    dev_sessions.reserve_session(session_id, "demo", f"nexus/dev/{session_id}")

    recovered = dev_sessions.recover_orphaned_sessions(lambda _host, _pid, _at: True)

    assert recovered == [session_id]
    row = dev_sessions.get_session(session_id)
    assert row["status"] == dev_sessions.STATUS_INTERRUPTED
    assert row["error_code"] == "SESSION_OUTCOME_UNKNOWN"
    assert row["worktree_path"] is None, "recovery is metadata-only; it must never invent a worktree path"


def test_recovery_leaves_live_owners_and_already_active_sessions_untouched(isolated_registry):
    _register_project(isolated_registry)
    live_id = dev_sessions.new_session_id()
    dev_sessions.reserve_session(live_id, "demo", f"nexus/dev/{live_id}")
    active_id = dev_sessions.new_session_id()
    dev_sessions.reserve_session(active_id, "demo", f"nexus/dev/{active_id}")
    dev_sessions.mark_active(active_id, worktree_path="/tmp/active", base_sha="abc")

    recovered = dev_sessions.recover_orphaned_sessions(lambda _host, _pid, _at: False)

    assert recovered == []
    assert dev_sessions.get_session(live_id)["status"] == dev_sessions.STATUS_CREATING
    assert dev_sessions.get_session(active_id)["status"] == dev_sessions.STATUS_ACTIVE


# -- reopening the database with existing sessions ---------------------------


def test_reopening_the_database_preserves_existing_dev_sessions(isolated_registry):
    _register_project(isolated_registry)
    session_id = dev_sessions.new_session_id()
    dev_sessions.reserve_session(session_id, "demo", f"nexus/dev/{session_id}")
    dev_sessions.mark_active(session_id, worktree_path="/tmp/wt", base_sha="abc123")

    # Simulate a restart: re-run the migration against the same database
    # file (a fresh connection is opened by every registry call anyway).
    database.initialize_database()

    row = dev_sessions.get_session(session_id)
    assert row is not None
    assert row["status"] == dev_sessions.STATUS_ACTIVE
    assert row["worktree_path"] == "/tmp/wt"


# -- compatibility with Chat and GO -----------------------------------------


def test_migration_does_not_disturb_existing_chat_sessions_or_projects(isolated_registry):
    workspace_path = _register_project(isolated_registry)
    chat_session_id = chat_sessions.create_session("demo", workspace_path)

    database.initialize_database()

    chat_row = chat_sessions.get_session(chat_session_id)
    assert chat_row is not None
    assert chat_row["status"] == "CREATED"
    project_row = projects.get_project("demo")
    assert project_row["enabled"] == 1
    assert project_row["path"] == workspace_path
