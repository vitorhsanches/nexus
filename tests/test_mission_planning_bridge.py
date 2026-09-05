"""Tests for the Nexus Mission Planning Bridge V1.

Covers ``POST /api/missions/{mission_id}/plan``, which bridges an existing
Mission through the existing ManagerAgent into materialized Tasks visible on
the existing Mission Board, without executing any Task.
"""

import unittest
from unittest.mock import patch

import nexus.board.service as board_service
import nexus.missions.service as mission_service
import nexus.tasks.registry as task_registry
import nexus.web.services as web_services
import nexus.workspaces.registry as session_registry
from nexus.agents.policy import resolve_execution_policy
from nexus.manager.agent import ManagerAgent, ManagerError
from nexus.missions.service import MissionNotFoundError
from nexus.web.agents import agent_registry


def _reset():
    """Clear the in-memory stores so each test starts clean."""
    mission_service._missions.clear()
    task_registry._tasks.clear()
    task_registry._attempts.clear()
    task_registry._next_attempt.clear()
    board_service._boards.clear()
    session_registry._sessions.clear()
    agent_registry._agents.clear()


def _client():
    from starlette.testclient import TestClient
    from nexus.web.app import app

    return TestClient(app)


class PlanMissionServiceTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_plan_mission_creates_manager_tasks(self):
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Implement the billing API"
        )

        result = web_services.plan_mission(mission.mission_id)

        self.assertGreater(len(result["tasks"]), 0)
        self.assertEqual(result["manager_id"], "manager-agent")
        self.assertTrue(result["board_seeded"])

        registry_tasks = task_registry.list_tasks()
        self.assertEqual(len(registry_tasks), len(result["tasks"]))
        for task in result["tasks"]:
            self.assertIn(task.task_id, {t.task_id for t in registry_tasks})

    def test_plan_mission_tasks_appear_on_board(self):
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Implement the billing API"
        )

        result = web_services.plan_mission(mission.mission_id)

        board = board_service.get_board(mission.mission_id)
        board_task_ids = {
            task_id
            for ids in board.columns.values()
            for task_id in ids
        }
        for task in result["tasks"]:
            self.assertIn(task.task_id, board_task_ids)

    def test_project_id_propagates_to_every_task(self):
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Implement the billing API", project_id="norte"
        )

        result = web_services.plan_mission(mission.mission_id)

        for task in result["tasks"]:
            self.assertEqual(task.execution_policy["project_id"], "norte")

    def test_execution_path_propagates_only_when_provided(self):
        mission = mission_service.create_mission(
            run_id="RUN-1",
            title="Implement the billing API",
            project_id="norte",
        )

        result = web_services.plan_mission(mission.mission_id)

        for task in result["tasks"]:
            self.assertNotIn("execution_path", task.execution_policy)

    def test_default_tasks_resolve_as_simulated(self):
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Implement the billing API", project_id="norte"
        )

        result = web_services.plan_mission(mission.mission_id)

        for task in result["tasks"]:
            resolved = resolve_execution_policy(task.execution_policy)
            self.assertEqual(resolved.execution_path, "simulated")

    def test_omniroute_tasks_are_structurally_ready(self):
        mission = mission_service.create_mission(
            run_id="RUN-1",
            title="Implement the billing API",
            project_id="norte",
            execution_path="omniroute",
        )

        result = web_services.plan_mission(mission.mission_id)

        for task in result["tasks"]:
            self.assertEqual(task.execution_policy["project_id"], "norte")
            self.assertEqual(task.execution_policy["execution_path"], "omniroute")

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
                resolved = resolve_execution_policy(
                    result["tasks"][0].execution_policy
                )

        self.assertEqual(resolved.execution_path, "omniroute")
        self.assertEqual(resolved.workspace_path, "/tmp/norte-repo-mocked")

    def test_unknown_mission_raises_mission_not_found(self):
        with self.assertRaises(MissionNotFoundError):
            web_services.plan_mission("MISSION-DOES-NOT-EXIST")

    def test_second_plan_call_is_idempotent(self):
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Implement the billing API"
        )

        first = web_services.plan_mission(mission.mission_id)
        first_task_ids = {t.task_id for t in first["tasks"]}

        second = web_services.plan_mission(mission.mission_id)
        second_task_ids = {t.task_id for t in second["tasks"]}

        self.assertEqual(first_task_ids, second_task_ids)
        self.assertEqual(len(task_registry.list_tasks()), len(first_task_ids))

        board = board_service.get_board(mission.mission_id)
        board_task_ids = [
            task_id
            for ids in board.columns.values()
            for task_id in ids
        ]
        # No duplicate task ids on the board after planning twice.
        self.assertEqual(len(board_task_ids), len(set(board_task_ids)))

    def test_second_plan_call_does_not_invoke_manager_again(self):
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Implement the billing API"
        )
        web_services.plan_mission(mission.mission_id)

        with patch.object(ManagerAgent, "execute") as mock_execute:
            web_services.plan_mission(mission.mission_id)
            mock_execute.assert_not_called()

    def test_plan_mission_never_executes_tasks(self):
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Implement the billing API"
        )

        with patch("nexus.web.execution.execute_task") as mock_execute_task, patch(
            "nexus.agents.executor.AgentExecutor.execute_task"
        ) as mock_agent_execute, patch(
            "nexus.agents.adapters.omniroute.OmniRouteAdapter.run"
        ) as mock_omniroute_run, patch(
            "subprocess.run"
        ) as mock_subprocess_run:
            web_services.plan_mission(mission.mission_id)

            mock_execute_task.assert_not_called()
            mock_agent_execute.assert_not_called()
            mock_omniroute_run.assert_not_called()
            mock_subprocess_run.assert_not_called()

    def test_manager_planning_error_is_surfaced(self):
        mission = mission_service.create_mission(
            run_id="RUN-1", title="Implement the billing API"
        )

        with patch.object(
            ManagerAgent, "execute", side_effect=ManagerError("boom")
        ):
            with self.assertRaises(ManagerError):
                web_services.plan_mission(mission.mission_id)

        # A failed planning attempt must not create any task.
        self.assertEqual(task_registry.list_tasks(), [])


class PlanMissionRouteTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_create_mission_still_creates_empty_mission(self):
        client = _client()
        with client:
            response = client.post(
                "/api/missions", json={"title": "Empty mission"}
            )
        self.assertEqual(response.status_code, 200)
        mission = response.json()["mission"]
        self.assertEqual(mission["title"], "Empty mission")
        self.assertEqual(mission["tasks"], [])

    def test_plan_route_creates_tasks_and_seeds_board(self):
        client = _client()
        with client:
            create_response = client.post(
                "/api/missions", json={"title": "Implement the billing API"}
            )
            mission_id = create_response.json()["mission"]["mission_id"]

            plan_response = client.post(f"/api/missions/{mission_id}/plan")

        self.assertEqual(plan_response.status_code, 200)
        payload = plan_response.json()
        self.assertGreater(len(payload["tasks"]), 0)
        self.assertTrue(payload["board_seeded"])
        self.assertEqual(payload["manager_id"], "manager-agent")

        board = board_service.get_board(mission_id)
        self.assertTrue(any(board.columns.values()))

    def test_plan_route_unknown_mission_returns_404(self):
        client = _client()
        with client:
            response = client.post("/api/missions/MISSION-UNKNOWN/plan")
        self.assertEqual(response.status_code, 404)

    def test_plan_route_twice_does_not_duplicate_tasks(self):
        client = _client()
        with client:
            create_response = client.post(
                "/api/missions", json={"title": "Implement the billing API"}
            )
            mission_id = create_response.json()["mission"]["mission_id"]

            first = client.post(f"/api/missions/{mission_id}/plan")
            second = client.post(f"/api/missions/{mission_id}/plan")

        first_ids = {t["task_id"] for t in first.json()["tasks"]}
        second_ids = {t["task_id"] for t in second.json()["tasks"]}
        self.assertEqual(first_ids, second_ids)
        self.assertEqual(len(task_registry.list_tasks()), len(first_ids))

    def test_execute_endpoint_unchanged_for_planned_task(self):
        client = _client()
        with client:
            create_response = client.post(
                "/api/missions", json={"title": "Implement the billing API"}
            )
            mission_id = create_response.json()["mission"]["mission_id"]
            plan_response = client.post(f"/api/missions/{mission_id}/plan")
            task_id = plan_response.json()["tasks"][0]["task_id"]

            execute_response = client.post(f"/api/tasks/{task_id}/execute")

        self.assertEqual(execute_response.status_code, 200)
        payload = execute_response.json()["execution"]
        self.assertEqual(payload["status"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
