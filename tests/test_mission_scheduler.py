"""Tests for the Nexus Mission Dependency Scheduler V1 (nexus/missions/scheduler.py)."""

import unittest
from dataclasses import dataclass, field
from typing import Optional

from nexus.missions.scheduler import (
    MissionDependencyError,
    dependency_ancestors,
    has_incomplete_work,
    is_eligible,
    next_eligible_task,
    validate_dependencies,
)


@dataclass
class FakeTask:
    task_id: str
    mission_id: str
    status: str = "CREATED"
    dependencies: Optional[list] = field(default_factory=list)


class ValidateDependenciesTestCase(unittest.TestCase):
    def test_linear_chain_is_valid(self):
        a = FakeTask("A", "M1", dependencies=[])
        b = FakeTask("B", "M1", dependencies=["A"])
        c = FakeTask("C", "M1", dependencies=["B"])
        validate_dependencies("M1", [a, b, c], {"A": a, "B": b, "C": c})

    def test_none_dependencies_means_no_dependency(self):
        a = FakeTask("A", "M1", dependencies=None)
        validate_dependencies("M1", [a], {"A": a})

    def test_self_dependency_rejected(self):
        a = FakeTask("A", "M1", dependencies=["A"])
        with self.assertRaises(MissionDependencyError):
            validate_dependencies("M1", [a], {"A": a})

    def test_duplicate_dependency_rejected(self):
        a = FakeTask("A", "M1", dependencies=[])
        b = FakeTask("B", "M1", dependencies=["A", "A"])
        with self.assertRaises(MissionDependencyError):
            validate_dependencies("M1", [a, b], {"A": a, "B": b})

    def test_unknown_dependency_rejected(self):
        a = FakeTask("A", "M1", dependencies=["GHOST"])
        with self.assertRaises(MissionDependencyError) as ctx:
            validate_dependencies("M1", [a], {"A": a})
        self.assertIn("unknown", str(ctx.exception).lower())

    def test_cross_mission_dependency_explicitly_rejected(self):
        other = FakeTask("X", "M2", dependencies=[])
        a = FakeTask("A", "M1", dependencies=["X"])
        with self.assertRaises(MissionDependencyError) as ctx:
            validate_dependencies("M1", [a], {"A": a, "X": other})
        self.assertIn("cross-mission", str(ctx.exception).lower())

    def test_cycle_rejected(self):
        a = FakeTask("A", "M1", dependencies=["B"])
        b = FakeTask("B", "M1", dependencies=["A"])
        with self.assertRaises(MissionDependencyError):
            validate_dependencies("M1", [a, b], {"A": a, "B": b})

    def test_task_object_owned_by_other_mission_is_rejected(self):
        foreign = FakeTask("X", "M2", dependencies=[])
        with self.assertRaises(MissionDependencyError) as ctx:
            validate_dependencies("M1", [foreign], {"X": foreign})
        self.assertIn("M2", str(ctx.exception))
        self.assertIn("M1", str(ctx.exception))


class EligibilityTestCase(unittest.TestCase):
    def test_no_dependency_task_is_eligible(self):
        a = FakeTask("A", "M1", status="CREATED", dependencies=[])
        self.assertTrue(is_eligible(a, {"A": a}))

    def test_created_dependency_blocks(self):
        a = FakeTask("A", "M1", status="CREATED")
        b = FakeTask("B", "M1", status="CREATED", dependencies=["A"])
        self.assertFalse(is_eligible(b, {"A": a, "B": b}))

    def test_completed_dependency_unlocks(self):
        a = FakeTask("A", "M1", status="COMPLETED")
        b = FakeTask("B", "M1", status="CREATED", dependencies=["A"])
        self.assertTrue(is_eligible(b, {"A": a, "B": b}))

    def test_failed_dependency_blocks(self):
        a = FakeTask("A", "M1", status="FAILED")
        b = FakeTask("B", "M1", status="CREATED", dependencies=["A"])
        self.assertFalse(is_eligible(b, {"A": a, "B": b}))

    def test_non_created_task_not_eligible(self):
        a = FakeTask("A", "M1", status="COMPLETED", dependencies=[])
        self.assertFalse(is_eligible(a, {"A": a}))

    def test_deterministic_independent_order(self):
        a = FakeTask("A", "M1", status="CREATED", dependencies=[])
        b = FakeTask("B", "M1", status="CREATED", dependencies=[])
        tasks_by_id = {"A": a, "B": b}
        self.assertEqual(next_eligible_task([a, b], tasks_by_id).task_id, "A")
        self.assertEqual(next_eligible_task([b, a], tasks_by_id).task_id, "B")

    def test_has_incomplete_work(self):
        a = FakeTask("A", "M1", status="COMPLETED")
        b = FakeTask("B", "M1", status="CREATED")
        self.assertTrue(has_incomplete_work([a, b]))
        self.assertFalse(has_incomplete_work([a]))


class DependencyAncestorsTestCase(unittest.TestCase):
    def test_linear_chain_ancestors(self):
        a = FakeTask("A", "M1", dependencies=[])
        b = FakeTask("B", "M1", dependencies=["A"])
        c = FakeTask("C", "M1", dependencies=["B"])
        tasks_by_id = {"A": a, "B": b, "C": c}
        self.assertEqual(dependency_ancestors(c, tasks_by_id), {"A", "B"})

    def test_ancestor_only_excludes_independent_task(self):
        a = FakeTask("A", "M1", dependencies=[])
        b = FakeTask("B", "M1", dependencies=[])
        c = FakeTask("C", "M1", dependencies=["A"])
        tasks_by_id = {"A": a, "B": b, "C": c}
        ancestors = dependency_ancestors(c, tasks_by_id)
        self.assertIn("A", ancestors)
        self.assertNotIn("B", ancestors)


if __name__ == "__main__":
    unittest.main()
