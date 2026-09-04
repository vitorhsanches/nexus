"""Tests for the Nexus Local Mission Board execution flow V1.

Covers the ``POST /api/tasks/{task_id}/execute`` endpoint and the underlying
execution orchestration service, verifying that executing a task drives the
existing Nexus pipelines: automatic agent selection through the capability
router, an execution attempt record, a workspace session, and the task status
lifecycle CREATED -> RUNNING -> COMPLETED.
"""

import unittest

import nexus.missions.service as mission_service
import nexus.tasks.registry as task_registry
import nexus.web.execution as execution_service
import nexus.web.services as web_services
import nexus.workspaces.registry as session_registry
from nexus.agents.models import Agent
from nexus.web.agents import agent_registry
from nexus.workspaces.registry import list_sessions


def _reset():
    """Clear the in-memory registries so each test starts clean."""
    mission_service._missions.clear()
    task_registry._tasks.clear()
    task_registry._attempts.clear()
    task_registry._next_attempt.clear()
    session_registry._sessions.clear()
    agent_registry._agents.clear()


def _make_task(**kwargs):
    mission = mission_service.create_mission(run_id="RUN-1", title="Mission")
    defaults = {
        "mission_id": mission.mission_id,
        "title": "Build the feature",
    }
    defaults.update(kwargs)
    return mission, task_registry.create_task(**defaults)


class ExecutionServiceTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def _register_agent(self, capabilities=None):
        return agent_registry.register_agent(
            Agent(
                agent_id="AGT-1",
                name="Alpha",
                provider="openai",
                model="gpt-5",
                capabilities=capabilities,
            )
        )

    def test_execute_summary_and_lifecycle(self):
        mission, task = _make_task()
        self._register_agent(capabilities=["coding"])
        self.assertEqual(task.status, "CREATED")

        summary = execution_service.execute_task(task.task_id)

        self.assertEqual(summary["task_id"], task.task_id)
        self.assertEqual(summary["status"], "COMPLETED")
        self.assertEqual(summary["assigned_agent"], "AGT-1")
        self.assertIsNotNone(summary["attempt_id"])
        self.assertIsNotNone(summary["session_id"])

        # Task lifecycle reached COMPLETED.
        stored = task_registry.get_task(task.task_id)
        self.assertEqual(stored.status, "COMPLETED")
        self.assertEqual(stored.assigned_agent, "AGT-1")

    def test_execute_creates_attempt(self):
        mission, task = _make_task()
        self._register_agent(capabilities=["coding"])

        summary = execution_service.execute_task(task.task_id)

        attempts = task_registry.list_attempts(task_id=task.task_id)
        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]
        self.assertEqual(attempt.attempt_id, summary["attempt_id"])
        self.assertEqual(attempt.task_id, task.task_id)
        self.assertEqual(attempt.agent_id, "AGT-1")
        self.assertEqual(attempt.model, "gpt-5")
        self.assertEqual(attempt.status, "COMPLETED")

    def test_execute_selects_agent_via_capability_router(self):
        # Register two agents; only one satisfies the required capability.
        mission, task = _make_task(execution_policy={"required_capabilities": ["coding"]})
        agent_registry.register_agent(
            Agent(
                agent_id="AGT-CODING",
                name="Coder",
                provider="openai",
                model="gpt-5",
                capabilities=["coding"],
            )
        )
        agent_registry.register_agent(
            Agent(
                agent_id="AGT-REVIEW",
                name="Reviewer",
                provider="anthropic",
                model="claude",
                capabilities=["review"],
            )
        )

        summary = execution_service.execute_task(task.task_id)

        self.assertEqual(summary["assigned_agent"], "AGT-CODING")
        task = task_registry.get_task(task.task_id)
        self.assertEqual(task.assigned_agent, "AGT-CODING")

    def test_execute_creates_workspace_session(self):
        mission, task = _make_task()
        self._register_agent(capabilities=["coding"])

        summary = execution_service.execute_task(task.task_id)

        sessions = list_sessions(task_id=task.task_id)
        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session.session_id, summary["session_id"])
        self.assertEqual(session.task_id, task.task_id)
        self.assertEqual(session.agent_id, "AGT-1")
        self.assertEqual(session.status, "COMPLETED")
        self.assertIsNotNone(session.started_at)
        self.assertIsNotNone(session.finished_at)

    def test_execute_missing_task_raises(self):
        with self.assertRaises(task_registry.TaskNotFoundError):
            execution_service.execute_task("TASK-DOES-NOT-EXIST")


class ExecuteEndpointTestCase(unittest.TestCase):
    """Validates the execute endpoint via the FastAPI test client."""

    def setUp(self):
        _reset()

    def test_execute_endpoint(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:  # pragma: no cover
            self.skipTest("TestClient not installed")

        from nexus.web.app import app

        mission, task = _make_task()
        agent_registry.register_agent(
            Agent(
                agent_id="AGT-1",
                name="Alpha",
                provider="openai",
                model="gpt-5",
                capabilities=["coding"],
            )
        )

        client = TestClient(app)
        with client:
            response = client.post(f"/api/tasks/{task.task_id}/execute")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            summary = payload["execution"]
            self.assertEqual(summary["task_id"], task.task_id)
            self.assertEqual(summary["status"], "COMPLETED")
            self.assertEqual(summary["assigned_agent"], "AGT-1")

            # Board reflects the COMPLETED task after execution.
            tasks = client.get("/api/tasks").json()["tasks"]
            self.assertEqual(tasks[0]["status"], "COMPLETED")
            self.assertEqual(tasks[0]["board_column"], "DONE")

    def test_execute_endpoint_missing_task_returns_404(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:  # pragma: no cover
            self.skipTest("TestClient not installed")

        from nexus.web.app import app

        client = TestClient(app)
        with client:
            response = client.post("/api/tasks/TASK-DOES-NOT-EXIST/execute")
            self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
