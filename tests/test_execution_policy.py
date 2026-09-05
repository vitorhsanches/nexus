"""Tests for Nexus v1.5 Real Board Task Execution V1: execution policy,
project-target resolution, failure lifecycle, and routed-model reporting.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nexus.missions.service as mission_service
import nexus.registry.database as registry_database
import nexus.tasks.registry as task_registry
import nexus.web.execution as execution_service
import nexus.workspaces.registry as session_registry
from nexus.agents.adapters.base import AdapterResult, ExecutionContext
from nexus.agents.adapters.omniroute import OmniRouteAdapter, STANDARD_CODING_MODEL
from nexus.agents.adapters.simulated import SimulatedAdapter
from nexus.agents.executor import AgentExecutor
from nexus.agents.models import Agent
from nexus.agents.policy import (
    ExecutionPolicyError,
    resolve_execution_policy,
)
from nexus.agents.registry import AgentRegistry
from nexus.router import ProjectNotFoundError
from nexus.routing.telemetry import OmniRouteTelemetrySnapshot
from nexus.web.agents import agent_registry


def _reset():
    mission_service._missions.clear()
    task_registry._tasks.clear()
    task_registry._attempts.clear()
    task_registry._next_attempt.clear()
    session_registry._sessions.clear()
    agent_registry._agents.clear()


def _make_task(**kwargs):
    mission = mission_service.create_mission(run_id="RUN-1", title="Mission")
    defaults = {"mission_id": mission.mission_id, "title": "Build the feature"}
    defaults.update(kwargs)
    return mission, task_registry.create_task(**defaults)


def _register_agent(agent_id="AGT-1", capabilities=None):
    return agent_registry.register_agent(
        Agent(
            agent_id=agent_id,
            name="Alpha",
            provider="openai",
            model="gpt-5",
            capabilities=capabilities,
        )
    )


class ExecutionPolicyResolutionTestCase(unittest.TestCase):
    def test_missing_execution_path_uses_simulated(self):
        resolved = resolve_execution_policy(None)
        self.assertEqual(resolved.execution_path, "simulated")
        self.assertIsInstance(resolved.adapter, SimulatedAdapter)

    def test_explicit_simulated_uses_simulated(self):
        resolved = resolve_execution_policy({"execution_path": "simulated"})
        self.assertEqual(resolved.execution_path, "simulated")
        self.assertIsInstance(resolved.adapter, SimulatedAdapter)

    def test_invalid_execution_path_raises_before_subprocess(self):
        with patch("nexus.agents.adapters.omniroute.subprocess.run") as mock_run:
            with self.assertRaises(ExecutionPolicyError):
                resolve_execution_policy({"execution_path": "bogus"})
            mock_run.assert_not_called()

    def test_omniroute_without_target_raises_before_subprocess(self):
        with patch("nexus.agents.adapters.omniroute.subprocess.run") as mock_run:
            with self.assertRaises(ExecutionPolicyError):
                resolve_execution_policy({"execution_path": "omniroute"})
            mock_run.assert_not_called()

    def test_omniroute_nonexistent_target_raises_before_subprocess(self):
        with patch("nexus.agents.adapters.omniroute.subprocess.run") as mock_run:
            with self.assertRaises(ExecutionPolicyError):
                resolve_execution_policy(
                    {
                        "execution_path": "omniroute",
                        "workspace_path": "C:/definitely/does/not/exist/xyz",
                    }
                )
            mock_run.assert_not_called()

    def test_omniroute_non_git_directory_raises_before_subprocess(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("nexus.agents.adapters.omniroute.subprocess.run") as mock_run:
                with self.assertRaises(ExecutionPolicyError):
                    resolve_execution_policy(
                        {"execution_path": "omniroute", "workspace_path": tmpdir}
                    )
                mock_run.assert_not_called()

    def test_omniroute_valid_workspace_path_resolves(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / ".git").mkdir()
            resolved = resolve_execution_policy(
                {"execution_path": "omniroute", "workspace_path": tmpdir}
            )
            self.assertEqual(resolved.execution_path, "omniroute")
            self.assertIsInstance(resolved.adapter, OmniRouteAdapter)
            self.assertEqual(resolved.workspace_path, tmpdir)

    def test_omniroute_valid_workspace_path_git_file_worktree(self):
        """A '.git' file (Git worktree marker) is also acceptable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / ".git").write_text("gitdir: ../somewhere\n")
            resolved = resolve_execution_policy(
                {"execution_path": "omniroute", "workspace_path": tmpdir}
            )
            self.assertEqual(resolved.execution_path, "omniroute")

    def test_project_id_uses_project_router(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / ".git").mkdir()
            with patch("nexus.agents.policy.resolve_project") as mock_resolve:
                from nexus.router import RoutedProject

                mock_resolve.return_value = RoutedProject(
                    id="proj-1",
                    name="Project One",
                    path=tmpdir,
                    aliases=[],
                    enabled=True,
                )
                resolved = resolve_execution_policy(
                    {"execution_path": "omniroute", "project_id": "proj-1"}
                )
                mock_resolve.assert_called_once_with("proj-1")
                self.assertEqual(resolved.workspace_path, tmpdir)
                self.assertEqual(resolved.project_id, "proj-1")

    def test_unknown_project_id_preserves_router_error(self):
        with patch("nexus.agents.policy.resolve_project") as mock_resolve:
            mock_resolve.side_effect = ProjectNotFoundError("PROJECT_NOT_FOUND: nope")
            with self.assertRaises(ExecutionPolicyError) as ctx:
                resolve_execution_policy(
                    {"execution_path": "omniroute", "project_id": "missing"}
                )
            self.assertIn("PROJECT_NOT_FOUND", str(ctx.exception))


class SimulatedSummaryTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_simulated_summary_routed_model_is_none(self):
        mission, task = _make_task()
        _register_agent(capabilities=["coding"])

        summary = execution_service.execute_task(task.task_id)

        self.assertEqual(summary["execution_path"], "simulated")
        self.assertIsNone(summary["routed_model"])
        self.assertEqual(summary["status"], "COMPLETED")

    def test_missing_execution_path_dispatches_to_simulated_adapter(self):
        mission, task = _make_task()
        _register_agent(capabilities=["coding"])

        with patch("nexus.agents.adapters.omniroute.subprocess.run") as mock_run:
            execution_service.execute_task(task.task_id)
            mock_run.assert_not_called()

    def test_attempt_model_falls_back_to_agent_model_for_simulated(self):
        mission, task = _make_task()
        _register_agent(capabilities=["coding"])

        summary = execution_service.execute_task(task.task_id)

        attempt = task_registry.get_attempt(summary["attempt_id"])
        self.assertEqual(attempt.model, "gpt-5")


class OmniRouteSummaryTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

        # These are unit/integration tests for execution policy and Worker
        # lifecycle, not live OmniRoute telemetry tests. The production
        # Adapter still creates its default AdaptiveRoutingService and
        # OmniRouteTelemetryClient, but collection is isolated here so the
        # suite can never perform real network I/O.
        telemetry_patcher = patch(
            "nexus.routing.service.OmniRouteTelemetryClient.collect",
            return_value=OmniRouteTelemetrySnapshot(),
        )
        telemetry_patcher.start()
        self.addCleanup(telemetry_patcher.stop)

    def test_omniroute_selected_and_routed_model_recorded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / ".git").mkdir()
            mission, task = _make_task(
                execution_policy={
                    "execution_path": "omniroute",
                    "workspace_path": tmpdir,
                    "required_capabilities": ["coding"],
                }
            )
            _register_agent(capabilities=["coding"])

            class FakeCompleted:
                returncode = 0
                stdout = "worker output"
                stderr = ""

            with patch(
                "nexus.agents.adapters.omniroute.subprocess.run",
                return_value=FakeCompleted(),
            ) as mock_run:
                summary = execution_service.execute_task(task.task_id)
                mock_run.assert_called_once()

            self.assertEqual(summary["execution_path"], "omniroute")
            self.assertEqual(summary["routed_model"], STANDARD_CODING_MODEL)
            self.assertEqual(summary["status"], "COMPLETED")
            self.assertEqual(summary["workspace_path"], tmpdir)

            attempt = task_registry.get_attempt(summary["attempt_id"])
            self.assertEqual(attempt.model, STANDARD_CODING_MODEL)

    def test_omniroute_adapter_failure_marks_task_session_agent_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / ".git").mkdir()
            mission, task = _make_task(
                execution_policy={
                    "execution_path": "omniroute",
                    "workspace_path": tmpdir,
                }
            )
            agent = _register_agent(capabilities=[])

            class FakeCompleted:
                returncode = 1
                stdout = ""
                stderr = "worker exploded"

            with patch(
                "nexus.agents.adapters.omniroute.subprocess.run",
                return_value=FakeCompleted(),
            ):
                with self.assertRaises(RuntimeError):
                    execution_service.execute_task(task.task_id)

            stored_task = task_registry.get_task(task.task_id)
            self.assertEqual(stored_task.status, "FAILED")

            sessions = session_registry.list_sessions(task_id=task.task_id)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].status, "FAILED")

            self.assertEqual(agent_registry.get_agent(agent.agent_id).status, "FAILED")


class ExecutorRuntimeWorkspaceTestCase(unittest.TestCase):
    """AgentExecutor accepts a runtime workspace_path without mutating the
    stored Task.execution_policy, and existing callers keep working."""

    def setUp(self):
        task_registry._tasks.clear()
        self.registry = AgentRegistry()
        self.registry.register_agent(
            Agent(
                agent_id="AGT-1",
                name="Alpha",
                provider="openai",
                model="gpt-5",
                capabilities=["coding"],
            )
        )
        self.task = task_registry.create_task(
            mission_id="MISSION-1",
            title="Build",
            execution_policy={"required_capabilities": ["coding"]},
        )

    def test_runtime_workspace_path_reaches_context_without_mutating_policy(self):
        captured = {}

        class RecordingAdapter:
            def run(self, context):
                captured["workspace_path"] = context.workspace_path
                return AdapterResult(success=True, output="done")

        executor = AgentExecutor(self.registry, adapter=RecordingAdapter())
        executor.execute_task(
            self.task.task_id, agent_id="AGT-1", workspace_path="C:/runtime-target"
        )

        self.assertEqual(captured["workspace_path"], "C:/runtime-target")
        stored_task = task_registry.get_task(self.task.task_id)
        self.assertNotIn("workspace_path", stored_task.execution_policy)

    def test_existing_callers_without_workspace_path_still_work(self):
        executor = AgentExecutor(self.registry)
        result = executor.execute_task(self.task.task_id, agent_id="AGT-1")
        self.assertEqual(result.status, "COMPLETED")


class FailureBeforeRunningTestCase(unittest.TestCase):
    """A lifecycle failure before RUNNING must not attempt an invalid
    task transition to FAILED (RUNNING -> FAILED is the only valid path)."""

    def setUp(self):
        task_registry._tasks.clear()
        self.registry = AgentRegistry()
        self.registry.register_agent(
            Agent(
                agent_id="AGT-1",
                name="Alpha",
                provider="openai",
                model="gpt-5",
                capabilities=["coding"],
            )
        )
        self.task = task_registry.create_task(
            mission_id="MISSION-1", title="Build the executor"
        )

    def test_lifecycle_failure_before_running_does_not_crash(self):
        executor = AgentExecutor(self.registry)

        with patch.object(
            AgentExecutor,
            "_run_task_lifecycle",
            side_effect=RuntimeError("boom before running"),
        ):
            with self.assertRaises(RuntimeError):
                executor.execute_task(self.task.task_id, agent_id="AGT-1")

        stored_task = task_registry.get_task(self.task.task_id)
        # Never reached RUNNING, so it stays CREATED rather than FAILED.
        self.assertEqual(stored_task.status, "CREATED")


class WebStartupBootstrapTestCase(unittest.TestCase):
    """Web app lifespan initializes the project registry + default agents."""

    def test_lifespan_initializes_database_sync_projects_and_agents(self):
        import asyncio

        from nexus.web.app import app, lifespan

        with patch("nexus.web.app.initialize_database") as mock_init_db, patch(
            "nexus.web.app.sync_projects"
        ) as mock_sync, patch(
            "nexus.web.app.initialize_default_agents"
        ) as mock_init_agents:

            async def _run():
                async with lifespan(app):
                    pass

            asyncio.run(_run())

            mock_init_db.assert_called_once()
            mock_sync.assert_called_once()
            mock_init_agents.assert_called_once()


if __name__ == "__main__":
    unittest.main()
