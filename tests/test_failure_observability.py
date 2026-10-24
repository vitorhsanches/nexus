"""
tests/test_failure_observability.py

End-to-end observability for Run failures. These tests cover what is
persisted at each failure boundary and what the operational API exposes. They
do not change execution semantics: each failing Run still fails the same way.

Contract under test (nexus/failure_contract.py):
  - a failure carries stage, code, failure_class, category, a bounded redacted
    single-line message, agent identity, worker ordinal, worktree and a fixed
    recovery hint, and every unknown field stays None;
  - the specific cause is recorded with the Run terminal record, while the
    legacy ``reason`` keeps its value;
  - secrets never reach the database or the API;
  - a Run recorded before this contract is derived from its checkpoints and
    labelled ``source: "derived"``, with nothing invented;
  - a completed Run has no failure.
"""

import json
import unittest
from unittest.mock import patch

from nexus import failure_contract as fc
from nexus.failure_contract import build_failure, sanitize_error_text
from nexus.dispatchers.omniroute import run_omniroute_worker
from nexus.orchestration import go
from nexus.orchestration.go import GoError
from nexus.orchestration.integration import (
    IntegrationError,
    _worker_outcome_to_integration_error,
)
from nexus.registry import agents as agents_registry
from nexus.registry.checkpoints import list_checkpoints, record_checkpoint
from nexus.registry.runs import create_run, list_runs
from nexus.web.operational import get_operational_run_detail
from tests.test_omniroute_exit_code_propagation import _DbMixin
from tests.test_go_concurrent_phase2 import VALID_SINGLE_WORKER_PLAN, _RealDatabaseTestCase

REPO = "C:/fake/worker worktree"
SECRET_TOKENS = ("sk-live-SECRET123", "SECRETVALUE", "eyJhbGciOiJIUzI1NiJ9")


def _new_run(status="RUNNING"):
    return create_run(
        project_id="norte",
        input_text="Norte: do a thing",
        intent="GO",
        status=status,
    )


def _terminal_payload(run_id):
    terminal = [cp for cp in list_checkpoints(run_id) if cp["boundary"] == "RUN_TERMINAL"]
    return terminal[-1]["payload"] if terminal else None


def _serialized_run(run_id):
    """Everything the API would return for this Run, as one string."""
    return json.dumps(get_operational_run_detail(run_id), default=str)


class SanitizeTests(unittest.TestCase):
    def test_bearer_key_value_and_json_secrets_are_redacted(self):
        text = (
            "Authorization: Bearer sk-live-SECRET123 "
            'api_key=SECRETVALUE {"access_token": "eyJhbGciOiJIUzI1NiJ9"}'
        )
        clean = sanitize_error_text(text)
        for token in SECRET_TOKENS:
            self.assertNotIn(token, clean)
        self.assertIn("<redacted>", clean)

    def test_output_is_single_line_and_bounded(self):
        clean = sanitize_error_text("line one\n\nline two " + "x" * 1000, limit=50)
        self.assertNotIn("\n", clean)
        self.assertLessEqual(len(clean), 53)
        self.assertTrue(clean.endswith("..."))

    def test_empty_input_is_none(self):
        self.assertIsNone(sanitize_error_text(None))
        self.assertIsNone(sanitize_error_text("   \n  "))


class FailureContractTests(unittest.TestCase):
    def test_unknown_fields_stay_none(self):
        failure = build_failure(stage="PLANNING", code="MANAGER_BLOCKED")
        self.assertIsNone(failure["failure_class"])
        self.assertIsNone(failure["category"])
        self.assertIsNone(failure["agent_id"])
        self.assertIsNone(failure["model"])
        self.assertIsNone(failure["recoverable"])
        self.assertIsNone(failure["recovery_hint"])

    def test_category_and_hint_are_deterministic(self):
        env = build_failure(stage="EXECUTION", code="WORKER_LAUNCH_FAILED", failure_class="TOOL_FAILURE")
        self.assertEqual(env["category"], "environmental")
        self.assertTrue(env["recoverable"])
        self.assertEqual(env["recovery_hint"], fc.RECOVERY_HINTS["environmental"])

        capability = build_failure(stage="REVIEW", code="REVIEW_BLOCKED", failure_class="CAPABILITY_FAILURE")
        self.assertEqual(capability["category"], "capability")

        task = build_failure(stage="REVIEW", code="REVIEW_BLOCKED", failure_class="VALIDATION_FAILURE")
        self.assertEqual(task["category"], "task")
        self.assertNotEqual(task["category"], env["category"])


class _ContractDbBase(_DbMixin):
    def _worker_outcome(self, worker_result, ordinal=1):
        return {"reason": "WORKER_EXECUTION_FAILED", "worker": worker_result}

    def _raise(self, run_id, outcome, ordinal=1):
        with self.assertRaises(GoError):
            go._raise_worker_failure(run_id, ordinal, outcome)


class WorkerLaunchFailureTests(_ContractDbBase):
    """CASE B: a Worker that never starts. Persisted, exposed, not generic."""

    def test_launch_failure_reaches_terminal_record_and_api(self):
        run_id = _new_run()
        with patch(
            "nexus.dispatchers.omniroute.codex_subprocess_env",
            side_effect=RuntimeError("synthetic environment failure"),
        ), patch("nexus.dispatchers.omniroute.subprocess.Popen"):
            worker = run_omniroute_worker(
                run_id=run_id, repo=REPO, task="do work",
                model="cc/claude-sonnet-5-low", effort="low",
            )
        self._raise(run_id, self._worker_outcome(worker))

        payload = _terminal_payload(run_id)
        self.assertEqual(payload["reason"], "WORKER_EXECUTION_FAILED")
        failure = payload["failure"]
        self.assertEqual(failure["code"], "WORKER_LAUNCH_FAILED")
        self.assertEqual(failure["stage"], "EXECUTION")
        self.assertEqual(failure["failure_class"], "TOOL_FAILURE")
        self.assertEqual(failure["category"], "environmental")
        self.assertEqual(failure["agent_id"], worker["agent_id"])
        self.assertEqual(failure["agent_role"], "Worker")
        self.assertEqual(failure["worktree"], REPO)
        self.assertEqual(failure["worker_ordinal"], 1)
        self.assertIn("RuntimeError: synthetic environment failure", failure["message"])
        self.assertEqual(failure["recovery_hint"], fc.RECOVERY_HINTS["environmental"])

        detail = get_operational_run_detail(run_id)
        self.assertEqual(detail["failure"]["code"], "WORKER_LAUNCH_FAILED")
        self.assertEqual(detail["failure"]["source"], "recorded")
        self.assertEqual(detail["failure"]["model"], "cc/claude-sonnet-5-low")
        # No route reached this outcome, so no provider is invented; the
        # transport (omniroute) is never reported as the provider.
        self.assertIsNone(detail["failure"]["provider"])


class RouteIdentityTests(_ContractDbBase):
    """The route the Worker was given is the provider reported, not the transport."""

    def test_route_provider_is_reported_and_transport_is_not(self):
        run_id = _new_run()
        agent_id = agents_registry.create_agent(
            run_id=run_id, role="Worker", provider="omniroute",
            model="cc/claude-sonnet-5-low", effort="low", status="FAILED",
        )
        worker = {
            "agent_id": agent_id,
            "status": "FAILED",
            "error": "exit 1",
            "model": "cc/claude-sonnet-5-low",
            "provider": "claude",
        }
        self._raise(run_id, self._worker_outcome(worker))

        failure = _terminal_payload(run_id)["failure"]
        self.assertEqual(failure["provider"], "claude")
        self.assertNotEqual(failure["provider"], "omniroute")


class WorkerExecutionFailureTests(_ContractDbBase):
    """CASE C: the Worker ran and failed. The generic reason gains the transcript root."""

    def test_execution_failure_keeps_specific_root_error(self):
        run_id = _new_run()
        agent_id = agents_registry.create_agent(
            run_id=run_id, role="Worker", provider="omniroute",
            model="cc/claude-sonnet-5-low", effort="low", status="FAILED",
        )
        worker = {
            "agent_id": agent_id,
            "status": "FAILED",
            "worktree": REPO,
            "error": "codex: sandbox denied write to .git/index\nexit 1",
        }
        self._raise(run_id, self._worker_outcome(worker))

        failure = _terminal_payload(run_id)["failure"]
        self.assertEqual(failure["code"], "WORKER_EXECUTION_FAILED")
        self.assertIn("sandbox denied write to .git/index", failure["message"])
        self.assertNotIn("\n", failure["message"])
        self.assertEqual(failure["agent_id"], agent_id)
        self.assertEqual(failure["model"], "cc/claude-sonnet-5-low")
        self.assertIsNone(failure["failure_class"])
        self.assertIsNone(failure["category"])


class ReviewFailureTests(_ContractDbBase):
    """CASE D and E: environmental review failure vs. a real review verdict."""

    def _review_outcome(self, failure_class, verdict="BLOCKED"):
        return {
            "reason": "BLOCKED",
            "verdict": "BLOCKED",
            "history": [{
                "reviewer_id": "AGENT-REVIEW1",
                "reviewer_model": "cc/claude-sonnet-5-low",
                "reviewer_provider": "claude",
            }],
            "review": {
                "verdict": verdict,
                "failure_class": failure_class,
                "summary": "git could not read the worktree",
                "evidence": ["git status: dubious ownership"],
            },
        }

    def test_case_d_tool_failure_is_preserved_and_exposed(self):
        run_id = _new_run()
        self._raise(run_id, self._review_outcome("TOOL_FAILURE"))

        payload = _terminal_payload(run_id)
        self.assertEqual(payload["reason"], "BLOCKED")
        failure = payload["failure"]
        self.assertEqual(failure["code"], "REVIEW_BLOCKED")
        self.assertEqual(failure["stage"], "REVIEW")
        self.assertEqual(failure["failure_class"], "TOOL_FAILURE")
        self.assertEqual(failure["category"], "environmental")
        self.assertEqual(failure["agent_id"], "AGENT-REVIEW1")
        self.assertEqual(failure["agent_role"], "ManagerReview")
        self.assertIn("dubious ownership", failure["message"])

    def test_case_e_real_review_verdict_is_distinguishable(self):
        run_id = _new_run()
        self._raise(run_id, self._review_outcome("VALIDATION_FAILURE"))

        failure = _terminal_payload(run_id)["failure"]
        self.assertEqual(failure["failure_class"], "VALIDATION_FAILURE")
        self.assertEqual(failure["category"], "task")
        self.assertNotEqual(failure["category"], "environmental")
        self.assertEqual(failure["recovery_hint"], fc.RECOVERY_HINTS["task"])


class SensitiveDataTests(_ContractDbBase):
    """CASE F: credentials in an exception never survive persistence or the API."""

    def test_secrets_do_not_reach_checkpoint_or_api(self):
        run_id = _new_run()
        with patch(
            "nexus.dispatchers.omniroute.codex_subprocess_env",
            side_effect=RuntimeError(
                "Authorization: Bearer sk-live-SECRET123 api_key=SECRETVALUE "
                'token {"access_token": "eyJhbGciOiJIUzI1NiJ9"}'
            ),
        ), patch("nexus.dispatchers.omniroute.subprocess.Popen"):
            worker = run_omniroute_worker(
                run_id=run_id, repo=REPO, task="do work",
                model="cc/claude-sonnet-5-low", effort="low",
            )
        self._raise(run_id, self._worker_outcome(worker))

        persisted = json.dumps(list_checkpoints(run_id), default=str)
        exposed = _serialized_run(run_id)
        for token in SECRET_TOKENS:
            self.assertNotIn(token, persisted)
            self.assertNotIn(token, exposed)
        self.assertIn("<redacted>", exposed)


class HistoricalRunTests(_ContractDbBase):
    """CASE G: a Run recorded before the contract. Derived, never invented."""

    def test_legacy_worker_failure_is_derived_and_labelled(self):
        run_id = _new_run(status="FAILED")
        record_checkpoint(
            run_id=run_id,
            boundary="WORKER_ATTEMPT",
            payload={"status": "FAILED", "worker_id": "AGENT-OLD1", "model": "cc/claude-sonnet-5-low"},
            worker_ordinal=1,
        )
        record_checkpoint(
            run_id=run_id,
            boundary="RUN_TERMINAL",
            payload={"status": "FAILED", "reason": "WORKER_EXECUTION_FAILED", "worker_ordinal": 1},
            worker_ordinal=1,
        )

        failure = get_operational_run_detail(run_id)["failure"]
        self.assertEqual(failure["source"], "derived")
        self.assertEqual(failure["code"], "WORKER_EXECUTION_FAILED")
        self.assertEqual(failure["agent_id"], "AGENT-OLD1")
        self.assertEqual(failure["model"], "cc/claude-sonnet-5-low")
        # Nothing the checkpoints do not hold is invented.
        self.assertIsNone(failure["message"])
        self.assertIsNone(failure["provider"])
        self.assertIsNone(failure["worktree"])
        self.assertIsNone(failure["failure_class"])

    def test_legacy_launch_failed_attempt_is_derived_as_launch_failure(self):
        # Before launch diagnostics, a Worker that never started was recorded
        # only as status LAUNCH_FAILED. That is the same launch failure.
        run_id = _new_run(status="FAILED")
        record_checkpoint(
            run_id=run_id,
            boundary="WORKER_ATTEMPT",
            payload={"status": "LAUNCH_FAILED", "worker_id": "AGENT-OLD2", "model": "cc/claude-sonnet-5-low", "provider": "claude"},
            worker_ordinal=1,
        )
        record_checkpoint(
            run_id=run_id,
            boundary="RUN_TERMINAL",
            payload={"status": "FAILED", "reason": "WORKER_EXECUTION_FAILED", "worker_ordinal": 1},
            worker_ordinal=1,
        )
        failure = get_operational_run_detail(run_id)["failure"]
        self.assertEqual(failure["source"], "derived")
        self.assertEqual(failure["code"], "WORKER_LAUNCH_FAILED")
        self.assertEqual(failure["failure_class"], "TOOL_FAILURE")
        self.assertEqual(failure["provider"], "claude")
        # Before worktree persistence the location was not recorded.
        self.assertIsNone(failure["worktree"])

    def test_legacy_review_block_gets_the_specific_review_code(self):
        run_id = _new_run(status="BLOCKED")
        record_checkpoint(
            run_id=run_id,
            boundary="REVIEW",
            payload={
                "status": "COMPLETED", "verdict": "BLOCKED", "failure_class": "TOOL_FAILURE",
                "summary": "git refused the worktree", "reviewer_id": "AGENT-R9",
                "reviewer_model": "cc/claude-sonnet-5-low", "reviewer_provider": "claude",
            },
            worker_ordinal=4,
        )
        record_checkpoint(
            run_id=run_id,
            boundary="RUN_TERMINAL",
            payload={"status": "BLOCKED", "reason": "BLOCKED"},
            worker_ordinal=4,
        )
        failure = get_operational_run_detail(run_id)["failure"]
        self.assertEqual(failure["code"], "REVIEW_BLOCKED")
        self.assertEqual(failure["stage"], "REVIEW")
        self.assertEqual(failure["category"], "environmental")
        self.assertEqual(failure["message"], "git refused the worktree")
        self.assertEqual(failure["agent_id"], "AGENT-R9")

    def test_run_without_any_evidence_still_returns_its_reason_only(self):
        run_id = _new_run(status="BLOCKED")
        record_checkpoint(
            run_id=run_id,
            boundary="RUN_TERMINAL",
            payload={"status": "BLOCKED", "reason": "BLOCKED"},
        )
        failure = get_operational_run_detail(run_id)["failure"]
        self.assertEqual(failure["source"], "derived")
        self.assertEqual(failure["code"], "BLOCKED")
        self.assertIsNone(failure["agent_id"])
        self.assertIsNone(failure["stage"])


class ManagerFailureTests(_RealDatabaseTestCase):
    """CASE A: the Manager cannot be launched. The run gets a recorded cause."""

    def test_manager_launch_failure_is_recorded_and_exposed(self):
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-M1",
                "status": "LAUNCH_FAILED",
                "exit_code": None,
                "plan": None,
                "error": "Codex executable not found under C:/codex",
            },
        ):
            with self.assertRaises(GoError) as raised:
                go.run_go("Norte: fix a bug", project_query="norte")

        self.assertEqual(raised.exception.code, "MANAGER_BLOCKED")
        run_id = raised.exception.run_id

        payload = _terminal_payload(run_id)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertEqual(payload["reason"], "MANAGER_BLOCKED")
        failure = payload["failure"]
        self.assertEqual(failure["stage"], "PLANNING")
        self.assertEqual(failure["code"], "MANAGER_BLOCKED")
        self.assertEqual(failure["failure_class"], "TOOL_FAILURE")
        self.assertEqual(failure["category"], "environmental")
        self.assertEqual(failure["agent_id"], "AGT-M1")
        self.assertEqual(failure["agent_role"], "Manager")
        self.assertEqual(failure["provider"], "omniroute")
        self.assertIn("Codex executable not found", failure["message"])

        detail = get_operational_run_detail(run_id)
        self.assertEqual(detail["failure"]["code"], "MANAGER_BLOCKED")
        self.assertEqual(detail["failure"]["source"], "recorded")

    def test_manager_nonzero_exit_preserves_diagnostic_and_tool_failure_class(self):
        """RUN-4EFBBB22 regression: the Manager subprocess ran and exited
        non-zero (a real-world cause was a Codex auth/token failure)
        without ever producing a plan. Before the fix, the RUN_TERMINAL
        failure showed only "exit_code=1" with failure_class/category
        both null -- the real diagnostic, already captured by the
        dispatcher, never reached the Control Center."""
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-M2",
                "status": "FAILED",
                "exit_code": 1,
                "plan": None,
                "error": "Failed to refresh <redacted>: refresh token already used",
            },
        ):
            with self.assertRaises(GoError) as raised:
                go.run_go("Norte: fix a bug", project_query="norte")

        self.assertEqual(raised.exception.code, "MANAGER_BLOCKED")
        run_id = raised.exception.run_id

        payload = _terminal_payload(run_id)
        failure = payload["failure"]
        self.assertEqual(failure["stage"], "PLANNING")
        self.assertEqual(failure["failure_class"], "TOOL_FAILURE")
        self.assertEqual(failure["category"], "environmental")
        self.assertIn("exit_code=1", failure["message"])
        self.assertIn("refresh token already used", failure["message"])

        detail = get_operational_run_detail(run_id)
        self.assertEqual(detail["failure"]["failure_class"], "TOOL_FAILURE")
        self.assertEqual(detail["failure"]["category"], "environmental")

    def test_manager_nonzero_exit_without_error_field_still_reports_exit_code(self):
        """Defensive: a caller/mock that never sets "error" at all (older
        shape) must not crash go.py's fallback -- it still gets exit_code
        in the message and the correct TOOL_FAILURE classification."""
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-M3",
                "status": "FAILED",
                "exit_code": 1,
                "plan": None,
            },
        ):
            with self.assertRaises(GoError) as raised:
                go.run_go("Norte: fix a bug", project_query="norte")

        self.assertEqual(raised.exception.code, "MANAGER_BLOCKED")
        payload = _terminal_payload(raised.exception.run_id)
        failure = payload["failure"]
        self.assertEqual(failure["failure_class"], "TOOL_FAILURE")
        self.assertEqual(failure["message"], "exit_code=1")


class FinalizationFailureTests(_RealDatabaseTestCase):
    """Finalization: the cause is recorded and the original error still propagates."""

    def test_finalization_failure_is_recorded_and_reraised(self):
        before = {row["id"] for row in list_runs()}
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-F1",
                "status": "COMPLETED",
                "exit_code": 0,
                "plan": VALID_SINGLE_WORKER_PLAN,
            },
        ), patch(
            "nexus.orchestration.go.execute_progressively",
            return_value={
                "status": "COMPLETED",
                "verdict": "PASS",
                "history": [],
                "worker": {"status": "COMPLETED"},
                "review": {"verdict": "PASS"},
            },
        ), patch(
            "nexus.orchestration.go.run_integrated_plan",
            return_value=[{
                "status": "COMPLETED",
                "worker": {"status": "COMPLETED"},
                "review": {"verdict": "PASS"},
                "_planned_scope": "Fix the thing.",
            }],
        ), patch(
            "nexus.orchestration.go.update_run_result",
            side_effect=RuntimeError("synthetic finalization failure"),
        ):
            with self.assertRaises(RuntimeError) as raised:
                go.run_go("Norte: fix a bug", project_query="norte")

        self.assertEqual(str(raised.exception), "synthetic finalization failure")
        created = [row["id"] for row in list_runs() if row["id"] not in before]
        self.assertEqual(len(created), 1)
        run_id = created[0]
        payload = _terminal_payload(run_id)
        self.assertEqual(payload["status"], "FAILED")
        self.assertEqual(payload["reason"], "FINALIZATION_FAILED")
        self.assertEqual(payload["failure"]["stage"], "FINALIZATION")
        self.assertIn("synthetic finalization failure", payload["failure"]["message"])


class ReviewBlockedIntegrationPathTests(unittest.TestCase):
    """RUN-E6B89A65 regression: the REVIEW_BLOCKED/TOOL_FAILURE propagation
    defect in the LIVE Phase 4.4 integration pipeline.

    ``ReviewFailureTests`` above covers the same failure_class/category
    contract, but only through the legacy ``go._raise_worker_failure()``
    path (Phase 1-3), which ``run_go()`` no longer calls. The real path is
    ``run_integrated_plan()`` -> ``IntegrationError`` -> go.py's
    ``except IntegrationError`` handler (nexus/orchestration/integration.py
    + nexus/orchestration/go.py). Before the fix, that handler discarded the
    Reviewer's already-computed failure_class/category/agent identity and
    rebuilt a generic one with everything nulled and the stage mislabeled
    "INTEGRATION" instead of "REVIEW" -- exactly what RUN-E6B89A65's
    RUN_TERMINAL checkpoint showed, while its REVIEW checkpoint (moments
    earlier, same Run) had the full structured failure.
    """

    def _blocked_outcome(self):
        """Mirrors execute_progressively()'s real return shape for a
        BLOCKED verdict (nexus/orchestration/progressive.py, verdict ==
        "BLOCKED" branch) -- the exact shape RUN-E6B89A65 produced."""
        return {
            "status": "BLOCKED",
            "verdict": "BLOCKED",
            "history": [{
                "attempt": 1,
                "worker_id": "AGENT-B4769434",
                "model": "cc/claude-sonnet-5-high",
                "worktree": r"C:\fake\nexus_intg_root\worker-0",
                "reviewer_id": "AGENT-EF26EB6B",
                "reviewer_model": "cc/claude-sonnet-5-low",
                "reviewer_provider": "claude",
            }],
            "review": {
                "verdict": "BLOCKED",
                "failure_class": "TOOL_FAILURE",
                "summary": "Mandatory validation could not be executed in this sandbox.",
                "evidence": ["node_modules exists but vitest is not installed"],
            },
            "worker_route": None,
        }

    def test_integration_error_carries_the_real_review_failure(self):
        """integration.py: the IntegrationError for a BLOCKED verdict now
        carries the Reviewer's full structured failure in its payload."""
        error = _worker_outcome_to_integration_error(self._blocked_outcome(), worker_ordinal=1)

        self.assertEqual(error.code, "REVIEW_BLOCKED")
        failure = error.payload.get("failure")
        self.assertIsNotNone(failure, "IntegrationError must carry a precomputed failure")
        self.assertEqual(failure["stage"], "REVIEW")
        self.assertEqual(failure["failure_class"], "TOOL_FAILURE")
        self.assertEqual(failure["category"], "environmental")
        self.assertEqual(failure["agent_id"], "AGENT-EF26EB6B")
        self.assertEqual(failure["agent_role"], "ManagerReview")
        self.assertEqual(failure["model"], "cc/claude-sonnet-5-low")
        self.assertEqual(failure["provider"], "claude")
        self.assertEqual(failure["worktree"], r"C:\fake\nexus_intg_root\worker-0")

    def test_review_failed_reason_uses_dispatcher_identity_not_history(self):
        """The REVIEW_FAILED (dispatcher-failed, no history yet) branch
        still resolves reviewer identity from the review dict itself."""
        outcome = {
            "status": "BLOCKED",
            "reason": "REVIEW_FAILED",
            "history": [],
            "review": {
                "reviewer_id": "AGENT-DISPATCH1",
                "error": "Reviewer process could not start",
                "routing": {"model": "cc/claude-sonnet-5-low", "provider": "claude"},
            },
        }
        error = _worker_outcome_to_integration_error(outcome, worker_ordinal=1)

        failure = error.payload.get("failure")
        self.assertIsNotNone(failure)
        self.assertEqual(failure["code"], "REVIEW_FAILED")
        self.assertEqual(failure["agent_id"], "AGENT-DISPATCH1")
        self.assertEqual(failure["model"], "cc/claude-sonnet-5-low")
        self.assertEqual(failure["provider"], "claude")

    def test_go_run_terminal_checkpoint_matches_review_checkpoint(self):
        """End-to-end through run_go(): the RUN_TERMINAL failure for a
        REVIEW_BLOCKED/TOOL_FAILURE outcome must carry the SAME
        failure_class/category/agent identity the REVIEW checkpoint would
        have recorded -- not a degraded, nulled-out generic one."""
        import nexus.registry.database as database
        import nexus.registry.projects as projects
        import json as _json
        import tempfile
        from pathlib import Path

        tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp_dir.cleanup)
        tmp_path = Path(tmp_dir.name)

        orig_db, orig_storage, orig_projects = (
            database.DATABASE_PATH, database.STORAGE_DIR, projects.PROJECTS_FILE,
        )

        def _restore():
            database.DATABASE_PATH = orig_db
            database.STORAGE_DIR = orig_storage
            projects.PROJECTS_FILE = orig_projects

        self.addCleanup(_restore)

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus-test.db"
        fixture_path = tmp_path / "projects.json"
        fixture_path.write_text(_json.dumps({
            "projects": [{
                "id": "norte", "name": "Norte", "path": r"C:\fake\norte",
                "aliases": ["norte"], "enabled": True,
            }]
        }), encoding="utf-8")
        projects.PROJECTS_FILE = fixture_path
        database.initialize_database()
        projects.sync_projects()

        blocked_outcome = self._blocked_outcome()
        integration_error = _worker_outcome_to_integration_error(blocked_outcome, worker_ordinal=1)

        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-REV1",
                "status": "COMPLETED",
                "exit_code": 0,
                "plan": VALID_SINGLE_WORKER_PLAN,
            },
        ), patch(
            "nexus.orchestration.go.run_integrated_plan",
            side_effect=integration_error,
        ):
            with self.assertRaises(GoError) as raised:
                go.run_go("Norte: fix a bug", project_query="norte")

        self.assertEqual(raised.exception.code, "REVIEW_BLOCKED")
        run_id = raised.exception.run_id
        payload = _terminal_payload(run_id)
        self.assertEqual(payload["status"], "BLOCKED")
        failure = payload["failure"]

        # This is the exact assertion that would have failed before the
        # fix: stage was "INTEGRATION" and failure_class/category/agent_id
        # were all None, even though the Reviewer had already produced a
        # fully-classified TOOL_FAILURE/environmental verdict.
        self.assertEqual(failure["stage"], "REVIEW")
        self.assertEqual(failure["failure_class"], "TOOL_FAILURE")
        self.assertEqual(failure["category"], "environmental")
        self.assertEqual(failure["agent_id"], "AGENT-EF26EB6B")
        self.assertEqual(failure["agent_role"], "ManagerReview")

        detail = get_operational_run_detail(run_id)
        self.assertEqual(detail["failure"]["failure_class"], "TOOL_FAILURE")
        self.assertEqual(detail["failure"]["category"], "environmental")

    def test_other_integration_codes_unaffected(self):
        """A code with no precomputed failure (e.g. a plain Git tool
        failure) still falls back to the original generic build_failure()
        -- this fix must not change behavior for any other error code."""
        error = IntegrationError(
            "GIT_TOOL_FAILURE",
            run_status="BLOCKED",
            worker_ordinal=1,
        )
        self.assertNotIn("failure", error.payload)


class CompletedRunTests(_ContractDbBase):
    """CASE H: a successful Run acquires no failure payload."""

    def test_completed_run_has_no_failure(self):
        run_id = _new_run(status="COMPLETED")
        record_checkpoint(
            run_id=run_id,
            boundary="RUN_TERMINAL",
            payload={"status": "COMPLETED", "worker_count": 1},
        )
        detail = get_operational_run_detail(run_id)
        self.assertIsNone(detail["failure"])


if __name__ == "__main__":
    unittest.main()
