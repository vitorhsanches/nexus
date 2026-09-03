import unittest

import nexus.tasks.registry as task_registry
from nexus.agents.executor import AgentExecutor
from nexus.agents.models import Agent
from nexus.agents.registry import AgentRegistry
from nexus.capabilities.models import Capability
from nexus.capabilities.registry import (
    CapabilityNotFoundError,
    CapabilityRegistry,
    get_capability,
    list_capabilities,
    register_capability,
)
from nexus.capabilities.router import (
    NoSuitableAgentError,
    select_agent_for_capabilities,
)
from nexus.tasks.registry import create_task


class CapabilityModelsTestCase(unittest.TestCase):
    def test_capability_fields(self):
        capability = Capability(name="coding", description="Implement code changes")
        self.assertEqual(capability.name, "coding")
        self.assertEqual(capability.description, "Implement code changes")


class CapabilityRegistryTestCase(unittest.TestCase):
    def setUp(self):
        self.registry = CapabilityRegistry()

    def test_registration_and_lookup(self):
        registered = self.registry.register_capability(
            Capability(name="coding", description="Implement code changes")
        )
        self.assertIs(self.registry.get_capability("coding"), registered)

    def test_lookup_missing_raises(self):
        with self.assertRaises(CapabilityNotFoundError):
            self.registry.get_capability("missing")

    def test_list_capabilities(self):
        self.registry.register_capability(Capability("coding", "implement"))
        self.registry.register_capability(Capability("review", "review changes"))
        names = {c.name for c in self.registry.list_capabilities()}
        self.assertEqual(names, {"coding", "review"})

    def test_module_level_convenience(self):
        try:
            capability = register_capability(
                Capability(name="testing", description="Write and run tests")
            )
            self.assertIs(get_capability("testing"), capability)
            self.assertIn("testing", {c.name for c in list_capabilities()})
        finally:
            # Leave the shared module registry clean for other tests.
            from nexus.capabilities import registry as capability_registry

            capability_registry._registry._capabilities.pop("testing", None)


class CapabilityRouterTestCase(unittest.TestCase):
    def _build_registry(self, agents, busy=None):
        registry = AgentRegistry()
        for agent in agents:
            registry.register_agent(agent)
        for agent_id in busy or []:
            registry.update_agent_status(agent_id, "BUSY")
        return registry

    def test_exact_agent_match(self):
        registry = self._build_registry(
            [
                Agent(
                    agent_id="AGT-CODING",
                    name="Coder",
                    provider="openai",
                    model="gpt-5",
                    capabilities=["coding"],
                )
            ]
        )
        agent = select_agent_for_capabilities(["coding"], agent_registry=registry)
        self.assertEqual(agent.agent_id, "AGT-CODING")

    def test_best_agent_selection_prefers_tightest_match(self):
        registry = self._build_registry(
            [
                Agent(
                    agent_id="AGT-GENERAL",
                    name="Generalist",
                    provider="openai",
                    model="gpt-5",
                    capabilities=["coding", "review", "testing"],
                ),
                Agent(
                    agent_id="AGT-CODING",
                    name="Coder",
                    provider="anthropic",
                    model="claude",
                    capabilities=["coding"],
                ),
            ]
        )
        agent = select_agent_for_capabilities(["coding"], agent_registry=registry)
        self.assertEqual(agent.agent_id, "AGT-CODING")

    def test_no_available_agent_raises(self):
        registry = self._build_registry(
            [
                Agent(
                    agent_id="AGT-CODING",
                    name="Coder",
                    provider="openai",
                    model="gpt-5",
                    capabilities=["coding"],
                )
            ],
            busy=["AGT-CODING"],
        )
        with self.assertRaises(NoSuitableAgentError):
            select_agent_for_capabilities(["coding"], agent_registry=registry)

    def test_missing_capability_raises(self):
        registry = self._build_registry(
            [
                Agent(
                    agent_id="AGT-CODING",
                    name="Coder",
                    provider="openai",
                    model="gpt-5",
                    capabilities=["coding"],
                )
            ]
        )
        with self.assertRaises(NoSuitableAgentError):
            select_agent_for_capabilities(["review"], agent_registry=registry)

    def test_multiple_required_capabilities(self):
        registry = self._build_registry(
            [
                Agent(
                    agent_id="AGT-CODING",
                    name="Coder",
                    provider="openai",
                    model="gpt-5",
                    capabilities=["coding"],
                ),
                Agent(
                    agent_id="AGT-FULLSTACK",
                    name="Fullstack",
                    provider="anthropic",
                    model="claude",
                    capabilities=["coding", "testing"],
                ),
            ]
        )
        agent = select_agent_for_capabilities(
            ["coding", "testing"], agent_registry=registry
        )
        self.assertEqual(agent.agent_id, "AGT-FULLSTACK")


class CapabilityExecutorTestCase(unittest.TestCase):
    def setUp(self):
        task_registry._tasks.clear()

        self.registry = AgentRegistry()
        self.executor = AgentExecutor(self.registry)

        self.registry.register_agent(
            Agent(
                agent_id="AGT-CODING",
                name="Coder",
                provider="openai",
                model="gpt-5",
                capabilities=["coding"],
            )
        )

    def test_executor_automatic_selection(self):
        task = create_task(
            mission_id="MISSION-1",
            title="Build the router",
            execution_policy={"required_capabilities": ["coding"]},
        )

        result = self.executor.execute_task(task.task_id)

        self.assertEqual(result.agent_id, "AGT-CODING")
        self.assertEqual(self.registry.get_agent("AGT-CODING").status, "AVAILABLE")

    def test_executor_automatic_selection_without_policy(self):
        task = create_task(mission_id="MISSION-1", title="Build the router")

        result = self.executor.execute_task(task.task_id)

        self.assertEqual(result.agent_id, "AGT-CODING")

    def test_executor_no_suitable_agent_raises(self):
        task = create_task(
            mission_id="MISSION-1",
            title="Build the router",
            execution_policy={"required_capabilities": ["review"]},
        )

        with self.assertRaises(NoSuitableAgentError):
            self.executor.execute_task(task.task_id)


if __name__ == "__main__":
    unittest.main()
