"""Phase 4.4 Integration tests.

Tests for nexus/orchestration/integration.py and the go.py wiring.

Test framework: unittest.TestCase + tempfile.TemporaryDirectory() + real git.
Mocks: execute_progressively (at nexus.orchestration.integration.execute_progressively)
       record_checkpoint (at nexus.orchestration.integration.record_checkpoint)

Run:
    python -m unittest tests.test_phase4_4_integration
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from nexus.orchestration.integration import (
    IntegrationError,
    _parse_porcelain,
    run_integrated_plan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_dir: str, name: str = "primary") -> str:
    """Create a minimal git repo with one commit and return its path."""
    repo = os.path.join(tmp_dir, name)
    os.makedirs(repo, exist_ok=True)
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}

    def git(*args):
        subprocess.run(["git"] + list(args), cwd=repo, check=True,
                       capture_output=True, env=env)

    git("init", "-b", "main")
    git("config", "user.email", "t@t.com")
    git("config", "user.name", "Test")
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("hello\n")
    git("add", "README.md")
    git("commit", "--no-verify", "-m", "init")
    return repo


def _make_worker_worktree(primary_repo: str, branch_name: str, tmp_dir: str) -> str:
    """Create a git worktree off primary_repo for simulating a worker."""
    wt_path = os.path.join(tmp_dir, branch_name)
    subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, wt_path],
        cwd=primary_repo, check=True, capture_output=True,
    )
    return wt_path


def _write_file(repo: str, name: str, content: str) -> None:
    with open(os.path.join(repo, name), "w") as f:
        f.write(content)


def _head_sha(repo: str) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
        stdin=subprocess.DEVNULL, stderr=subprocess.PIPE,
    ).strip()


def _branch_exists(repo: str, branch: str) -> bool:
    out = subprocess.check_output(
        ["git", "branch", "--list", branch], cwd=repo, text=True,
        stdin=subprocess.DEVNULL, stderr=subprocess.PIPE,
    ).strip()
    return bool(out)


def _make_completed_outcome(worktree: str, agent_id: str = "agent-1") -> dict:
    return {
        "status": "COMPLETED",
        "worker": {"worktree": worktree, "agent_id": agent_id, "output": "done"},
        "review": {"reviewer_id": "rev-1"},
    }


def _make_failed_outcome(reason: str = "REVIEW_FAILED") -> dict:
    return {"status": "BLOCKED", "reason": reason, "error": "blocked"}


def _make_workers(n: int, authority: str = "WORKSPACE_WRITE") -> list[dict]:
    return [
        {"scope": f"worker {i}", "authority": authority, "depends_on": []}
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Test A: empty workers list
# ---------------------------------------------------------------------------


class TestA_EmptyWorkers(unittest.TestCase):
    def test_empty_workers_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            with (patch("nexus.orchestration.integration.execute_progressively") as ep,
                  patch("nexus.orchestration.integration.record_checkpoint")):
                result = run_integrated_plan(
                    run_id="RUN-A",
                    primary_repo=repo,
                    manager_id="mgr-1",
                    request_text="do nothing",
                    workers=[],
                    plan_risk="LOW",
                )
            self.assertEqual(result, [])
            ep.assert_not_called()


# ---------------------------------------------------------------------------
# Test B: single worker, no file changes (no-diff path)
# ---------------------------------------------------------------------------


class TestB_SingleWorkerNoDiff(unittest.TestCase):
    def test_clean_worker_skipped_no_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            wt = _make_worker_worktree(repo, "worker-b", tmp)
            original_sha = _head_sha(repo)
            outcome = _make_completed_outcome(wt)

            with (patch("nexus.orchestration.integration.execute_progressively",
                        return_value=outcome),
                  patch("nexus.orchestration.integration.record_checkpoint")):
                result = run_integrated_plan(
                    run_id="RUN-B",
                    primary_repo=repo,
                    manager_id="mgr-1",
                    request_text="task b",
                    workers=_make_workers(1),
                    plan_risk="LOW",
                )

            final_sha = _head_sha(repo)
            self.assertEqual(original_sha, final_sha)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["status"], "COMPLETED")


# ---------------------------------------------------------------------------
# Test C: single worker with file change, integration merged to primary
# ---------------------------------------------------------------------------


class TestC_SingleWorkerWithChange(unittest.TestCase):
    def test_file_change_merged_to_primary(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            wt = _make_worker_worktree(repo, "worker-c", tmp)
            _write_file(wt, "new_file.py", "# new\n")
            outcome = _make_completed_outcome(wt)

            with (patch("nexus.orchestration.integration.execute_progressively",
                        return_value=outcome),
                  patch("nexus.orchestration.integration.record_checkpoint")):
                result = run_integrated_plan(
                    run_id="RUN-C",
                    primary_repo=repo,
                    manager_id="mgr-1",
                    request_text="add file",
                    workers=_make_workers(1),
                    plan_risk="LOW",
                )

            # new_file.py should now be in the primary repo
            self.assertTrue(os.path.exists(os.path.join(repo, "new_file.py")))
            self.assertEqual(result[0]["status"], "COMPLETED")


# ---------------------------------------------------------------------------
# Test D: READ_ONLY worker with no filesystem changes (clean pass)
# ---------------------------------------------------------------------------


class TestD_ReadOnlyWorkerClean(unittest.TestCase):
    def test_read_only_clean_no_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            wt = _make_worker_worktree(repo, "worker-d", tmp)
            outcome = _make_completed_outcome(wt)
            workers = _make_workers(1, authority="READ_ONLY")
            original_sha = _head_sha(repo)

            with (patch("nexus.orchestration.integration.execute_progressively",
                        return_value=outcome),
                  patch("nexus.orchestration.integration.record_checkpoint")):
                run_integrated_plan(
                    run_id="RUN-D",
                    primary_repo=repo,
                    manager_id="mgr-1",
                    request_text="read task",
                    workers=workers,
                    plan_risk="LOW",
                )

            final_sha = _head_sha(repo)
            self.assertEqual(original_sha, final_sha)


# ---------------------------------------------------------------------------
# Test E: READ_ONLY worker that modifies the repo → FAILED
# ---------------------------------------------------------------------------


class TestE_ReadOnlyWorkerDirty(unittest.TestCase):
    def test_read_only_dirty_raises_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            wt = _make_worker_worktree(repo, "worker-e", tmp)
            _write_file(wt, "secret.py", "oops\n")
            outcome = _make_completed_outcome(wt)
            workers = _make_workers(1, authority="READ_ONLY")

            with (patch("nexus.orchestration.integration.execute_progressively",
                        return_value=outcome),
                  patch("nexus.orchestration.integration.record_checkpoint")):
                with self.assertRaises(IntegrationError) as ctx:
                    run_integrated_plan(
                        run_id="RUN-E",
                        primary_repo=repo,
                        manager_id="mgr-1",
                        request_text="read task",
                        workers=workers,
                        plan_risk="LOW",
                    )

        err = ctx.exception
        self.assertEqual(err.code, "READ_ONLY_WORKER_MODIFIED_REPOSITORY")
        self.assertEqual(err.run_status, "FAILED")
        self.assertIn("secret.py", err.payload.get("dirty_files", []))


# ---------------------------------------------------------------------------
# Test F: cherry-pick conflict → BLOCKED
# ---------------------------------------------------------------------------


class TestF_CherryPickConflict(unittest.TestCase):
    def test_conflict_raises_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            wt = _make_worker_worktree(repo, "worker-f", tmp)
            _write_file(wt, "README.md", "worker change\n")

            # Advance primary so integration branch starts from a different base
            env = {**os.environ,
                   "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t.com",
                   "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t.com"}
            _write_file(repo, "README.md", "primary change\n")
            subprocess.run(["git", "add", "README.md"], cwd=repo,
                           check=True, capture_output=True, env=env)
            subprocess.run(["git", "commit", "--no-verify", "-m", "primary diverge"],
                           cwd=repo, check=True, capture_output=True, env=env)

            outcome = _make_completed_outcome(wt)

            with (patch("nexus.orchestration.integration.execute_progressively",
                        return_value=outcome),
                  patch("nexus.orchestration.integration.record_checkpoint")):
                with self.assertRaises(IntegrationError) as ctx:
                    run_integrated_plan(
                        run_id="RUN-F",
                        primary_repo=repo,
                        manager_id="mgr-1",
                        request_text="conflict task",
                        workers=_make_workers(1),
                        plan_risk="LOW",
                    )

        err = ctx.exception
        self.assertEqual(err.code, "INTEGRATION_CONFLICT")
        self.assertEqual(err.run_status, "BLOCKED")


# ---------------------------------------------------------------------------
# Test G: dirty-overlap check → BLOCKED
# ---------------------------------------------------------------------------


class TestG_DirtyOverlap(unittest.TestCase):
    def test_dirty_overlap_raises_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            wt = _make_worker_worktree(repo, "worker-g", tmp)
            _write_file(wt, "README.md", "worker says hello\n")
            _write_file(repo, "README.md", "local draft\n")
            outcome = _make_completed_outcome(wt)

            with (patch("nexus.orchestration.integration.execute_progressively",
                        return_value=outcome),
                  patch("nexus.orchestration.integration.record_checkpoint")):
                with self.assertRaises(IntegrationError) as ctx:
                    run_integrated_plan(
                        run_id="RUN-G",
                        primary_repo=repo,
                        manager_id="mgr-1",
                        request_text="overlap task",
                        workers=_make_workers(1),
                        plan_risk="LOW",
                    )

        err = ctx.exception
        self.assertEqual(err.code, "INTEGRATION_DIRTY_OVERLAP")
        self.assertEqual(err.run_status, "BLOCKED")
        self.assertIn("README.md", err.payload.get("overlap", []))


# ---------------------------------------------------------------------------
# Test H: primary HEAD changed during run → BLOCKED
# ---------------------------------------------------------------------------


class TestH_PrimaryHeadChanged(unittest.TestCase):
    def test_primary_head_changed_raises_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            wt = _make_worker_worktree(repo, "worker-h", tmp)
            _write_file(wt, "new.py", "x = 1\n")
            outcome = _make_completed_outcome(wt)

            env = {**os.environ,
                   "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t.com",
                   "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t.com"}

            def _sneaky_checkpoint(*, run_id, boundary, payload, **kw):
                if boundary == "INTEGRATION_STAGE_CREATED":
                    _write_file(repo, "external.py", "external\n")
                    subprocess.run(["git", "add", "external.py"], cwd=repo,
                                   check=True, capture_output=True, env=env)
                    subprocess.run(["git", "commit", "--no-verify", "-m", "external"],
                                   cwd=repo, check=True, capture_output=True, env=env)

            with (patch("nexus.orchestration.integration.execute_progressively",
                        return_value=outcome),
                  patch("nexus.orchestration.integration.record_checkpoint",
                        side_effect=_sneaky_checkpoint)):
                with self.assertRaises(IntegrationError) as ctx:
                    run_integrated_plan(
                        run_id="RUN-H",
                        primary_repo=repo,
                        manager_id="mgr-1",
                        request_text="external change",
                        workers=_make_workers(1),
                        plan_risk="LOW",
                    )

        err = ctx.exception
        self.assertEqual(err.code, "PRIMARY_REPO_CHANGED_UNEXPECTEDLY")
        self.assertEqual(err.run_status, "BLOCKED")


# ---------------------------------------------------------------------------
# Test I: worker execution raises exception → WORKER_EXECUTION_FAILED FAILED
# ---------------------------------------------------------------------------


class TestI_WorkerException(unittest.TestCase):
    def test_worker_exception_raises_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)

            with (patch("nexus.orchestration.integration.execute_progressively",
                        side_effect=RuntimeError("crash")),
                  patch("nexus.orchestration.integration.record_checkpoint")):
                with self.assertRaises(IntegrationError) as ctx:
                    run_integrated_plan(
                        run_id="RUN-I",
                        primary_repo=repo,
                        manager_id="mgr-1",
                        request_text="crash task",
                        workers=_make_workers(1),
                        plan_risk="LOW",
                    )

        err = ctx.exception
        self.assertEqual(err.code, "WORKER_EXECUTION_FAILED")
        self.assertEqual(err.run_status, "FAILED")


# ---------------------------------------------------------------------------
# Test J: worker returns BLOCKED outcome → REVIEW_BLOCKED BLOCKED
# ---------------------------------------------------------------------------


class TestJ_WorkerReviewBlocked(unittest.TestCase):
    def test_review_blocked_outcome_raises_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            outcome = _make_failed_outcome("REVIEW_FAILED")

            with (patch("nexus.orchestration.integration.execute_progressively",
                        return_value=outcome),
                  patch("nexus.orchestration.integration.record_checkpoint")):
                with self.assertRaises(IntegrationError) as ctx:
                    run_integrated_plan(
                        run_id="RUN-J",
                        primary_repo=repo,
                        manager_id="mgr-1",
                        request_text="blocked task",
                        workers=_make_workers(1),
                        plan_risk="LOW",
                    )

        err = ctx.exception
        self.assertEqual(err.code, "REVIEW_BLOCKED")
        self.assertEqual(err.run_status, "BLOCKED")


# ---------------------------------------------------------------------------
# Test K: worker returns INITIAL_ROUTING_UNAVAILABLE → BLOCKED
# ---------------------------------------------------------------------------


class TestK_RoutingUnavailable(unittest.TestCase):
    def test_routing_unavailable_raises_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            outcome = {
                "status": "BLOCKED",
                "reason": "INITIAL_ROUTING_UNAVAILABLE",
                "error": "no route",
                "error_type": "CapabilityGap",
                "route_class": "CODEX",
                "capability": "CODE_WRITE",
                "risk": "HIGH",
            }

            with (patch("nexus.orchestration.integration.execute_progressively",
                        return_value=outcome),
                  patch("nexus.orchestration.integration.record_checkpoint")):
                with self.assertRaises(IntegrationError) as ctx:
                    run_integrated_plan(
                        run_id="RUN-K",
                        primary_repo=repo,
                        manager_id="mgr-1",
                        request_text="routing task",
                        workers=_make_workers(1),
                        plan_risk="LOW",
                    )

        err = ctx.exception
        self.assertEqual(err.code, "INITIAL_ROUTING_UNAVAILABLE")
        self.assertEqual(err.run_status, "BLOCKED")


# ---------------------------------------------------------------------------
# Test L: worker returns ESCALATION_UNAVAILABLE → BLOCKED
# ---------------------------------------------------------------------------


class TestL_EscalationUnavailable(unittest.TestCase):
    def test_escalation_unavailable_raises_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            outcome = {
                "status": "BLOCKED",
                "reason": "ESCALATION_UNAVAILABLE",
                "error": "no escalation",
            }

            with (patch("nexus.orchestration.integration.execute_progressively",
                        return_value=outcome),
                  patch("nexus.orchestration.integration.record_checkpoint")):
                with self.assertRaises(IntegrationError) as ctx:
                    run_integrated_plan(
                        run_id="RUN-L",
                        primary_repo=repo,
                        manager_id="mgr-1",
                        request_text="escalation task",
                        workers=_make_workers(1),
                        plan_risk="LOW",
                    )

        err = ctx.exception
        self.assertEqual(err.code, "ESCALATION_UNAVAILABLE")
        self.assertEqual(err.run_status, "BLOCKED")


# ---------------------------------------------------------------------------
# Test M: two-round sequential DAG — both rounds' changes reach primary
# ---------------------------------------------------------------------------


class TestM_TwoRoundDAG(unittest.TestCase):
    def test_two_round_dag_both_changes_in_primary(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            wt1 = _make_worker_worktree(repo, "worker-m1", tmp)
            wt2 = _make_worker_worktree(repo, "worker-m2", tmp)

            _write_file(wt1, "file_a.py", "A = 1\n")
            _write_file(wt2, "file_b.py", "B = 2\n")

            outcomes = [
                _make_completed_outcome(wt1, "agent-m1"),
                _make_completed_outcome(wt2, "agent-m2"),
            ]
            call_count = [0]

            def ep_side_effect(**kwargs):
                idx = call_count[0]
                call_count[0] += 1
                return outcomes[idx]

            workers = [
                {"scope": "w0", "authority": "WORKSPACE_WRITE", "depends_on": []},
                {"scope": "w1", "authority": "WORKSPACE_WRITE", "depends_on": [0]},
            ]

            with (patch("nexus.orchestration.integration.execute_progressively",
                        side_effect=ep_side_effect),
                  patch("nexus.orchestration.integration.record_checkpoint")):
                result = run_integrated_plan(
                    run_id="RUN-M",
                    primary_repo=repo,
                    manager_id="mgr-1",
                    request_text="two round",
                    workers=workers,
                    plan_risk="LOW",
                )

            self.assertTrue(os.path.exists(os.path.join(repo, "file_a.py")))
            self.assertTrue(os.path.exists(os.path.join(repo, "file_b.py")))
            self.assertEqual(len(result), 2)


# ---------------------------------------------------------------------------
# Test N: two concurrent workers in round 0 — both applied
# ---------------------------------------------------------------------------


class TestN_ConcurrentRound(unittest.TestCase):
    def test_two_concurrent_workers_both_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            wt0 = _make_worker_worktree(repo, "worker-n0", tmp)
            wt1 = _make_worker_worktree(repo, "worker-n1", tmp)

            _write_file(wt0, "alpha.py", "A\n")
            _write_file(wt1, "beta.py", "B\n")

            outcomes = {
                0: _make_completed_outcome(wt0, "agent-n0"),
                1: _make_completed_outcome(wt1, "agent-n1"),
            }

            def ep_side_effect(**kwargs):
                wi = kwargs["worker_ordinal"] - 1
                return outcomes[wi]

            workers = [
                {"scope": "w0", "authority": "WORKSPACE_WRITE", "depends_on": []},
                {"scope": "w1", "authority": "WORKSPACE_WRITE", "depends_on": []},
            ]

            with (patch("nexus.orchestration.integration.execute_progressively",
                        side_effect=ep_side_effect),
                  patch("nexus.orchestration.integration.record_checkpoint")):
                result = run_integrated_plan(
                    run_id="RUN-N",
                    primary_repo=repo,
                    manager_id="mgr-1",
                    request_text="concurrent round",
                    workers=workers,
                    plan_risk="LOW",
                )

            self.assertTrue(os.path.exists(os.path.join(repo, "alpha.py")))
            self.assertTrue(os.path.exists(os.path.join(repo, "beta.py")))


# ---------------------------------------------------------------------------
# Test O: integration worktree and branch cleaned up after success
# ---------------------------------------------------------------------------


class TestO_CleanupOnSuccess(unittest.TestCase):
    def test_integration_artifacts_cleaned_up_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            wt = _make_worker_worktree(repo, "worker-o", tmp)
            _write_file(wt, "clean.py", "pass\n")
            integration_wts: list[str] = []

            def mock_checkpoint(*, run_id, boundary, payload, **kw):
                if boundary == "INTEGRATION_STAGE_CREATED":
                    integration_wts.append(payload.get("integration_worktree", ""))

            with (patch("nexus.orchestration.integration.execute_progressively",
                        return_value=_make_completed_outcome(wt)),
                  patch("nexus.orchestration.integration.record_checkpoint",
                        side_effect=mock_checkpoint)):
                run_integrated_plan(
                    run_id="RUN-O",
                    primary_repo=repo,
                    manager_id="mgr-1",
                    request_text="cleanup test",
                    workers=_make_workers(1),
                    plan_risk="LOW",
                )

            self.assertTrue(integration_wts, "should have captured integration_wt path")
            self.assertFalse(os.path.exists(integration_wts[0]),
                             f"integration worktree still exists: {integration_wts[0]}")
            self.assertFalse(_branch_exists(repo, "nexus/integration/RUN-O"),
                             "integration branch should be deleted")


# ---------------------------------------------------------------------------
# Test P: integration worktree and branch cleaned up after failure
# ---------------------------------------------------------------------------


class TestP_CleanupOnFailure(unittest.TestCase):
    def test_integration_artifacts_cleaned_up_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            integration_wts: list[str] = []

            def mock_checkpoint(*, run_id, boundary, payload, **kw):
                if boundary == "INTEGRATION_STAGE_CREATED":
                    integration_wts.append(payload.get("integration_worktree", ""))

            with (patch("nexus.orchestration.integration.execute_progressively",
                        side_effect=RuntimeError("boom")),
                  patch("nexus.orchestration.integration.record_checkpoint",
                        side_effect=mock_checkpoint)):
                with self.assertRaises(IntegrationError):
                    run_integrated_plan(
                        run_id="RUN-P",
                        primary_repo=repo,
                        manager_id="mgr-1",
                        request_text="fail clean",
                        workers=_make_workers(1),
                        plan_risk="LOW",
                    )

            self.assertTrue(integration_wts, "should have captured wt path")
            self.assertFalse(os.path.exists(integration_wts[0]),
                             "integration worktree should be cleaned up after failure")
            self.assertFalse(_branch_exists(repo, "nexus/integration/RUN-P"),
                             "integration branch should be deleted after failure")


# ---------------------------------------------------------------------------
# Test Q: INTEGRATION_STAGE_CREATED and INTEGRATION_COMPLETED checkpoints
# ---------------------------------------------------------------------------


class TestQ_CheckpointStagedCreated(unittest.TestCase):
    def test_integration_stage_created_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            wt = _make_worker_worktree(repo, "worker-q", tmp)
            checkpoints: list[dict] = []

            def capture_cp(*, run_id, boundary, payload, **kw):
                checkpoints.append({"boundary": boundary, "payload": payload})

            with (patch("nexus.orchestration.integration.execute_progressively",
                        return_value=_make_completed_outcome(wt)),
                  patch("nexus.orchestration.integration.record_checkpoint",
                        side_effect=capture_cp)):
                run_integrated_plan(
                    run_id="RUN-Q",
                    primary_repo=repo,
                    manager_id="mgr-1",
                    request_text="cp test",
                    workers=_make_workers(1),
                    plan_risk="LOW",
                )

        created = next((c for c in checkpoints
                        if c["boundary"] == "INTEGRATION_STAGE_CREATED"), None)
        self.assertIsNotNone(created)
        self.assertIn("integration_branch", created["payload"])
        self.assertIn("original_sha", created["payload"])
        self.assertIn("original_branch", created["payload"])
        self.assertTrue(
            created["payload"]["integration_branch"].startswith("nexus/integration/")
        )

        completed_cp = next((c for c in checkpoints
                             if c["boundary"] == "INTEGRATION_COMPLETED"), None)
        self.assertIsNotNone(completed_cp)


# ---------------------------------------------------------------------------
# Test R: INTEGRATION_FAILED checkpoint recorded for integration-layer errors
# ---------------------------------------------------------------------------


class TestR_IntegrationFailedCheckpoint(unittest.TestCase):
    def test_integration_failed_checkpoint_on_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            wt = _make_worker_worktree(repo, "worker-r", tmp)
            _write_file(wt, "README.md", "worker version\n")

            env = {**os.environ,
                   "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t.com",
                   "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t.com"}
            _write_file(repo, "README.md", "primary version\n")
            subprocess.run(["git", "add", "README.md"], cwd=repo,
                           check=True, capture_output=True, env=env)
            subprocess.run(["git", "commit", "--no-verify", "-m", "primary"],
                           cwd=repo, check=True, capture_output=True, env=env)

            checkpoints: list[dict] = []

            def capture_cp(*, run_id, boundary, payload, **kw):
                checkpoints.append({"boundary": boundary, "payload": payload})

            with (patch("nexus.orchestration.integration.execute_progressively",
                        return_value=_make_completed_outcome(wt)),
                  patch("nexus.orchestration.integration.record_checkpoint",
                        side_effect=capture_cp)):
                with self.assertRaises(IntegrationError) as ctx:
                    run_integrated_plan(
                        run_id="RUN-R",
                        primary_repo=repo,
                        manager_id="mgr-1",
                        request_text="conflict cp",
                        workers=_make_workers(1),
                        plan_risk="LOW",
                    )

        self.assertEqual(ctx.exception.code, "INTEGRATION_CONFLICT")
        failed_cp = next((c for c in checkpoints
                          if c["boundary"] == "INTEGRATION_FAILED"), None)
        self.assertIsNotNone(failed_cp, "INTEGRATION_FAILED checkpoint expected")
        self.assertEqual(failed_cp["payload"]["error_code"], "INTEGRATION_CONFLICT")


# ---------------------------------------------------------------------------
# Test S: worker with no worktree reported → skipped cleanly
# ---------------------------------------------------------------------------


class TestS_NoWorktreeReported(unittest.TestCase):
    def test_worker_with_no_worktree_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            outcome = {
                "status": "COMPLETED",
                "worker": {"agent_id": "agent-s", "output": "analysis"},
                "review": {"reviewer_id": "rev-s"},
            }
            original_sha = _head_sha(repo)

            with (patch("nexus.orchestration.integration.execute_progressively",
                        return_value=outcome),
                  patch("nexus.orchestration.integration.record_checkpoint")):
                result = run_integrated_plan(
                    run_id="RUN-S",
                    primary_repo=repo,
                    manager_id="mgr-1",
                    request_text="no wt",
                    workers=_make_workers(1),
                    plan_risk="LOW",
                )

            final_sha = _head_sha(repo)
            self.assertEqual(original_sha, final_sha)
            self.assertEqual(result[0]["status"], "COMPLETED")


# ---------------------------------------------------------------------------
# Test T: IntegrationError constructor, _parse_porcelain
# ---------------------------------------------------------------------------


class TestT_IntegrationErrorValidation(unittest.TestCase):
    def test_valid_blocked(self):
        err = IntegrationError("SOME_CODE", run_status="BLOCKED", key="val")
        self.assertEqual(err.code, "SOME_CODE")
        self.assertEqual(err.run_status, "BLOCKED")
        self.assertEqual(err.payload["key"], "val")

    def test_valid_failed(self):
        err = IntegrationError("OTHER", run_status="FAILED")
        self.assertEqual(err.run_status, "FAILED")

    def test_invalid_run_status_raises(self):
        with self.assertRaises(ValueError):
            IntegrationError("X", run_status="UNKNOWN")

    def test_str_includes_code(self):
        err = IntegrationError("MY_CODE", run_status="BLOCKED")
        self.assertIn("MY_CODE", str(err))

    def test_parse_porcelain_renamed_file(self):
        output = "R  old.py -> new.py\n"
        paths = _parse_porcelain(output)
        self.assertIn("new.py", paths)

    def test_parse_porcelain_empty(self):
        paths = _parse_porcelain("")
        self.assertEqual(paths, set())


class IntegrationContainerTests(unittest.TestCase):
    def test_container_is_plain_directory_not_mkdtemp(self):
        # mkdtemp's owner-only ACL blocks the sandboxed Worker/Review principal
        # from reaching a worktree placed inside the container.
        from nexus.orchestration import integration

        with patch.object(integration.tempfile, "mkdtemp", side_effect=AssertionError):
            container = integration._create_integration_container()
        try:
            self.assertTrue(os.path.isdir(container))
            self.assertEqual(
                os.path.realpath(os.path.dirname(container)),
                os.path.realpath(tempfile.gettempdir()),
            )
            self.assertTrue(os.path.basename(container).startswith("nexus_intg_root_"))
        finally:
            os.rmdir(container)

    def test_worker_commit_succeeds_without_configured_git_identity(self):
        from nexus.orchestration import integration

        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            with open(os.path.join(repo, "a.txt"), "w", encoding="utf-8") as f:
                f.write("x\n")
            isolated = {
                **os.environ,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "HOME": tmp,
                "USERPROFILE": tmp,
            }
            for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
                isolated.pop(var, None)
            with patch.dict(os.environ, isolated, clear=True):
                sha = integration._commit_worker_changes(
                    worktree=repo, run_id="RUN-ID", worker_index=0, agent_id="AGENT-ID",
                )
            self.assertIsNotNone(sha)

    def test_cherry_pick_onto_integration_succeeds_without_configured_git_identity(self):
        from nexus.orchestration import integration

        with tempfile.TemporaryDirectory() as tmp:
            primary = os.path.join(tmp, "primary")
            os.makedirs(primary)
            isolated = {
                **os.environ,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "HOME": tmp,
                "USERPROFILE": tmp,
            }
            for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
                isolated.pop(var, None)
            with patch.dict(os.environ, isolated, clear=True):
                subprocess.run(["git", "init", "-q", "-b", "main"], cwd=primary, check=True)
                with open(os.path.join(primary, "base.txt"), "w", encoding="utf-8") as f:
                    f.write("base\n")
                subprocess.run(["git", "add", "-A"], cwd=primary, check=True)
                subprocess.run(
                    ["git", *integration._identity_args(primary), "commit", "-qm", "base"],
                    cwd=primary, check=True,
                )
                container = integration._create_integration_container()
                integration_wt = os.path.join(container, "worktree")
                subprocess.run(
                    ["git", "worktree", "add", "-q", "-b", "nexus/integration/t", integration_wt],
                    cwd=primary, check=True,
                )
                try:
                    sub_wt = integration._add_worker_worktree(
                        integration_wt=integration_wt,
                        cleanup_root=container,
                        run_id="RUN-T",
                        worker_index=0,
                    )
                    with open(os.path.join(sub_wt, "new.txt"), "w", encoding="utf-8") as f:
                        f.write("new\n")
                    with patch.object(integration, "record_checkpoint"), patch.object(
                        integration, "_safe_record_checkpoint"
                    ):
                        integration._apply_round(
                            run_id="RUN-T",
                            integration_wt=integration_wt,
                            workers=[{"authority": "WORKSPACE_WRITE"}],
                            eligible=[0],
                            round_outcomes={
                                0: {
                                    "worker": {"worktree": sub_wt, "agent_id": "AGENT-T"},
                                    "review": {"reviewer_id": "REVIEWER-T"},
                                }
                            },
                        )
                    self.assertTrue(os.path.isfile(os.path.join(integration_wt, "new.txt")))
                finally:
                    integration._cleanup_integration_worktree(
                        primary, container, integration_wt, "nexus/integration/t"
                    )

    def test_worker_gets_own_subworktree_from_integration_head(self):
        # A worker running in the integration worktree itself commits onto the
        # integration branch, so the later cherry-pick onto it always conflicts.
        from nexus.orchestration import integration

        with tempfile.TemporaryDirectory() as tmp:
            primary = os.path.join(tmp, "primary")
            os.makedirs(primary)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=primary, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=primary, check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=primary, check=True)
            with open(os.path.join(primary, "README.md"), "w", encoding="utf-8") as f:
                f.write("x\n")
            subprocess.run(["git", "add", "-A"], cwd=primary, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=primary, check=True)

            container = integration._create_integration_container()
            integration_wt = os.path.join(container, "worktree")
            subprocess.run(
                ["git", "worktree", "add", "-b", "nexus/integration/t", integration_wt],
                cwd=primary, check=True, capture_output=True,
            )
            try:
                sub_wt = integration._add_worker_worktree(
                    integration_wt=integration_wt,
                    cleanup_root=container,
                    run_id="RUN-T",
                    worker_index=0,
                )
                self.assertNotEqual(os.path.realpath(sub_wt), os.path.realpath(integration_wt))
                self.assertTrue(os.path.isfile(os.path.join(sub_wt, "README.md")))
                head_sub = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=sub_wt,
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
                head_int = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=integration_wt,
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
                self.assertEqual(head_sub, head_int)
                integration._remove_worker_worktree(
                    integration_wt=integration_wt,
                    sub_wt=sub_wt,
                    run_id="RUN-T",
                    worker_index=0,
                )
                self.assertFalse(os.path.exists(sub_wt))
            finally:
                integration._cleanup_integration_worktree(
                    primary, container, integration_wt, "nexus/integration/t"
                )


if __name__ == "__main__":
    unittest.main()
