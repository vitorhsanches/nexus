"""Tests for the Nexus Agent Bootstrap V1.

Covers seeding the default agent pool, the idempotency of repeated
initialization, capability-routing of each default agent, and that demo tasks
route to the expected default agent through the execute endpoint.
"""
import unittest

import nexus.missions.service as mission_service
import nexus.tasks.registry as task_registry
import nexus.workspaces.registry as session_registry
from nexus.agents.bootstrap import DEFAULT_AGENTS, initialize_default_agents
from nexus.agents.registry import AgentRegistry
from nexus.capabilities.router import select_agent_for_capabilities
from nexus.web.agents import agent_registry


def _reset():
    """Clear the in-memory registries so each test starts clean."""
    mission_service._missions.clear()
    task_registry._tasks.clear()
    task_registry._attempts.clear()
    task_registry._next_attempt.clear()
    session_registry._sessions.clear()
    agent_registry._agents.clear()


class BootstrapTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_initialize_default_agents_creates_agents(self):
        created = initialize_default_agents()

        self.assertEqual(len(created), 3)
        agents = agent_registry.list_agents()
        self.assertEqual(len(agents), 3)
        by_id = {agent.agent_id: agent for agent in agents}
        self.assertEqual(
            set(by_id),
            {"analysis-agent", "architect-agent", "developer-agent"},
        )
        analysis = by_id["analysis-agent"]
        self.assertEqual(analysis.name, "Analysis Agent")
        self.assertEqual(analysis.provider, "omniroute")
        self.assertEqual(analysis.model, "analysis-agent")
        self.assertEqual(analysis.capabilities, ["analysis", "requirements"])
        self.assertEqual(analysis.status, "AVAILABLE")

    def test_initialize_twice_does_not_duplicate_agents(self):
        initialize_default_agents()
        second = initialize_default_agents()

        self.assertEqual(second, [])
        self.assertEqual(len(agent_registry.list_agents()), 3)

    def test_custom_registry_can_be_seeded_idempotently(self):
        registry = AgentRegistry()
        created = initialize_default_agents(registry)
        second = initialize_default_agents(registry)

        self.assertEqual(len(created), 3)
        self.assertEqual(second, [])
        self.assertEqual(len(registry.list_agents()), 3)

    def test_defaults_definition_is_well_formed(self):
        self.assertEqual(len(DEFAULT_AGENTS), 3)
        for definition in DEFAULT_AGENTS:
            self.assertIsInstance(definition["agent_id"], str)
            self.assertIsInstance(definition["capabilities"], list)


class BootstrapRouterTestCase(unittest.TestCase):
    """Verifies each default agent can be selected by the capability router."""

    def setUp(self):
        _reset()
        initialize_default_agents()

    def test_router_selects_default_agents(self):
        cases = {
            "analysis-agent": ["analysis"],
            "architect-agent": ["architecture"],
            "developer-agent": ["coding"],
        }
        for expected_agent_id, required in cases.items():
            with self.subTest(agent_id=expected_agent_id):
                agent = select_agent_for_capabilities(required, agent_registry)
                self.assertEqual(agent.agent_id, expected_agent_id)

    def test_router_selects_secondary_default_capability(self):
        cases = {
            "analysis-agent": ["requirements"],
            "architect-agent": ["design"],
            "developer-agent": ["implementation"],
        }
        for expected_agent_id, required in cases.items():
            with self.subTest(agent_id=expected_agent_id):
                agent = select_agent_for_capabilities(required, agent_registry)
                self.assertEqual(agent.agent_id, expected_agent_id)


class BootstrapDemoRoutingTestCase(unittest.TestCase):
    """Verifies demo tasks carry required capabilities and route correctly."""

    def setUp(self):
        _reset()
        initialize_default_agents()

    def _create_demo_mission(self):
        import nexus.web.services as web_services

        return web_services.create_demo_mission(run_id="RUN-DEMO")

    def test_demo_tasks_route_to_correct_agent(self):
        mission = self._create_demo_mission()

        expectations = {
            "Analyze authentication requirements": "analysis-agent",
            "Design authentication architecture": "architect-agent",
            "Implement authentication flow": "developer-agent",
        }

        for task in mission.tasks:
            expected = expectations[task.title]
            with self.subTest(task=task.title):
                self.assertIn(
                    "required_capabilities",
                    task.execution_policy,
                    "demo task must declare required capabilities",
                )
                agent = select_agent_for_capabilities(
                    task.execution_policy["required_capabilities"],
                    agent_registry,
                )
                self.assertEqual(agent.agent_id, expected)



class BootstrapExecuteEndpointTestCase(unittest.TestCase):
    """Verifies the execute endpoint succeeds after default bootstrap."""

    def setUp(self):
        _reset()
        initialize_default_agents()

    def test_execute_endpoint_succeeds_after_bootstrap(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:  # pragma: no cover
            self.skipTest("TestClient not installed")

        from nexus.web.app import app
        import nexus.web.services as web_services

        mission = web_services.create_demo_mission(run_id="RUN-DEMO")

        client = TestClient(app)
        with client:
            for task in mission.tasks:
                response = client.post(f"/api/tasks/{task.task_id}/execute")
                self.assertEqual(response.status_code, 200, task.title)
                summary = response.json()["execution"]
                self.assertEqual(summary["status"], "COMPLETED")
                self.assertIn(summary["assigned_agent"], {
                    "analysis-agent",
                    "architect-agent",
                    "developer-agent",
                })


if __name__ == "__main__":
    unittest.main()
