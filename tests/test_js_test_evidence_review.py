"""Tests for the opt-in `run_js_tests` wiring (NEXUS_GO_JS_DEPENDENCY_
READINESS, Phase 5): separating test execution from Review.

Covers:
  - review_worker() accepts an optional `test_evidence` string and injects
    it into the prompt as a clearly labeled, independent fact -- distinct
    from, and never substituting for, the Reviewer's own verdict.
  - execute_progressively() only runs the project's test command (via
    nexus.orchestration.js_dependency_prep.run_baseline_tests) when
    `run_js_tests=True`; default False touches nothing new.
  - When it does run, the result is persisted as its own JS_TEST_EVIDENCE
    checkpoint (TEST_STATUS as an established fact) and passed to
    review_worker() as `test_evidence`, independently of REVIEW_VERDICT.

Pattern matches tests/test_phase4_3_contract_propagation.py (Groups 2-3):
pure unit tests, no subprocess, no DB, no filesystem I/O.
"""

from __future__ import annotations

import io
import unittest
from unittest.mock import MagicMock, patch


def _fake_routing_service(model="claude-sonnet-4-5", effort="medium"):
    from nexus.routing.service import RoutingDecision
    svc = MagicMock()
    svc.select_route_for_capability.return_value = RoutingDecision(
        model=model,
        provider="claude",
        effort=effort,
        execution_path="OMNIROUTE",
        reason="test",
        fallbacks=(),
        degraded=False,
    )
    return svc


def _call_review_worker(*, test_evidence=None, captured_prompt=None):
    fake_output = (
        'NEXUS_REVIEW_BEGIN\n'
        '{"verdict":"PASS","failure_class":null,"summary":"ok","evidence":["e"]}\n'
        'NEXUS_REVIEW_END\n'
    )
    fake_proc = MagicMock()
    fake_proc.stdout = io.StringIO(fake_output)
    fake_proc.wait.return_value = 0

    def fake_launcher(cmd):
        if captured_prompt is not None:
            captured_prompt.append(cmd[-1])
        return fake_proc

    with patch("nexus.dispatchers.review._find_codex", return_value="/fake/codex"), \
         patch("nexus.dispatchers.review.create_agent", return_value="rev-001"), \
         patch("nexus.dispatchers.review.update_agent_execution"), \
         patch("nexus.dispatchers.review.update_agent_status"):
        from nexus.dispatchers.review import review_worker
        return review_worker(
            run_id="run-test",
            worker_id="wkr-001",
            worktree="/fake/worktree",
            original_task="original task text",
            worker_scope="scope text",
            routing_service=_fake_routing_service(),
            process_launcher=fake_launcher,
            test_evidence=test_evidence,
        )


class ReviewWorkerTestEvidenceInjectionTests(unittest.TestCase):

    def test_absent_test_evidence_no_section_in_prompt(self):
        captured = []
        _call_review_worker(captured_prompt=captured)
        self.assertNotIn("TEST EXECUTION EVIDENCE", captured[0])

    def test_test_evidence_injected_and_labeled(self):
        captured = []
        _call_review_worker(
            test_evidence="TEST_STATUS: PASS\nCommand executed: npm test\nExit code: 0",
            captured_prompt=captured,
        )
        prompt = captured[0]
        self.assertIn("TEST EXECUTION EVIDENCE", prompt)
        self.assertIn("TEST_STATUS: PASS", prompt)

    def test_test_evidence_does_not_instruct_automatic_pass(self):
        """The prompt must make explicit that TEST_STATUS never implies or
        is implied by REVIEW_VERDICT -- the two facts stay independent."""
        captured = []
        _call_review_worker(
            test_evidence="TEST_STATUS: PASS\nCommand executed: npm test\nExit code: 0",
            captured_prompt=captured,
        )
        prompt = captured[0]
        self.assertIn("never by itself justifies a PASS verdict", prompt)

    def test_backward_compatible_without_test_evidence(self):
        result = _call_review_worker()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["review"]["verdict"], "PASS")


class ExecuteProgressivelyRunJsTestsTests(unittest.TestCase):

    def _run_progressive(self, *, run_js_tests, baseline_result=None):
        from nexus.routing.service import RoutingDecision

        decision = RoutingDecision(
            model="claude-sonnet-4-5", provider="claude", effort="medium",
            execution_path="OMNIROUTE", reason="test", fallbacks=(), degraded=False,
        )
        routing_service = MagicMock()
        routing_service.select_route_for_capability.return_value = decision

        worker_result = {
            "status": "COMPLETED",
            "agent_id": "wkr-prog-001",
            "worktree": "/fake/wt",
            "output": "semantic output",
        }
        review_result = {
            "status": "COMPLETED",
            "reviewer_id": "rev-prog-001",
            "review": {
                "verdict": "PASS", "failure_class": None,
                "summary": "all good", "evidence": ["evidence line"],
            },
            "exit_code": 0,
            "routing": {
                "model": "claude-sonnet-4-5", "provider": "claude",
                "effort": "medium", "execution_path": "OMNIROUTE",
                "reason": "test", "degraded": False,
            },
        }
        mock_review_worker = MagicMock(return_value=review_result)
        mock_run_baseline_tests = MagicMock(
            return_value=baseline_result or {
                "command": "npm test", "exit_code": 0, "timed_out": False,
                "passed": True, "output_tail": "ok",
            }
        )
        mock_record_checkpoint = MagicMock()

        worker = {
            "route_class": "standard-coding",
            "execution_path": "OMNIROUTE",
            "provider": "claude",
            "model": "claude-sonnet-4-5",
            "effort": "medium",
            "scope": "fix things",
            "authority": "WORKSPACE_WRITE",
        }

        with patch("nexus.orchestration.progressive.execute_worker", return_value=worker_result), \
             patch("nexus.orchestration.progressive.review_worker", mock_review_worker), \
             patch("nexus.orchestration.progressive.record_checkpoint", mock_record_checkpoint), \
             patch("nexus.orchestration.progressive.update_run_status"), \
             patch("nexus.orchestration.progressive.run_baseline_tests", mock_run_baseline_tests):
            from nexus.orchestration.progressive import execute_progressively
            execute_progressively(
                run_id="run-prog",
                repo="/fake/repo",
                manager_id="mgr-001",
                original_task="the original task",
                planned_worker=worker,
                plan_risk="medium",
                routing_service=routing_service,
                run_js_tests=run_js_tests,
            )

        return mock_review_worker, mock_run_baseline_tests, mock_record_checkpoint

    def test_default_false_never_runs_tests_or_passes_evidence(self):
        mock_rw, mock_baseline, mock_checkpoint = self._run_progressive(run_js_tests=False)
        mock_baseline.assert_not_called()
        _, kwargs = mock_rw.call_args
        self.assertIn("test_evidence", kwargs)
        self.assertIsNone(kwargs["test_evidence"])
        boundaries = [c.kwargs.get("boundary") for c in mock_checkpoint.call_args_list]
        self.assertNotIn("JS_TEST_EVIDENCE", boundaries)

    def test_enabled_runs_tests_against_worker_repo(self):
        mock_rw, mock_baseline, _ = self._run_progressive(run_js_tests=True)
        mock_baseline.assert_called_once_with("/fake/repo")

    def test_enabled_passes_formatted_evidence_to_review_worker(self):
        mock_rw, _, _ = self._run_progressive(run_js_tests=True)
        _, kwargs = mock_rw.call_args
        self.assertIn("TEST_STATUS: PASS", kwargs["test_evidence"])
        self.assertIn("npm test", kwargs["test_evidence"])

    def test_enabled_records_js_test_evidence_checkpoint_as_independent_fact(self):
        """TEST_STATUS is persisted on its own checkpoint boundary --
        distinct from the REVIEW checkpoint the Reviewer's verdict is
        recorded on -- so a passing test run is never conflated with, or
        mistaken for, a Reviewer PASS."""
        _, _, mock_checkpoint = self._run_progressive(run_js_tests=True)
        js_test_calls = [
            c for c in mock_checkpoint.call_args_list
            if c.kwargs.get("boundary") == "JS_TEST_EVIDENCE"
        ]
        self.assertEqual(len(js_test_calls), 1)
        payload = js_test_calls[0].kwargs["payload"]
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["command"], "npm test")

    def test_failing_test_run_reported_as_fail_not_blocking_review(self):
        """A failing project test is evidence for the Reviewer to weigh,
        not a hard stop -- run_js_tests must never itself raise or skip
        calling review_worker."""
        mock_rw, _, mock_checkpoint = self._run_progressive(
            run_js_tests=True,
            baseline_result={
                "command": "npm test", "exit_code": 1, "timed_out": False,
                "passed": False, "output_tail": "1 failing",
            },
        )
        _, kwargs = mock_rw.call_args
        self.assertIn("TEST_STATUS: FAIL", kwargs["test_evidence"])
        js_test_calls = [
            c for c in mock_checkpoint.call_args_list
            if c.kwargs.get("boundary") == "JS_TEST_EVIDENCE"
        ]
        self.assertFalse(js_test_calls[0].kwargs["payload"]["passed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
