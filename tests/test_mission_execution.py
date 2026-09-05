"""Tests for the Nexus Mission Execution Orchestrator V1.

Covers POST /api/missions/{mission_id}/execute and the underlying
nexus.web.mission_execution service: Mission lifecycle transitions,
dependency-aware sequential Task scheduling, evidence chaining across
Task boundaries, and idempotent summaries.
"""

import unittest
from unittest.mock import patch

import nexus.board.service as board_service
import nexus.missions.service as mission_service
import nexus.tasks.registry as task_registry
import nexus.web.mission_execution as mission_execution
import nexus.web.services as web_services
import nexus.workspaces.registry as session_registry
from nexus.agents.bootstrap import initialize_default_agents
from nexus.agents.policy import ExecutionPolicyError
from nexus.manager.agent import ManagerAgent, ManagerError
from nexus.missions.scheduler import MissionDependencyError
from nexus.web.agents import agent_registry
from nexus.web.mission_execution import (
    MAX_MISSION_CONTEXT_CHARS,
    MissionConflictError,
    MissionExecutionError,
    execute_mission,
)


def _reset():
    mission_service._missions.clear()
    task_registry._tasks.clear()
    task_registry._attempts.clear()
    task_registry._next_attempt.clear()
    board_service._boards.clear()
    session_registry._sessions.clear()
    agent_registry._agents.clear()
    initialize_default_agents()


def _client():
    from starlette.testclient import TestClient
    from nexus.web.app import app

    return TestClient(app)


def _create_mission(title="Implement the billing API", **kwargs):
    return mission_service.create_mission(run_id="RUN-1", title=title, **kwargs)


class ExecuteMissionAutoPlanTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_create_then_execute_plans_and_completes(self):
        mission = _create_mission()

        summary = execute_mission(mission.mission_id)

        self.assertEqual(summary["status"], "COMPLETED")
        self.assertEqual(summary["total_tasks"], 3)
        self.assertEqual(summary["completed_tasks"], 3)
        self.assertEqual(summary["failed_tasks"], 0)
        self.assertEqual(summary["skipped_tasks"], 0)

        tasks = [t for t in task_registry.list_tasks() if t.mission_id == mission.mission_id]
        self.assertEqual(len(tasks), 3)
        for task in tasks:
            self.assertEqual(task.status, "COMPLETED")

        stored = mission_service.get_mission(mission.mission_id)
        self.assertEqual(stored.status, "COMPLETED")

    def test_attempts_and_sessions_created_once(self):
        mission = _create_mission()
        execute_mission(mission.mission_id)

        tasks = [t for t in task_registry.list_tasks() if t.mission_id == mission.mission_id]
        for task in tasks:
            attempts = task_registry.list_attempts(task_id=task.task_id)
            sessions = session_registry.list_sessions(task_id=task.task_id)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(len(sessions), 1)

    def test_simulated_is_default_no_real_provider(self):
        mission = _create_mission()
        with patch("subprocess.run") as mock_subprocess:
            execute_mission(mission.mission_id)
            mock_subprocess.assert_not_called()


class MissionFailureMidwayTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_second_task_failure_stops_third(self):
        mission = _create_mission()
        web_services.plan_mission(mission.mission_id)
        tasks = [t for t in task_registry.list_tasks() if t.mission_id == mission.mission_id]
        self.assertEqual(len(tasks), 3)
        task2_id = tasks[1].task_id

        from nexus.web.execution import execute_task as real_execute

        def failing_execute_task(task_id, mission_context=None):
            if task_id == task2_id:
                task_registry.update_task_status(task_id, "READY")
                task_registry.update_task_status(task_id, "CLAIMED")
                task_registry.update_task_status(task_id, "RUNNING")
                task_registry.update_task_status(task_id, "FAILED")
                raise RuntimeError("boom")
            return real_execute(task_id, mission_context=mission_context)

        with patch(
            "nexus.web.mission_execution.execution_service.execute_task",
            side_effect=failing_execute_task,
        ):
            with self.assertRaises(MissionExecutionError):
                execute_mission(mission.mission_id)

        refreshed = {t.task_id: task_registry.get_task(t.task_id) for t in tasks}
        self.assertEqual(refreshed[tasks[0].task_id].status, "COMPLETED")
        self.assertEqual(refreshed[tasks[1].task_id].status, "FAILED")
        self.assertEqual(refreshed[tasks[2].task_id].status, "CREATED")

        self.assertEqual(len(task_registry.list_attempts(task_id=tasks[2].task_id)), 0)
        self.assertEqual(len(session_registry.list_sessions(task_id=tasks[2].task_id)), 0)

        mission_state = mission_service.get_mission(mission.mission_id)
        self.assertEqual(mission_state.status, "FAILED")


class PreExistingFailedBlockerTestCase(unittest.TestCase):
    """Mandatory: a pre-existing FAILED Task must block Mission execution."""

    def setUp(self):
        _reset()

    def test_pre_existing_failed_task_blocks_execution(self):
        mission = _create_mission()
        web_services.plan_mission(mission.mission_id)
        tasks = [t for t in task_registry.list_tasks() if t.mission_id == mission.mission_id]

        task1_id = tasks[0].task_id
        task_registry.update_task_status(task1_id, "READY")
        task_registry.update_task_status(task1_id, "CLAIMED")
        task_registry.update_task_status(task1_id, "RUNNING")
        task_registry.update_task_status(task1_id, "FAILED")

        with self.assertRaises(MissionConflictError):
            execute_mission(mission.mission_id)

        task2_id = tasks[1].task_id
        self.assertEqual(task_registry.get_task(task2_id).status, "CREATED")
        self.assertEqual(len(task_registry.list_attempts(task_id=task2_id)), 0)


class PartialResumeContextTestCase(unittest.TestCase):
    """Mandatory: partial resume must feed existing Attempt evidence forward."""

    def setUp(self):
        _reset()

    def test_task2_receives_task1_existing_attempt_output(self):
        mission = _create_mission()
        web_services.plan_mission(mission.mission_id)
        tasks = [t for t in task_registry.list_tasks() if t.mission_id == mission.mission_id]
        task1, task2 = tasks[0], tasks[1]

        task_registry.update_task_status(task1.task_id, "READY")
        task_registry.update_task_status(task1.task_id, "CLAIMED")
        task_registry.update_task_status(task1.task_id, "RUNNING")
        task_registry.update_task_status(task1.task_id, "REVIEW")
        task_registry.update_task_status(task1.task_id, "COMPLETED")
        task_registry.create_attempt(
            task_id=task1.task_id,
            agent_id="analysis-agent",
            model="analysis-agent",
            status="COMPLETED",
            result="Task1 evidence output",
        )

        captured = {}
        from nexus.web.execution import execute_task as real_execute_task

        def spy_execute_task(task_id, mission_context=None):
            if task_id == task2.task_id:
                captured["mission_context"] = mission_context
            return real_execute_task(task_id, mission_context=mission_context)

        with patch(
            "nexus.web.mission_execution.execution_service.execute_task",
            side_effect=spy_execute_task,
        ):
            execute_mission(mission.mission_id)

        completed = captured["mission_context"]["completed_tasks"]
        outputs = [entry["output"] for entry in completed]
        self.assertIn("Task1 evidence output", outputs)


class AncestorOnlyContextTestCase(unittest.TestCase):
    """Mandatory: only dependency ancestors flow into a Task's context."""

    def setUp(self):
        _reset()

    def test_independent_task_evidence_not_leaked(self):
        mission = _create_mission()
        task_a = task_registry.create_task(
            mission_id=mission.mission_id,
            title="Task A",
            dependencies=[],
            execution_policy={"required_capabilities": ["analysis"]},
        )
        task_b = task_registry.create_task(
            mission_id=mission.mission_id,
            title="Task B",
            dependencies=[],
            execution_policy={"required_capabilities": ["architecture"]},
        )
        task_c = task_registry.create_task(
            mission_id=mission.mission_id,
            title="Task C",
            dependencies=[task_a.task_id],
            execution_policy={"required_capabilities": ["coding"]},
        )
        mission.tasks = [task_a, task_b, task_c]

        captured = {}
        from nexus.web.execution import execute_task as real_execute_task

        def spy_execute_task(task_id, mission_context=None):
            if task_id == task_c.task_id:
                captured["mission_context"] = mission_context
            return real_execute_task(task_id, mission_context=mission_context)

        with patch(
            "nexus.web.mission_execution.execution_service.execute_task",
            side_effect=spy_execute_task,
        ):
            execute_mission(mission.mission_id)

        completed_ids = {e["task_id"] for e in captured["mission_context"]["completed_tasks"]}
        self.assertIn(task_a.task_id, completed_ids)
        self.assertNotIn(task_b.task_id, completed_ids)


class TruncationTestCase(unittest.TestCase):
    """Mandatory: an oversized predecessor output must not be dropped entirely."""

    def setUp(self):
        _reset()

    def test_oversized_output_is_truncated_not_dropped(self):
        mission = _create_mission()
        task_a = task_registry.create_task(
            mission_id=mission.mission_id,
            title="Task A",
            dependencies=[],
            execution_policy={"required_capabilities": ["analysis"]},
        )
        task_b = task_registry.create_task(
            mission_id=mission.mission_id,
            title="Task B",
            dependencies=[task_a.task_id],
            execution_policy={"required_capabilities": ["coding"]},
        )
        mission.tasks = [task_a, task_b]

        big_output = "X" * (MAX_MISSION_CONTEXT_CHARS + 5000)

        def fake_execute_task(task_id, mission_context=None):
            task = task_registry.get_task(task_id)
            for target in ("READY", "CLAIMED", "RUNNING", "REVIEW", "COMPLETED"):
                task_registry.update_task_status(task_id, target)
            result = big_output if task_id == task_a.task_id else "b-output"
            task_registry.create_attempt(
                task_id=task_id,
                agent_id="analysis-agent",
                model="analysis-agent",
                status="COMPLETED",
                result=result,
            )
            if task_id == task_b.task_id:
                captured["mission_context"] = mission_context
            return {"task_id": task_id, "status": "COMPLETED"}

        captured = {}
        with patch(
            "nexus.web.mission_execution.execution_service.execute_task",
            side_effect=fake_execute_task,
        ):
            execute_mission(mission.mission_id)

        completed = captured["mission_context"]["completed_tasks"]
        self.assertEqual(len(completed), 1)
        entry = completed[0]
        self.assertGreater(len(entry["output"]), 0)
        self.assertTrue(entry.get("output_truncated"))
        self.assertLessEqual(len(entry["output"]), MAX_MISSION_CONTEXT_CHARS)



class EvidencePriorityTestCase(unittest.TestCase):
    """Closest dependency evidence must not be starved by older huge output."""

    def setUp(self):
        _reset()

    def test_direct_predecessor_keeps_context_budget_priority(self):
        mission = _create_mission(title="Context priority")

        task_a = task_registry.create_task(
            mission_id=mission.mission_id,
            title="Analysis",
            dependencies=[],
        )
        task_b = task_registry.create_task(
            mission_id=mission.mission_id,
            title="Architecture",
            dependencies=[task_a.task_id],
        )
        task_c = task_registry.create_task(
            mission_id=mission.mission_id,
            title="Coding",
            dependencies=[task_b.task_id],
        )
        mission.tasks = [task_a, task_b, task_c]

        for task in (task_a, task_b):
            for target in ("READY", "CLAIMED", "RUNNING", "REVIEW", "COMPLETED"):
                task_registry.update_task_status(task.task_id, target)

        task_registry.create_attempt(
            task_id=task_a.task_id,
            agent_id="analysis-agent",
            model="analysis-model",
            status="COMPLETED",
            result="A" * MAX_MISSION_CONTEXT_CHARS,
        )
        task_registry.create_attempt(
            task_id=task_b.task_id,
            agent_id="architecture-agent",
            model="architecture-model",
            status="COMPLETED",
            result="DIRECT-ARCHITECTURE-EVIDENCE",
        )

        tasks_by_id = {
            task_a.task_id: task_a,
            task_b.task_id: task_b,
            task_c.task_id: task_c,
        }

        context = mission_execution._build_mission_context(
            mission.mission_id,
            task_c,
            tasks_by_id,
        )

        evidence = {
            entry["task_id"]: entry
            for entry in context["completed_tasks"]
        }

        self.assertEqual(
            evidence[task_b.task_id]["output"],
            "DIRECT-ARCHITECTURE-EVIDENCE",
        )
        self.assertGreater(len(evidence[task_a.task_id]["output"]), 0)
        self.assertTrue(evidence[task_a.task_id].get("output_truncated"))

        total_output_chars = sum(
            len(entry.get("output") or "")
            for entry in context["completed_tasks"]
        )
        self.assertLessEqual(total_output_chars, MAX_MISSION_CONTEXT_CHARS)


class ForeignMissionTaskSafetyTestCase(unittest.TestCase):
    """Mission.tasks must never authorize execution of another Mission's Task."""

    def setUp(self):
        _reset()

    def test_foreign_task_reference_fails_closed_before_execution(self):
        mission_a = _create_mission(title="Mission A")
        mission_b = mission_service.create_mission(
            run_id="RUN-2",
            title="Mission B",
        )

        foreign_task = task_registry.create_task(
            mission_id=mission_b.mission_id,
            title="Foreign Task",
            dependencies=[],
        )

        mission_a.tasks = [foreign_task]

        with patch(
            "nexus.web.mission_execution.execution_service.execute_task"
        ) as mock_execute:
            with self.assertRaises(MissionDependencyError):
                execute_mission(mission_a.mission_id)
            mock_execute.assert_not_called()

        self.assertEqual(
            mission_service.get_mission(mission_a.mission_id).status,
            "FAILED",
        )
        self.assertEqual(foreign_task.status, "CREATED")


class MissionLifecycleFailureVisibilityTestCase(unittest.TestCase):
    """Failure to record Mission FAILED must never be silently swallowed."""

    def setUp(self):
        _reset()

    def test_fail_mission_propagates_lifecycle_failure(self):
        mission = _create_mission(title="Lifecycle failure")

        with patch(
            "nexus.web.mission_execution.update_mission_status",
            side_effect=RuntimeError("lifecycle write failed"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "lifecycle write failed",
            ):
                mission_execution._fail_mission(mission.mission_id)


class IdempotencyTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_execute_after_completed_creates_nothing_new(self):
        mission = _create_mission()
        execute_mission(mission.mission_id)

        tasks_before = len(task_registry.list_tasks())
        attempts_before = len(task_registry.list_attempts())
        sessions_before = len(session_registry.list_sessions())

        with patch("subprocess.run") as mock_subprocess:
            summary = execute_mission(mission.mission_id)
            mock_subprocess.assert_not_called()

        self.assertEqual(len(task_registry.list_tasks()), tasks_before)
        self.assertEqual(len(task_registry.list_attempts()), attempts_before)
        self.assertEqual(len(session_registry.list_sessions()), sessions_before)
        self.assertEqual(summary["status"], "COMPLETED")

    def test_simulated_routed_model_remains_none_on_idempotent_summary(self):
        mission = _create_mission()
        execute_mission(mission.mission_id)
        summary = execute_mission(mission.mission_id)

        for result in summary["task_results"]:
            self.assertIsNone(result["routed_model"])

    def test_existing_session_id_reconstructed(self):
        mission = _create_mission()
        execute_mission(mission.mission_id)
        summary = execute_mission(mission.mission_id)

        for result in summary["task_results"]:
            self.assertIsNotNone(result["session_id"])


class DeadlockTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_deadlock_raises_and_fails_mission(self):
        mission = _create_mission()
        task_a = task_registry.create_task(
            mission_id=mission.mission_id,
            title="Task A",
            status="RUNNING",
            dependencies=[],
        )
        mission.tasks = [task_a]

        with self.assertRaises(MissionConflictError):
            execute_mission(mission.mission_id)


class ExecutionPolicyErrorTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_policy_error_fails_mission_no_subprocess(self):
        mission = _create_mission(project_id="norte", execution_path="omniroute")
        task_a = task_registry.create_task(
            mission_id=mission.mission_id,
            title="Task A",
            dependencies=[],
            execution_policy={
                "execution_path": "omniroute",
                "workspace_path": "/does/not/exist",
            },
        )
        mission.tasks = [task_a]

        with patch("subprocess.run") as mock_subprocess:
            with self.assertRaises(ExecutionPolicyError):
                execute_mission(mission.mission_id)
            mock_subprocess.assert_not_called()

        mission_state = mission_service.get_mission(mission.mission_id)
        self.assertEqual(mission_state.status, "FAILED")


class MissionExecuteRouteTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_execute_route_completes_mission(self):
        client = _client()
        with client:
            create_response = client.post(
                "/api/missions", json={"title": "Implement the billing API"}
            )
            mission_id = create_response.json()["mission"]["mission_id"]

            response = client.post(f"/api/missions/{mission_id}/execute")

        self.assertEqual(response.status_code, 200)
        payload = response.json()["execution"]
        self.assertEqual(payload["status"], "COMPLETED")

    def test_execute_route_unknown_mission_returns_404(self):
        client = _client()
        with client:
            response = client.post("/api/missions/MISSION-UNKNOWN/execute")
        self.assertEqual(response.status_code, 404)

    def test_execute_route_idempotent_second_call(self):
        client = _client()
        with client:
            create_response = client.post(
                "/api/missions", json={"title": "Implement the billing API"}
            )
            mission_id = create_response.json()["mission"]["mission_id"]
            client.post(f"/api/missions/{mission_id}/execute")
            second = client.post(f"/api/missions/{mission_id}/execute")

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["execution"]["status"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
