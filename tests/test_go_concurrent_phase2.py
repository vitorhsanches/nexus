"""Regression tests for Nexus Multi-Agent MVP Phase 2: concurrent,
round-based execution of independent Workers.

Phase 2 replaces orchestration/go.py's sequential Worker loop with a
round-based scheduler (_run_workers_by_round) driven by Phase 1's
eligible_worker_batch(), dispatching each round's independent Workers to
a concurrent.futures.ThreadPoolExecutor. execute_progressively() itself is
never modified and remains the complete, isolated unit of execution for a
single Worker; these tests mock it at the orchestration/go.py boundary
(exactly like the pre-existing tests/test_go_error_handling.py and
tests/test_progressive_review_routing.py already do) so they exercise the
scheduler's concurrency/ordering/fail-closed behavior without any real
Codex/OmniRoute/subprocess/network call.

No multiprocessing, no asyncio -- only threading/concurrent.futures
primitives are used to prove real concurrency (threading.Barrier), never
fragile timestamp comparisons.
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.orchestration.go import GoError, _run_workers_by_round, run_go
from nexus.registry.checkpoints import (
    CheckpointWriteError,
    list_checkpoints,
    record_checkpoint,
)
from nexus.registry.runs import create_run, get_run


FIXTURE_PROJECTS = {
    "projects": [
        {
            "id": "norte",
            "name": "Norte",
            "path": r"C:\fake\interface-life",
            "aliases": ["norte"],
            "enabled": True,
        },
    ]
}


def _worker(scope="do work", depends_on=None):
    worker = {
        "route_class": "mechanical",
        "execution_path": "OMNIROUTE",
        "provider": "omniroute",
        "model": "oc/big-pickle",
        "effort": "low",
        "scope": scope,
        "reason": "test worker",
    }
    if depends_on is not None:
        worker["depends_on"] = depends_on
    return worker


def _plan(workers, risk="LOW", summary="Test plan."):
    return {
        "complexity": "LOW",
        "risk": risk,
        "parallelism": 1,
        "summary": summary,
        "workers": workers,
    }


VALID_SINGLE_WORKER_PLAN = _plan([_worker("Fix the thing.")])


class _RealDatabaseTestCase(unittest.TestCase):
    """Base class giving each test its own throwaway sqlite database --
    never the user's real nexus/storage/nexus.db."""

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp_dir.name)

        self._original_db_path = database.DATABASE_PATH
        self._original_storage_dir = database.STORAGE_DIR
        self._original_projects_file = projects.PROJECTS_FILE

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus-test.db"

        fixture_path = tmp_path / "projects.json"
        fixture_path.write_text(json.dumps(FIXTURE_PROJECTS), encoding="utf-8")
        projects.PROJECTS_FILE = fixture_path

        database.initialize_database()
        projects.sync_projects()

    def tearDown(self) -> None:
        database.DATABASE_PATH = self._original_db_path
        database.STORAGE_DIR = self._original_storage_dir
        projects.PROJECTS_FILE = self._original_projects_file
        self._tmp_dir.cleanup()


class SingleWorkerStillWorksTests(_RealDatabaseTestCase):
    """Requirement 11/14: a plan with exactly one Worker keeps working
    exactly as before Phase 2 (execution_model == "sequential")."""

    def test_single_worker_plan_completes(self):
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={
                "manager_id": "AGT-0001",
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
        ):
            result = run_go("Norte: fix a bug", project_query="norte")

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(len(result["workers"]), 1)

        checkpoints = list_checkpoints(result["run_id"])
        plan_approved = [c for c in checkpoints if c["boundary"] == "PLAN_APPROVED"]
        self.assertEqual(len(plan_approved), 1)
        self.assertEqual(
            plan_approved[0]["payload"]["execution_model"], "sequential"
        )


class ConcurrencyProofTests(unittest.TestCase):
    """Requirement 14: real proof of concurrency via threading.Barrier,
    never timestamp comparison. If the scheduler ran Workers 0 and 1
    sequentially instead of concurrently, the second Worker's call to
    barrier.wait() would never be reached while the first is still
    blocked on its own barrier.wait(), and the test would hang until the
    Barrier's timeout raises BrokenBarrierError, failing the test."""

    def test_two_independent_workers_run_in_the_same_round_concurrently(self):
        barrier = threading.Barrier(2, timeout=5)
        started_order = []
        lock = threading.Lock()

        def fake_execute_progressively(**kwargs):
            worker_index = kwargs["worker_ordinal"] - 1
            with lock:
                started_order.append(worker_index)
            # Both Workers must reach this point before either proceeds --
            # only possible if they are genuinely running concurrently.
            barrier.wait()
            return {
                "status": "COMPLETED",
                "worker": {"status": "COMPLETED", "index": worker_index},
            }

        workers = [_worker("A", depends_on=[]), _worker("B", depends_on=[])]

        with patch(
            "nexus.orchestration.go.execute_progressively",
            side_effect=fake_execute_progressively,
        ):
            results = _run_workers_by_round(
                run_id="RUN-CONCURRENCY",
                repo="C:/fake/repo",
                manager_id="MANAGER-1",
                request_text="do the thing",
                workers=workers,
                plan_risk="LOW",
            )

        self.assertEqual(len(results), 2)
        self.assertEqual({0, 1}, set(started_order))
        self.assertEqual(results[0]["worker"]["index"], 0)
        self.assertEqual(results[1]["worker"]["index"], 1)


class DependencyGateTests(unittest.TestCase):
    def test_dependent_worker_only_starts_after_both_parents_complete(self):
        lock = threading.Lock()

        # Track a shared "completed" set updated (under lock) the moment
        # each parent's fake call returns, and assert both are present by
        # the time C's fake call runs.
        completed_before_c_snapshot = set()

        def fake_execute_progressively(**kwargs):
            worker_index = kwargs["worker_ordinal"] - 1
            if worker_index in (0, 1):
                with lock:
                    completed_before_c_snapshot.add(worker_index)
            elif worker_index == 2:
                with lock:
                    snapshot = set(completed_before_c_snapshot)
                self.assertEqual(snapshot, {0, 1})
            return {
                "status": "COMPLETED",
                "worker": {"status": "COMPLETED", "index": worker_index},
            }

        workers = [
            _worker("A", depends_on=[]),
            _worker("B", depends_on=[]),
            _worker("C", depends_on=[0, 1]),
        ]

        with patch(
            "nexus.orchestration.go.execute_progressively",
            side_effect=fake_execute_progressively,
        ):
            results = _run_workers_by_round(
                run_id="RUN-DEPGATE",
                repo="C:/fake/repo",
                manager_id="MANAGER-1",
                request_text="do the thing",
                workers=workers,
                plan_risk="LOW",
            )

        self.assertEqual(len(results), 3)
        for outcome in results:
            self.assertEqual(outcome["status"], "COMPLETED")

    def test_old_plan_without_depends_on_runs_workers_as_independent_concurrent(self):
        """Requirement 11: an old-format plan with multiple Workers and no
        depends_on key at all is normalized (Phase 1) to depends_on=[] for
        every Worker, so after Phase 2 those Workers now run concurrently
        in a single round instead of sequentially -- an explicit, intended
        semantic change."""
        barrier = threading.Barrier(2, timeout=5)

        def fake_execute_progressively(**kwargs):
            barrier.wait()
            return {"status": "COMPLETED", "worker": {"status": "COMPLETED"}}

        # No "depends_on" key at all -- exactly the pre-Phase-1 plan shape.
        workers = [_worker("A"), _worker("B")]
        self.assertNotIn("depends_on", workers[0])
        self.assertNotIn("depends_on", workers[1])

        from nexus.orchestration.dag import normalize_worker_dependencies

        normalized = normalize_worker_dependencies(workers)
        self.assertEqual(normalized[0]["depends_on"], [])
        self.assertEqual(normalized[1]["depends_on"], [])

        with patch(
            "nexus.orchestration.go.execute_progressively",
            side_effect=fake_execute_progressively,
        ):
            results = _run_workers_by_round(
                run_id="RUN-OLDPLAN",
                repo="C:/fake/repo",
                manager_id="MANAGER-1",
                request_text="do the thing",
                workers=normalized,
                plan_risk="LOW",
            )

        self.assertEqual(len(results), 2)


class WorkerIdentityAndOrderTests(unittest.TestCase):
    """Requirement 5/6: worker_ordinal is fixed by plan position, and the
    returned list is always ordered by plan index -- regardless of which
    thread's Worker actually finishes first."""

    def test_worker_ordinal_and_result_order_survive_reversed_completion(self):
        # Worker 0 is deliberately held up until Worker 1 has already
        # finished, proving completion order never determines identity
        # or storage position.
        worker_0_release = threading.Event()
        worker_1_finished = threading.Event()

        captured_ordinals = []
        lock = threading.Lock()

        def fake_execute_progressively(**kwargs):
            worker_ordinal = kwargs["worker_ordinal"]
            with lock:
                captured_ordinals.append(worker_ordinal)

            if worker_ordinal == 1:
                # Wait for Worker 2 to finish first.
                worker_1_finished.wait(timeout=5)
                return {
                    "status": "COMPLETED",
                    "worker": {"status": "COMPLETED", "which": "first-plan-slot"},
                }

            # worker_ordinal == 2
            result = {
                "status": "COMPLETED",
                "worker": {"status": "COMPLETED", "which": "second-plan-slot"},
            }
            worker_1_finished.set()
            return result

        workers = [_worker("A", depends_on=[]), _worker("B", depends_on=[])]

        with patch(
            "nexus.orchestration.go.execute_progressively",
            side_effect=fake_execute_progressively,
        ):
            results = _run_workers_by_round(
                run_id="RUN-ORDER",
                repo="C:/fake/repo",
                manager_id="MANAGER-1",
                request_text="do the thing",
                workers=workers,
                plan_risk="LOW",
            )

        self.assertEqual(sorted(captured_ordinals), [1, 2])
        # Even though ordinal 2 physically finished first, the result list
        # must still be ordered by plan position (index 0 first).
        self.assertEqual(results[0]["worker"]["which"], "first-plan-slot")
        self.assertEqual(results[1]["worker"]["which"], "second-plan-slot")


class RetryIsolationTests(unittest.TestCase):
    """Requirement 4/14: retry of Worker A must not alter Worker B's
    state. The real execute_progressively() (unchanged by Phase 2, see
    orchestration/progressive.py line ~94: ``worker = deepcopy(
    planned_worker)``) never mutates the ``planned_worker`` dict it is
    given -- it deep-copies it first and mutates only its own local copy
    across retries/escalation. This test's fake reproduces that exact,
    real contract (deepcopy-then-mutate-locally) to prove the isolation
    holds end-to-end through the scheduler: two threads calling
    execute_progressively concurrently with two different plan dicts must
    never observe or leak each other's local retry mutations."""

    def test_mutating_one_workers_dict_does_not_affect_another(self):
        from copy import deepcopy

        seen_scopes = {}

        def fake_execute_progressively(**kwargs):
            worker_ordinal = kwargs["worker_ordinal"]
            planned_worker = kwargs["planned_worker"]
            seen_scopes[worker_ordinal] = planned_worker["scope"]
            # Reproduce execute_progressively's real contract: deep-copy
            # before mutating, never touch the caller's dict in place.
            local_worker = deepcopy(planned_worker)
            local_worker["scope"] = "MUTATED-BY-RETRY"
            return {"status": "COMPLETED", "worker": {"status": "COMPLETED"}}

        workers = [
            _worker("Scope A", depends_on=[]),
            _worker("Scope B", depends_on=[]),
        ]

        with patch(
            "nexus.orchestration.go.execute_progressively",
            side_effect=fake_execute_progressively,
        ):
            _run_workers_by_round(
                run_id="RUN-RETRY-ISOLATION",
                repo="C:/fake/repo",
                manager_id="MANAGER-1",
                request_text="do the thing",
                workers=workers,
                plan_risk="LOW",
            )

        self.assertEqual(seen_scopes[1], "Scope A")
        self.assertEqual(seen_scopes[2], "Scope B")
        # The original plan's Worker B dict must be untouched by
        # whatever the (mocked) execution of Worker A did to its own
        # dict.
        self.assertEqual(workers[1]["scope"], "Scope B")


class FailClosedTests(unittest.TestCase):
    def test_failure_of_a_prevents_execution_of_its_dependent(self):
        calls = []

        def fake_execute_progressively(**kwargs):
            worker_ordinal = kwargs["worker_ordinal"]
            calls.append(worker_ordinal)
            if worker_ordinal == 1:
                return {
                    "status": "FAILED",
                    "reason": "WORKER_EXECUTION_FAILED",
                    "worker": {"status": "FAILED", "error": "boom"},
                }
            return {"status": "COMPLETED", "worker": {"status": "COMPLETED"}}

        workers = [
            _worker("A", depends_on=[]),
            _worker("B", depends_on=[0]),
        ]

        with patch(
            "nexus.orchestration.go.execute_progressively",
            side_effect=fake_execute_progressively,
        ), patch("nexus.orchestration.go.update_run_status"), patch(
            "nexus.orchestration.go.record_checkpoint"
        ):
            with self.assertRaises(GoError) as ctx:
                _run_workers_by_round(
                    run_id="RUN-DEP-FAIL",
                    repo="C:/fake/repo",
                    manager_id="MANAGER-1",
                    request_text="do the thing",
                    workers=workers,
                    plan_risk="LOW",
                )

        self.assertEqual(ctx.exception.code, "WORKER_EXECUTION_FAILED")
        # B (depends_on=[0]) must never have been submitted at all.
        self.assertEqual(calls, [1])

    def test_independent_sibling_already_started_finishes_normally_after_failure(
        self,
    ):
        """Requirement 8: no unsafe cancellation of an already-started
        same-round Worker. A (independent) fails almost immediately; B
        (also independent, same round) is still running and must be
        allowed to finish naturally before the Run is aborted."""
        b_started = threading.Event()
        b_allowed_to_finish = threading.Event()
        b_finished = threading.Event()

        def fake_execute_progressively(**kwargs):
            worker_ordinal = kwargs["worker_ordinal"]
            if worker_ordinal == 1:
                # Let B know it's safe to report "started", then fail
                # immediately.
                b_started.wait(timeout=5)
                return {
                    "status": "FAILED",
                    "reason": "WORKER_EXECUTION_FAILED",
                    "worker": {"status": "FAILED", "error": "boom"},
                }

            # worker_ordinal == 2 (B)
            b_started.set()
            b_allowed_to_finish.wait(timeout=5)
            b_finished.set()
            return {"status": "COMPLETED", "worker": {"status": "COMPLETED"}}

        # Release B shortly after both Workers have been submitted so it
        # is guaranteed to still be "in flight" when A's failure is
        # observed by the scheduler.
        def _release_b_later():
            b_started.wait(timeout=5)
            b_allowed_to_finish.set()

        releaser = threading.Thread(target=_release_b_later)
        releaser.start()

        workers = [_worker("A", depends_on=[]), _worker("B", depends_on=[])]

        with patch(
            "nexus.orchestration.go.execute_progressively",
            side_effect=fake_execute_progressively,
        ), patch("nexus.orchestration.go.update_run_status"), patch(
            "nexus.orchestration.go.record_checkpoint"
        ):
            with self.assertRaises(GoError):
                _run_workers_by_round(
                    run_id="RUN-SIBLING-FINISH",
                    repo="C:/fake/repo",
                    manager_id="MANAGER-1",
                    request_text="do the thing",
                    workers=workers,
                    plan_risk="LOW",
                )

        releaser.join(timeout=5)
        self.assertTrue(b_finished.is_set())

    def test_no_worker_executes_twice(self):
        call_counts: dict[int, int] = {}
        lock = threading.Lock()

        def fake_execute_progressively(**kwargs):
            worker_ordinal = kwargs["worker_ordinal"]
            with lock:
                call_counts[worker_ordinal] = call_counts.get(worker_ordinal, 0) + 1
            return {"status": "COMPLETED", "worker": {"status": "COMPLETED"}}

        workers = [
            _worker("A", depends_on=[]),
            _worker("B", depends_on=[]),
            _worker("C", depends_on=[0, 1]),
        ]

        with patch(
            "nexus.orchestration.go.execute_progressively",
            side_effect=fake_execute_progressively,
        ):
            _run_workers_by_round(
                run_id="RUN-NO-DOUBLE-EXEC",
                repo="C:/fake/repo",
                manager_id="MANAGER-1",
                request_text="do the thing",
                workers=workers,
                plan_risk="LOW",
            )

        self.assertEqual(call_counts, {1: 1, 2: 1, 3: 1})

    def test_future_exception_preserves_real_cause(self):
        class _CustomWorkerBug(RuntimeError):
            pass

        def fake_execute_progressively(**kwargs):
            raise _CustomWorkerBug("a genuinely unexpected programming error")

        workers = [_worker("A", depends_on=[])]

        with patch(
            "nexus.orchestration.go.execute_progressively",
            side_effect=fake_execute_progressively,
        ):
            with self.assertRaises(_CustomWorkerBug) as ctx:
                _run_workers_by_round(
                    run_id="RUN-REAL-CAUSE",
                    repo="C:/fake/repo",
                    manager_id="MANAGER-1",
                    request_text="do the thing",
                    workers=workers,
                    plan_risk="LOW",
                )

        self.assertEqual(
            str(ctx.exception), "a genuinely unexpected programming error"
        )

    def test_checkpoint_write_error_is_translated_and_not_hidden(self):
        def fake_execute_progressively(**kwargs):
            raise CheckpointWriteError("disk full")

        workers = [_worker("A", depends_on=[])]

        with patch(
            "nexus.orchestration.go.execute_progressively",
            side_effect=fake_execute_progressively,
        ), patch("nexus.orchestration.go.update_run_status") as mock_status:
            with self.assertRaises(GoError) as ctx:
                _run_workers_by_round(
                    run_id="RUN-CKPT-ERR",
                    repo="C:/fake/repo",
                    manager_id="MANAGER-1",
                    request_text="do the thing",
                    workers=workers,
                    plan_risk="LOW",
                )

        self.assertEqual(ctx.exception.code, "CHECKPOINT_PERSISTENCE_FAILED")
        mock_status.assert_called_with("RUN-CKPT-ERR", "BLOCKED")

    def test_deadlock_no_progress_fails_closed(self):
        """Defensive branch: if eligible_worker_batch() ever returns an
        empty batch while work remains (should be unreachable given the
        DAG was already validated acyclic upstream), the scheduler must
        fail closed with PLAN_INVALID instead of hanging or looping
        forever."""
        # A dependency graph with a cycle that (hypothetically) slipped
        # past validation -- eligible_worker_batch() will never return
        # either index, so no progress is ever possible.
        workers = [_worker("A", depends_on=[1]), _worker("B", depends_on=[0])]

        with patch("nexus.orchestration.go.update_run_status") as mock_status:
            with self.assertRaises(GoError) as ctx:
                _run_workers_by_round(
                    run_id="RUN-DEADLOCK",
                    repo="C:/fake/repo",
                    manager_id="MANAGER-1",
                    request_text="do the thing",
                    workers=workers,
                    plan_risk="LOW",
                )

        self.assertEqual(ctx.exception.code, "PLAN_INVALID")
        mock_status.assert_called_with("RUN-DEADLOCK", "BLOCKED")


class ConcurrentCheckpointWriteTests(_RealDatabaseTestCase):
    """Requirement 9: two Workers registering checkpoints concurrently
    against the real (throwaway, temp-file) sqlite persistence layer must
    never corrupt data, collide sequences, or lose a checkpoint."""

    def test_concurrent_checkpoint_writes_do_not_collide_or_lose_data(self):
        run_id = create_run(
            project_id="norte",
            input_text="concurrent checkpoint stress",
            intent="GO",
            status="RUNNING",
        )

        thread_count = 8
        errors = []

        def _write_one(worker_ordinal: int) -> None:
            try:
                record_checkpoint(
                    run_id=run_id,
                    boundary="WORKER_ATTEMPT",
                    payload={"worker_ordinal": worker_ordinal},
                    worker_ordinal=worker_ordinal,
                )
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [
            threading.Thread(target=_write_one, args=(i,))
            for i in range(1, thread_count + 1)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])

        checkpoints = list_checkpoints(run_id)
        self.assertEqual(len(checkpoints), thread_count)

        sequences = [c["sequence"] for c in checkpoints]
        self.assertEqual(len(sequences), len(set(sequences)))

        recorded_ordinals = sorted(
            c["payload"]["worker_ordinal"] for c in checkpoints
        )
        self.assertEqual(recorded_ordinals, list(range(1, thread_count + 1)))


class ResumeConcurrentRunGateTests(_RealDatabaseTestCase):
    """Requirement 13: a partially executed round-based (concurrent) Run
    must fail closed on resume with a clear diagnostic, never silently
    re-execute Workers or corrupt state. A single-Worker ("sequential")
    Run must be entirely unaffected."""

    def test_resume_of_round_based_run_fails_closed(self):
        from nexus.orchestration.resume import ResumeError, resume_run
        from nexus.orchestration.ownership import establish_ownership
        from nexus.registry.checkpoints import record_approved_plan

        run_id = create_run(
            project_id="norte",
            input_text="two independent workers",
            intent="GO",
            status="RUNNING",
        )
        establish_ownership(run_id)

        plan = _plan(
            [_worker("A", depends_on=[]), _worker("B", depends_on=[])]
        )
        record_approved_plan(run_id, plan)
        record_checkpoint(
            run_id=run_id,
            boundary="PLAN_APPROVED",
            payload={
                "manager_id": "AGT-0001",
                "risk": "LOW",
                "complexity": "LOW",
                "parallelism": 1,
                "worker_count": 2,
                "execution_model": "round_based",
                "risk_consistency_notice": None,
            },
        )

        with self.assertRaises(ResumeError) as ctx:
            resume_run(run_id)

        self.assertEqual(ctx.exception.code, "CONCURRENT_RESUME_UNSUPPORTED")


class CriticalRiskHardGateUnaffectedTests(unittest.TestCase):
    """Requirement 12/14: Phase 2 must not add, weaken, or otherwise
    touch the CRITICAL risk hard gate. No approved route may carry
    max_risk_level == "critical" -- verified directly against the live
    catalog Phase 2 did not modify."""

    def test_no_approved_route_permits_critical_risk(self):
        from nexus.routing.catalog import APPROVED_CATALOG

        for route in APPROVED_CATALOG:
            self.assertNotEqual(
                getattr(route, "max_risk_level", None),
                "critical",
                f"Route {route!r} must never permit CRITICAL risk.",
            )


if __name__ == "__main__":
    unittest.main()
