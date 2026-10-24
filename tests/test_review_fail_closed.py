"""
tests/test_review_fail_closed.py

Review must never return PASS when a required Git inspection failed.

The Reviewer's verdict is model output. Nexus therefore verifies, itself and
deterministically, that Git can inspect the assigned worktree before the
Reviewer launches and again after a PASS. A failed check becomes BLOCKED with
TOOL_FAILURE, which the existing contract maps to an environment failure, never
to CAPABILITY_FAILURE.

The mocked cases exercise review_worker() with scripted Reviewer output. The
real-Git cases exercise the actual check against real repositories, reusing the
ownership hook from tests/test_git_scoped_trust.py.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from nexus import failure_classification as fc
from nexus.dispatchers import review as review_module
from nexus.dispatchers.review import _verify_review_prerequisites, review_worker

GIT = shutil.which("git")
OWNERSHIP_HOOK = {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
WORKTREE = "C:/fake/worktree"
GIT_FAILURE = "git -C C:/fake/worktree status --porcelain failed with exit 128"


class _FakeProcess:
    def __init__(self, output, exit_code=0):
        self.stdout = iter(output.splitlines(keepends=True))
        self._exit_code = exit_code

    def wait(self):
        return self._exit_code


def _envelope(verdict, failure_class=None, summary="reviewed"):
    body = {
        "verdict": verdict,
        "failure_class": failure_class,
        "summary": summary,
        "evidence": ["checked the worktree"],
    }
    return "NEXUS_REVIEW_BEGIN\n" + json.dumps(body) + "\nNEXUS_REVIEW_END\n"


class _GoError(Exception):
    def __init__(self, code, detail):
        super().__init__(code)
        self.code = code
        self.detail = detail


class ReviewFailClosedTests(unittest.TestCase):
    """Scripted Reviewer output with the Git prerequisite check mocked."""

    def _run(self, reviewer_output, checker_results, exit_code=0):
        launches = []

        def launcher(command):
            launches.append(command)
            return _FakeProcess(reviewer_output, exit_code)

        checker = MagicMock(side_effect=checker_results)
        status = MagicMock()

        with patch.object(review_module, "_verify_review_prerequisites", checker), patch.object(
            review_module, "_find_codex", return_value=Path("codex.exe")
        ), patch.object(review_module, "create_agent", return_value="AGENT-1"), patch.object(
            review_module, "update_agent_status", status
        ), patch.object(review_module, "update_agent_execution"):
            result = review_worker(
                run_id="RUN-1",
                worker_id="WORKER-1",
                worktree=WORKTREE,
                original_task="do something",
                worker_scope="fix things",
                model="cc/claude-sonnet-5-high",
                effort="high",
                risk_level="medium",
                process_launcher=launcher,
            )

        return result, launches, checker, status

    # CASE A: mandatory Git check succeeds and the Reviewer says PASS.
    def test_case_a_pass_with_healthy_git_is_accepted(self):
        result, launches, checker, status = self._run(
            _envelope("PASS"), checker_results=[None, None]
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["review"]["verdict"], "PASS")
        self.assertIsNone(result["review"]["failure_class"])
        self.assertEqual(len(launches), 1)
        self.assertEqual(checker.call_count, 2)
        status.assert_called_with("AGENT-1", "COMPLETED")

    # CASE B: Git succeeds before the Reviewer runs but fails after it says PASS.
    def test_case_b_pass_is_overridden_when_git_fails_after_review(self):
        result, launches, checker, status = self._run(
            _envelope("PASS", summary="looks good"),
            checker_results=[None, GIT_FAILURE],
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["review"]["verdict"], "BLOCKED")
        self.assertEqual(result["review"]["failure_class"], "TOOL_FAILURE")
        self.assertIn(GIT_FAILURE, result["review"]["evidence"])
        self.assertIn("Reviewer had returned PASS: looks good", result["review"]["evidence"])
        self.assertEqual(len(launches), 1)
        status.assert_called_with("AGENT-1", "BLOCKED")

    # CASE B (pre-launch): a required Git check fails, so the Reviewer never runs.
    def test_case_b_failed_prerequisite_never_launches_reviewer(self):
        result, launches, checker, status = self._run(
            _envelope("PASS"), checker_results=[GIT_FAILURE]
        )

        self.assertEqual(launches, [])
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["review"]["verdict"], "BLOCKED")
        self.assertEqual(result["review"]["failure_class"], "TOOL_FAILURE")
        self.assertEqual(result["review"]["evidence"], [GIT_FAILURE])
        self.assertIsNone(result["exit_code"])
        status.assert_called_with("AGENT-1", "BLOCKED")

    # CASE C: the Reviewer itself reports a tool failure as BLOCKED.
    def test_case_c_reviewer_tool_failure_is_preserved(self):
        result, _, checker, _ = self._run(
            _envelope("BLOCKED", "TOOL_FAILURE", summary="git was denied"),
            checker_results=[None],
        )

        self.assertEqual(result["review"]["verdict"], "BLOCKED")
        self.assertEqual(result["review"]["failure_class"], "TOOL_FAILURE")
        self.assertEqual(result["review"]["summary"], "git was denied")
        self.assertEqual(checker.call_count, 1)

    # CASE D: a real code defect is still a RETRY.
    def test_case_d_real_defect_retry_is_unchanged(self):
        result, _, _, _ = self._run(
            _envelope("RETRY", "VALIDATION_FAILURE"),
            checker_results=[None],
        )

        self.assertEqual(result["review"]["verdict"], "RETRY")
        self.assertEqual(result["review"]["failure_class"], "VALIDATION_FAILURE")

    # CASE E: a genuine model capability failure is still CAPABILITY_FAILURE.
    def test_case_e_capability_failure_is_unchanged(self):
        result, _, _, _ = self._run(
            _envelope("BLOCKED", "CAPABILITY_FAILURE"),
            checker_results=[None],
        )

        self.assertEqual(result["review"]["verdict"], "BLOCKED")
        self.assertEqual(result["review"]["failure_class"], "CAPABILITY_FAILURE")

    # CASE F: an environment/tool failure must not exclude the reviewing model.
    def test_case_f_tool_failure_classifies_as_environment_not_capability(self):
        result, _, _, _ = self._run(_envelope("PASS"), checker_results=[GIT_FAILURE])

        error = _GoError("REVIEW_BLOCKED", {"review": result["review"]})
        classification = fc.classify_go_error(error)

        self.assertEqual(classification, fc.ENVIRONMENT_FAILURE)
        self.assertNotEqual(classification, fc.CAPABILITY_INSUFFICIENT)


class RealGitPrerequisiteTests(unittest.TestCase):
    """The real check against real repositories. No mocks."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="nexus-review-git-"))
        self.repo = self.root / "assigned worktree"
        self.repo.mkdir()
        self._git("init", "-q")
        (self.repo / "file.txt").write_text("content\n", encoding="utf-8")
        self._git("add", "file.txt")
        self._git(
            "-c", "user.name=Nexus Test", "-c", "user.email=nexus@example.invalid",
            "commit", "-q", "-m", "init",
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _git(self, *args):
        subprocess.run(
            [GIT, "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_healthy_repository_passes(self):
        self.assertIsNone(_verify_review_prerequisites(str(self.repo)))

    def test_healthy_repository_passes_under_ownership_hook(self):
        with patch.dict(os.environ, OWNERSHIP_HOOK):
            self.assertIsNone(_verify_review_prerequisites(str(self.repo)))

    def test_non_repository_fails_with_git_detail(self):
        plain = self.root / "not-a-repo"
        plain.mkdir()

        detail = _verify_review_prerequisites(str(plain))

        self.assertIsNotNone(detail)
        self.assertIn("rev-parse --show-toplevel", detail)

    def test_subdirectory_of_another_repository_is_rejected(self):
        # Git would happily inspect the enclosing repository from a subfolder,
        # so the assigned path must be the worktree root itself.
        subfolder = self.repo / "nested"
        subfolder.mkdir()

        detail = _verify_review_prerequisites(str(subfolder))

        self.assertIsNotNone(detail)
        self.assertIn("not the assigned worktree", detail)

    def test_relative_path_is_rejected(self):
        self.assertIsNotNone(_verify_review_prerequisites("relative/worktree"))

    def test_git_that_cannot_run_fails_closed(self):
        with patch.object(
            review_module.subprocess, "run", side_effect=FileNotFoundError("git")
        ):
            detail = _verify_review_prerequisites(str(self.repo))

        self.assertIsNotNone(detail)
        self.assertIn("could not run", detail)


if __name__ == "__main__":
    unittest.main()
