"""Focused tests for the Operational Runs panel added to the web board UI.

Scope: board.html/board.js/board.css presentation wiring only. These tests
exercise the existing read-only /api/operational/* endpoints (already
covered functionally by test_operational_registry_projection.py) purely to
confirm the UI-facing contract the frontend depends on: run listing,
run detail with agent lineage, and reviewer routing metadata that may be
null for legacy ManagerReview agents. No orchestration/schema/write paths
are touched.
"""

import json
import tempfile
import unittest
from pathlib import Path

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.registry.agents import create_agent
from nexus.registry.runs import create_run


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


class OperationalBoardStaticAssetsTestCase(unittest.TestCase):
    """The board template/script/style expose the operational panel hooks."""

    def test_board_html_has_operational_panel_markup(self):
        web_dir = Path(__file__).resolve().parents[1] / "nexus" / "web"
        html = (web_dir / "templates" / "board.html").read_text(encoding="utf-8")
        self.assertIn('id="panel-operational"', html)
        self.assertIn('id="operational-run-list"', html)
        self.assertIn('id="operational-run-detail"', html)

    def test_board_js_wires_operational_endpoints_and_isolates_failures(self):
        web_dir = Path(__file__).resolve().parents[1] / "nexus" / "web"
        js = (web_dir / "static" / "js" / "board.js").read_text(encoding="utf-8")
        self.assertIn("/api/operational/runs", js)
        self.assertIn("function loadOperational", js)
        self.assertIn("function selectOperationalRun", js)
        # Operational fetch failures must not break the core board load chain.
        self.assertIn("showOperationalError", js)

    def test_board_css_styles_operational_panel(self):
        web_dir = Path(__file__).resolve().parents[1] / "nexus" / "web"
        css = (web_dir / "static" / "css" / "board.css").read_text(encoding="utf-8")
        self.assertIn(".panel-operational", css)
        self.assertIn(".operational-run-card", css)
        self.assertIn(".operational-agent-lineage", css)


class OperationalBoardApiContractTestCase(unittest.TestCase):
    """The /api/operational/* contract the board UI depends on."""

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp_dir.name)

        self._original_db_path = database.DATABASE_PATH
        self._original_storage_dir = database.STORAGE_DIR
        self._original_projects_file = projects.PROJECTS_FILE

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus-test.db"

        fixture_path = tmp_path / "projects.json"
        fixture_path.write_text(json.dumps(FIXTURE_PROJECTS), encoding="utf-8")
        projects.PROJECTS_FILE = fixture_path

        database.initialize_database()
        projects.sync_projects()

    def tearDown(self) -> None:
        database.DATABASE_PATH = self._original_db_path
        database.STORAGE_DIR = self._original_storage_dir
        projects.PROJECTS_FILE = self._original_projects_file
        self._tmp_dir.cleanup()

    def test_run_detail_endpoint_includes_agent_lineage_for_ui(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:  # pragma: no cover
            self.skipTest("TestClient not installed")

        from nexus.web.app import app

        run_id = create_run(project_id="norte", input_text="do the thing", intent="GO")
        manager_id = create_agent(
            run_id=run_id,
            role="Manager",
            provider="codex",
            model="gpt-5.6-luna",
            effort="low",
            status="COMPLETED",
        )
        create_agent(
            run_id=run_id,
            role="Worker",
            provider="omniroute",
            model="oc/big-pickle",
            effort="low",
            status="COMPLETED",
            parent_agent_id=manager_id,
        )

        client = TestClient(app)
        with client:
            listing = client.get("/api/operational/runs")
            self.assertEqual(listing.status_code, 200)
            run_ids = {r["id"] for r in listing.json()["runs"]}
            self.assertIn(run_id, run_ids)

            detail = client.get(f"/api/operational/runs/{run_id}")
            self.assertEqual(detail.status_code, 200)
            body = detail.json()["run"]
            self.assertEqual(body["id"], run_id)
            roles = {a["role"] for a in body["agents"]}
            self.assertEqual(roles, {"Manager", "Worker"})

    def test_run_detail_missing_returns_404_for_ui_error_handling(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:  # pragma: no cover
            self.skipTest("TestClient not installed")

        from nexus.web.app import app

        client = TestClient(app)
        with client:
            response = client.get("/api/operational/runs/RUN-MISSING")
            self.assertEqual(response.status_code, 404)

    def test_reviewer_routing_is_null_for_legacy_manager_review_agents(self):
        """ManagerReview agents with no persisted routing metadata (legacy
        rows, or rows where branch is NULL) must expose reviewer_routing as
        None rather than raising, so the board UI can render a legacy badge
        instead of crashing on missing routing fields."""
        try:
            from starlette.testclient import TestClient
        except ImportError:  # pragma: no cover
            self.skipTest("TestClient not installed")

        from nexus.web.app import app

        run_id = create_run(project_id="norte", input_text="do the thing", intent="GO")
        worker_id = create_agent(
            run_id=run_id,
            role="Worker",
            provider="omniroute",
            model="oc/big-pickle",
            effort="low",
            status="COMPLETED",
        )
        reviewer_id = create_agent(
            run_id=run_id,
            role="ManagerReview",
            provider="codex",
            model="cc/claude-sonnet-5-low",
            effort="low",
            status="COMPLETED",
            parent_agent_id=worker_id,
        )

        client = TestClient(app)
        with client:
            agents = client.get(f"/api/operational/agents?run_id={run_id}").json()["agents"]
            reviewer = next(a for a in agents if a["id"] == reviewer_id)
            self.assertIsNone(reviewer["reviewer_routing"])

            detail = client.get(f"/api/operational/runs/{run_id}").json()["run"]
            detail_reviewer = next(a for a in detail["agents"] if a["id"] == reviewer_id)
            self.assertIsNone(detail_reviewer["reviewer_routing"])


if __name__ == "__main__":
    unittest.main()
