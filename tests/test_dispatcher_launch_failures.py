import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nexus.registry.database as database
from nexus.registry.agents import create_agent


class DispatcherLaunchFailureTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp_dir.name)

        self._original_db_path = database.DATABASE_PATH
        self._original_storage_dir = database.STORAGE_DIR

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus-test.db"

        database.initialize_database()

        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO projects (id, name, path, aliases_json, enabled)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("norte", "Norte", r"C:\fake\interface-life", "[]", 1),
            )
            connection.execute(
                """
                INSERT INTO runs (id, project_id, input, intent, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("RUN-TEST001", "norte", "task", "GO", "RUNNING", "now"),
            )

        self.worker_agent_id = create_agent(
            run_id="RUN-TEST001",
            role="Worker",
            provider="omniroute",
            model="oc/big-pickle",
            effort="low",
            status="COMPLETED",
        )

    def tearDown(self) -> None:
        database.DATABASE_PATH = self._original_db_path
        database.STORAGE_DIR = self._original_storage_dir
        self._tmp_dir.cleanup()

    def test_manager_missing_codex_returns_launch_failed(self):
        from nexus.dispatchers.manager import run_manager

        with patch(
            "nexus.dispatchers.manager._find_codex",
            side_effect=FileNotFoundError("Codex executable not found"),
        ):
            result = run_manager(
                run_id="RUN-TEST001",
                repo=r"C:\fake\interface-life",
                task="do something",
            )

        self.assertEqual(result["status"], "LAUNCH_FAILED")
        self.assertIn("Codex executable not found", result["error"])

    def test_review_missing_codex_returns_launch_failed(self):
        from nexus.dispatchers.review import review_worker

        with patch(
            "nexus.dispatchers.review._find_codex",
            side_effect=FileNotFoundError("Codex executable not found"),
        ):
            result = review_worker(
                run_id="RUN-TEST001",
                worker_id=self.worker_agent_id,
                worktree=r"C:\fake\worktree",
                original_task="do something",
                worker_scope="fix things",
            )

        self.assertEqual(result["status"], "LAUNCH_FAILED")
        self.assertIn("Codex executable not found", result["error"])

    def test_omniroute_missing_adapter_returns_launch_failed(self):
        from nexus.dispatchers.omniroute import run_omniroute_worker

        with patch(
            "nexus.dispatchers.omniroute.ADAPTER_PATH",
        ) as mock_path:
            mock_path.exists.return_value = False

            result = run_omniroute_worker(
                run_id="RUN-TEST001",
                repo=r"C:\fake\interface-life",
                task="do something",
                model="oc/big-pickle",
            )

        self.assertEqual(result["status"], "LAUNCH_FAILED")
        self.assertIn("adapter not found", result["error"])


if __name__ == "__main__":
    unittest.main()
