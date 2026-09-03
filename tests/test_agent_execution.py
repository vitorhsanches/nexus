import unittest

import nexus.tasks.registry as task_registry
from nexus.agents.executor import AgentExecutor
from nexus.agents.lifecycle import InvalidTransitionError, can_transition, transition
from nexus.agents.models import Agent, TaskExecutionResult
from nexus.agents.registry import (
    AgentNotFoundError,
    AgentRegistry,
    NoAvailableAgentError,
)
from nexus.tasks.registry import create_task, get_task


class AgentModelsTestCase(unittest.TestCase):
    def test_agent_defaults(self):
        agent = Agent(
            agent_id="AGT-1",
            name="Alpha",
            provider="openai",
            model="gpt-5",
        )
        self.assertEqual(agent.status, "AVAILABLE")
        self.assertIsNone(agent.capabilities)

    def test_agent_capabilities_copied(self):
        caps = ["coding", "review"]
        agent = Agent(
            agent_id="AGT-1",
            name="Alpha",
            provider="openai",
            model="gpt-5",
            capabilities=caps,
        )
        self.assertEqual(agent.capabilities, ["coding", "review"])
        caps.append("mutated")
        self.assertEqual(agent.capabilities, ["coding", "review"])

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValueError):
            Agent(
                agent_id="AGT-1",
                name="Alpha",
                provider="openai",
                model="gpt-5",
                status="BOGUS",
            )

    def test_task_execution_result_fields(self):
        result = TaskExecutionResult(
            task_id="TASK-1",
            agent_id="AGT-1",
            status="COMPLETED",
            output="done",
            error=None,
        )
        self.assertEqual(result.task_id, "TASK-1")
        self.assertEqual(result.agent_id, "AGT-1")
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.output, "done")


class AgentLifecycleTestCase(unittest.TestCase):
    def test_supported_transitions(self):
        self.assertTrue(can_transition("AVAILABLE", "BUSY"))
        self.assertTrue(can_transition("BUSY", "AVAILABLE"))
        self.assertTrue(can_transition("BUSY", "FAILED"))
        self.assertTrue(can_transition("FAILED", "OFFLINE"))
        self.assertEqual(transition("AVAILABLE", "BUSY"), "BUSY")

    def test_invalid_transition_rejected(self):
        with self.assertRaises(InvalidTransitionError):
            transition("AVAILABLE", "OFFLINE")

    def test_invalid_from_status_rejected(self):
        with self.assertRaises(InvalidTransitionError):
            transition("BOGUS", "AVAILABLE")

    def test_offline_is_terminal(self):
        for candidate in ("AVAILABLE", "BUSY", "FAILED", "OFFLINE"):
            self.assertFalse(can_transition("OFFLINE", candidate))


class AgentRegistryTestCase(unittest.TestCase):
    def setUp(self):
        self.registry = AgentRegistry()

    def test_register_and_get(self):
        agent = self.registry.register_agent(
            Agent(agent_id="AGT-1", name="Alpha", provider="openai", model="gpt-5")
        )
        self.assertIs(self.registry.get_agent("AGT-1"), agent)

    def test_multiple_agents(self):
        self.registry.register_agent(
            Agent(agent_id="AGT-1", name="Alpha", provider="openai", model="gpt-5")
        )
        self.registry.register_agent(
            Agent(agent_id="AGT-2", name="Beta", provider="anthropic", model="claude")
        )
        self.assertEqual(len(self.registry.list_agents()), 2)
        self.assertEqual(
            {a.agent_id for a in self.registry.list_agents()}, {"AGT-1", "AGT-2"}
        )

    def test_get_missing_agent_raises(self):
        with self.assertRaises(AgentNotFoundError):
            self.registry.get_agent("AGT-MISSING")

    def test_find_available_agent(self):
        self.registry.register_agent(
            Agent(agent_id="AGT-1", name="Alpha", provider="openai", model="gpt-5")
        )
        agent = self.registry.find_available_agent()
        self.assertEqual(agent.agent_id, "AGT-1")

    def test_find_available_agent_respects_capability(self):
        self.registry.register_agent(
            Agent(
                agent_id="AGT-CODING",
                name="Alpha",
                provider="openai",
                model="gpt-5",
                capabilities=["coding"],
            )
        )
        self.registry.register_agent(
            Agent(
                agent_id="AGT-REVIEW",
                name="Beta",
                provider="anthropic",
                model="claude",
                capabilities=["review"],
            )
        )
        agent = self.registry.find_available_agent(capability="review")
        self.assertEqual(agent.agent_id, "AGT-REVIEW")

    def test_find_available_agent_skips_busy(self):
        busy = Agent(
            agent_id="AGT-1", name="Alpha", provider="openai", model="gpt-5"
        )
        free = Agent(
            agent_id="AGT-2", name="Beta", provider="anthropic", model="claude"
        )
        self.registry.register_agent(busy)
        self.registry.register_agent(free)
        self.registry.update_agent_status("AGT-1", "BUSY")
        agent = self.registry.find_available_agent()
        self.assertEqual(agent.agent_id, "AGT-2")

    def test_no_matching_capability_raises(self):
        self.registry.register_agent(
            Agent(
                agent_id="AGT-1",
                name="Alpha",
                provider="openai",
                model="gpt-5",
                capabilities=["coding"],
            )
        )
        with self.assertRaises(NoAvailableAgentError):
            self.registry.find_available_agent(capability="review")


class AgentExecutorTestCase(unittest.TestCase):
    def setUp(self):
        task_registry._tasks.clear()

        self.registry = AgentRegistry()
        self.executor = AgentExecutor(self.registry)

        self.agent = self.registry.register_agent(
            Agent(
                agent_id="AGT-1",
                name="Alpha",
                provider="openai",
                model="gpt-5",
                capabilities=["coding"],
            )
        )
        self.task = create_task(mission_id="MISSION-1", title="Build the executor")

    def test_execution_with_assigned_agent(self):
        result = self.executor.execute_task(self.task.task_id, agent_id="AGT-1")

        self.assertIsInstance(result, TaskExecutionResult)
        self.assertEqual(result.task_id, self.task.task_id)
        self.assertEqual(result.agent_id, "AGT-1")
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.output, "Task executed successfully by agent")
        self.assertIsNone(result.error)

        task = get_task(self.task.task_id)
        self.assertEqual(task.status, "COMPLETED")
        self.assertEqual(task.assigned_agent, "AGT-1")
        # Agent released back to available after success.
        self.assertEqual(self.registry.get_agent("AGT-1").status, "AVAILABLE")

    def test_automatic_agent_selection(self):
        result = self.executor.execute_task(self.task.task_id)

        self.assertEqual(result.agent_id, "AGT-1")
        task = get_task(self.task.task_id)
        self.assertEqual(task.assigned_agent, "AGT-1")
        self.assertEqual(task.status, "COMPLETED")
        self.assertEqual(self.registry.get_agent("AGT-1").status, "AVAILABLE")


if __name__ == "__main__":
    unittest.main()

