"""Focused tests for Nexus v2.0-D plan risk propagation and reviewer
routing metadata recorded in progressive execution history.

Test Gap 3: proves the operational chain
    run_go -> execute_progressively -> reviewer risk selection
propagates the Manager plan risk as plan_risk.

Test Gap 4: proves execute_progressively records reviewer routing
metadata (reviewer_model / reviewer_provider / reviewer_effort /
reviewer_execution_path / reviewer_routing_reason / reviewer_degraded)
in its successful history entry.

No real Manager, Worker, Codex, OmniRoute, subprocess, or network calls
are made; everything is mocked/injected.
"""

import unittest
from unittest.mock import patch

from nexus.orchestration.progressive import execute_progressively


class PlanRiskPropagationThroughRunGoTests(unittest.TestCase):
    def test_run_go_passes_plan_risk_into_execute_progressively(self):
        from nexus.orchestration import go as go_module

        fake_project = type(
            "FakeProject",
            (),
            {"id": "PROJECT-1", "name": "demo", "path": "C:/fake/repo"},
        )()

        manager_result = {
            "status": "COMPLETED",
            "manager_id": "MANAGER-1",
            "plan": {
                "risk": "MEDIUM",
                "summary": "did the thing",
                "workers": [
                    {
                        "model": "gpt-5.6-luna",
                        "scope": "fix things",
                    }
                ],
            },
        }

        progressive_result = {
            "status": "COMPLETED",
            "verdict": "PASS",
            "history": [],
            "worker": {"status": "COMPLETED"},
            "review": {"verdict": "PASS"},
        }

        captured_calls = []

        def _fake_execute_progressively(**kwargs):
            captured_calls.append(kwargs)
            return progressive_result

        with patch.object(
            go_module,
            "resolve_project_from_text",
            return_value=fake_project,
        ), patch.object(
            go_module,
            "create_run",
            return_value="RUN-1",
        ), patch.object(
            go_module,
            "update_run_status",
        ), patch.object(
            go_module,
            "update_run_result",
        ), patch.object(
            go_module,
            "run_manager",
            return_value=manager_result,
        ), patch.object(
            go_module,
            "execute_progressively",
            side_effect=_fake_execute_progressively,
        ), patch.object(
            go_module,
            "record_approved_plan",
        ), patch.object(
            go_module,
            "record_checkpoint",
        ):
            result = go_module.run_go("do the thing")

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(len(captured_calls), 1)
        self.assertEqual(captured_calls[0]["plan_risk"], "MEDIUM")


class ProgressiveHistoryReviewRoutingMetadataTests(unittest.TestCase):
    def test_history_records_reviewer_routing_metadata_on_pass(self):
        worker_result = {
            "status": "COMPLETED",
            "agent_id": "WORKER-AGENT-1",
            "worktree": "C:/fake/worktree",
        }

        review_result = {
            "status": "COMPLETED",
            "reviewer_id": "REVIEWER-1",
            "review": {
                "verdict": "PASS",
                "failure_class": None,
                "summary": "looks good",
            },
            "routing": {
                "model": "cc/claude-sonnet-5-low",
                "provider": "claude",
                "effort": "low",
                "execution_path": "OMNIROUTE",
                "reason": "adaptive routing selected approved route",
                "degraded": False,
            },
        }

        resolved_worker = {
            "route_class": "standard-coding",
            "execution_path": "OMNIROUTE",
            "provider": "claude",
            "model": "cc/claude-sonnet-5-low",
            "effort": "low",
            "scope": "fix things",
            "reason": "test initial adaptive route",
        }

        initial_routing = {
            "capability": "standard-coding",
            "risk": "MEDIUM",
            "model": "cc/claude-sonnet-5-low",
            "provider": "claude",
            "effort": "low",
            "execution_path": "OMNIROUTE",
            "reason": "adaptive initial routing",
            "degraded": False,
        }

        with patch(
            "nexus.orchestration.progressive.resolve_initial_worker_route",
            return_value=(resolved_worker, initial_routing),
        ), patch(
            "nexus.orchestration.progressive.execute_worker",
            return_value=worker_result,
        ), patch(
            "nexus.orchestration.progressive.review_worker",
            return_value=review_result,
        ), patch(
            "nexus.orchestration.progressive.update_run_status",
        ), patch(
            "nexus.orchestration.progressive.record_checkpoint",
        ):
            outcome = execute_progressively(
                run_id="RUN-1",
                repo="C:/fake/repo",
                manager_id="MANAGER-1",
                original_task="do the thing",
                planned_worker={
                    "model": "gpt-5.6-luna",
                    "scope": "fix things",
                },
                plan_risk="MEDIUM",
            )

        self.assertEqual(outcome["status"], "COMPLETED")
        self.assertEqual(len(outcome["history"]), 1)

        entry = outcome["history"][0]

        self.assertEqual(entry["reviewer_model"], "cc/claude-sonnet-5-low")
        self.assertEqual(entry["reviewer_provider"], "claude")
        self.assertEqual(entry["reviewer_effort"], "low")
        self.assertEqual(entry["reviewer_execution_path"], "OMNIROUTE")
        self.assertEqual(
            entry["reviewer_routing_reason"],
            "adaptive routing selected approved route",
        )
        self.assertEqual(entry["reviewer_degraded"], False)


if __name__ == "__main__":
    unittest.main()
