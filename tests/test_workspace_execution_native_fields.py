"""P5.2B: persistence of Codex Native evidence on workspace plan execution
steps (native_turn_id, native_review_id) -- schema, migration, read/write.

Scope is strictly storage: nothing here drives coordinator._run, handoff, or
any HTTP route. See test_workspace_coordinator.py for the backend-aware
test_profile validation in coordinator.authorize_execution.
"""

from __future__ import annotations

import pytest

from nexus.registry import database
from nexus.registry import workspace_execution as executions


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    original_db, original_storage = database.DATABASE_PATH, database.STORAGE_DIR
    database.STORAGE_DIR = tmp_path / "storage"
    database.DATABASE_PATH = database.STORAGE_DIR / "nexus.db"
    yield tmp_path
    database.DATABASE_PATH, database.STORAGE_DIR = original_db, original_storage


def _pre_migration_schema(connection) -> None:
    """The exact shape of workspace_plan_execution_steps before P5.2B."""
    connection.executescript(
        """
        CREATE TABLE workspace_plan_executions (
            plan_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL,
            agent_keys_json TEXT, test_profiles_json TEXT, max_turns_per_agent INTEGER,
            mission_timeout_seconds INTEGER, current_index INTEGER, interrupt_requested INTEGER,
            error_code TEXT, error_message TEXT, result_json TEXT,
            created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL,
            owner_host TEXT, owner_pid INTEGER, owner_acquired_at TEXT,
            owner_token TEXT, lease_renewed_at TEXT, resume_history_json TEXT
        );
        CREATE TABLE workspace_plan_execution_steps (
            plan_id TEXT NOT NULL, agent_key TEXT NOT NULL, ordinal INTEGER NOT NULL,
            pane_id TEXT NOT NULL, test_profile TEXT NOT NULL, status TEXT NOT NULL,
            mission_id TEXT, result_json TEXT, error_code TEXT, error_message TEXT,
            started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL,
            PRIMARY KEY (plan_id, agent_key), UNIQUE (plan_id, ordinal)
        );
        """
    )
    connection.execute(
        """INSERT INTO workspace_plan_executions
           (plan_id, project_id, status, agent_keys_json, test_profiles_json,
            max_turns_per_agent, mission_timeout_seconds, current_index, interrupt_requested,
            created_at, updated_at)
           VALUES ('PLAN-OLD', 'demo', 'AWAITING_REVIEW', '["api"]', '{"api": "unit"}',
                   1, 60, 1, 0, 'then', 'then')""",
    )
    connection.execute(
        """INSERT INTO workspace_plan_execution_steps
           (plan_id, agent_key, ordinal, pane_id, test_profile, status, mission_id,
            updated_at)
           VALUES ('PLAN-OLD', 'api', 0, 'PANE-OLD', 'unit', 'READY_FOR_REVIEW', 'DEVMISSION-OLD',
                   'then')""",
    )


def test_migration_adds_native_columns_without_losing_existing_rows(db_env):
    database.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    with database.get_connection() as connection:
        _pre_migration_schema(connection)

    # The real entry point every Nexus process calls at startup.
    database.initialize_database()

    with database.get_connection() as connection:
        cols = {row[1] for row in connection.execute("PRAGMA table_info(workspace_plan_execution_steps)")}
        assert {"native_turn_id", "native_review_id"} <= cols
        row = connection.execute(
            "SELECT * FROM workspace_plan_execution_steps WHERE plan_id='PLAN-OLD'"
        ).fetchone()
        # The pre-existing OmniRoute row survives untouched.
        assert row["mission_id"] == "DEVMISSION-OLD"
        assert row["status"] == "READY_FOR_REVIEW"
        assert row["native_turn_id"] is None and row["native_review_id"] is None

    # Running the migration again (as every later process start does) is
    # a no-op, not a second ALTER TABLE failure.
    database.initialize_database()
    view = executions.view("PLAN-OLD")
    assert view["steps"][0]["mission_id"] == "DEVMISSION-OLD"
    assert view["steps"][0]["native_turn_id"] is None
    assert view["steps"][0]["native_review_id"] is None


def test_native_fields_round_trip_through_update_step_and_view(db_env):
    database.initialize_database()
    with database.get_connection() as connection:
        # Only workspace_plan_executions/_steps are under test here; skip
        # the unrelated projects/dev_sessions/workspace_panes FK chain.
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """INSERT INTO workspace_plan_executions
               (plan_id, project_id, status, agent_keys_json, test_profiles_json,
                max_turns_per_agent, mission_timeout_seconds, owner_token, lease_renewed_at,
                created_at, updated_at)
               VALUES ('PLAN-1', 'demo', 'RUNNING', '["api"]', '{"api": ""}', 1, 60,
                       'tok', '2999-01-01T00:00:00+00:00', 'now', 'now')""",
        )
        connection.execute(
            """INSERT INTO workspace_plan_execution_steps
               (plan_id, agent_key, ordinal, pane_id, test_profile, status, updated_at)
               VALUES ('PLAN-1', 'api', 0, 'PANE-1', '', 'STARTING', 'now')""",
        )

    executions.update_step("PLAN-1", "api", "RUNNING", native_turn_id="DEVTURN-X", owner_token="tok")
    step = executions.view("PLAN-1")["steps"][0]
    assert step["native_turn_id"] == "DEVTURN-X"
    assert step["native_review_id"] is None
    assert step["mission_id"] is None, "a native step must never pick up a mission_id"

    executions.update_step("PLAN-1", "api", "READY_FOR_REVIEW",
                          native_review_id="a" * 64, owner_token="tok")
    step = executions.view("PLAN-1")["steps"][0]
    # The earlier write is not clobbered by the later, independent one.
    assert step["native_turn_id"] == "DEVTURN-X"
    assert step["native_review_id"] == "a" * 64
    assert step["mission_id"] is None


def test_native_and_mission_evidence_are_never_the_same_column(db_env):
    """A step can only ever carry one kind of evidence at a time in
    practice (one backend per session), but the storage itself must keep
    the two concepts distinct rather than overloading one field."""
    database.initialize_database()
    with database.get_connection() as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """INSERT INTO workspace_plan_executions
               (plan_id, project_id, status, agent_keys_json, test_profiles_json,
                max_turns_per_agent, mission_timeout_seconds, owner_token, lease_renewed_at,
                created_at, updated_at)
               VALUES ('PLAN-2', 'demo', 'RUNNING', '["api"]', '{"api": "unit"}', 1, 60,
                       'tok', '2999-01-01T00:00:00+00:00', 'now', 'now')""",
        )
        connection.execute(
            """INSERT INTO workspace_plan_execution_steps
               (plan_id, agent_key, ordinal, pane_id, test_profile, status, updated_at)
               VALUES ('PLAN-2', 'api', 0, 'PANE-2', 'unit', 'STARTING', 'now')""",
        )
    executions.update_step("PLAN-2", "api", "RUNNING", mission_id="DEVMISSION-Y", owner_token="tok")
    step = executions.view("PLAN-2")["steps"][0]
    assert step["mission_id"] == "DEVMISSION-Y"
    assert step["native_turn_id"] is None and step["native_review_id"] is None
