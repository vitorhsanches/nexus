import unittest

import nexus.missions.service as service
import nexus.tasks.registry as task_registry
from nexus.missions.generator import generate_mission_from_plan
from nexus.missions.models import MISSION_STATUSES
from nexus.missions.service import (
    MissionNotFoundError,
    create_mission,
    create_mission_from_plan,
    get_mission,
    list_missions,
)


def execution_plan():
    return {
        "complexity": "MEDIUM",
        "risk": "LOW",
        "parallelism": 2,
        "summary": "Add Google Calendar support",
        "intent": "EXECUTION",
        "workers": [
            {
                "route_class": "standard-coding",
                "execution_path": "OMNIROUTE",
                "provider": "openai",
                "model": "gpt-5.6-luna",
                "effort": "medium",
                "scope": "Create OAuth service",
                "reason": "Routing path for OAuth",
            },
            {
                "route_class": "mechanical",
                "execution_path": "NATIVE_CODEX",
                "provider": "openai",
                "model": "gpt-5.6-terra",
                "effort": "low",
                "scope": "Write integration tests",
                "reason": "Routing path for tests",
            },
        ],
    }


class MissionServiceTestCase(unittest.TestCase):
    def setUp(self):
        service._missions.clear()
        task_registry._tasks.clear()
        task_registry._attempts.clear()
        task_registry._next_attempt.clear()

    def test_mission_creation(self):
        mission = create_mission(
            run_id="RUN-123",
            title="Google Calendar",
            description="Integrate calendar",
        )
        self.assertTrue(mission.mission_id.startswith("MISSION-"))
        self.assertEqual(mission.run_id, "RUN-123")
        self.assertEqual(mission.status, "CREATED")
        self.assertEqual(mission.tasks, [])
        self.assertIsNotNone(mission.created_at)

    def test_mission_retrieval(self):
        mission = create_mission(run_id="RUN-1", title="Mission")
        fetched = get_mission(mission.mission_id)
        self.assertIs(fetched, mission)

    def test_list_missions(self):
        create_mission(run_id="RUN-1", title="Mission A")
        create_mission(run_id="RUN-2", title="Mission B")
        self.assertEqual(len(list_missions()), 2)

    def test_get_missing_mission_raises(self):
        with self.assertRaises(MissionNotFoundError):
            get_mission("MISSION-DOES-NOT-EXIST")

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValueError):
            create_mission(run_id="RUN-1", title="Mission", status="BOGUS")


class MissionGenerationTestCase(unittest.TestCase):
    def setUp(self):
        service._missions.clear()
        task_registry._tasks.clear()
        task_registry._attempts.clear()
        task_registry._next_attempt.clear()

    def test_generation_from_execution_plan(self):
        mission = create_mission_from_plan(execution_plan(), run_id="RUN-123")
        self.assertTrue(mission.mission_id.startswith("MISSION-"))
        self.assertEqual(mission.run_id, "RUN-123")
        self.assertEqual(mission.status, "CREATED")
        self.assertEqual(len(mission.tasks), 2)

        # Tasks were created through the Task Registry.
        for task in mission.tasks:
            self.assertTrue(task.task_id.startswith("TASK-"))
            self.assertEqual(task.mission_id, mission.mission_id)

        registry_tasks = task_registry.list_tasks()
        self.assertEqual(len(registry_tasks), 2)

    def test_generation_without_workers(self):
        plan = {
            "complexity": "LOW",
            "risk": "LOW",
            "parallelism": 1,
            "summary": "Simple analysis",
            "intent": "ANALYSIS",
            "workers": [],
        }
        mission = generate_mission_from_plan(plan, run_id="RUN-1")
        self.assertEqual(mission.tasks, [])
        self.assertEqual(mission.status, "CREATED")
        self.assertEqual(task_registry.list_tasks(), [])

    def test_generation_uses_worker_scope_for_task_title(self):
        mission = generate_mission_from_plan(execution_plan(), run_id="RUN-1")
        titles = {task.title for task in mission.tasks}
        self.assertIn("Create OAuth service", titles)
        self.assertIn("Write integration tests", titles)



class MissionExecutionPolicyTestCase(unittest.TestCase):
    """Verifies required capabilities propagate from plans into tasks."""

    def _build_plan(self, workers):
        return {
            "title": "Plan Mission",
            "description": "desc",
            "workers": workers,
        }

    def test_required_capabilities_carried_into_execution_policy(self):
        plan = self._build_plan(
            [
                {
                    "scope": "Analyze auth requirements",
                    "reason": "analysis",
                    "required_capabilities": ["analysis"],
                }
            ]
        )
        mission = generate_mission_from_plan(plan, run_id="RUN-1")
        task = mission.tasks[0]
        self.assertEqual(task.execution_policy["required_capabilities"], ["analysis"])

    def test_missing_required_capabilities_yields_empty_policy(self):
        plan = self._build_plan([{"scope": "Build", "reason": "impl"}])
        mission = generate_mission_from_plan(plan, run_id="RUN-1")
        task = mission.tasks[0]
        self.assertNotIn("required_capabilities", task.execution_policy or {})


if __name__ == "__main__":
    unittest.main()
