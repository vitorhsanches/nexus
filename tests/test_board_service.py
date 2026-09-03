import unittest

import nexus.board.service as service
import nexus.missions.service as mission_service
import nexus.tasks.registry as task_registry
from nexus.board.events import BoardEvent
from nexus.board.models import BoardColumn
from nexus.board.service import (
    BoardNotFoundError,
    assign_task,
    create_board,
    get_board,
    move_task,
    record_event,
)


class BoardServiceTestCase(unittest.TestCase):
    def setUp(self):
        service._boards.clear()
        mission_service._missions.clear()
        task_registry._tasks.clear()
        task_registry._attempts.clear()
        task_registry._next_attempt.clear()

    def _make_task(self, mission_id, title="Task"):
        return task_registry.create_task(mission_id=mission_id, title=title)

    def test_board_creation_seeds_columns(self):
        mission = mission_service.create_mission(run_id="RUN-1", title="Mission")
        task = self._make_task(mission.mission_id)

        board = create_board(mission)

        self.assertEqual(board.mission_id, mission.mission_id)
        self.assertEqual(board.board_id, f"BOARD-{mission.mission_id}")
        self.assertIn(task.task_id, board.columns[BoardColumn.TODO])

    def test_board_creation_by_mission_id(self):
        mission = mission_service.create_mission(run_id="RUN-1", title="Mission")
        board = create_board(mission.mission_id)
        self.assertEqual(board.mission_id, mission.mission_id)

    def test_task_assignment(self):
        mission = mission_service.create_mission(run_id="RUN-1", title="Mission")
        task = self._make_task(mission.mission_id)
        create_board(mission)

        assignment = assign_task(task.task_id, "agent-1")

        self.assertEqual(assignment.task_id, task.task_id)
        self.assertEqual(assignment.agent, "agent-1")
        self.assertEqual(task.assigned_agent, "agent-1")

        board = get_board(mission.mission_id)
        self.assertEqual(len(board.assignments), 1)
        self.assertEqual(board.assignments[0].agent, "agent-1")

        events = [e.event_type for e in board.events]
        self.assertIn("TASK_ASSIGNED", events)

    def test_task_movement(self):
        mission = mission_service.create_mission(run_id="RUN-1", title="Mission")
        task = self._make_task(mission.mission_id)
        create_board(mission)

        move_task(task.task_id, BoardColumn.IN_PROGRESS)
        move_task(task.task_id, BoardColumn.REVIEW)
        move_task(task.task_id, BoardColumn.DONE)

        board = get_board(mission.mission_id)
        for column in (BoardColumn.TODO, BoardColumn.IN_PROGRESS, BoardColumn.REVIEW):
            self.assertNotIn(task.task_id, board.columns[column])
        self.assertIn(task.task_id, board.columns[BoardColumn.DONE])
        self.assertEqual(board.assignments[-1].column, BoardColumn.DONE)

    def test_task_movement_accepts_string_column(self):
        mission = mission_service.create_mission(run_id="RUN-1", title="Mission")
        task = self._make_task(mission.mission_id)
        create_board(mission)

        move_task(task.task_id, "IN_PROGRESS")

        board = get_board(mission.mission_id)
        self.assertIn(task.task_id, board.columns[BoardColumn.IN_PROGRESS])

    def test_event_recording(self):
        mission = mission_service.create_mission(run_id="RUN-1", title="Mission")
        task = self._make_task(mission.mission_id)
        create_board(mission)

        event = record_event(
            BoardEvent(
                event_type="TASK_CREATED",
                task_id=task.task_id,
                mission_id=mission.mission_id,
                metadata={"source": "test"},
            )
        )

        board = get_board(mission.mission_id)
        self.assertIs(board.events[-1], event)
        self.assertIsNotNone(event.timestamp)
        self.assertEqual(event.metadata["source"], "test")

    def test_retrieving_board_state(self):
        mission = mission_service.create_mission(run_id="RUN-1", title="Mission")
        task = self._make_task(mission.mission_id)
        create_board(mission)
        assign_task(task.task_id, "agent-1")
        move_task(task.task_id, BoardColumn.IN_PROGRESS)

        board = get_board(mission.mission_id)
        self.assertEqual(board.mission_id, mission.mission_id)
        self.assertIn(task.task_id, board.columns[BoardColumn.IN_PROGRESS])
        self.assertGreaterEqual(len(board.assignments), 2)
        self.assertTrue(any(e.event_type == "TASK_ASSIGNED" for e in board.events))

    def test_get_missing_board_raises(self):
        with self.assertRaises(BoardNotFoundError):
            get_board("MISSION-DOES-NOT-EXIST")

    def test_invalid_event_type_rejected(self):
        with self.assertRaises(ValueError):
            BoardEvent(
                event_type="BOGUS",
                task_id="TASK-1",
                mission_id="MISSION-1",
            )


if __name__ == "__main__":
    unittest.main()
