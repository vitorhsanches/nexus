"""Nexus v2.0-D.3.1 initial adaptive Worker binding regressions."""

import unittest
from unittest.mock import patch

from nexus.orchestration.progressive import (
    InitialRoutingUnavailable,
    execute_progressively,
    resolve_initial_worker_route,
)
from nexus.routing.service import (
    AdaptiveRoutingService,
    RoutingDecision,
)
from nexus.routing.telemetry import (
    DiscoveredModel,
    OmniRouteTelemetrySnapshot,
)


def planned_worker(
    route_class="complex-coding",
    model="gpt-5.6-sol",
    provider="openai",
    effort="high",
):
    return {
        "route_class": route_class,
        "execution_path": "OMNIROUTE",
        "provider": provider,
        "model": model,
        "effort": effort,
        "scope": "Implement the bounded change.",
        "reason": "Manager planning recommendation.",
    }


def review_result(verdict):
    return {
        "status": "COMPLETED",
        "reviewer_id": "REVIEWER-1",
        "routing": {
            "model": "cc/claude-sonnet-5-high",
            "provider": "claude",
            "effort": "high",
            "execution_path": "OMNIROUTE",
            "reason": "test",
            "degraded": False,
        },
        "review": {
            "verdict": verdict,
            "failure_class": (
                None if verdict == "PASS" else "TEST_FAILURE"
            ),
            "summary": f"review={verdict}",
            "evidence": [],
        },
    }


class FakeRoutingService:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def select_route_for_capability(
        self,
        capability,
        risk_level=None,
    ):
        self.calls.append((capability, risk_level))
        return self.decision


class InitialAdaptiveBindingTests(unittest.TestCase):
    def test_high_complex_coding_overrides_manager_model(self):
        snapshot = OmniRouteTelemetrySnapshot(
            discovered_models=(
                DiscoveredModel(
                    model_id="cc/claude-sonnet-5-high",
                    provider="claude",
                ),
            ),
        )

        service = AdaptiveRoutingService(
            telemetry_client=snapshot,
        )

        original = planned_worker()

        resolved, routing = resolve_initial_worker_route(
            original,
            plan_risk="HIGH",
            routing_service=service,
        )

        self.assertEqual(
            resolved["model"],
            "cc/claude-sonnet-5-high",
        )
        self.assertEqual(resolved["provider"], "claude")
        self.assertEqual(resolved["effort"], "low")
        self.assertEqual(
            resolved["execution_path"],
            "OMNIROUTE",
        )
        self.assertEqual(
            routing["capability"],
            "advanced-coding",
        )

        # Manager recommendation is not mutated.
        self.assertEqual(original["model"], "gpt-5.6-sol")
        self.assertEqual(original["provider"], "openai")
        self.assertEqual(original["effort"], "high")

    def test_critical_complex_coding_fails_closed(self):
        snapshot = OmniRouteTelemetrySnapshot(
            discovered_models=(
                DiscoveredModel(
                    model_id="cc/claude-sonnet-5-high",
                    provider="claude",
                ),
            ),
        )

        service = AdaptiveRoutingService(
            telemetry_client=snapshot,
        )

        with self.assertRaises(InitialRoutingUnavailable):
            resolve_initial_worker_route(
                planned_worker(),
                plan_risk="CRITICAL",
                routing_service=service,
            )

    def test_non_executable_route_class_fails_closed(self):
        with self.assertRaises(InitialRoutingUnavailable):
            resolve_initial_worker_route(
                planned_worker(
                    route_class="security-critical",
                ),
                plan_risk="HIGH",
                routing_service=FakeRoutingService(
                    RoutingDecision(
                        model="should-not-run",
                        provider="test",
                        effort="low",
                        execution_path="OMNIROUTE",
                        reason="should not be called",
                    )
                ),
            )

    @patch(
        "nexus.orchestration.progressive.update_run_status"
    )
    @patch(
        "nexus.orchestration.progressive.review_worker"
    )
    @patch(
        "nexus.orchestration.progressive.execute_worker"
    )
    def test_same_tier_retry_does_not_reroute(
        self,
        execute_worker_mock,
        review_worker_mock,
        _update_run_status_mock,
    ):
        decision = RoutingDecision(
            model="cc/claude-sonnet-5-low",
            provider="claude",
            effort="low",
            execution_path="OMNIROUTE",
            reason="test route",
            degraded=False,
        )
        service = FakeRoutingService(decision)

        execute_worker_mock.side_effect = [
            {
                "agent_id": "WORKER-1",
                "status": "COMPLETED",
                "exit_code": 0,
                "worktree": "C:/tmp/w1",
                "branch": "b1",
            },
            {
                "agent_id": "WORKER-2",
                "status": "COMPLETED",
                "exit_code": 0,
                "worktree": "C:/tmp/w2",
                "branch": "b2",
            },
        ]

        review_worker_mock.side_effect = [
            review_result("RETRY"),
            review_result("PASS"),
        ]

        outcome = execute_progressively(
            run_id="RUN-1",
            repo="C:/repo",
            manager_id="MANAGER-1",
            original_task="task",
            planned_worker=planned_worker(
                route_class="standard-coding",
            ),
            plan_risk="MEDIUM",
            routing_service=service,
        )

        self.assertEqual(outcome["status"], "COMPLETED")
        self.assertEqual(
            service.calls,
            [("standard-coding", "MEDIUM")],
        )
        self.assertEqual(execute_worker_mock.call_count, 2)

        first = execute_worker_mock.call_args_list[0].kwargs[
            "worker"
        ]
        second = execute_worker_mock.call_args_list[1].kwargs[
            "worker"
        ]

        self.assertEqual(
            first["model"],
            "cc/claude-sonnet-5-low",
        )
        self.assertEqual(second["model"], first["model"])
        self.assertEqual(second["effort"], first["effort"])

    @patch(
        "nexus.orchestration.progressive.update_run_status"
    )
    @patch(
        "nexus.orchestration.progressive.review_worker"
    )
    @patch(
        "nexus.orchestration.progressive.execute_worker"
    )
    def test_existing_escalation_ladder_remains_authoritative(
        self,
        execute_worker_mock,
        review_worker_mock,
        _update_run_status_mock,
    ):
        decision = RoutingDecision(
            model="oc/big-pickle",
            provider="opencode",
            effort="low",
            execution_path="OMNIROUTE",
            reason="test route",
            degraded=False,
        )
        service = FakeRoutingService(decision)

        execute_worker_mock.side_effect = [
            {
                "agent_id": "WORKER-1",
                "status": "COMPLETED",
                "exit_code": 0,
                "worktree": "C:/tmp/w1",
                "branch": "b1",
            },
            {
                "agent_id": "WORKER-2",
                "status": "COMPLETED",
                "exit_code": 0,
                "worktree": "C:/tmp/w2",
                "branch": "b2",
            },
        ]

        review_worker_mock.side_effect = [
            review_result("ESCALATE"),
            review_result("PASS"),
        ]

        outcome = execute_progressively(
            run_id="RUN-2",
            repo="C:/repo",
            manager_id="MANAGER-2",
            original_task="task",
            planned_worker=planned_worker(
                route_class="mechanical",
            ),
            plan_risk="LOW",
            routing_service=service,
        )

        self.assertEqual(outcome["status"], "COMPLETED")

        # Initial Adaptive Router is called exactly once.
        self.assertEqual(
            service.calls,
            [("mechanical", "LOW")],
        )

        self.assertEqual(execute_worker_mock.call_count, 2)

        first = execute_worker_mock.call_args_list[0].kwargs[
            "worker"
        ]
        second = execute_worker_mock.call_args_list[1].kwargs[
            "worker"
        ]

        self.assertEqual(first["model"], "oc/big-pickle")
        self.assertEqual(
            second["model"],
            "cc/claude-sonnet-5-low",
        )


if __name__ == "__main__":
    unittest.main()
