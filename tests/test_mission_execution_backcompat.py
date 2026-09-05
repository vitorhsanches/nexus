"""Backwards-compatibility and OmniRoute-mocked flow tests for Nexus V1.8."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nexus.board.service as board_service
import nexus.missions.service as mission_service
import nexus.tasks.registry as task_registry
import nexus.web.execution as execution_service
import nexus.workspaces.registry as session_registry
from nexus.agents.adapters.base import AdapterResult, ExecutionContext
from nexus.agents.adapters.omniroute import build_prompt
from nexus.agents.bootstrap import initialize_default_agents
from nexus.agents.executor import AgentExecutor
from nexus.agents.models import Agent
from nexus.web.agents import agent_registry
from nexus.web.mission_execution import execute_mission


def _reset():
    mission_service._missions.clear()
    task_registry._tasks.clear()
    task_registry._attempts.clear()
    task_registry._next_attempt.clear()
    board_service._boards.clear()
    session_registry._sessions.clear()
    agent_registry._agents.clear()


def _make_task(**kwargs):
    mission = mission_service.create_mission(run_id="RUN-1", title="Mission")
    defaults = {"mission_id": mission.mission_id, "title": "Build the feature"}
    defaults.update(kwargs)
    return mission, task_registry.create_task(**defaults)


class DirectTaskExecutionBackwardCompatTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_direct_execution_without_mission_context_still_works(self):
        mission, task = _make_task()
        agent_registry.register_agent(
            Agent(agent_id="AGT-1", name="A", provider="p", model="m", capabilities=["coding"])
        )
        summary = execution_service.execute_task(task.task_id)
        self.assertEqual(summary["status"], "COMPLETED")

    def test_task_description_reaches_execution_context(self):
        mission, task = _make_task(description="Do the important thing")
        agent_registry.register_agent(
            Agent(agent_id="AGT-1", name="A", provider="p", model="m", capabilities=["coding"])
        )
        captured = {}
        real_build_context = AgentExecutor._build_context

        def spy_build_context(task_obj, agent, required, workspace_path=None, mission_context=None):
            ctx = real_build_context(
                task_obj, agent, required, workspace_path=workspace_path, mission_context=mission_context
            )
            captured["context"] = ctx
            return ctx

        with patch.object(AgentExecutor, "_build_context", staticmethod(spy_build_context)):
            execution_service.execute_task(task.task_id)

        self.assertEqual(captured["context"].task_description, "Do the important thing")

    def test_acceptance_criteria_reaches_execution_context(self):
        mission, task = _make_task(acceptance_criteria=["Must pass tests", "Must be documented"])
        agent_registry.register_agent(
            Agent(agent_id="AGT-1", name="A", provider="p", model="m", capabilities=["coding"])
        )
        captured = {}
        real_build_context = AgentExecutor._build_context

        def spy_build_context(task_obj, agent, required, workspace_path=None, mission_context=None):
            ctx = real_build_context(
                task_obj, agent, required, workspace_path=workspace_path, mission_context=mission_context
            )
            captured["context"] = ctx
            return ctx

        with patch.object(AgentExecutor, "_build_context", staticmethod(spy_build_context)):
            execution_service.execute_task(task.task_id)

        self.assertEqual(
            captured["context"].acceptance_criteria,
            ["Must pass tests", "Must be documented"],
        )

    def test_execution_policy_not_mutated_by_runtime_context(self):
        mission, task = _make_task(execution_policy={"required_capabilities": ["coding"]})
        agent_registry.register_agent(
            Agent(agent_id="AGT-1", name="A", provider="p", model="m", capabilities=["coding"])
        )
        original_policy = dict(task.execution_policy)

        execution_service.execute_task(
            task.task_id,
            mission_context={"mission": {"id": "M1"}, "current_task": {}, "completed_tasks": []},
        )

        stored = task_registry.get_task(task.task_id)
        self.assertEqual(stored.execution_policy, original_policy)

    def test_build_prompt_includes_description_and_acceptance_criteria(self):
        context = ExecutionContext(
            task_id="T1",
            task_title="Do X",
            required_capabilities=["coding"],
            task_description="A description",
            acceptance_criteria=["Criterion 1"],
        )
        prompt = build_prompt(context)
        self.assertIn("Task Description: A description", prompt)
        self.assertIn("Acceptance criteria: Criterion 1", prompt)


class OmniRouteMockedMissionFlowTestCase(unittest.TestCase):
    def setUp(self):
        _reset()
        initialize_default_agents()
        self._tmpdir = tempfile.TemporaryDirectory()
        repo_path = Path(self._tmpdir.name)
        (repo_path / ".git").mkdir()
        self.repo_path = str(repo_path)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_omniroute_mission_completes_with_mocked_worker(self):
        mission = mission_service.create_mission(
            run_id="RUN-1",
            title="Implement the billing API",
            project_id="norte",
            execution_path="omniroute",
        )

        with patch("nexus.agents.policy.resolve_project") as mock_resolve_project:
            from nexus.router import RoutedProject

            mock_resolve_project.return_value = RoutedProject(
                id="norte", name="Norte", path=self.repo_path, aliases=[], enabled=True
            )

            with patch.object(
                __import__("nexus.agents.adapters.omniroute", fromlist=["OmniRouteAdapter"])
                .OmniRouteAdapter,
                "run",
            ) as mock_run:
                mock_run.return_value = AdapterResult(
                    success=True,
                    output="mocked worker output",
                    error=None,
                    routed_model="cc/claude-sonnet-5-low",
                )

                with patch("subprocess.run") as mock_subprocess:
                    summary = execute_mission(mission.mission_id)
                    mock_subprocess.assert_not_called()

        self.assertEqual(summary["status"], "COMPLETED")
        for result in summary["task_results"]:
            self.assertEqual(result["execution_path"], "omniroute")
            self.assertEqual(result["project_id"], "norte")
            self.assertEqual(result["routed_model"], "cc/claude-sonnet-5-low")


if __name__ == "__main__":
    unittest.main()
