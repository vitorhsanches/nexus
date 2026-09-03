import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.orchestration.go import GoError, run_go
from nexus.registry.runs import get_run


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


class GoOperationalFailureTestCase(unittest.TestCase):
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

    def test_unresolved_project_prevents_manager_launch(self):
        with patch(
            "nexus.orchestration.go.run_manager"
        ) as mock_manager:
            with self.assertRaises(GoError) as ctx:
                run_go("Do something unrelated to any registered project")

            mock_manager.assert_not_called()

        self.assertEqual(ctx.exception.code, "PROJECT_NOT_FOUND")

    def test_manager_launch_failure_raises_manager_blocked(self):
        with patch(
            "nexus.orchestration.go.run_manager",
            side_effect=FileNotFoundError("Codex executable not found"),
        ):
            with self.assertRaises(GoError) as ctx:
                run_go("Norte: fix a bug", project_query="norte")

        self.assertEqual(ctx.exception.code, "MANAGER_BLOCKED")
        run = get_run(ctx.exception.run_id)
        self.assertEqual(run["status"], "BLOCKED")

    def test_manager_launch_failed_status_raises_manager_blocked(self):
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-0001",
                "status": "LAUNCH_FAILED",
                "exit_code": None,
                "plan": None,
                "error": "Codex executable not found",
            },
        ):
            with self.assertRaises(GoError) as ctx:
                run_go("Norte: fix a bug", project_query="norte")

        self.assertEqual(ctx.exception.code, "MANAGER_BLOCKED")
        run = get_run(ctx.exception.run_id)
        self.assertEqual(run["status"], "BLOCKED")

    def test_manager_planning_operational_failure_raises_plan_invalid(self):
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-0001",
                "status": "BLOCKED",
                "exit_code": 0,
                "plan": None,
                "error": "Manager did not return a NEXUS plan envelope.",
            },
        ):
            with self.assertRaises(GoError) as ctx:
                run_go("Norte: fix a bug", project_query="norte")

        self.assertEqual(ctx.exception.code, "PLAN_INVALID")
        run = get_run(ctx.exception.run_id)
        self.assertEqual(run["status"], "BLOCKED")

    def test_executor_exception_raises_worker_execution_failed(self):
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
                "status": "FAILED",
                "reason": "WORKER_EXECUTION_FAILED",
                "history": [],
                "worker": {"status": "FAILED", "error": "boom"},
            },
        ):
            with self.assertRaises(GoError) as ctx:
                run_go("Norte: fix a bug", project_query="norte")

        self.assertEqual(ctx.exception.code, "WORKER_EXECUTION_FAILED")
        run = get_run(ctx.exception.run_id)
        self.assertEqual(run["status"], "FAILED")

    def test_progressive_execution_os_error_is_translated(self):
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
            side_effect=OSError("launcher unavailable"),
        ):
            with self.assertRaises(GoError) as ctx:
                run_go("Norte: fix a bug", project_query="norte")

        self.assertEqual(ctx.exception.code, "ESCALATION_UNAVAILABLE")
        run = get_run(ctx.exception.run_id)
        self.assertEqual(run["status"], "BLOCKED")

    def test_progressive_execution_unexpected_runtime_error_propagates(self):
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
            side_effect=RuntimeError("unexpected programming error"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                run_go("Norte: fix a bug", project_query="norte")

        self.assertNotIsInstance(ctx.exception, GoError)

    def test_manager_launch_unexpected_runtime_error_propagates(self):
        with patch(
            "nexus.orchestration.go.run_manager",
            side_effect=RuntimeError("unexpected programming error"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                run_go("Norte: fix a bug", project_query="norte")

        self.assertNotIsInstance(ctx.exception, GoError)

    def test_review_blocked_propagates_as_review_blocked(self):
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
                "status": "BLOCKED",
                "reason": "REVIEW_FAILED",
                "history": [],
                "review": {
                    "reviewer_id": "AGT-0002",
                    "status": "BLOCKED",
                    "review": None,
                    "exit_code": 0,
                    "error": "No valid review envelope found.",
                },
            },
        ):
            with self.assertRaises(GoError) as ctx:
                run_go("Norte: fix a bug", project_query="norte")

        self.assertEqual(ctx.exception.code, "REVIEW_BLOCKED")
        run = get_run(ctx.exception.run_id)
        self.assertEqual(run["status"], "BLOCKED")

    def test_escalation_unavailable_propagates(self):
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
                "status": "BLOCKED",
                "verdict": "ESCALATE",
                "reason": "ESCALATION_UNAVAILABLE",
                "error": "No stronger approved route.",
                "history": [],
                "review": {},
            },
        ):
            with self.assertRaises(GoError) as ctx:
                run_go("Norte: fix a bug", project_query="norte")

        self.assertEqual(ctx.exception.code, "ESCALATION_UNAVAILABLE")
        run = get_run(ctx.exception.run_id)
        self.assertEqual(run["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
