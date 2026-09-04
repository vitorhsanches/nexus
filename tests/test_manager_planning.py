"""Tests for the Nexus Manager Agent Planning Engine V1."""

import unittest

import nexus.board.service as board_service
import nexus.missions.service as mission_service
import nexus.tasks.registry as task_registry
import nexus.web.services as web_services
import nexus.workspaces.registry as session_registry
from nexus.board.models import BoardColumn
from nexus.manager.agent import ManagerAgent, ManagerError
from nexus.manager.models import (
    CAPABILITY_ANALYSIS,
    CAPABILITY_ARCHITECTURE,
    CAPABILITY_CODING,
)
from nexus.manager.planner import MissionError, build_execution_plan
from nexus.router.intent import ANALYSIS, EXECUTION, QUESTION
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


def _create_mission(title, description=None):
    return mission_service.create_mission(
        run_id="RUN-1",
        title=title,
        description=description,
    )


class PlannerTestCase(unittest.TestCase):
    """Verifies deterministic, mission-driven planning."""

    def setUp(self):
        _reset()

    def test_coding_mission_yields_execution_intent(self):
        plan = build_execution_plan(_create_mission("Implement the auth module"))
        self.assertEqual(plan.intent, EXECUTION)

    def test_coding_mission_yields_coding_chain_with_all_capabilities(self):
        plan = build_execution_plan(
            _create_mission("Build the API", "Fix the reported bug and add tests")
        )
        caps = {
            tuple(sorted(task.required_capabilities)) for task in plan.tasks
        }
        self.assertEqual(
            caps,
            {
                (CAPABILITY_ANALYSIS,),
                (CAPABILITY_ARCHITECTURE,),
                (CAPABILITY_CODING,),
            },
        )

    def test_analysis_mission_yields_analysis_intent(self):
        plan = build_execution_plan(_create_mission("Review architecture risks"))
        self.assertEqual(plan.intent, ANALYSIS)

    def test_question_mission_yields_question_intent(self):
        plan = build_execution_plan(
            _create_mission("What is the recommended approach?")
        )
        self.assertEqual(plan.intent, QUESTION)

    def test_description_contributes_to_classification(self):
        plan = build_execution_plan(
            _create_mission(
                "Auth module",
                "Refactor the login flow and migrate to the new store",
            )
        )
        self.assertEqual(plan.intent, EXECUTION)

    def test_plan_is_deterministic(self):
        mission = _create_mission(
            "Implement OAuth", "Design the architecture and fix the client"
        )
        first = build_execution_plan(mission)
        second = build_execution_plan(mission)
        self.assertEqual(
            [t.scope for t in first.tasks],
            [t.scope for t in second.tasks],
        )
        self.assertEqual(first.intent, second.intent)

    def test_missing_title_rejected(self):
        with self.assertRaises(MissionError):
            build_execution_plan(_create_mission("   "))


class ManagerAgentTestCase(unittest.TestCase):
    """Verifies the Manager materializes tasks through the Mission Engine."""

    def setUp(self):
        _reset()

    def test_manager_plans_a_mission(self):
        mission = _create_mission("Implement the dark mode toggle")
        manager = ManagerAgent()
        plan = manager.plan(mission)
        self.assertEqual(plan.intent, EXECUTION)
        self.assertEqual(plan.mission_id, mission.mission_id)

    def test_manager_materializes_tasks_via_engine(self):
        mission = _create_mission(
            "Implement the signup flow", "Fix validation and add tests"
        )
        manager = ManagerAgent()
        result = manager.execute(mission)

        self.assertEqual(result.mission.status, "CREATED")
        self.assertEqual(len(result.tasks), 3)

        # Tasks live in the shared Task Registry (engine source of truth).
        registry_tasks = task_registry.list_tasks()
        self.assertEqual(len(registry_tasks), 3)
        for task in result.tasks:
            self.assertEqual(task.mission_id, result.mission.mission_id)
            self.assertIn(task.task_id, {t.task_id for t in registry_tasks})

    def test_tasks_carry_required_capabilities(self):
        mission = _create_mission("Create the billing API")
        manager = ManagerAgent()
        result = manager.execute(mission)

        caps = {
            tuple(task.execution_policy["required_capabilities"])
            for task in result.tasks
        }
        self.assertEqual(
            caps,
            {
                (CAPABILITY_ANALYSIS,),
                (CAPABILITY_ARCHITECTURE,),
                (CAPABILITY_CODING,),
            },
        )

    def test_generated_tasks_appear_on_mission_board(self):
        mission = _create_mission("Implement the dashboard")
        manager = ManagerAgent()
        result = manager.execute(mission)

        # The Manager seeds a board, and the web view reflects the tasks.
        board = board_service.get_board(result.mission.mission_id)
        all_task_ids = [
            task_id
            for column in BoardColumn
            for task_id in board.columns[column]
        ]
        self.assertEqual(len(all_task_ids), 3)

        web_tasks = web_services.get_tasks()
        board_task_ids = {
            task["task_id"] for task in web_tasks
            if task["mission_id"] == result.mission.mission_id
        }
        self.assertEqual(board_task_ids, {t.task_id for t in result.tasks})

    def test_manager_routing_selects_agent_for_task(self):
        # Seed the default agents so the capability router can resolve them.
        from nexus.agents.bootstrap import initialize_default_agents

        registered = initialize_default_agents(agent_registry)
        self.assertTrue(registered)

        mission = _create_mission("Implement the API")
        manager = ManagerAgent()
        result = manager.execute(mission)

        coding_task = next(
            task
            for task in result.tasks
            if task.execution_policy.get("required_capabilities") == [CAPABILITY_CODING]
        )
        agent = manager.select_agent_for_task(coding_task, agent_registry)
        self.assertEqual(agent.agent_id, "developer-agent")
        self.assertIn(CAPABILITY_CODING, agent.capabilities)

    def test_run_convenience(self):
        from nexus.manager.agent import run

        mission = _create_mission("Add export feature")
        result = run(mission)
        self.assertEqual(len(result.tasks), 3)
        self.assertEqual(task_registry.list_tasks(), result.tasks)


class ManagerPlanAndCreateTestCase(unittest.TestCase):
    """Verifies the plan_and_create and error paths."""

    def setUp(self):
        _reset()

    def test_plan_and_create_returns_mission(self):
        mission = _create_mission("Implement the mobile client")
        manager = ManagerAgent()
        created = manager.plan_and_create(mission)
        self.assertTrue(created.mission_id.startswith("MISSION-"))
        self.assertEqual(len(created.tasks), 3)

    def test_plan_and_create_accepts_dict_mission(self):
        manager = ManagerAgent()
        created = manager.plan_and_create(
            {
                "mission_id": "MISSION-X",
                "run_id": "RUN-X",
                "title": "Fix the login bug",
            }
        )
        self.assertEqual(created.mission_id, "MISSION-X")
        self.assertEqual(created.run_id, "RUN-X")
        self.assertEqual(len(created.tasks), 3)

    def test_invalid_mission_rejected(self):
        manager = ManagerAgent()
        with self.assertRaises((MissionError, ManagerError)):
            manager.execute(_create_mission("   "))


if __name__ == "__main__":
    unittest.main()
