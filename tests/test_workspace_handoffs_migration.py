"""P5.3A: workspace_handoffs schema migration from the pre-native shape
(source_mission_id NOT NULL) to the evidence-typed shape (source_backend,
nullable source_mission_id / source_native_review_id, CHECK constraint).

No model calls; this is pure SQLite schema/data verification.
"""

from __future__ import annotations

import pytest

from nexus.registry import database
from nexus.registry import workspace as store


@pytest.fixture
def db_env(tmp_path):
    original_db, original_storage = database.DATABASE_PATH, database.STORAGE_DIR
    database.STORAGE_DIR = tmp_path / "storage"
    database.DATABASE_PATH = database.STORAGE_DIR / "nexus.db"
    yield tmp_path
    database.DATABASE_PATH, database.STORAGE_DIR = original_db, original_storage


def test_fresh_database_gets_the_evidence_typed_schema(db_env):
    database.initialize_database()
    with database.get_connection() as connection:
        cols = {row[1] for row in connection.execute("PRAGMA table_info(workspace_handoffs)")}
        assert {"source_backend", "source_mission_id", "source_native_review_id"} <= cols
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(workspace_handoffs)")}
        assert "idx_workspace_handoffs_target" in indexes
        assert "idx_workspace_handoffs_target_transferring" in indexes
        # The NOT NULL constraint is gone; the CHECK constraint is what
        # guards the invariant now.
        info = {row[1]: row for row in connection.execute("PRAGMA table_info(workspace_handoffs)")}
        assert info["source_mission_id"][3] == 0, "source_mission_id must no longer be NOT NULL"
        assert info["source_native_review_id"][3] == 0


def _legacy_schema(connection) -> None:
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("DROP TABLE workspace_handoffs")
    connection.execute(
        """
        CREATE TABLE workspace_handoffs (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, plan_id TEXT NOT NULL,
            source_agent_key TEXT NOT NULL, target_agent_key TEXT NOT NULL,
            source_pane_id TEXT NOT NULL, target_pane_id TEXT NOT NULL,
            source_mission_id TEXT NOT NULL, source_session_id TEXT NOT NULL,
            target_session_id TEXT NOT NULL, source_base_sha TEXT NOT NULL,
            source_head_sha TEXT NOT NULL, target_base_sha TEXT NOT NULL,
            target_head_sha TEXT NOT NULL, snapshot_fingerprint TEXT NOT NULL,
            files_json TEXT NOT NULL, status TEXT NOT NULL,
            owner_token TEXT, owner_host TEXT, owner_pid INTEGER, owner_acquired_at TEXT,
            created_at TEXT NOT NULL, approved_at TEXT, started_at TEXT, finished_at TEXT,
            error_code TEXT, error_message TEXT, result_json TEXT
        )
        """
    )
    connection.execute("CREATE INDEX idx_workspace_handoffs_target ON workspace_handoffs(target_pane_id, created_at)")
    connection.execute(
        "CREATE UNIQUE INDEX idx_workspace_handoffs_target_transferring "
        "ON workspace_handoffs(target_session_id) WHERE status='TRANSFERRING'"
    )


def _seed_legacy_row(connection, plan_id: str, *, handoff_id="HANDOFF-OLD", status="TRANSFERRED") -> None:
    connection.execute(
        """INSERT INTO workspace_handoffs
           (id, project_id, plan_id, source_agent_key, target_agent_key, source_pane_id, target_pane_id,
            source_mission_id, source_session_id, target_session_id, source_base_sha, source_head_sha,
            target_base_sha, target_head_sha, snapshot_fingerprint, files_json, status, created_at)
           VALUES (?, 'demo', ?, 'api', 'tests', 'PANE-A', 'PANE-T',
                   'DEVMISSION-OLD', 'DEV-A', 'DEV-T', 'base', 'base', 'base', 'base',
                   'fingerprint-old', '[]', ?, 'then')""",
        (handoff_id, plan_id, status),
    )


def test_migration_preserves_legacy_handoffs_and_adds_native_columns(db_env):
    database.initialize_database()
    with database.get_connection() as connection:
        connection.execute("INSERT INTO projects (id, name, path, enabled, dev_enabled) VALUES ('demo','Demo','.',1,1)")
        connection.execute("INSERT INTO dev_sessions (id, project_id, branch, status) VALUES ('DEV-A','demo','b1','ACTIVE')")
        connection.execute("INSERT INTO dev_sessions (id, project_id, branch, status) VALUES ('DEV-T','demo','b2','ACTIVE')")
    plan_id = store.create_plan("demo", "t", policy_version="v1", complexity="simple", proposal={"agents": []}, source="llm")
    with database.get_connection() as connection:
        connection.execute(
            "INSERT INTO workspace_panes (id, project_id, dev_session_id, plan_id, origin, role, title, position, plan_agent_key) "
            "VALUES ('PANE-A','demo','DEV-A',?, 'maestro','implementer','A',0,'api')", (plan_id,),
        )
        connection.execute(
            "INSERT INTO workspace_panes (id, project_id, dev_session_id, plan_id, origin, role, title, position, plan_agent_key) "
            "VALUES ('PANE-T','demo','DEV-T',?, 'maestro','tester','T',1,'tests')", (plan_id,),
        )
        _legacy_schema(connection)
        _seed_legacy_row(connection, plan_id)

    database.initialize_database()

    with database.get_connection() as connection:
        row = dict(connection.execute("SELECT * FROM workspace_handoffs WHERE id='HANDOFF-OLD'").fetchone())
        assert row["source_mission_id"] == "DEVMISSION-OLD"
        assert row["source_backend"] == "omniroute"
        assert row["source_native_review_id"] is None
        assert row["status"] == "TRANSFERRED"
        assert row["snapshot_fingerprint"] == "fingerprint-old"
        gone = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_handoffs_pre_p53a'"
        ).fetchone()
        assert gone is None
        indexes = {r[1] for r in connection.execute("PRAGMA index_list(workspace_handoffs)")}
        assert {"idx_workspace_handoffs_target", "idx_workspace_handoffs_target_transferring"} <= indexes

        # A fresh native-shaped row is now accepted...
        connection.execute(
            """INSERT INTO workspace_handoffs
               (id, project_id, plan_id, source_agent_key, target_agent_key, source_pane_id, target_pane_id,
                source_backend, source_native_review_id, source_session_id, target_session_id, source_base_sha,
                source_head_sha, target_base_sha, target_head_sha, snapshot_fingerprint, files_json, status, created_at)
               VALUES ('HANDOFF-NEW','demo',?,'api','tests','PANE-A','PANE-T',
                       'codex_native', ?, 'DEV-A','DEV-T','base','base','base','base',
                       'fingerprint-new', '[]', 'PREVIEWED', 'now')""",
            (plan_id, "a" * 64),
        )
        connection.commit()
        # ...but never with both kinds of evidence at once.
        with pytest.raises(Exception):
            connection.execute(
                """INSERT INTO workspace_handoffs
                   (id, project_id, plan_id, source_agent_key, target_agent_key, source_pane_id, target_pane_id,
                    source_backend, source_mission_id, source_native_review_id, source_session_id, target_session_id,
                    source_base_sha, source_head_sha, target_base_sha, target_head_sha, snapshot_fingerprint,
                    files_json, status, created_at)
                   VALUES ('HANDOFF-BAD','demo',?,'api','tests','PANE-A','PANE-T',
                           'codex_native', 'MISSION-X', ?, 'DEV-A','DEV-T','base','base','base','base',
                           'fp-bad', '[]', 'PREVIEWED', 'now')""",
                (plan_id, "b" * 64),
            )
            connection.commit()


def test_migration_is_idempotent_across_repeated_startups(db_env):
    database.initialize_database()
    with database.get_connection() as connection:
        connection.execute("INSERT INTO projects (id, name, path, enabled, dev_enabled) VALUES ('demo','Demo','.',1,1)")
        connection.execute("INSERT INTO dev_sessions (id, project_id, branch, status) VALUES ('DEV-A','demo','b1','ACTIVE')")
        connection.execute("INSERT INTO dev_sessions (id, project_id, branch, status) VALUES ('DEV-T','demo','b2','ACTIVE')")
    plan_id = store.create_plan("demo", "t", policy_version="v1", complexity="simple", proposal={"agents": []}, source="llm")
    with database.get_connection() as connection:
        connection.execute(
            "INSERT INTO workspace_panes (id, project_id, dev_session_id, plan_id, origin, role, title, position, plan_agent_key) "
            "VALUES ('PANE-A','demo','DEV-A',?, 'maestro','implementer','A',0,'api')", (plan_id,),
        )
        connection.execute(
            "INSERT INTO workspace_panes (id, project_id, dev_session_id, plan_id, origin, role, title, position, plan_agent_key) "
            "VALUES ('PANE-T','demo','DEV-T',?, 'maestro','tester','T',1,'tests')", (plan_id,),
        )
        _legacy_schema(connection)
        _seed_legacy_row(connection, plan_id)

    # Three startups in a row: the first migrates, the next two are no-ops.
    database.initialize_database()
    database.initialize_database()
    database.initialize_database()

    with database.get_connection() as connection:
        row = dict(connection.execute("SELECT * FROM workspace_handoffs WHERE id='HANDOFF-OLD'").fetchone())
        assert row["source_mission_id"] == "DEVMISSION-OLD"
        assert row["source_backend"] == "omniroute"
        assert connection.execute("SELECT COUNT(*) FROM workspace_handoffs").fetchone()[0] == 1


def test_database_reopens_after_migration_with_data_intact(db_env):
    database.initialize_database()
    with database.get_connection() as connection:
        connection.execute("INSERT INTO projects (id, name, path, enabled, dev_enabled) VALUES ('demo','Demo','.',1,1)")
        connection.execute("INSERT INTO dev_sessions (id, project_id, branch, status) VALUES ('DEV-A','demo','b1','ACTIVE')")
        connection.execute("INSERT INTO dev_sessions (id, project_id, branch, status) VALUES ('DEV-T','demo','b2','ACTIVE')")
    plan_id = store.create_plan("demo", "t", policy_version="v1", complexity="simple", proposal={"agents": []}, source="llm")
    with database.get_connection() as connection:
        connection.execute(
            "INSERT INTO workspace_panes (id, project_id, dev_session_id, plan_id, origin, role, title, position, plan_agent_key) "
            "VALUES ('PANE-A','demo','DEV-A',?, 'maestro','implementer','A',0,'api')", (plan_id,),
        )
        connection.execute(
            "INSERT INTO workspace_panes (id, project_id, dev_session_id, plan_id, origin, role, title, position, plan_agent_key) "
            "VALUES ('PANE-T','demo','DEV-T',?, 'maestro','tester','T',1,'tests')", (plan_id,),
        )
        _legacy_schema(connection)
        _seed_legacy_row(connection, plan_id)
    database.initialize_database()

    # Simulate a process restart: nothing but re-opening the same file.
    with database.get_connection() as connection:
        row = dict(connection.execute("SELECT * FROM workspace_handoffs WHERE id='HANDOFF-OLD'").fetchone())
        assert row["status"] == "TRANSFERRED"
        assert row["source_backend"] == "omniroute"
