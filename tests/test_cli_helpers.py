import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.orchestration.go import GoError, run_go
from nexus.orchestration.show import RunNotFoundError, format_run_report
from nexus.registry.agents import create_agent
from nexus.registry.runs import create_run, update_run_status


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


class NexusCliHelpersTestCase(unittest.TestCase):
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

    def test_go_unresolved_project_prevents_manager_launch(self):
        with self.assertRaises(GoError) as ctx:
            run_go("Do something unrelated to any registered project")

        self.assertEqual(ctx.exception.code, "PROJECT_NOT_FOUND")

    def test_show_existing_run(self):
        run_id = create_run(
            project_id="norte",
            input_text="Sample request",
            intent="GO",
            status="RUNNING",
            risk="LOW",
        )

        create_agent(
            run_id=run_id,
            role="Manager",
            provider="codex",
            model="gpt-5.6-luna",
            effort="low",
            status="RUNNING",
        )

        report = format_run_report(run_id)

        self.assertIn(run_id, report)
        self.assertIn("Norte", report)
        self.assertIn("Manager", report)

    def test_show_unknown_run(self):
        with self.assertRaises(RunNotFoundError):
            format_run_report("RUN-DOESNOTEXIST")




class NexusCliGoArgumentTestCase(unittest.TestCase):
    """Verifies CLI --project wiring forwards to run_go without duplicating routing."""

    def test_cli_parses_project_before_request(self):
        from nexus.cli.app import main

        with patch(
            "sys.argv",
            ["nexus", "go", "--project", "Nexus", "request text"],
        ), patch("nexus.cli.app._bootstrap"), patch(
            "nexus.cli.app.run_go",
            return_value={"run_id": "RUN-X", "status": "COMPLETED"},
        ) as mock_run_go:
            main()

        mock_run_go.assert_called_once_with(
            "request text", project_query="Nexus"
        )

    def test_cli_parses_project_after_request(self):
        from nexus.cli.app import main

        with patch(
            "sys.argv",
            ["nexus", "go", "request text", "--project", "Nexus"],
        ), patch("nexus.cli.app._bootstrap"), patch(
            "nexus.cli.app.run_go",
            return_value={"run_id": "RUN-X", "status": "COMPLETED"},
        ) as mock_run_go:
            main()

        mock_run_go.assert_called_once_with(
            "request text", project_query="Nexus"
        )

    def test_cli_without_project_remains_backward_compatible(self):
        from nexus.cli.app import main

        with patch(
            "sys.argv",
            ["nexus", "go", "request text"],
        ), patch("nexus.cli.app._bootstrap"), patch(
            "nexus.cli.app.run_go",
            return_value={"run_id": "RUN-X", "status": "COMPLETED"},
        ) as mock_run_go:
            main()

        mock_run_go.assert_called_once_with("request text", project_query=None)


if __name__ == "__main__":
    unittest.main()
