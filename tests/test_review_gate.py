"""Tests for Nexus v1.9 Real Review Gate + Retry Foundation V1."""

import unittest

import nexus.missions.service as mission_service
import nexus.tasks.registry as task_registry
import nexus.web.execution as execution_service
import nexus.workspaces.registry as session_registry
from nexus.agents.adapters.base import AdapterResult
from nexus.agents.adapters.omniroute import select_route
from nexus.agents.executor import AgentExecutor
from nexus.agents.models import Agent
from nexus.agents.registry import AgentRegistry
from nexus.reviews.execution import (
    ReviewedTaskFailedError,
    execute_reviewed_task,
)
from nexus.reviews.models import InvalidReviewDecisionError, ReviewDecision
from nexus.reviews.policy import decide_next_action
from nexus.reviews.reviewer import AlwaysPassReviewer, SequenceReviewer
from nexus.reviews.service import apply_review_decision
from nexus.tasks.lifecycle import InvalidTransitionError
from nexus.web.agents import agent_registry
from nexus.web.mission_execution import execute_mission


def _reset():
    mission_service._missions.clear()
    task_registry._tasks.clear()
    task_registry._attempts.clear()
    task_registry._next_attempt.clear()
    session_registry._sessions.clear()
    agent_registry._agents.clear()


def _make_task(**kwargs):
    mission = mission_service.create_mission(run_id="RUN-1", title="Mission")
    defaults = {"mission_id": mission.mission_id, "title": "Build the feature"}
    defaults.update(kwargs)
    return mission, task_registry.create_task(**defaults)


def _register_agent(agent_id="AGT-1", capabilities=None):
    return agent_registry.register_agent(
        Agent(
            agent_id=agent_id,
            name="Alpha",
            provider="openai",
            model="gpt-5",
            capabilities=capabilities,
        )
    )


class RoutingFakeAdapter:
    """Fake adapter that honors route_override like OmniRouteAdapter would,
    without any subprocess/provider call."""

    def run(self, context):
        model, effort = select_route(
            context.required_capabilities, context.route_override
        )
        return AdapterResult(success=True, output="ok", error=None, routed_model=model)


class DirectExecutionBackwardCompatTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_direct_execution_still_completes_immediately(self):
        mission, task = _make_task()
        _register_agent(capabilities=["coding"])
        summary = execution_service.execute_task(task.task_id)
        self.assertEqual(summary["status"], "COMPLETED")
        self.assertEqual(task_registry.get_task(task.task_id).status, "COMPLETED")


class HoldForReviewTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_hold_for_review_leaves_task_in_review(self):
        mission, task = _make_task()
        _register_agent(capabilities=["coding"])
        summary = execution_service.execute_task(task.task_id, hold_for_review=True)
        self.assertEqual(summary["status"], "REVIEW")
        self.assertEqual(task_registry.get_task(task.task_id).status, "REVIEW")

    def test_session_completed_agent_available_while_task_in_review(self):
        mission, task = _make_task()
        agent = _register_agent(capabilities=["coding"])
        executor = AgentExecutor(agent_registry)
        result = executor.execute_task(task.task_id, hold_for_review=True)
        self.assertEqual(result.status, "REVIEW")
        session = executor.last_session
        self.assertEqual(session.status, "COMPLETED")
        self.assertEqual(agent_registry.get_agent(agent.agent_id).status, "AVAILABLE")

    def test_reviewed_attempt_status_is_review(self):
        mission, task = _make_task()
        _register_agent(capabilities=["coding"])
        summary = execution_service.execute_task(task.task_id, hold_for_review=True)
        attempt = task_registry.get_attempt(summary["attempt_id"])
        self.assertEqual(attempt.status, "REVIEW")


class ApplyReviewDecisionTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def _held_attempt(self):
        mission, task = _make_task()
        _register_agent(capabilities=["coding"])
        summary = execution_service.execute_task(task.task_id, hold_for_review=True)
        return task.task_id, summary["attempt_id"]

    def test_pass_completes_task_and_attempt(self):
        task_id, attempt_id = self._held_attempt()
        decision = ReviewDecision(verdict="PASS", summary="ok", evidence=["e"])
        result = apply_review_decision(task_id, attempt_id, decision)
        self.assertEqual(result.task_status, "COMPLETED")
        self.assertEqual(result.attempt_status, "COMPLETED")
        self.assertEqual(task_registry.get_task(task_id).status, "COMPLETED")

    def test_retry_returns_task_to_ready(self):
        task_id, attempt_id = self._held_attempt()
        decision = ReviewDecision(
            verdict="RETRY", failure_class="TRANSIENT", summary="try again", evidence=["e"]
        )
        result = apply_review_decision(task_id, attempt_id, decision)
        self.assertEqual(result.task_status, "READY")

    def test_blocked_fails_task(self):
        task_id, attempt_id = self._held_attempt()
        decision = ReviewDecision(
            verdict="BLOCKED", failure_class="REQUIREMENT_FAILURE", summary="stop", evidence=["e"]
        )
        result = apply_review_decision(task_id, attempt_id, decision)
        self.assertEqual(result.task_status, "FAILED")

    def test_completed_cannot_be_reopened(self):
        task_id, attempt_id = self._held_attempt()
        decision = ReviewDecision(verdict="PASS", summary="ok", evidence=["e"])
        apply_review_decision(task_id, attempt_id, decision)
        with self.assertRaises(Exception):
            apply_review_decision(task_id, attempt_id, decision)

    def test_failed_cannot_be_reopened(self):
        task_id, attempt_id = self._held_attempt()
        decision = ReviewDecision(
            verdict="BLOCKED", failure_class="REQUIREMENT_FAILURE", summary="stop", evidence=["e"]
        )
        apply_review_decision(task_id, attempt_id, decision)
        with self.assertRaises(Exception):
            apply_review_decision(task_id, attempt_id, decision)

    def test_task_transition_rejects_arbitrary_terminal_revival(self):
        from nexus.tasks.lifecycle import transition
        with self.assertRaises(InvalidTransitionError):
            transition("COMPLETED", "READY")
        with self.assertRaises(InvalidTransitionError):
            transition("FAILED", "READY")


class ReviewDecisionValidationTestCase(unittest.TestCase):
    def test_unknown_verdict_rejected(self):
        with self.assertRaises(InvalidReviewDecisionError):
            ReviewDecision(verdict="MAYBE", summary="x", evidence=["e"])

    def test_pass_with_failure_class_rejected(self):
        with self.assertRaises(InvalidReviewDecisionError):
            ReviewDecision(verdict="PASS", failure_class="TRANSIENT", summary="x", evidence=["e"])

    def test_missing_summary_rejected(self):
        with self.assertRaises(InvalidReviewDecisionError):
            ReviewDecision(verdict="PASS", summary="", evidence=["e"])

    def test_empty_evidence_rejected(self):
        with self.assertRaises(InvalidReviewDecisionError):
            ReviewDecision(verdict="PASS", summary="x", evidence=[])

    def test_invalid_failure_class_rejected(self):
        with self.assertRaises(InvalidReviewDecisionError):
            ReviewDecision(verdict="RETRY", failure_class="NOT_A_CLASS", summary="x", evidence=["e"])


class ReviewedTaskRetryTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_retry_then_pass_same_model_two_attempts(self):
        mission, task = _make_task()
        _register_agent(capabilities=["coding"])
        reviewer = SequenceReviewer(["RETRY", "PASS"])
        result = execute_reviewed_task(task.task_id, reviewer=reviewer)
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(task_registry.get_task(task.task_id).status, "COMPLETED")

        attempts = task_registry.list_attempts(task.task_id)
        self.assertEqual(len(attempts), 2)
        ids = {a.attempt_id for a in attempts}
        self.assertEqual(len(ids), 2)
        self.assertEqual(attempts[0].status, "FAILED")
        self.assertEqual(attempts[1].status, "COMPLETED")
        self.assertEqual(attempts[0].model, attempts[1].model)

        sessions = session_registry.list_sessions(task_id=task.task_id)
        self.assertEqual(len(sessions), 2)

    def test_failure_context_reaches_retry_worker(self):
        mission, task = _make_task()
        _register_agent(capabilities=["coding"])
        captured = []
        real_execute = execution_service.execute_task

        def spy(task_id, mission_context=None, **kwargs):
            captured.append(mission_context)
            return real_execute(task_id, mission_context=mission_context, **kwargs)

        import nexus.reviews.execution as reviewed_module
        original = reviewed_module.execution_service.execute_task
        reviewed_module.execution_service.execute_task = spy
        try:
            reviewer = SequenceReviewer(["RETRY", "PASS"])
            execute_reviewed_task(task.task_id, reviewer=reviewer)
        finally:
            reviewed_module.execution_service.execute_task = original

        self.assertEqual(len(captured), 2)
        self.assertIsNone(captured[0])
        self.assertIn("review_feedback", captured[1])
        self.assertIn("RETRY", captured[1]["review_feedback"])


class ReviewedTaskRetryExhaustionTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_mechanical_retry_exhaustion_escalates_to_sonnet(self):
        mission, task = _make_task(
            execution_policy={"required_capabilities": ["mechanical"]}
        )
        _register_agent(capabilities=["mechanical"])
        reviewer = SequenceReviewer(["RETRY", "RETRY", "PASS"])
        result = execute_reviewed_task(task.task_id, reviewer=reviewer, adapter=RoutingFakeAdapter())
        self.assertEqual(result.status, "COMPLETED")

        attempts = task_registry.list_attempts(task.task_id)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(
            [a.model for a in attempts],
            ["oc/big-pickle", "oc/big-pickle", "cc/claude-sonnet-5-low"],
        )


class ReviewedTaskImmediateEscalationTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_mechanical_immediate_escalation_to_sonnet(self):
        mission, task = _make_task(
            execution_policy={"required_capabilities": ["mechanical"]}
        )
        _register_agent(capabilities=["mechanical"])
        reviewer = SequenceReviewer(["ESCALATE", "PASS"])
        result = execute_reviewed_task(task.task_id, reviewer=reviewer, adapter=RoutingFakeAdapter())
        self.assertEqual(result.status, "COMPLETED")

        attempts = task_registry.list_attempts(task.task_id)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(
            [a.model for a in attempts],
            ["oc/big-pickle", "cc/claude-sonnet-5-low"],
        )
        self.assertEqual(task_registry.get_task(task.task_id).status, "COMPLETED")


class ReviewedTaskEscalationUnavailableTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_standard_coding_escalation_unavailable_fails_closed(self):
        mission, task = _make_task(
            execution_policy={"required_capabilities": ["coding"]}
        )
        _register_agent(capabilities=["coding"])
        reviewer = SequenceReviewer(["ESCALATE"])

        with self.assertRaises(ReviewedTaskFailedError) as ctx:
            execute_reviewed_task(task.task_id, reviewer=reviewer, adapter=RoutingFakeAdapter())

        self.assertIn("ESCALATION_UNAVAILABLE", ctx.exception.reason)
        self.assertEqual(task_registry.get_task(task.task_id).status, "FAILED")

        attempts = task_registry.list_attempts(task.task_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].model, "cc/claude-sonnet-5-low")


class ReviewedTaskBlockedTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_blocked_stops_immediately_no_retry_no_escalation(self):
        mission, task = _make_task()
        _register_agent(capabilities=["coding"])
        reviewer = SequenceReviewer(["BLOCKED"])

        with self.assertRaises(ReviewedTaskFailedError) as ctx:
            execute_reviewed_task(task.task_id, reviewer=reviewer)

        self.assertEqual(ctx.exception.verdict, "BLOCKED")
        self.assertEqual(task_registry.get_task(task.task_id).status, "FAILED")
        attempts = task_registry.list_attempts(task.task_id)
        self.assertEqual(len(attempts), 1)


class ReviewPolicyTestCase(unittest.TestCase):
    def test_pass_completes(self):
        decision = ReviewDecision(verdict="PASS", summary="ok", evidence=["e"])
        route = {"route_class": "mechanical", "model": "oc/big-pickle", "effort": "low"}
        action = decide_next_action(decision, route, same_tier_retries=0)
        self.assertEqual(action.action, "COMPLETE")

    def test_blocked_stops(self):
        decision = ReviewDecision(
            verdict="BLOCKED", failure_class="UNKNOWN", summary="stop", evidence=["e"]
        )
        route = {"route_class": "mechanical", "model": "oc/big-pickle", "effort": "low"}
        action = decide_next_action(decision, route, same_tier_retries=0)
        self.assertEqual(action.action, "STOP")


class MissionReviewFlowTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_mission_requires_pass_before_dependency_unlocks(self):
        mission = mission_service.create_mission(run_id="RUN-1", title="Mission")
        task1 = task_registry.create_task(
            mission_id=mission.mission_id, title="Task 1"
        )
        task2 = task_registry.create_task(
            mission_id=mission.mission_id,
            title="Task 2",
            dependencies=[task1.task_id],
        )
        mission.tasks = [task1, task2]
        _register_agent(capabilities=["coding"])

        summary = execute_mission(
            mission.mission_id, review=True, reviewer=AlwaysPassReviewer()
        )

        self.assertEqual(summary["status"], "COMPLETED")
        self.assertEqual(task_registry.get_task(task1.task_id).status, "COMPLETED")
        self.assertEqual(task_registry.get_task(task2.task_id).status, "COMPLETED")

    def test_mission_blocked_task_fails_mission_dependents_dont_run(self):
        mission = mission_service.create_mission(run_id="RUN-1", title="Mission")
        task1 = task_registry.create_task(
            mission_id=mission.mission_id, title="Task 1"
        )
        task2 = task_registry.create_task(
            mission_id=mission.mission_id,
            title="Task 2",
            dependencies=[task1.task_id],
        )
        mission.tasks = [task1, task2]
        _register_agent(capabilities=["coding"])

        with self.assertRaises(Exception):
            execute_mission(
                mission.mission_id,
                review=True,
                reviewer=SequenceReviewer(["BLOCKED"]),
            )

        self.assertEqual(task_registry.get_task(task1.task_id).status, "FAILED")
        self.assertEqual(task_registry.get_task(task2.task_id).status, "CREATED")



class NeverRunAdapter:
    """Adapter used to prove invalid routes fail before execution."""

    def __init__(self):
        self.called = False

    def run(self, context):
        self.called = True
        return AdapterResult(
            success=True,
            output="should-not-run",
            error=None,
            routed_model="should-not-run",
        )


class RouteOverrideSafetyRegressionTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_unapproved_model_rejected_before_adapter(self):
        mission, task = _make_task(
            execution_policy={
                "required_capabilities": ["coding"]
            }
        )
        adapter = NeverRunAdapter()

        with self.assertRaises(Exception):
            execution_service.execute_task(
                task.task_id,
                route_override={
                    "route_class": "standard-coding",
                    "model": "unapproved/model",
                    "effort": "low",
                },
                adapter=adapter,
            )

        self.assertFalse(adapter.called)
        self.assertEqual(
            task_registry.get_task(task.task_id).status,
            "CREATED",
        )
        self.assertEqual(
            len(task_registry.list_attempts(task.task_id)),
            0,
        )

    def test_wrong_approved_route_class_is_rejected(self):
        mission, task = _make_task(
            execution_policy={
                "required_capabilities": ["coding"]
            }
        )
        adapter = NeverRunAdapter()

        with self.assertRaises(Exception):
            execution_service.execute_task(
                task.task_id,
                route_override={
                    "route_class": "mechanical",
                    "model": "oc/big-pickle",
                    "effort": "low",
                },
                adapter=adapter,
            )

        self.assertFalse(adapter.called)

    def test_adapter_boundary_rejects_arbitrary_model(self):
        with self.assertRaises(Exception):
            select_route(
                ["coding"],
                {
                    "route_class": "standard-coding",
                    "model": "arbitrary/model",
                    "effort": "low",
                },
            )


class MalformedReviewerFailClosedTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_malformed_reviewer_fails_task_and_attempt(self):
        class MalformedReviewer:
            def review(self, evidence):
                return {
                    "verdict": "PASS",
                    "summary": "",
                    "evidence": [],
                }

        mission, task = _make_task(
            execution_policy={
                "required_capabilities": ["coding"]
            }
        )
        _register_agent(capabilities=["coding"])

        with self.assertRaises(
            InvalidReviewDecisionError
        ):
            execute_reviewed_task(
                task.task_id,
                reviewer=MalformedReviewer(),
            )

        current = task_registry.get_task(
            task.task_id
        )
        attempts = task_registry.list_attempts(
            task.task_id
        )

        self.assertEqual(
            current.status,
            "FAILED",
        )
        self.assertEqual(
            len(attempts),
            1,
        )
        self.assertEqual(
            attempts[0].status,
            "FAILED",
        )
        self.assertIn(
            "REVIEW_ERROR",
            attempts[0].result,
        )


class AttemptTerminalEvidenceRegressionTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_completed_attempt_cannot_be_reopened(self):
        mission, task = _make_task()
        _register_agent(capabilities=["coding"])

        summary = execution_service.execute_task(
            task.task_id,
            hold_for_review=True,
        )

        decision = ReviewDecision(
            verdict="PASS",
            summary="approved",
            evidence=["evidence"],
        )

        apply_review_decision(
            task.task_id,
            summary["attempt_id"],
            decision,
        )

        from nexus.tasks.registry import (
            InvalidAttemptTransitionError,
        )

        with self.assertRaises(
            InvalidAttemptTransitionError
        ):
            task_registry.update_attempt_status(
                summary["attempt_id"],
                "FAILED",
            )

    def test_escalation_unavailable_rejects_cross_task_attempt(self):
        from nexus.reviews.service import (
            ReviewApplicationError,
            apply_escalation_unavailable,
        )

        mission1, task1 = _make_task()
        _register_agent(capabilities=["coding"])

        first = execution_service.execute_task(
            task1.task_id,
            hold_for_review=True,
        )

        mission2, task2 = _make_task()

        second = execution_service.execute_task(
            task2.task_id,
            hold_for_review=True,
        )

        decision = ReviewDecision(
            verdict="ESCALATE",
            failure_class="CAPABILITY_FAILURE",
            summary="needs stronger model",
            evidence=["evidence"],
        )

        with self.assertRaises(
            ReviewApplicationError
        ):
            apply_escalation_unavailable(
                task1.task_id,
                second["attempt_id"],
                decision,
                "ESCALATION_UNAVAILABLE",
            )

        self.assertEqual(
            task_registry.get_attempt(
                first["attempt_id"]
            ).status,
            "REVIEW",
        )
        self.assertEqual(
            task_registry.get_attempt(
                second["attempt_id"]
            ).status,
            "REVIEW",
        )


class ReviewedResultEvidenceRegressionTestCase(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_result_reports_terminal_attempt_statuses(self):
        mission, task = _make_task(
            execution_policy={
                "required_capabilities": ["coding"]
            }
        )
        _register_agent(capabilities=["coding"])

        result = execute_reviewed_task(
            task.task_id,
            reviewer=SequenceReviewer(
                ["RETRY", "PASS"]
            ),
        )

        self.assertEqual(
            [
                attempt["status"]
                for attempt in result.attempts
            ],
            [
                "FAILED",
                "COMPLETED",
            ],
        )
