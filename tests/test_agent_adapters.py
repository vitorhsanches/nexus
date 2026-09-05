"""Tests for the Real Agent Adapter Layer V1."""

import unittest
from unittest.mock import patch

import nexus.tasks.registry as task_registry
from nexus.agents.adapters.base import AdapterResult, ExecutionContext
from nexus.agents.adapters.omniroute import (
    DEFAULT_WORKER_SCRIPT,
    MECHANICAL_MODEL,
    STANDARD_CODING_MODEL,
    OmniRouteAdapter,
    build_command,
    resolve_shell,
    select_route,
)
from nexus.agents.adapters.simulated import SimulatedAdapter
from nexus.agents.executor import AgentExecutor
from nexus.agents.models import Agent
from nexus.agents.registry import AgentRegistry
from nexus.tasks.registry import create_task, get_task
from nexus.routing.service import RoutingDecision


class SimulatedAdapterTestCase(unittest.TestCase):
    def test_simulated_adapter_is_default(self):
        registry = AgentRegistry()
        executor = AgentExecutor(registry)
        self.assertIsInstance(executor.adapter, SimulatedAdapter)

    def test_simulated_adapter_always_succeeds(self):
        adapter = SimulatedAdapter()
        result = adapter.run(
            ExecutionContext(task_id="T-1", task_title="Do a thing")
        )
        self.assertTrue(result.success)
        self.assertIsNone(result.error)


class RecordingAdapter:
    """Test double that records the context it receives."""

    def __init__(self, result):
        self.result = result
        self.received_context = None

    def run(self, context):
        self.received_context = context
        return self.result


class NoNetworkRoutingService:
    """Deterministic routing double for adapter unit tests."""

    def select_route_for_task(
        self,
        required_capabilities=None,
        execution_policy=None,
    ):
        model, effort = select_route(
            required_capabilities
        )
        provider = (
            "opencode"
            if model == MECHANICAL_MODEL
            else "claude"
        )
        return RoutingDecision(
            model=model,
            provider=provider,
            effort=effort,
            execution_path="OMNIROUTE",
            reason="offline unit-test routing double",
            fallbacks=(),
            degraded=False,
        )


class AgentExecutorAdapterIntegrationTestCase(unittest.TestCase):
    def setUp(self):
        task_registry._tasks.clear()
        self.registry = AgentRegistry()
        self.agent = self.registry.register_agent(
            Agent(
                agent_id="AGT-1",
                name="Alpha",
                provider="openai",
                model="gpt-5",
                capabilities=["coding"],
            )
        )
        self.task = create_task(
            mission_id="MISSION-1",
            title="Build the adapter",
            execution_policy={"required_capabilities": ["coding"]},
        )

    def test_executor_invokes_adapter(self):
        adapter = RecordingAdapter(AdapterResult(success=True, output="done"))
        executor = AgentExecutor(self.registry, adapter=adapter)

        result = executor.execute_task(self.task.task_id, agent_id="AGT-1")

        self.assertIsNotNone(adapter.received_context)
        self.assertEqual(adapter.received_context.task_id, self.task.task_id)
        self.assertEqual(adapter.received_context.task_title, "Build the adapter")
        self.assertEqual(adapter.received_context.required_capabilities, ["coding"])
        self.assertEqual(result.status, "COMPLETED")

    def test_adapter_success_completes_session_and_task(self):
        adapter = RecordingAdapter(AdapterResult(success=True, output="done"))
        executor = AgentExecutor(self.registry, adapter=adapter)

        executor.execute_task(self.task.task_id, agent_id="AGT-1")

        session = executor.last_session
        self.assertEqual(session.status, "COMPLETED")
        self.assertIsNone(session.error)
        self.assertEqual(get_task(self.task.task_id).status, "COMPLETED")
        self.assertEqual(self.registry.get_agent("AGT-1").status, "AVAILABLE")

    def test_adapter_failure_marks_session_and_agent_failed(self):
        adapter = RecordingAdapter(
            AdapterResult(success=False, output=None, error="boom")
        )
        executor = AgentExecutor(self.registry, adapter=adapter)

        with self.assertRaises(RuntimeError):
            executor.execute_task(self.task.task_id, agent_id="AGT-1")

        session = executor.last_session
        self.assertEqual(session.status, "FAILED")
        self.assertEqual(session.error, "boom")
        self.assertEqual(self.registry.get_agent("AGT-1").status, "FAILED")
        # Task never reaches COMPLETED when the adapter fails.
        self.assertNotEqual(get_task(self.task.task_id).status, "COMPLETED")


class RoutePolicyTestCase(unittest.TestCase):
    def test_mechanical_routes_to_big_pickle(self):
        model, effort = select_route(["mechanical"])
        self.assertEqual(model, MECHANICAL_MODEL)
        self.assertEqual(effort, "low")

    def test_standard_coding_routes_to_claude_sonnet(self):
        model, effort = select_route(["coding"])
        self.assertEqual(model, STANDARD_CODING_MODEL)
        self.assertEqual(effort, "low")

    def test_empty_capabilities_route_to_standard_coding(self):
        model, _ = select_route([])
        self.assertEqual(model, STANDARD_CODING_MODEL)

    def test_no_gpt_terra_in_route_pool(self):
        for capabilities in (["mechanical"], ["coding"], []):
            model, _ = select_route(capabilities)
            self.assertNotIn("terra", model.lower())


class OmniRouteCommandConstructionTestCase(unittest.TestCase):
    def test_build_command_passes_task_as_dash_task_flag(self):
        context = ExecutionContext(
            task_id="TASK-1",
            task_title="Fix the thing\nwith multiple lines",
            required_capabilities=["coding"],
            workspace_path="C:/repo",
        )
        command = build_command(context, script_path="worker.ps1", shell="powershell")

        self.assertIn("-Task", command)
        task_index = command.index("-Task")
        task_value = command[task_index + 1]

        self.assertIn("Fix the thing", task_value)
        self.assertIn("with multiple lines", task_value)
        self.assertIn("worker.ps1", command)
        self.assertIn("C:/repo", command)
        self.assertIn(STANDARD_CODING_MODEL, command)

    def test_build_command_multiline_prompt_is_value_of_task_flag(self):
        context = ExecutionContext(
            task_id="TASK-1",
            task_title="Line one",
            required_capabilities=["coding"],
            mission_context={"note": "second line context"},
        )
        command = build_command(context, script_path="worker.ps1", shell="powershell")

        task_index = command.index("-Task")
        task_value = command[task_index + 1]

        self.assertIn("\n", task_value)
        self.assertIn("Line one", task_value)
        self.assertIn("second line context", task_value)
        # The prompt must not appear anywhere else in the command list.
        other_args = command[:task_index] + command[task_index + 2 :]
        self.assertNotIn(task_value, other_args)

    def test_build_command_preserves_wrapper_path(self):
        context = ExecutionContext(task_id="TASK-5", task_title="Any")
        command = build_command(context, shell="powershell")
        self.assertIn(DEFAULT_WORKER_SCRIPT, command)
        self.assertIn("-File", command)

    def test_build_command_routes_mechanical_capability(self):
        context = ExecutionContext(
            task_id="TASK-2",
            task_title="Cleanup formatting",
            required_capabilities=["mechanical"],
        )
        command = build_command(context, script_path="worker.ps1", shell="powershell")
        self.assertIn(MECHANICAL_MODEL, command)

    def test_build_command_prefers_pwsh_when_available(self):
        context = ExecutionContext(task_id="TASK-6", task_title="Any")
        with patch("shutil.which", return_value=r"C:\pwsh\pwsh.exe"):
            self.assertEqual(resolve_shell(), "pwsh")

    def test_build_command_falls_back_to_powershell(self):
        context = ExecutionContext(task_id="TASK-7", task_title="Any")
        with patch("shutil.which", return_value=None):
            self.assertEqual(resolve_shell(), "powershell")


class OmniRouteAdapterRunnerTestCase(unittest.TestCase):
    def test_adapter_success_via_fake_runner(self):
        class FakeCompleted:
            returncode = 0
            stdout = "worker output"
            stderr = ""

        captured = {}

        def fake_runner(command):
            captured["command"] = command
            return FakeCompleted()

        adapter = OmniRouteAdapter(
            script_path="worker.ps1",
            runner=fake_runner,
            shell="powershell",
            routing_service=NoNetworkRoutingService(),
        )
        result = adapter.run(
            ExecutionContext(task_id="TASK-3", task_title="Do work")
        )
        self.assertTrue(result.success)
        self.assertEqual(result.output, "worker output")
        self.assertIn("-Task", captured["command"])

    def test_adapter_failure_via_fake_runner(self):
        class FakeCompleted:
            returncode = 1
            stdout = ""
            stderr = "worker exploded"

        def fake_runner(command):
            return FakeCompleted()

        adapter = OmniRouteAdapter(
            script_path="worker.ps1",
            runner=fake_runner,
            shell="powershell",
            routing_service=NoNetworkRoutingService(),
        )
        result = adapter.run(
            ExecutionContext(task_id="TASK-4", task_title="Do work")
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error, "worker exploded")

    def test_run_subprocess_does_not_pipe_task_to_stdin(self):
        """The real subprocess runner must not use stdin to deliver the task.

        The task is already embedded as the -Task argument value; passing
        stdin content here would be an unused/incorrect transport and could
        mask the -Task flag as the source of truth.
        """
        with patch("nexus.agents.adapters.omniroute.subprocess.run") as mock_run:
            mock_run.return_value = type(
                "Completed", (), {"returncode": 0, "stdout": "ok", "stderr": ""}
            )()

            adapter = OmniRouteAdapter(
                script_path="worker.ps1",
                shell="powershell",
                routing_service=NoNetworkRoutingService(),
            )
            adapter.run(
                ExecutionContext(
                    task_id="TASK-8",
                    task_title="Do work",
                )
            )

            _, kwargs = mock_run.call_args
            self.assertEqual(kwargs.get("input"), "")
            passed_command = mock_run.call_args[0][0]
            self.assertIn("-Task", passed_command)


if __name__ == "__main__":
    unittest.main()
