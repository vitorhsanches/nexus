import unittest

import nexus.tasks.registry as task_registry
import nexus.workspaces.registry as session_registry
from nexus.agents.executor import AgentExecutor
from nexus.agents.models import Agent, TaskExecutionResult
from nexus.agents.registry import AgentRegistry
from nexus.tasks.registry import create_task, get_task
from nexus.workspaces.models import AgentSession
from nexus.workspaces.registry import (
    SessionNotFoundError,
    create_session,
    get_session,
    list_sessions,
    update_session,
)
from nexus.workspaces.service import (
    complete_session,
    fail_session,
    start_session,
)


def _reset_registries():
    session_registry._sessions.clear()
    task_registry._tasks.clear()


class AgentWorkspaceRegistryTestCase(unittest.TestCase):
    def setUp(self):
        _reset_registries()

    def test_create_session(self):
        session = create_session(task_id="TASK-1", agent_id="AGT-1")
        self.assertIsInstance(session, AgentSession)
        self.assertTrue(session.session_id.startswith("SESS-"))
        self.assertEqual(session.task_id, "TASK-1")
        self.assertEqual(session.agent_id, "AGT-1")
        self.assertEqual(session.status, "CREATED")
        self.assertIsNone(session.result)
        self.assertIsNone(session.error)

    def test_retrieve_session(self):
        session = create_session(task_id="TASK-1", agent_id="AGT-1")
        stored = get_session(session.session_id)
        self.assertIs(stored, session)

    def test_retrieve_missing_session_raises(self):
        with self.assertRaises(SessionNotFoundError):
            get_session("SESS-MISSING")

    def test_update_session(self):
        session = create_session(task_id="TASK-1", agent_id="AGT-1")
        updated = update_session(
            session.session_id,
            status="RUNNING",
            current_action="working",
            context={"step": 1},
        )
        self.assertIs(updated, session)
        self.assertEqual(session.status, "RUNNING")
        self.assertEqual(session.current_action, "working")
        self.assertEqual(session.context, {"step": 1})

    def test_update_session_rejects_unknown_field(self):
        session = create_session(task_id="TASK-1", agent_id="AGT-1")
        with self.assertRaises(AttributeError):
            update_session(session.session_id, bogus="value")

    def test_update_session_rejects_invalid_status(self):
        session = create_session(task_id="TASK-1", agent_id="AGT-1")
        with self.assertRaises(ValueError):
            update_session(session.session_id, status="BOGUS")

    def test_list_sessions(self):
        create_session(task_id="TASK-1", agent_id="AGT-1")
        create_session(task_id="TASK-1", agent_id="AGT-2")
        create_session(task_id="TASK-2", agent_id="AGT-1")
        self.assertEqual(len(list_sessions()), 3)
        self.assertEqual(len(list_sessions(task_id="TASK-1")), 2)
        self.assertEqual(len(list_sessions(agent_id="AGT-1")), 2)
        self.assertEqual(
            len(list_sessions(task_id="TASK-1", agent_id="AGT-1")), 1
        )


class AgentSessionLifecycleTestCase(unittest.TestCase):
    def setUp(self):
        _reset_registries()

    def test_start_marks_running(self):
        session = start_session(task_id="TASK-1", agent_id="AGT-1")
        self.assertEqual(session.status, "RUNNING")
        self.assertIsNotNone(session.started_at)

    def test_complete_session(self):
        session = start_session(task_id="TASK-1", agent_id="AGT-1")
        completed = complete_session(session.session_id, result="done")
        self.assertIs(completed, session)
        self.assertEqual(session.status, "COMPLETED")
        self.assertEqual(session.result, "done")
        self.assertIsNone(session.error)
        self.assertIsNotNone(session.finished_at)

    def test_fail_session(self):
        session = start_session(task_id="TASK-1", agent_id="AGT-1")
        failed = fail_session(session.session_id, error="boom")
        self.assertIs(failed, session)
        self.assertEqual(session.status, "FAILED")
        self.assertEqual(session.error, "boom")
        self.assertIsNotNone(session.finished_at)
        self.assertIsNone(session.result)

    def test_lifecycle_created_to_completed(self):
        session = start_session(task_id="TASK-1", agent_id="AGT-1")
        self.assertEqual(session.status, "RUNNING")
        complete_session(session.session_id, result="ok")
        stored = get_session(session.session_id)
        self.assertEqual(stored.status, "COMPLETED")
        self.assertEqual(stored.result, "ok")
        self.assertIsNotNone(stored.started_at)
        self.assertIsNotNone(stored.finished_at)


class AgentWorkspaceExecutorIntegrationTestCase(unittest.TestCase):
    def setUp(self):
        _reset_registries()

        self.registry = AgentRegistry()
        self.executor = AgentExecutor(self.registry)

        self.agent = self.registry.register_agent(
            Agent(
                agent_id="AGT-1",
                name="Alpha",
                provider="openai",
                model="gpt-5",
                capabilities=["coding"],
            )
        )
        self.task = create_task(mission_id="MISSION-1", title="Build the workspace")

    def test_executor_creates_and_completes_session(self):
        result = self.executor.execute_task(self.task.task_id, agent_id="AGT-1")

        self.assertIsInstance(result, TaskExecutionResult)
        self.assertEqual(result.status, "COMPLETED")

        session = self.executor.last_session
        self.assertIsNotNone(session)
        self.assertEqual(session.task_id, self.task.task_id)
        self.assertEqual(session.agent_id, "AGT-1")
        self.assertEqual(session.status, "COMPLETED")
        self.assertEqual(session.result, "Task executed successfully")
        self.assertIsNone(session.error)
        self.assertIsNotNone(session.started_at)
        self.assertIsNotNone(session.finished_at)

        # Session is retrievable through the registry.
        stored = get_session(session.session_id)
        self.assertIs(stored, session)

        # Task reached COMPLETED and agent was released.
        self.assertEqual(get_task(self.task.task_id).status, "COMPLETED")
        self.assertEqual(self.registry.get_agent("AGT-1").status, "AVAILABLE")

    def test_executor_marks_session_failed_on_error(self):
        # Force the lifecycle to raise so the session is marked FAILED.
        original = vars(AgentExecutor)["_run_task_lifecycle"]

        def _boom(task_id):
            raise RuntimeError("simulated failure")

        AgentExecutor._run_task_lifecycle = staticmethod(_boom)
        try:
            with self.assertRaises(RuntimeError):
                self.executor.execute_task(self.task.task_id, agent_id="AGT-1")
        finally:
            AgentExecutor._run_task_lifecycle = original

        session = self.executor.last_session
        self.assertIsNotNone(session)
        self.assertEqual(session.status, "FAILED")
        self.assertEqual(session.error, "simulated failure")

        # Agent released to FAILED after the failed run.
        self.assertEqual(self.registry.get_agent("AGT-1").status, "FAILED")


if __name__ == "__main__":
    unittest.main()
