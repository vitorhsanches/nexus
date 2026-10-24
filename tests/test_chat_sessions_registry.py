"""Registry-level tests for nexus.registry.chat_sessions.

Isolated from the production nexus.db using the same
STORAGE_DIR/DATABASE_PATH/PROJECTS_FILE monkeypatch pattern as
tests/test_project_registry_lifecycle.py, so these never touch real
Nexus state.
"""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import nexus.registry.database as database
import nexus.registry.projects as projects
import pytest
from nexus.registry import chat_sessions


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


def _register_project(tmp_path, project_id="demo", workspace_path=None):
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
                        "enabled": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    projects.sync_projects()
    return workspace_path


def test_create_and_get_session(isolated_registry):
    workspace_path = _register_project(isolated_registry)
    session_id = chat_sessions.create_session("demo", workspace_path)

    row = chat_sessions.get_session(session_id)
    assert row["id"] == session_id
    assert row["project_id"] == "demo"
    assert row["workspace_path"] == workspace_path
    assert row["status"] == "CREATED"
    assert row["runtime"] == "omniroute"
    assert row["codex_thread_id"] is None


def test_requested_model_persists_and_old_database_migrates(isolated_registry):
    workspace_path = _register_project(isolated_registry)
    session_id = chat_sessions.create_session("demo", workspace_path, "cx/gpt-5.6-sol-high", "high")
    assert chat_sessions.get_session(session_id)["requested_model"] == "cx/gpt-5.6-sol-high"
    assert chat_sessions.get_session(session_id)["requested_effort"] == "high"


def test_old_chat_sessions_table_gets_nullable_requested_model_column(isolated_registry):
    with sqlite3.connect(database.DATABASE_PATH) as connection:
        connection.execute("DROP TABLE chat_turns")
        connection.execute("DROP TABLE chat_sessions")
        connection.execute(
            """CREATE TABLE chat_sessions (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, workspace_path TEXT NOT NULL,
                codex_thread_id TEXT, codex_home TEXT, model TEXT, provider TEXT,
                status TEXT NOT NULL DEFAULT 'CREATED', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO chat_sessions (id, project_id, workspace_path, status, created_at, updated_at) VALUES ('OLD', 'demo', '/old', 'ACTIVE', 'a', 'b')"
        )
    database.initialize_database()
    row = chat_sessions.get_session("OLD")
    assert row["requested_model"] is None
    assert row["requested_effort"] is None
    assert row["runtime"] == "omniroute"


def test_list_sessions_filters_by_project(isolated_registry):
    workspace_a = _register_project(isolated_registry, "alpha", str(isolated_registry / "a"))
    _register_project(isolated_registry, "beta", str(isolated_registry / "b"))
    # sync_projects() only keeps the LAST write_config call's set enabled;
    # register both by writing one combined file instead.
    projects.PROJECTS_FILE.write_text(
        json.dumps(
            {
                "projects": [
                    {"id": "alpha", "name": "Alpha", "path": str(isolated_registry / "a"), "aliases": [], "enabled": True},
                    {"id": "beta", "name": "Beta", "path": str(isolated_registry / "b"), "aliases": [], "enabled": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    projects.sync_projects()

    session_a = chat_sessions.create_session("alpha", workspace_a)
    session_b = chat_sessions.create_session("beta", str(isolated_registry / "b"))

    alpha_sessions = [row["id"] for row in chat_sessions.list_sessions("alpha")]
    assert alpha_sessions == [session_a]
    all_sessions = {row["id"] for row in chat_sessions.list_sessions()}
    assert all_sessions == {session_a, session_b}


def test_update_session_thread_persists_codex_identity(isolated_registry):
    workspace_path = _register_project(isolated_registry)
    session_id = chat_sessions.create_session("demo", workspace_path)

    chat_sessions.update_session_thread(
        session_id,
        codex_thread_id="01a-thread",
        codex_home=r"C:\fake\codex-home",
        model="cc/claude-sonnet-5-low",
        provider="omniroute",
    )

    row = chat_sessions.get_session(session_id)
    assert row["codex_thread_id"] == "01a-thread"
    assert row["codex_home"] == r"C:\fake\codex-home"
    assert row["model"] == "cc/claude-sonnet-5-low"
    assert row["provider"] == "omniroute"


def test_create_turn_if_absent_is_idempotent(isolated_registry):
    workspace_path = _register_project(isolated_registry)
    session_id = chat_sessions.create_session("demo", workspace_path)

    turn_id_1, created_1 = chat_sessions.create_turn_if_absent(session_id, "key-a", "hello")
    turn_id_2, created_2 = chat_sessions.create_turn_if_absent(session_id, "key-a", "DIFFERENT TEXT")

    assert created_1 is True
    assert created_2 is False
    assert turn_id_1 == turn_id_2

    row = chat_sessions.get_turn(turn_id_1)
    assert row["request_text"] == "hello"  # the second call's text never overwrote it


def test_create_turn_with_different_keys_requires_terminal_first(isolated_registry):
    workspace_path = _register_project(isolated_registry)
    session_id = chat_sessions.create_session("demo", workspace_path)

    turn_id_1, _ = chat_sessions.create_turn_if_absent(session_id, "key-a", "first")
    with pytest.raises(chat_sessions.SessionBusyError):
        chat_sessions.create_turn_if_absent(session_id, "key-b", "second")
    chat_sessions.update_turn_status(turn_id_1, "COMPLETED", response_text="done")
    turn_id_2, _ = chat_sessions.create_turn_if_absent(session_id, "key-b", "second")

    assert turn_id_1 != turn_id_2


def test_parallel_reservations_allow_exactly_one_active_turn(isolated_registry):
    workspace_path = _register_project(isolated_registry)
    session_id = chat_sessions.create_session("demo", workspace_path)
    start = threading.Barrier(3)

    def reserve(key):
        start.wait(timeout=5)
        try:
            return chat_sessions.create_turn_if_absent(session_id, key, key)
        except chat_sessions.SessionBusyError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(reserve, "key-a")
        second = pool.submit(reserve, "key-b")
        start.wait(timeout=5)
        results = [first.result(timeout=10), second.result(timeout=10)]

    assert sum(isinstance(result, tuple) for result in results) == 1
    assert sum(isinstance(result, chat_sessions.SessionBusyError) for result in results) == 1
    assert len(chat_sessions.list_turns(session_id)) == 1


def test_recovery_marks_dead_owner_outcome_unknown_without_replaying(isolated_registry):
    workspace_path = _register_project(isolated_registry)
    session_id = chat_sessions.create_session("demo", workspace_path)
    turn_id, created = chat_sessions.create_turn_if_absent(session_id, "recovery-key", "do work")
    assert created
    chat_sessions.update_turn_status(turn_id, "RUNNING")

    recovered = chat_sessions.recover_orphaned_turns(lambda _host, _pid, _at: True)

    assert recovered == [turn_id]
    turn = chat_sessions.get_turn(turn_id)
    assert turn["status"] == "FAILED"
    assert turn["error_code"] == "TURN_OUTCOME_UNKNOWN"
    assert "not replayed" in turn["error_message"]
    assert chat_sessions.get_active_turn_for_session(session_id) is None
    retry_id, created = chat_sessions.create_turn_if_absent(session_id, "recovery-key", "different")
    assert retry_id == turn_id
    assert created is False


def test_recovery_preserves_live_and_legacy_reservations(isolated_registry):
    workspace_path = _register_project(isolated_registry)
    session_id = chat_sessions.create_session("demo", workspace_path)
    live_turn, _ = chat_sessions.create_turn_if_absent(session_id, "live-key", "still running")
    legacy_session_id = chat_sessions.create_session("demo", workspace_path)
    legacy_turn, _ = chat_sessions.create_turn_if_absent(legacy_session_id, "legacy-key", "old row")
    with database.get_connection() as connection:
        connection.execute(
            "UPDATE chat_turns SET owner_host = NULL, owner_pid = NULL, owner_acquired_at = NULL WHERE id = ?",
            (legacy_turn,),
        )

    recovered = chat_sessions.recover_orphaned_turns(lambda _host, _pid, _at: False)

    assert recovered == []
    assert chat_sessions.get_turn(live_turn)["status"] == "PENDING"
    assert chat_sessions.get_turn(legacy_turn)["status"] == "PENDING"


def test_update_turn_status_sets_timestamps(isolated_registry):
    workspace_path = _register_project(isolated_registry)
    session_id = chat_sessions.create_session("demo", workspace_path)
    turn_id, _ = chat_sessions.create_turn_if_absent(session_id, "key-a", "hello")

    chat_sessions.update_turn_status(turn_id, "RUNNING")
    running = chat_sessions.get_turn(turn_id)
    assert running["status"] == "RUNNING"
    assert running["started_at"] is not None
    assert running["finished_at"] is None

    chat_sessions.update_turn_status(
        turn_id, "COMPLETED", response_text="PONG", model="cc/test", provider="omniroute"
    )
    completed = chat_sessions.get_turn(turn_id)
    assert completed["status"] == "COMPLETED"
    assert completed["response_text"] == "PONG"
    assert completed["finished_at"] is not None


def test_get_active_turn_for_session_only_returns_pending_or_running(isolated_registry):
    workspace_path = _register_project(isolated_registry)
    session_id = chat_sessions.create_session("demo", workspace_path)
    turn_id, _ = chat_sessions.create_turn_if_absent(session_id, "key-a", "hello")

    assert chat_sessions.get_active_turn_for_session(session_id)["id"] == turn_id

    chat_sessions.update_turn_status(turn_id, "COMPLETED", response_text="done")
    assert chat_sessions.get_active_turn_for_session(session_id) is None


def test_session_requires_an_existing_project_row(isolated_registry):
    # FK enforcement (PRAGMA foreign_keys = ON in get_connection()):
    # a session can never reference a project that was never synced.
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        chat_sessions.create_session("does-not-exist", str(isolated_registry))
