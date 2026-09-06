import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.orchestration.go import run_go
from nexus.registry.checkpoints import (
    CheckpointWriteError,
    get_approved_plan,
    get_latest_checkpoint,
    list_checkpoints,
    record_approved_plan,
    record_checkpoint,
)
from nexus.orchestration.progressive import execute_progressively
from nexus.registry.runs import create_run, get_run


FIXTURE_PROJECTS = {
    "projects": [
        {
            "id": "norte",
            "name": "Norte",
            "path": r"C:\fake\interface-life",
            "aliases": ["norte"],
            "enabled": True,
        },
    ]
}


VALID_PLAN = {
    "complexity": "LOW",
    "risk": "LOW",
    "parallelism": 1,
    "summary": "Do a small fix.",
    "workers": [
        {
            "route_class": "mechanical",
            "execution_path": "OMNIROUTE",
            "provider": "omniroute",
            "model": "oc/big-pickle",
            "effort": "low",
            "scope": "Fix the thing.",
            "reason": "Simple mechanical change.",
        }
    ],
}


class CheckpointRegistryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp_dir.name)

        self._original_db_path = database.DATABASE_PATH
        self._original_storage_dir = database.STORAGE_DIR
        self._original_projects_file = projects.PROJECTS_FILE

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus-test.db"

        fixture_path = tmp_path / "projects.json"
        fixture_path.write_text(
            json.dumps(FIXTURE_PROJECTS), encoding="utf-8"
        )
        projects.PROJECTS_FILE = fixture_path

        database.initialize_database()
        projects.sync_projects()

    def tearDown(self) -> None:
        database.DATABASE_PATH = self._original_db_path
        database.STORAGE_DIR = self._original_storage_dir
        projects.PROJECTS_FILE = self._original_projects_file
        self._tmp_dir.cleanup()

    def _make_run(self) -> str:
        return create_run(
            project_id="norte",
            input_text="Norte: do a thing",
            intent="GO",
            status="CREATED",
        )

    def test_fresh_database_creates_checkpoint_tables(self):
        with database.get_connection() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }

        self.assertIn("approved_plans", tables)
        self.assertIn("checkpoints", tables)
        self.assertIn("projects", tables)
        self.assertIn("runs", tables)
        self.assertIn("agents", tables)

    def test_reinitializing_existing_database_is_idempotent(self):
        database.initialize_database()
        database.initialize_database()

        with database.get_connection() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }

        self.assertIn("checkpoints", tables)

    def test_record_and_get_approved_plan(self):
        run_id = self._make_run()

        record_approved_plan(run_id, VALID_PLAN)

        stored = get_approved_plan(run_id)

        self.assertEqual(stored, VALID_PLAN)

    def test_record_approved_plan_is_idempotent_for_same_plan(self):
        run_id = self._make_run()

        record_approved_plan(run_id, VALID_PLAN)
        record_approved_plan(run_id, VALID_PLAN)

        self.assertEqual(get_approved_plan(run_id), VALID_PLAN)

    def test_record_approved_plan_rejects_replacement(self):
        run_id = self._make_run()

        record_approved_plan(run_id, VALID_PLAN)

        replacement = dict(
            VALID_PLAN,
            summary="A different approved plan.",
        )

        with self.assertRaises(CheckpointWriteError):
            record_approved_plan(run_id, replacement)

        self.assertEqual(get_approved_plan(run_id), VALID_PLAN)

    def test_get_approved_plan_missing_returns_none(self):
        run_id = self._make_run()

        self.assertIsNone(get_approved_plan(run_id))

    def test_checkpoint_sequence_is_ordered_per_run(self):
        run_id = self._make_run()

        seq1 = record_checkpoint(
            run_id=run_id,
            boundary="PLAN_APPROVED",
            payload={"foo": "bar"},
        )
        seq2 = record_checkpoint(
            run_id=run_id,
            boundary="EXECUTION_START",
            payload={"foo": "baz"},
            worker_ordinal=1,
        )

        self.assertEqual(seq1, 1)
        self.assertEqual(seq2, 2)

        checkpoints = list_checkpoints(run_id)

        self.assertEqual(len(checkpoints), 2)
        self.assertEqual(checkpoints[0]["boundary"], "PLAN_APPROVED")
        self.assertEqual(checkpoints[1]["boundary"], "EXECUTION_START")
        self.assertEqual(checkpoints[1]["worker_ordinal"], 1)
        self.assertEqual(checkpoints[1]["payload"], {"foo": "baz"})

    def test_checkpoint_sequences_are_independent_per_run(self):
        run_a = self._make_run()
        run_b = self._make_run()

        record_checkpoint(run_id=run_a, boundary="PLAN_APPROVED", payload={})
        seq_b = record_checkpoint(
            run_id=run_b, boundary="PLAN_APPROVED", payload={}
        )

        self.assertEqual(seq_b, 1)
        self.assertEqual(len(list_checkpoints(run_a)), 1)
        self.assertEqual(len(list_checkpoints(run_b)), 1)

    def test_checkpoint_payload_json_round_trips_deterministically(self):
        run_id = self._make_run()

        payload = {"b": 2, "a": 1, "nested": {"z": True, "y": None}}
        record_checkpoint(
            run_id=run_id, boundary="LIFECYCLE", payload=payload
        )

        checkpoints = list_checkpoints(run_id)

        self.assertEqual(checkpoints[0]["payload"], payload)

    def test_record_checkpoint_rejects_non_serializable_payload(self):
        run_id = self._make_run()

        with self.assertRaises(CheckpointWriteError):
            record_checkpoint(
                run_id=run_id,
                boundary="LIFECYCLE",
                payload={"bad": object()},
            )

    def test_record_checkpoint_fail_closed_on_db_error(self):
        run_id = self._make_run()

        with patch(
            "nexus.registry.checkpoints.get_connection",
            side_effect=sqlite3.Error("boom"),
        ):
            with self.assertRaises(CheckpointWriteError):
                record_checkpoint(
                    run_id=run_id,
                    boundary="LIFECYCLE",
                    payload={"x": 1},
                )

    @patch("nexus.orchestration.progressive.review_worker")
    @patch("nexus.orchestration.progressive.execute_worker")
    @patch("nexus.orchestration.progressive.AdaptiveRoutingService")
    def test_worker_boundary_checkpoints_include_worker_ordinal(
        self,
        mock_routing_service_cls,
        mock_execute_worker,
        mock_review_worker,
    ):
        run_id = self._make_run()

        mock_decision = unittest.mock.Mock(
            execution_path="OMNIROUTE",
            provider="omniroute",
            model="oc/big-pickle",
            effort="low",
            reason="test",
            degraded=False,
        )
        mock_routing_service_cls.return_value.select_route_for_capability.return_value = (
            mock_decision
        )

        mock_execute_worker.return_value = {
            "status": "COMPLETED",
            "agent_id": "AGENT-0001",
            "worktree": r"C:\fake\worktree",
        }
        mock_review_worker.return_value = {
            "status": "COMPLETED",
            "reviewer_id": "AGENT-0002",
            "review": {
                "verdict": "PASS",
                "failure_class": None,
                "summary": "Looks good.",
            },
            "routing": {
                "model": "cc/claude-sonnet-5-high",
                "provider": "claude",
                "effort": "high",
                "execution_path": "OMNIROUTE",
                "reason": "qualified reviewer",
                "degraded": False,
            },
        }

        outcome = execute_progressively(
            run_id=run_id,
            repo=r"C:\fake\interface-life",
            manager_id="AGENT-0000",
            original_task="Fix the thing.",
            planned_worker=VALID_PLAN["workers"][0],
            plan_risk="LOW",
            worker_ordinal=3,
        )

        self.assertEqual(outcome["status"], "COMPLETED")

        checkpoints = list_checkpoints(run_id)
        boundaries = [c["boundary"] for c in checkpoints]

        self.assertIn("EXECUTION_START", boundaries)
        self.assertIn("WORKER_ATTEMPT", boundaries)
        self.assertIn("REVIEW", boundaries)
        self.assertIn("LIFECYCLE", boundaries)

        for checkpoint in checkpoints:
            self.assertEqual(checkpoint["worker_ordinal"], 3)

    def test_run_go_records_terminal_completed_checkpoint(self):
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-0001",
                "status": "COMPLETED",
                "exit_code": 0,
                "plan": VALID_PLAN,
            },
        ), patch(
            "nexus.orchestration.go.execute_progressively",
            return_value={
                "status": "COMPLETED",
                "verdict": "PASS",
                "history": [],
                "worker": {"status": "COMPLETED"},
                "review": {"verdict": "PASS"},
            },
        ):
            result = run_go("Norte: fix a bug", project_query="norte")

        run_id = result["run_id"]
        run = get_run(run_id)
        self.assertEqual(run["status"], "COMPLETED")

        checkpoints = list_checkpoints(run_id)
        boundaries = [c["boundary"] for c in checkpoints]

        self.assertIn("PLAN_APPROVED", boundaries)
        self.assertIn("RUN_TERMINAL", boundaries)

        terminal = [
            c for c in checkpoints if c["boundary"] == "RUN_TERMINAL"
        ][0]
        self.assertEqual(terminal["payload"]["status"], "COMPLETED")
        self.assertEqual(terminal["worker_ordinal"], 1)

        stored_plan = get_approved_plan(run_id)
        self.assertEqual(stored_plan, VALID_PLAN)


    def test_existing_pre_f1_database_is_upgraded_without_data_loss(self):
        legacy_path = Path(self._tmp_dir.name) / "legacy-pre-f1.db"

        connection = sqlite3.connect(legacy_path)

        connection.executescript(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                path TEXT NOT NULL,
                aliases_json TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE runs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                input TEXT NOT NULL,
                intent TEXT NOT NULL,
                status TEXT NOT NULL,
                risk TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT,
                result TEXT,
                commit_sha TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            );

            CREATE TABLE agents (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                role TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                effort TEXT NOT NULL,
                status TEXT NOT NULL,
                branch TEXT,
                worktree TEXT,
                parent_agent_id TEXT,
                started_at TEXT,
                finished_at TEXT,
                result TEXT,
                FOREIGN KEY (run_id) REFERENCES runs(id),
                FOREIGN KEY (parent_agent_id) REFERENCES agents(id)
            );
            """
        )

        connection.execute(
            """
            INSERT INTO projects (
                id,
                name,
                path,
                aliases_json
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                "legacy",
                "Legacy",
                r"C:\fake\legacy",
                "[]",
            ),
        )

        connection.execute(
            """
            INSERT INTO runs (
                id,
                project_id,
                input,
                intent,
                status
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "RUN-LEGACY",
                "legacy",
                "old request",
                "GO",
                "COMPLETED",
            ),
        )

        connection.commit()
        connection.close()

        previous_path = database.DATABASE_PATH
        database.DATABASE_PATH = legacy_path

        try:
            database.initialize_database()

            with database.get_connection() as upgraded:
                run = upgraded.execute(
                    "SELECT id FROM runs WHERE id = ?",
                    ("RUN-LEGACY",),
                ).fetchone()

                tables = {
                    row["name"]
                    for row in upgraded.execute(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'table'
                        """
                    ).fetchall()
                }

            self.assertIsNotNone(run)
            self.assertIn("approved_plans", tables)
            self.assertIn("checkpoints", tables)

        finally:
            database.DATABASE_PATH = previous_path

    def test_checkpoint_tables_reference_runs(self):
        with database.get_connection() as connection:
            approved_fk = connection.execute(
                "PRAGMA foreign_key_list(approved_plans)"
            ).fetchall()

            checkpoint_fk = connection.execute(
                "PRAGMA foreign_key_list(checkpoints)"
            ).fetchall()

        self.assertTrue(
            any(row["table"] == "runs" for row in approved_fk)
        )
        self.assertTrue(
            any(row["table"] == "runs" for row in checkpoint_fk)
        )

    def test_orphan_checkpoint_and_plan_are_rejected(self):
        with self.assertRaises(CheckpointWriteError):
            record_approved_plan("RUN-DOES-NOT-EXIST", VALID_PLAN)

        with self.assertRaises(CheckpointWriteError):
            record_checkpoint(
                run_id="RUN-DOES-NOT-EXIST",
                boundary="PLAN_APPROVED",
                payload={},
            )

    def test_concurrent_checkpoint_writers_get_unique_sequences(self):
        run_id = self._make_run()

        def write_checkpoint(index):
            return record_checkpoint(
                run_id=run_id,
                boundary="CONCURRENT_TEST",
                payload={"index": index},
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            sequences = list(
                executor.map(write_checkpoint, range(12))
            )

        self.assertEqual(
            sorted(sequences),
            list(range(1, 13)),
        )

        checkpoints = list_checkpoints(run_id)

        self.assertEqual(
            [checkpoint["sequence"] for checkpoint in checkpoints],
            list(range(1, 13)),
        )

    def test_latest_checkpoint_returns_last_boundary(self):
        run_id = self._make_run()

        record_checkpoint(
            run_id=run_id,
            boundary="PLAN_APPROVED",
            payload={"step": 1},
        )

        record_checkpoint(
            run_id=run_id,
            boundary="EXECUTION_START",
            payload={"step": 2},
            worker_ordinal=1,
        )

        latest = get_latest_checkpoint(run_id)

        self.assertEqual(latest["sequence"], 2)
        self.assertEqual(latest["boundary"], "EXECUTION_START")
        self.assertEqual(latest["worker_ordinal"], 1)

    def test_plan_approved_checkpoint_contains_manager_id(self):
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-MANAGER",
                "status": "COMPLETED",
                "exit_code": 0,
                "plan": VALID_PLAN,
            },
        ), patch(
            "nexus.orchestration.go.execute_progressively",
            return_value={
                "status": "COMPLETED",
                "verdict": "PASS",
                "history": [],
                "worker": {"status": "COMPLETED"},
                "review": {"verdict": "PASS"},
            },
        ):
            result = run_go(
                "Norte: checkpoint manager",
                project_query="norte",
            )

        checkpoints = list_checkpoints(result["run_id"])

        approved = next(
            checkpoint
            for checkpoint in checkpoints
            if checkpoint["boundary"] == "PLAN_APPROVED"
        )

        self.assertEqual(
            approved["payload"]["manager_id"],
            "AGT-MANAGER",
        )

    def test_worker_and_review_boundaries_store_resume_context(self):
        run_id = self._make_run()

        decision = unittest.mock.Mock(
            execution_path="OMNIROUTE",
            provider="omniroute",
            model="oc/big-pickle",
            effort="low",
            reason="test",
            degraded=False,
        )

        with patch(
            "nexus.orchestration.progressive.AdaptiveRoutingService"
        ) as routing_cls, patch(
            "nexus.orchestration.progressive.execute_worker"
        ) as execute_worker_mock, patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:

            routing_cls.return_value.select_route_for_capability.return_value = (
                decision
            )

            execute_worker_mock.return_value = {
                "status": "COMPLETED",
                "agent_id": "AGENT-WORKER",
                "worktree": r"C:\fake\worker",
            }

            review_worker_mock.return_value = {
                "status": "COMPLETED",
                "reviewer_id": "AGENT-REVIEWER",
                "review": {
                    "verdict": "PASS",
                    "failure_class": None,
                    "summary": "Approved.",
                },
                "routing": {
                    "model": "cc/claude-sonnet-5-high",
                    "provider": "claude",
                    "effort": "high",
                    "execution_path": "OMNIROUTE",
                    "reason": "qualified",
                    "degraded": False,
                },
            }

            outcome = execute_progressively(
                run_id=run_id,
                repo=r"C:\fake\repo",
                manager_id="AGENT-MANAGER",
                original_task="Do the thing.",
                planned_worker=VALID_PLAN["workers"][0],
                plan_risk="LOW",
                worker_ordinal=2,
            )

        self.assertEqual(outcome["status"], "COMPLETED")

        checkpoints = list_checkpoints(run_id)

        worker = next(
            checkpoint
            for checkpoint in checkpoints
            if checkpoint["boundary"] == "WORKER_ATTEMPT"
        )

        review = next(
            checkpoint
            for checkpoint in checkpoints
            if checkpoint["boundary"] == "REVIEW"
        )

        self.assertEqual(worker["worker_ordinal"], 2)
        self.assertEqual(worker["payload"]["attempt"], 1)
        self.assertEqual(
            worker["payload"]["worker_id"],
            "AGENT-WORKER",
        )
        self.assertEqual(
            worker["payload"]["execution_path"],
            "OMNIROUTE",
        )

        self.assertEqual(review["payload"]["attempt"], 1)
        self.assertEqual(
            review["payload"]["worker_id"],
            "AGENT-WORKER",
        )
        self.assertEqual(
            review["payload"]["reviewer_id"],
            "AGENT-REVIEWER",
        )
        self.assertEqual(
            review["payload"]["reviewer_provider"],
            "claude",
        )
        self.assertEqual(
            review["payload"]["reviewer_execution_path"],
            "OMNIROUTE",
        )


if __name__ == "__main__":
    unittest.main()
