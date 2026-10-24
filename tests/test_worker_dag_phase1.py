"""Regression tests for Nexus Multi-Agent MVP Phase 1: PLAN -> DAG -> ELIGIBILITY.

Phase 1 adds a deterministic dependency graph over plan["workers"]
(integer "depends_on" indices) plus a pure eligibility-batch function a
later phase will use to dispatch independent Workers concurrently. This
phase changes NOTHING about execution: orchestration/go.py still runs
plan["workers"] sequentially, in order, exactly as before; the same is
true of execute_progressively(), AdaptiveRoutingService,
select_best_route(), hard gates, review, escalation, and recovery.

Covers both layers:
  - nexus.orchestration.dag: the new Worker-graph primitives
    (normalize_worker_dependencies, validate_worker_dependencies,
    eligible_worker_batch), plus the shared nexus.orchestration.graph
    cycle-detection primitive they (and nexus.missions.scheduler) both
    delegate to.
  - nexus.dispatchers.manager._validate_plan: the integration point
    where a Manager plan's depends_on graph is actually validated,
    including backward compatibility with plans that predate this field.
"""

import json
import unittest

from nexus.dispatchers.manager import _validate_plan
from nexus.missions.scheduler import (
    MissionDependencyError,
    validate_dependencies as validate_task_dependencies,
)
from nexus.orchestration.dag import (
    WorkerDependencyError,
    eligible_worker_batch,
    normalize_worker_dependencies,
    validate_worker_dependencies,
)
from nexus.orchestration.graph import GraphCycleError, detect_cycle


def _base_plan(workers, intent="EXECUTION"):
    return {
        "complexity": "MEDIUM",
        "risk": "LOW",
        "intent": intent,
        "parallelism": 1,
        "summary": "Test plan.",
        "workers": workers,
    }


def _worker(scope="do work", depends_on=None, **overrides):
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
    worker.update(overrides)
    return worker


class BackwardCompatibilityTests(unittest.TestCase):
    """Requirement 8: old plans without depends_on keep working unchanged."""

    def test_plan_without_depends_on_is_accepted(self):
        plan = _base_plan([_worker("A"), _worker("B")])
        validated = _validate_plan(json.loads(json.dumps(plan)))
        self.assertEqual(len(validated["workers"]), 2)

    def test_missing_depends_on_normalizes_to_empty_list(self):
        plan = _base_plan([_worker("A"), _worker("B")])
        validated = _validate_plan(json.loads(json.dumps(plan)))
        for worker in validated["workers"]:
            self.assertEqual(worker["depends_on"], [])

    def test_explicit_none_depends_on_normalizes_to_empty_list(self):
        workers = [_worker("A")]
        workers[0]["depends_on"] = None
        plan = _base_plan(workers)
        validated = _validate_plan(json.loads(json.dumps(plan)))
        self.assertEqual(validated["workers"][0]["depends_on"], [])

    def test_normalize_never_mutates_input(self):
        workers = [_worker("A")]
        normalized = normalize_worker_dependencies(workers)
        self.assertNotIn("depends_on", workers[0])
        self.assertEqual(normalized[0]["depends_on"], [])


class ValidWorkerGraphTests(unittest.TestCase):
    def test_two_independent_workers(self):
        workers = [_worker("A", depends_on=[]), _worker("B", depends_on=[])]
        validate_worker_dependencies(normalize_worker_dependencies(workers))

    def test_worker_depending_on_two_predecessors(self):
        workers = [
            _worker("A", depends_on=[]),
            _worker("B", depends_on=[]),
            _worker("C", depends_on=[0, 1]),
        ]
        validate_worker_dependencies(normalize_worker_dependencies(workers))

    def test_deterministic_order_preserved(self):
        workers = [
            _worker("A", depends_on=[]),
            _worker("B", depends_on=[]),
            _worker("C", depends_on=[0, 1]),
        ]
        plan = _base_plan(workers)
        validated = _validate_plan(json.loads(json.dumps(plan)))
        self.assertEqual(
            [w["scope"] for w in validated["workers"]], ["A", "B", "C"]
        )
        self.assertEqual(validated["workers"][2]["depends_on"], [0, 1])

    def test_full_plan_with_dependency_is_accepted_end_to_end(self):
        workers = [
            _worker("Analyze module A", depends_on=[]),
            _worker("Analyze module B", depends_on=[]),
            _worker("Integrate A and B", depends_on=[0, 1]),
        ]
        plan = _base_plan(workers)
        validated = _validate_plan(json.loads(json.dumps(plan)))
        self.assertEqual(len(validated["workers"]), 3)


class EligibleBatchTests(unittest.TestCase):
    """Matches the spec example exactly:
    0 depends_on [], 1 depends_on [], 2 depends_on [0, 1].
    """

    def setUp(self):
        self.workers = [
            _worker("A", depends_on=[]),
            _worker("B", depends_on=[]),
            _worker("C", depends_on=[0, 1]),
        ]

    def test_initial_eligible_batch(self):
        self.assertEqual(eligible_worker_batch(self.workers, set()), [0, 1])

    def test_eligible_batch_after_partial_completion(self):
        self.assertEqual(eligible_worker_batch(self.workers, {0}), [1])

    def test_eligible_batch_final(self):
        self.assertEqual(eligible_worker_batch(self.workers, {0, 1}), [2])

    def test_eligible_batch_when_all_completed_is_empty(self):
        self.assertEqual(eligible_worker_batch(self.workers, {0, 1, 2}), [])

    def test_eligible_batch_ignores_already_completed_worker_even_if_independent(
        self,
    ):
        # Worker 0 is independent but already completed -- must not be
        # returned again.
        self.assertEqual(eligible_worker_batch(self.workers, {0}), [1])


class InvalidWorkerGraphTests(unittest.TestCase):
    def test_nonexistent_dependency_index_rejected(self):
        workers = [_worker("A", depends_on=[5])]
        with self.assertRaises(WorkerDependencyError):
            validate_worker_dependencies(normalize_worker_dependencies(workers))

    def test_negative_dependency_index_rejected(self):
        workers = [_worker("A", depends_on=[]), _worker("B", depends_on=[-1])]
        with self.assertRaises(WorkerDependencyError):
            validate_worker_dependencies(normalize_worker_dependencies(workers))

    def test_self_dependency_rejected(self):
        workers = [_worker("A", depends_on=[0])]
        with self.assertRaises(WorkerDependencyError):
            validate_worker_dependencies(normalize_worker_dependencies(workers))

    def test_duplicate_dependency_rejected(self):
        workers = [
            _worker("A", depends_on=[]),
            _worker("B", depends_on=[0, 0]),
        ]
        with self.assertRaises(WorkerDependencyError):
            validate_worker_dependencies(normalize_worker_dependencies(workers))

    def test_simple_cycle_rejected(self):
        workers = [_worker("A", depends_on=[1]), _worker("B", depends_on=[0])]
        with self.assertRaises(WorkerDependencyError):
            validate_worker_dependencies(normalize_worker_dependencies(workers))

    def test_indirect_cycle_rejected(self):
        workers = [
            _worker("A", depends_on=[2]),
            _worker("B", depends_on=[0]),
            _worker("C", depends_on=[1]),
        ]
        with self.assertRaises(WorkerDependencyError):
            validate_worker_dependencies(normalize_worker_dependencies(workers))

    def test_bool_rejected_as_index_true(self):
        workers = [_worker("A", depends_on=[]), _worker("B", depends_on=[True])]
        with self.assertRaises(WorkerDependencyError):
            validate_worker_dependencies(normalize_worker_dependencies(workers))

    def test_bool_rejected_as_index_false(self):
        # False == 0 in Python, but must still be rejected: it is not an
        # int, even though it would coincidentally point at a real index.
        workers = [_worker("A", depends_on=[]), _worker("B", depends_on=[False])]
        with self.assertRaises(WorkerDependencyError):
            validate_worker_dependencies(normalize_worker_dependencies(workers))

    def test_non_integer_dependency_rejected(self):
        workers = [_worker("A", depends_on=[]), _worker("B", depends_on=["0"])]
        with self.assertRaises(WorkerDependencyError):
            validate_worker_dependencies(normalize_worker_dependencies(workers))

    def test_non_list_depends_on_rejected(self):
        workers = [_worker("A", depends_on=0)]
        with self.assertRaises(WorkerDependencyError):
            validate_worker_dependencies(normalize_worker_dependencies(workers))

    def test_invalid_graph_rejected_end_to_end_through_validate_plan(self):
        workers = [_worker("A", depends_on=[1]), _worker("B", depends_on=[0])]
        plan = _base_plan(workers)
        with self.assertRaises(ValueError):
            _validate_plan(json.loads(json.dumps(plan)))


class SharedGraphPrimitiveTests(unittest.TestCase):
    """Requirement 6: reuse, don't duplicate -- both the Task DAG
    (nexus.missions.scheduler) and the Worker DAG (nexus.orchestration.dag)
    delegate to the same nexus.orchestration.graph.detect_cycle."""

    def test_generic_detect_cycle_raises_on_cycle(self):
        with self.assertRaises(GraphCycleError):
            detect_cycle({"A": ["B"], "B": ["A"]})

    def test_generic_detect_cycle_passes_on_dag(self):
        detect_cycle({"A": [], "B": ["A"], "C": ["A", "B"]})

    def test_task_scheduler_cycle_still_raises_mission_dependency_error(self):
        # Confirms the missions/scheduler.py refactor to delegate to the
        # shared primitive preserved its original public contract.
        from dataclasses import dataclass, field
        from typing import Optional

        @dataclass
        class FakeTask:
            task_id: str
            mission_id: str
            status: str = "CREATED"
            dependencies: Optional[list] = field(default_factory=list)

        a = FakeTask("A", "M1", dependencies=["B"])
        b = FakeTask("B", "M1", dependencies=["A"])
        with self.assertRaises(MissionDependencyError):
            validate_task_dependencies("M1", [a, b], {"A": a, "B": b})


if __name__ == "__main__":
    unittest.main()
