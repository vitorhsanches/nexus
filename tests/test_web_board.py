"""Tests for the Nexus Local Mission Board UI V1."""

import unittest

import nexus.missions.service as mission_service
import nexus.tasks.registry as task_registry
import nexus.web.services as web_services
import nexus.workspaces.registry as session_registry
from nexus.agents.models import Agent
from nexus.web.agents import agent_registry
from nexus.workspaces.registry import create_session


def _reset():
    """Clear the in-memory registries so each test starts clean."""
    mission_service._missions.clear()
    task_registry._tasks.clear()
    task_registry._attempts.clear()
    task_registry._next_attempt.clear()
    session_registry._sessions.clear()
    agent_registry._agents.clear()


class WebServicesTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def _make_mission_with_tasks(self):
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Alpha mission"
        )
        task = task_registry.create_task(
            mission_id=mission.mission_id,
            title="Build the board",
            status="RUNNING",
        )
        return mission, task

    def test_get_missions_aggregates_board(self):
        mission, task = self._make_mission_with_tasks()

        missions = web_services.get_missions()

        self.assertEqual(len(missions), 1)
        self.assertEqual(missions[0]["mission_id"], mission.mission_id)
        self.assertEqual(missions[0]["title"], "Alpha mission")
        board = missions[0]["board"]
        self.assertIn(task.task_id, board["task_ids"])
        by_name = {c["name"]: c for c in board["columns"]}
        self.assertIn(task.task_id, by_name["RUNNING"]["task_ids"])

    def test_get_tasks_enriches_board_column(self):
        mission, task = self._make_mission_with_tasks()

        tasks = web_services.get_tasks()

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["board_column"], "RUNNING")
        self.assertEqual(tasks[0]["attempts"], [])

    def test_get_agents_includes_sessions(self):
        mission, task = self._make_mission_with_tasks()
        agent = agent_registry.register_agent(
            Agent(
                agent_id="AGT-1",
                name="Alpha",
                provider="openai",
                model="gpt-5",
                capabilities=["coding"],
            )
        )
        agent_registry.update_agent_status(agent.agent_id, "BUSY")
        session = create_session(
            task_id=task.task_id,
            agent_id=agent.agent_id,
            status="RUNNING",
        )

        agents = web_services.get_agents()

        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["agent_id"], "AGT-1")
        self.assertEqual(agents[0]["status"], "BUSY")
        self.assertEqual(len(agents[0]["sessions"]), 1)
        self.assertEqual(agents[0]["sessions"][0]["session_id"], session.session_id)

    def test_get_sessions_enriches_labels(self):
        mission, task = self._make_mission_with_tasks()
        agent_registry.register_agent(
            Agent(
                agent_id="AGT-1",
                name="Alpha",
                provider="openai",
                model="gpt-5",
            )
        )
        create_session(task_id=task.task_id, agent_id="AGT-1")

        sessions = web_services.get_sessions()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["agent_name"], "Alpha")
        self.assertEqual(sessions[0]["task_title"], "Build the board")
        self.assertEqual(sessions[0]["mission_id"], mission.mission_id)

    def test_get_board_returns_six_columns(self):
        self._make_mission_with_tasks()

        board = web_services.get_board()

        names = [c["name"] for c in board["columns"]]
        self.assertEqual(
            names,
            ["BACKLOG", "READY", "RUNNING", "REVIEW", "DONE", "FAILED"],
        )
        self.assertEqual(len(board["tasks"]), 1)

    def test_get_summary_counts(self):
        mission, task = self._make_mission_with_tasks()
        agent_registry.register_agent(
            Agent(
                agent_id="AGT-1",
                name="Alpha",
                provider="openai",
                model="gpt-5",
            )
        )
        create_session(task_id=task.task_id, agent_id="AGT-1")

        summary = web_services.get_summary()

        self.assertEqual(summary["missions"], 1)
        self.assertEqual(summary["tasks"], 1)
        self.assertEqual(summary["agents"], 1)
        self.assertEqual(summary["sessions"], 1)
        self.assertEqual(summary["active_sessions"], 1)


class WebAppTestCase(unittest.TestCase):
    """Validates the FastAPI app and its routes when dependencies exist.

    FastAPI, Starlette, and the test HTTP client are not bundled with the
    core Nexus package, so these checks emit ``skip`` (rather than fail) when
    they are unavailable in the local environment.
    """

    def setUp(self):
        _reset()

    def test_app_imports_and_serves_index(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:  # pragma: no cover
            self.skipTest("TestClient not installed")

        from nexus.web.app import app

        client = TestClient(app)
        with client:
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Nexus Mission Board", response.text)

    def test_create_mission_flow(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:  # pragma: no cover
            self.skipTest("TestClient not installed")

        from nexus.web.app import app

        client = TestClient(app)
        with client:
            created = client.post(
                "/api/missions",
                json={"title": "Ship the board", "description": "From the UI"},
            )
            self.assertEqual(created.status_code, 200)
            mission = created.json()["mission"]
            self.assertEqual(mission["title"], "Ship the board")
            self.assertEqual(mission["description"], "From the UI")

            missions = client.get("/api/missions").json()["missions"]
            self.assertIn(mission["mission_id"], {m["mission_id"] for m in missions})

    def test_create_mission_requires_title(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:  # pragma: no cover
            self.skipTest("TestClient not installed")

        from nexus.web.app import app

        client = TestClient(app)
        with client:
            response = client.post("/api/missions", json={"title": "   "})
            self.assertEqual(response.status_code, 422)

    def test_routes_respond(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:  # pragma: no cover
            self.skipTest("TestClient not installed")

        from nexus.web.app import app

        mission = mission_service.create_mission(
            run_id="RUN-1", title="Alpha mission"
        )
        task_registry.create_task(
            mission_id=mission.mission_id, title="Build the board"
        )

        client = TestClient(app)
        with client:
            for path in (
                "/api/missions",
                "/api/tasks",
                "/api/agents",
                "/api/sessions",
                "/api/board",
                "/api/summary",
            ):
                response = client.get(path)
                self.assertEqual(response.status_code, 200, path)
                self.assertIsInstance(response.json(), dict)


if __name__ == "__main__":
    unittest.main()