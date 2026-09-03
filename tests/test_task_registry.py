import unittest

import nexus.tasks.models as models
import nexus.tasks.registry as registry
from nexus.tasks.lifecycle import InvalidTransitionError, can_transition, transition
from nexus.tasks.registry import (
    TaskNotFoundError,
    create_attempt,
    create_task,
    get_attempt,
    get_task,
    list_attempts,
    list_tasks,
    update_task_status,
)

VALID_CHAIN = [
    "CREATED",
    "READY",
    "CLAIMED",
    "RUNNING",
    "REVIEW",
    "COMPLETED",
]


class TaskModelsTestCase(unittest.TestCase):
    def test_task_defaults(self):
        task = models.Task(task_id="TASK-1", mission_id="MISSION-1", title="T")
        self.assertEqual(task.status, "CREATED")
        self.assertEqual(task.priority, "MEDIUM")
        self.assertIsNone(task.dependencies)
        self.assertIsNone(task.assigned_agent)

    def test_mission_fields(self):
        mission = models.Mission(
            mission_id="MISSION-1",
            run_id="RUN-1",
            title="Mission title",
            description="Description",
        )
        self.assertEqual(mission.run_id, "RUN-1")

    def test_attempt_defaults(self):
        attempt = models.Attempt(
            attempt_id="ATT-1", task_id="TASK-1", agent_id="AGT-1", model="gpt"
        )
        self.assertEqual(attempt.status, "PENDING")
        self.assertIsNone(attempt.result)


class TaskLifecycleTestCase(unittest.TestCase):
    def test_full_valid_chain(self):
        for index in range(len(VALID_CHAIN) - 1):
            current = VALID_CHAIN[index]
            nxt = VALID_CHAIN[index + 1]
            self.assertTrue(can_transition(current, nxt))
            self.assertEqual(transition(current, nxt), nxt)

    def test_invalid_transition_raises(self):
        with self.assertRaises(InvalidTransitionError):
            transition("CREATED", "COMPLETED")

    def test_invalid_from_status_raises(self):
        with self.assertRaises(InvalidTransitionError):
            transition("BOGUS", "READY")

    def test_terminal_states_reject_all(self):
        for terminal in ("COMPLETED", "FAILED"):
            for candidate in VALID_CHAIN:
                with self.subTest(terminal=terminal, candidate=candidate):
                    self.assertFalse(can_transition(terminal, candidate))


class TaskRegistryTestCase(unittest.TestCase):
    def setUp(self):
        registry._tasks.clear()
        registry._attempts.clear()
        registry._next_attempt.clear()
        self.task = create_task(
            mission_id="MISSION-1",
            title="Build OAuth service",
            description="Wrap the OAuth client.",
            dependencies=[],
            acceptance_criteria=["tests pass"],
        )

    def test_task_creation(self):
        self.assertEqual(self.task.status, "CREATED")
        self.assertEqual(self.task.mission_id, "MISSION-1")
        self.assertEqual(self.task.acceptance_criteria, ["tests pass"])
        self.assertEqual(self.task.dependencies, [])

    def test_task_retrieval(self):
        fetched = get_task(self.task.task_id)
        self.assertIs(fetched, self.task)

    def test_get_missing_task_raises(self):
        with self.assertRaises(TaskNotFoundError):
            get_task("TASK-DOES-NOT-EXIST")

    def test_list_tasks(self):
        create_task(mission_id="MISSION-2", title="Another task")
        ids = {task.task_id for task in list_tasks()}
        self.assertIn(self.task.task_id, ids)
        self.assertEqual(len(ids), 2)

    def test_status_transitions(self):
        for status in VALID_CHAIN[1:]:
            updated = update_task_status(self.task.task_id, status)
            self.assertEqual(updated.status, status)

    def test_invalid_transition_rejected(self):
        with self.assertRaises(InvalidTransitionError):
            update_task_status(self.task.task_id, "COMPLETED")
        self.assertEqual(self.task.status, "CREATED")

    def test_attempt_creation(self):
        attempt = create_attempt(
            task_id=self.task.task_id,
            agent_id="AGT-1",
            model="gpt-5",
        )
        self.assertEqual(attempt.task_id, self.task.task_id)
        self.assertEqual(attempt.status, "PENDING")
        self.assertEqual(get_attempt(attempt.attempt_id), attempt)

    def test_multiple_attempts_history(self):
        first = create_attempt(self.task.task_id, "AGT-1", "gpt")
        second = create_attempt(self.task.task_id, "AGT-2", "claude")
        attempts = list_attempts(task_id=self.task.task_id)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].attempt_id, first.attempt_id)
        self.assertEqual(attempts[1].attempt_id, second.attempt_id)
        # history is never overwritten
        self.assertEqual(get_attempt(first.attempt_id), first)
        self.assertEqual(get_attempt(second.attempt_id), second)

    def test_create_attempt_for_missing_task_raises(self):
        with self.assertRaises(TaskNotFoundError):
            create_attempt("TASK-MISSING", "AGT-1", "gpt")


if __name__ == "__main__":
    unittest.main()
