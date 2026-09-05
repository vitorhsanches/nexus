"""Tests for Manager-generated linear Task dependency chains (Nexus V1.8)."""

import unittest

import nexus.board.service as board_service
import nexus.missions.service as mission_service
import nexus.tasks.registry as task_registry
import nexus.web.services as web_services
import nexus.workspaces.registry as session_registry
from nexus.web.agents import agent_registry


def _reset():
    mission_service._missions.clear()
    task_registry._tasks.clear()
    task_registry._attempts.clear()
    task_registry._next_attempt.clear()
    board_service._boards.clear()
    session_registry._sessions.clear()
    agent_registry._agents.clear()


class ManagerDependencyChainTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_generated_tasks_form_linear_chain(self):
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Implement the billing API"
        )
        result = web_services.plan_mission(mission.mission_id)
        tasks = result["tasks"]

        self.assertEqual(len(tasks), 3)
        self.assertEqual(tasks[0].dependencies, [])
        self.assertEqual(tasks[1].dependencies, [tasks[0].task_id])
        self.assertEqual(tasks[2].dependencies, [tasks[1].task_id])

    def test_dependencies_reference_same_mission(self):
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Implement the billing API"
        )
        result = web_services.plan_mission(mission.mission_id)
        tasks = result["tasks"]
        for task in tasks:
            for dep_id in task.dependencies or []:
                dep_task = task_registry.get_task(dep_id)
                self.assertEqual(dep_task.mission_id, mission.mission_id)

    def test_manual_task_dependencies_none_means_no_dependency(self):
        mission = mission_service.create_mission(run_id="RUN-1", title="Manual")
        task = task_registry.create_task(
            mission_id=mission.mission_id, title="Manual Task"
        )
        self.assertIsNone(task.dependencies)


if __name__ == "__main__":
    unittest.main()
