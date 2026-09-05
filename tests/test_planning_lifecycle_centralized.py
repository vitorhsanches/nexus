"""Tests for the centralized planning lifecycle owned by plan_mission (V1.8)."""

import unittest
from unittest.mock import patch

import nexus.board.service as board_service
import nexus.missions.service as mission_service
import nexus.tasks.registry as task_registry
import nexus.web.services as web_services
import nexus.workspaces.registry as session_registry
from nexus.manager.agent import ManagerAgent, ManagerError
from nexus.manager.planner import MissionError
from nexus.web.agents import agent_registry


def _reset():
    mission_service._missions.clear()
    task_registry._tasks.clear()
    task_registry._attempts.clear()
    task_registry._next_attempt.clear()
    board_service._boards.clear()
    session_registry._sessions.clear()
    agent_registry._agents.clear()


class PlanningLifecycleTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_successful_plan_reaches_ready(self):
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Implement the billing API"
        )
        result = web_services.plan_mission(mission.mission_id)
        self.assertEqual(result["mission"].status, "READY")

    def test_never_left_stuck_planning_on_manager_error(self):
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Implement the billing API"
        )
        with patch.object(ManagerAgent, "execute", side_effect=ManagerError("boom")):
            with self.assertRaises(ManagerError):
                web_services.plan_mission(mission.mission_id)

        stored = mission_service.get_mission(mission.mission_id)
        self.assertEqual(stored.status, "FAILED")

    def test_planner_error_before_materialization_also_fails_mission(self):
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Implement the billing API"
        )
        with patch(
            "nexus.manager.planner.build_execution_plan",
            side_effect=MissionError("planner exploded"),
        ):
            with self.assertRaises(MissionError):
                web_services.plan_mission(mission.mission_id)

        stored = mission_service.get_mission(mission.mission_id)
        self.assertEqual(stored.status, "FAILED")

    def test_legacy_created_with_tasks_normalizes_to_ready(self):
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Implement the billing API"
        )
        task_registry.create_task(mission_id=mission.mission_id, title="Manual Task")
        self.assertEqual(mission.status, "CREATED")

        result = web_services.plan_mission(mission.mission_id)
        self.assertEqual(result["mission"].status, "READY")

    def test_legacy_planning_with_tasks_normalizes_to_ready(self):
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Implement the billing API"
        )
        task_registry.create_task(mission_id=mission.mission_id, title="Manual Task")
        mission_service.update_mission_status(mission.mission_id, "PLANNING")

        result = web_services.plan_mission(mission.mission_id)
        self.assertEqual(result["mission"].status, "READY")

    def test_already_ready_mission_remains_ready(self):
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Implement the billing API"
        )
        task_registry.create_task(mission_id=mission.mission_id, title="Manual Task")
        mission_service.update_mission_status(mission.mission_id, "PLANNING")
        mission_service.update_mission_status(mission.mission_id, "READY")

        result = web_services.plan_mission(mission.mission_id)
        self.assertEqual(result["mission"].status, "READY")

    def test_failed_mission_not_revived_by_planning(self):
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Implement the billing API"
        )
        mission_service.update_mission_status(mission.mission_id, "FAILED")

        result = web_services.plan_mission(mission.mission_id)
        self.assertEqual(result["mission"].status, "FAILED")
        self.assertEqual(task_registry.list_tasks(), [])


    def test_planning_lifecycle_failure_is_not_silently_swallowed(self):
        mission = mission_service.create_mission(
            run_id="RUN-1",
            title="Implement the billing API",
        )

        real_update = mission_service.update_mission_status

        def controlled_update(mission_id, status):
            if status == "FAILED":
                raise RuntimeError("cannot persist FAILED")
            return real_update(mission_id, status)

        with patch.object(
            ManagerAgent,
            "execute",
            side_effect=ManagerError("planning exploded"),
        ), patch(
            "nexus.web.services.update_mission_status",
            side_effect=controlled_update,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "could not transition to FAILED",
            ) as ctx:
                web_services.plan_mission(mission.mission_id)

        self.assertIsInstance(ctx.exception.__cause__, ManagerError)


    def test_materialization_status_reset_is_handled_safely(self):
        # ManagerAgent materialization may replace the Mission object with
        # the same mission_id and a freshly-created status of CREATED; the
        # planning lifecycle must still normalize the current stored
        # Mission (not the original PLANNING reference) through to READY.
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Implement the billing API"
        )
        result = web_services.plan_mission(mission.mission_id)

        stored = mission_service.get_mission(mission.mission_id)
        self.assertEqual(stored.status, "READY")
        self.assertEqual(result["mission"].mission_id, mission.mission_id)


if __name__ == "__main__":
    unittest.main()
