"""Focused tests for the real Runs/Agents operational web/read projection.

Proves:
  1. Real Runs/Agents created via nexus.registry.runs/nexus.registry.agents
     are visible through nexus.web.operational without touching
     nexus.tasks.registry.
  2. Manager/Worker/ManagerReview agent data is exposed per Run.
  3. Plan risk selected by the Manager is persisted into runs.risk via
     nexus.orchestration.go.run_go and survives in the operational
     projection.
  4. Reviewer routing metadata (reason/degraded/execution_path) is
     persisted through nexus.dispatchers.review.review_worker and
     recoverable from SQLite via the operational projection.
  5. The existing Mission/Task Board projection (nexus.web.services) is
     unaffected by any of this.
  6. No schema/migration changes were introduced: nexus.registry.database's
     CREATE TABLE statements are unchanged in shape (columns untouched).
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.dispatchers.review import review_worker
from nexus.orchestration.go import run_go
from nexus.registry.agents import create_agent
from nexus.registry.runs import create_run, get_run
from nexus.routing.service import RoutingDecision
from nexus.web import operational


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


class _FakeRoutingService:
    def __init__(self, decision):
        self._decision = decision
        self.calls = []

    def select_route_for_capability(self, capability, risk_level=None):
        self.calls.append((capability, risk_level))
        return self._decision


class _FakeProcess:
    def __init__(self, output):
        self.stdout = iter(output.splitlines(keepends=True))

    def wait(self):
        return 0


PASS_OUTPUT = (
    "NEXUS_REVIEW_BEGIN\n"
    + json.dumps(
        {
            "verdict": "PASS",
            "failure_class": None,
            "summary": "Looks good.",
            "evidence": ["git status clean"],
        }
    )
    + "\nNEXUS_REVIEW_END\n"
)


class OperationalRegistryProjectionTestCase(unittest.TestCase):
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

    def test_real_run_and_agents_are_visible_in_operational_projection(self):
        run_id = create_run(
            project_id="norte", input_text="do the thing", intent="GO"
        )
        manager_id = create_agent(
            run_id=run_id,
            role="Manager",
            provider="codex",
            model="gpt-5.6-luna",
            effort="low",
            status="COMPLETED",
        )
        worker_id = create_agent(
            run_id=run_id,
            role="Worker",
            provider="omniroute",
            model="oc/big-pickle",
            effort="low",
            status="COMPLETED",
            parent_agent_id=manager_id,
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

        runs = operational.get_operational_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["id"], run_id)

        agents = operational.get_operational_agents(run_id=run_id)
        roles = {agent["id"]: agent["role"] for agent in agents}
        self.assertEqual(roles[manager_id], "Manager")
        self.assertEqual(roles[worker_id], "Worker")
        self.assertEqual(roles[reviewer_id], "ManagerReview")

        detail = operational.get_operational_run_detail(run_id)
        self.assertEqual(detail["id"], run_id)
        self.assertEqual(len(detail["agents"]), 3)

    def test_unknown_run_detail_returns_none(self):
        self.assertIsNone(operational.get_operational_run_detail("RUN-MISSING"))

    def test_run_go_persists_plan_risk_into_runs_registry(self):
        fake_project = type(
            "FakeProject",
            (),
            {"id": "norte", "name": "Norte", "path": r"C:\fake\interface-life"},
        )()

        manager_result = {
            "status": "COMPLETED",
            "manager_id": "MANAGER-1",
            "plan": {
                "risk": "HIGH",
                "summary": "did the risky thing",
                "workers": [],
            },
        }

        with patch(
            "nexus.orchestration.go.resolve_project_from_text",
            return_value=fake_project,
        ), patch(
            "nexus.orchestration.go.run_manager",
            return_value=manager_result,
        ):
            result = run_go("do the risky thing", project_query="norte")

        run_id = result["run_id"]
        stored_run = get_run(run_id)
        self.assertEqual(stored_run["risk"], "HIGH")

        operational_run = operational.get_operational_run(run_id)
        self.assertEqual(operational_run["risk"], "HIGH")

    def test_reviewer_routing_metadata_persisted_and_recoverable(self):
        run_id = create_run(
            project_id="norte", input_text="do the thing", intent="GO"
        )
        worker_id = create_agent(
            run_id=run_id,
            role="Worker",
            provider="omniroute",
            model="oc/big-pickle",
            effort="low",
            status="COMPLETED",
        )

        decision = RoutingDecision(
            model="cc/claude-sonnet-5-low",
            provider="claude",
            effort="low",
            execution_path="OMNIROUTE",
            reason="capability match",
            degraded=False,
        )
        fake_service = _FakeRoutingService(decision)

        with patch("nexus.dispatchers.review._find_codex", return_value="C:/fake/codex.exe"):
            review_result = review_worker(
                run_id=run_id,
                worker_id=worker_id,
                worktree="C:/fake/worktree",
                original_task="do the thing",
                worker_scope="fix things",
                routing_service=fake_service,
                process_launcher=lambda command: _FakeProcess(PASS_OUTPUT),
            )

        self.assertEqual(review_result["status"], "COMPLETED")
        reviewer_id = review_result["reviewer_id"]

        history = operational.get_reviewer_routing_history(run_id=run_id)
        self.assertEqual(len(history), 1)
        entry = history[0]
        self.assertEqual(entry["reviewer_id"], reviewer_id)
        self.assertEqual(entry["worker_id"], worker_id)
        self.assertIsNotNone(entry["routing"])
        self.assertEqual(entry["routing"]["reason"], "capability match")
        self.assertEqual(entry["routing"]["execution_path"], "OMNIROUTE")
        self.assertFalse(entry["routing"]["degraded"])

    def test_operational_projection_never_reads_tasks_registry(self):
        import nexus.web.operational as operational_module

        source = Path(operational_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import nexus.tasks", source)
        self.assertNotIn("from nexus.tasks import registry", source)
        self.assertNotIn("from nexus.tasks.registry", source)

    def test_board_projection_unaffected_by_operational_module(self):
        from nexus.tasks import registry as task_registry

        before = list(task_registry.list_tasks())

        create_run(project_id="norte", input_text="noise", intent="GO")

        after = list(task_registry.list_tasks())
        self.assertEqual(len(before), len(after))

    def test_no_schema_changes_in_database_module(self):
        source = Path(database.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS runs (\n"
            "                id TEXT PRIMARY KEY,\n"
            "                project_id TEXT NOT NULL,\n"
            "                input TEXT NOT NULL,\n"
            "                intent TEXT NOT NULL,\n"
            "                status TEXT NOT NULL,\n"
            "                risk TEXT,\n",
            source,
        )
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS agents (\n"
            "                id TEXT PRIMARY KEY,\n"
            "                run_id TEXT NOT NULL,\n"
            "                role TEXT NOT NULL,\n"
            "                provider TEXT NOT NULL,\n"
            "                model TEXT NOT NULL,\n"
            "                effort TEXT NOT NULL,\n"
            "                status TEXT NOT NULL,\n"
            "                branch TEXT,\n"
            "                worktree TEXT,\n",
            source,
        )


if __name__ == "__main__":
    unittest.main()
