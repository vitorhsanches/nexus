"""Tests for Nexus v1.6 Project Context Propagation V1."""

import unittest
from unittest.mock import patch

from nexus.agents.policy import resolve_execution_policy
from nexus.manager.agent import ManagerAgent
from nexus.manager.models import ExecutionPlan, TaskDraft
from nexus.missions.generator import generate_mission_from_plan
from nexus.missions.models import Mission
from nexus.missions.service import create_mission


class MissionModelBackwardCompatTests(unittest.TestCase):
    def test_mission_construction_without_project_context(self):
        mission = Mission(
            mission_id="MISSION-1",
            run_id="RUN-1",
            title="Legacy mission",
        )
        self.assertIsNone(mission.project_id)
        self.assertIsNone(mission.execution_path)


class CreateMissionBackwardCompatTests(unittest.TestCase):
    def test_create_mission_without_context_is_unaffected(self):
        mission = create_mission(run_id="RUN-1", title="No context")
        self.assertIsNone(mission.project_id)
        self.assertIsNone(mission.execution_path)

    def test_create_mission_preserves_project_id(self):
        mission = create_mission(
            run_id="RUN-1", title="Norte project", project_id="norte"
        )
        self.assertEqual(mission.project_id, "norte")

    def test_create_mission_execution_path_none_stays_none(self):
        mission = create_mission(
            run_id="RUN-1",
            title="Norte project",
            project_id="norte",
            execution_path=None,
        )
        self.assertIsNone(mission.execution_path)


class ManagerAgentContextPropagationTests(unittest.TestCase):
    def _plan(self):
        return ExecutionPlan(
            mission_id="MISSION-PLAN",
            title="Do the thing",
            description=None,
            intent="EXECUTION",
            tasks=[
                TaskDraft(
                    scope="Implement",
                    reason="Because",
                    required_capabilities=["coding"],
                )
            ],
        )

    def test_mission_instance_with_project_id_propagates(self):
        mission = Mission(
            mission_id="MISSION-IN",
            run_id="RUN-1",
            title="Mission with project",
            project_id="norte",
        )
        manager = ManagerAgent(planner=lambda m: self._plan())
        result = manager.execute(mission)
        self.assertEqual(result.mission.project_id, "norte")

    def test_dict_mission_with_project_id_propagates(self):
        mission = {
            "mission_id": "MISSION-DICT",
            "run_id": "RUN-1",
            "title": "Dict mission",
            "project_id": "norte",
        }
        manager = ManagerAgent(planner=lambda m: self._plan())
        result = manager.execute(mission)
        self.assertEqual(result.mission.project_id, "norte")

    def test_execution_path_is_preserved_when_present(self):
        mission = Mission(
            mission_id="MISSION-EP",
            run_id="RUN-1",
            title="With execution path",
            project_id="norte",
            execution_path="omniroute",
        )
        manager = ManagerAgent(planner=lambda m: self._plan())
        result = manager.execute(mission)
        self.assertEqual(result.mission.execution_path, "omniroute")

    def test_execution_path_absent_by_default(self):
        mission = Mission(
            mission_id="MISSION-NOEP",
            run_id="RUN-1",
            title="No execution path",
            project_id="norte",
        )
        manager = ManagerAgent(planner=lambda m: self._plan())
        result = manager.execute(mission)
        self.assertIsNone(result.mission.execution_path)
        for task in result.tasks:
            self.assertNotIn("execution_path", task.execution_policy)


class GeneratedTaskProjectContextTests(unittest.TestCase):
    def test_tasks_inherit_project_id(self):
        plan = {
            "title": "Norte mission",
            "project_id": "norte",
            "workers": [
                {"scope": "Do work", "required_capabilities": ["coding"]},
            ],
        }
        mission = generate_mission_from_plan(plan, run_id="RUN-1")
        self.assertEqual(mission.project_id, "norte")
        self.assertEqual(
            mission.tasks[0].execution_policy["project_id"], "norte"
        )

    def test_tasks_inherit_execution_path_only_when_supplied(self):
        plan_without = {
            "title": "Norte mission",
            "project_id": "norte",
            "workers": [
                {"scope": "Do work", "required_capabilities": ["coding"]},
            ],
        }
        mission_without = generate_mission_from_plan(plan_without, run_id="RUN-1")
        self.assertNotIn(
            "execution_path", mission_without.tasks[0].execution_policy
        )

        plan_with = {
            "title": "Norte mission",
            "project_id": "norte",
            "execution_path": "omniroute",
            "workers": [
                {"scope": "Do work", "required_capabilities": ["coding"]},
            ],
        }
        mission_with = generate_mission_from_plan(plan_with, run_id="RUN-1")
        self.assertEqual(
            mission_with.tasks[0].execution_policy["execution_path"], "omniroute"
        )

    def test_no_workspace_path_persisted_automatically(self):
        plan = {
            "title": "Norte mission",
            "project_id": "norte",
            "execution_path": "omniroute",
            "workers": [
                {"scope": "Do work", "required_capabilities": ["coding"]},
            ],
        }
        mission = generate_mission_from_plan(plan, run_id="RUN-1")
        self.assertNotIn("workspace_path", mission.tasks[0].execution_policy)

    def test_worker_project_id_overrides_plan_level(self):
        plan = {
            "title": "Norte mission",
            "project_id": "norte",
            "workers": [
                {
                    "scope": "Do work",
                    "required_capabilities": ["coding"],
                    "project_id": "sul",
                },
            ],
        }
        mission = generate_mission_from_plan(plan, run_id="RUN-1")
        self.assertEqual(
            mission.tasks[0].execution_policy["project_id"], "sul"
        )

    def test_worker_execution_path_overrides_plan_level(self):
        plan = {
            "title": "Norte mission",
            "project_id": "norte",
            "execution_path": "omniroute",
            "workers": [
                {
                    "scope": "Do work",
                    "required_capabilities": ["coding"],
                    "execution_path": "simulated",
                },
            ],
        }
        mission = generate_mission_from_plan(plan, run_id="RUN-1")
        self.assertEqual(
            mission.tasks[0].execution_policy["execution_path"], "simulated"
        )

    def test_required_capabilities_remain_intact(self):
        plan = {
            "title": "Norte mission",
            "project_id": "norte",
            "workers": [
                {
                    "scope": "Do work",
                    "required_capabilities": ["coding", "analysis"],
                },
            ],
        }
        mission = generate_mission_from_plan(plan, run_id="RUN-1")
        self.assertEqual(
            mission.tasks[0].execution_policy["required_capabilities"],
            ["coding", "analysis"],
        )

    def test_provider_model_effort_route_class_preserved(self):
        plan = {
            "title": "Norte mission",
            "project_id": "norte",
            "workers": [
                {
                    "scope": "Do work",
                    "required_capabilities": ["coding"],
                    "provider": "openai",
                    "model": "gpt-5.6-luna",
                    "effort": "low",
                    "route_class": "mechanical",
                },
            ],
        }
        mission = generate_mission_from_plan(plan, run_id="RUN-1")
        policy = mission.tasks[0].execution_policy
        self.assertEqual(policy["provider"], "openai")
        self.assertEqual(policy["model"], "gpt-5.6-luna")
        self.assertEqual(policy["effort"], "low")
        self.assertEqual(policy["route_class"], "mechanical")


class DefaultManagerTaskSimulatedResolutionTests(unittest.TestCase):
    def test_default_manager_tasks_resolve_as_simulated(self):
        plan = {
            "title": "Norte mission",
            "project_id": "norte",
            "workers": [
                {"scope": "Do work", "required_capabilities": ["coding"]},
            ],
        }
        mission = generate_mission_from_plan(plan, run_id="RUN-1")
        resolved = resolve_execution_policy(mission.tasks[0].execution_policy)
        self.assertEqual(resolved.execution_path, "simulated")
        self.assertIsInstance(resolved.adapter.__class__.__name__, str)
        self.assertEqual(resolved.adapter.__class__.__name__, "SimulatedAdapter")


class OmnirouteReadyTaskResolutionTests(unittest.TestCase):
    def test_task_with_project_and_execution_path_resolves_via_router(self):
        plan = {
            "title": "Norte mission",
            "project_id": "norte",
            "execution_path": "omniroute",
            "workers": [
                {"scope": "Do work", "required_capabilities": ["coding"]},
            ],
        }
        mission = generate_mission_from_plan(plan, run_id="RUN-1")
        task = mission.tasks[0].execution_policy
        self.assertEqual(task["project_id"], "norte")
        self.assertEqual(task["execution_path"], "omniroute")

        from nexus.router import RoutedProject

        with patch("nexus.agents.policy.resolve_project") as mock_resolve:
            mock_resolve.return_value = RoutedProject(
                id="norte",
                name="Norte",
                path="/tmp/norte-repo-mocked",
                aliases=[],
                enabled=True,
            )
            with patch("nexus.agents.policy._validate_git_target"):
                resolved = resolve_execution_policy(task)

        self.assertEqual(resolved.execution_path, "omniroute")
        self.assertEqual(resolved.workspace_path, "/tmp/norte-repo-mocked")
        mock_resolve.assert_called_once_with("norte")


if __name__ == "__main__":
    unittest.main()
