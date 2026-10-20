"""Phase 4.1 real-path fix: REVIEW_BLOCKED detail must survive

    execute_progressively (real outcome shape)
    -> integration._worker_outcome_to_integration_error (real)
    -> run_integrated_plan (real)
    -> run_go's `except IntegrationError` clause (real)
    -> GoError.detail
    -> nexus.failure_classification.classify_go_error / describe_go_error

Only ``execute_progressively`` and ``run_manager`` are mocked (the two
model-calling boundaries); every other function on this path is real,
including a real git repository, matching the production E2E that
exposed the bug (PROGRAM-24CA481D, Phase 1, attempt 4: verdict=BLOCKED,
failure_class=CAPABILITY_FAILURE misclassified as UNKNOWN_FAILURE).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus import failure_classification as fc
from nexus.orchestration.go import GoError, run_go
from nexus.orchestration.integration import _worker_outcome_to_integration_error
from nexus.registry.runs import get_run


FIXTURE_PROJECTS = {
    "projects": [
        {
            "id": "norte",
            "name": "Norte",
            "path": "__REPO__",
            "aliases": ["norte"],
            "enabled": True,
        },
    ]
}

VALID_PLAN = {
    "complexity": "LOW",
    "risk": "LOW",
    "parallelism": 1,
    "summary": "Do a small fix.",
    "workers": [
        {
            "route_class": "mechanical",
            "execution_path": "OMNIROUTE",
            "provider": "omniroute",
            "model": "oc/big-pickle",
            "effort": "low",
            "scope": "Fix the thing.",
            "reason": "Simple mechanical change.",
            "authority": "WORKSPACE_WRITE",
        }
    ],
}


def _review_blocked_outcome(failure_class: str, summary: str, evidence: list[str]) -> dict:
    """Shape exactly matches progressive.py's real BLOCKED return (the
    ``verdict == "BLOCKED"`` branch): status/verdict/history/review."""
    return {
        "status": "BLOCKED",
        "verdict": "BLOCKED",
        "history": [],
        "review": {
            "verdict": "BLOCKED",
            "failure_class": failure_class,
            "summary": summary,
            "evidence": evidence,
        },
    }


def _git(repo, *args):
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"},
    )


def _make_repo(tmp_dir: str) -> str:
    repo = os.path.join(tmp_dir, "primary")
    os.makedirs(repo, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "Test")
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--no-verify", "-m", "init")
    return repo


class RealPathReviewBlockedPropagationTestCase(unittest.TestCase):
    """Drives the REAL run_go -> run_integrated_plan -> IntegrationError ->
    GoError path with a real git repo; only the two model-calling
    boundaries (run_manager, execute_progressively) are mocked."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp.name)
        self._repo = _make_repo(self._tmp.name)

        self._orig_db_path = database.DATABASE_PATH
        self._orig_storage_dir = database.STORAGE_DIR
        self._orig_projects_file = projects.PROJECTS_FILE

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus-test.db"

        fixture_path = tmp_path / "projects.json"
        payload = json.loads(json.dumps(FIXTURE_PROJECTS))
        payload["projects"][0]["path"] = self._repo
        fixture_path.write_text(json.dumps(payload), encoding="utf-8")
        projects.PROJECTS_FILE = fixture_path

        database.initialize_database()
        projects.sync_projects()

    def tearDown(self) -> None:
        database.DATABASE_PATH = self._orig_db_path
        database.STORAGE_DIR = self._orig_storage_dir
        projects.PROJECTS_FILE = self._orig_projects_file
        self._tmp.cleanup()

    def _run_with_review_outcome(self, outcome: dict) -> GoError:
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-0001",
                "status": "COMPLETED",
                "exit_code": 0,
                "plan": VALID_PLAN,
            },
        ), patch(
            "nexus.orchestration.integration.execute_progressively",
            return_value=outcome,
        ), patch("nexus.orchestration.integration.record_checkpoint"):
            with self.assertRaises(GoError) as ctx:
                run_go("Norte: fix a bug", project_query="norte")
        return ctx.exception

    # -- CASE A: REQUIREMENT_FAILURE ----------------------------------------

    def test_case_a_requirement_failure_detail_survives_to_go_error(self):
        outcome = _review_blocked_outcome(
            "REQUIREMENT_FAILURE",
            "The /inventory endpoint was never implemented.",
            ["grep found no route for /inventory"],
        )
        error = self._run_with_review_outcome(outcome)

        self.assertEqual(error.code, "REVIEW_BLOCKED")
        self.assertIsNotNone(error.detail)
        self.assertEqual(error.detail["review"]["failure_class"], "REQUIREMENT_FAILURE")
        self.assertEqual(
            error.detail["review"]["summary"],
            "The /inventory endpoint was never implemented.",
        )

        classified = fc.classify_go_error(error)
        self.assertEqual(classified, fc.REQUIREMENT_UNSATISFIED)
        self.assertNotEqual(classified, fc.UNKNOWN_FAILURE)

        description = fc.describe_go_error(error)
        self.assertIn("REQUIREMENT_FAILURE", description)
        self.assertIn("/inventory endpoint", description)

        run = get_run(error.run_id)
        self.assertEqual(run["status"], "BLOCKED")

    # -- CASE B: CAPABILITY_FAILURE ------------------------------------------

    def test_case_b_capability_failure_classified_as_capability_insufficient(self):
        outcome = _review_blocked_outcome(
            "CAPABILITY_FAILURE",
            "The current model cannot resolve the async migration correctly.",
            ["three attempts produced inconsistent schemas"],
        )
        error = self._run_with_review_outcome(outcome)

        classified = fc.classify_go_error(error)
        self.assertEqual(classified, fc.CAPABILITY_INSUFFICIENT)
        self.assertNotEqual(classified, fc.UNKNOWN_FAILURE)
        self.assertIn("CAPABILITY_FAILURE", fc.describe_go_error(error))

    # -- CASE C: TOOL_FAILURE -------------------------------------------------

    def test_case_c_tool_failure_is_environment_not_requirement(self):
        outcome = _review_blocked_outcome(
            "TOOL_FAILURE",
            "The test runner itself crashed before producing output.",
            ["pytest exited with a segfault, no test results"],
        )
        error = self._run_with_review_outcome(outcome)

        classified = fc.classify_go_error(error)
        self.assertEqual(classified, fc.ENVIRONMENT_FAILURE)
        self.assertNotEqual(classified, fc.REQUIREMENT_UNSATISFIED)
        self.assertNotEqual(classified, fc.UNKNOWN_FAILURE)

    # -- CASE D: final blocker preserves the real class, not UNKNOWN --------

    def test_case_d_final_blocker_never_reports_unknown_for_a_known_review_failure(self):
        for failure_class, expected in (
            ("REQUIREMENT_FAILURE", fc.REQUIREMENT_UNSATISFIED),
            ("CAPABILITY_FAILURE", fc.CAPABILITY_INSUFFICIENT),
            ("TOOL_FAILURE", fc.ENVIRONMENT_FAILURE),
        ):
            with self.subTest(failure_class=failure_class):
                outcome = _review_blocked_outcome(failure_class, "summary text", [])
                error = self._run_with_review_outcome(outcome)
                self.assertEqual(fc.classify_go_error(error), expected)


class WorkerExecutionFailedDetailTestCase(unittest.TestCase):
    """WORKER_EXECUTION_FAILED must also carry its worker sub-dict through
    IntegrationError, not just REVIEW_BLOCKED."""

    def test_worker_execution_failed_preserves_worker_reason(self):
        outcome = {
            "status": "FAILED",
            "reason": "WORKER_EXECUTION_FAILED",
            "history": [],
            "worker": {"status": "FAILED", "reason": "WORKER_TIMEOUT"},
        }
        error = _worker_outcome_to_integration_error(outcome, 1)

        self.assertEqual(error.payload["worker"]["reason"], "WORKER_TIMEOUT")

        go_error = GoError(error.code, str(error), run_id="RUN-X", detail=error.payload)
        self.assertEqual(fc.classify_go_error(go_error), fc.TRANSIENT_PROVIDER)


if __name__ == "__main__":
    unittest.main()
