import json
import unittest

from nexus.engineering import EngineeringPlanValidationError, ImplementationPlan


class ImplementationPlanTests(unittest.TestCase):
    def plan(self):
        return ImplementationPlan("IP-1", "EP-1", "Improve parser", ("inspect", "test"),
            ("src/parser.py",), ("Python 3.12",), ("unit tests",), "Restore prior state",
            ("Tests pass", "Review approves"), "low", "2026-01-01T00:00:00Z")

    def test_creation_and_proposal_linkage(self):
        plan = self.plan()
        self.assertEqual(plan.plan_id, "IP-1")
        self.assertEqual(plan.proposal_id, "EP-1")

    def test_deterministic_serialization(self):
        first = self.plan()
        second = ImplementationPlan(**first.to_dict())
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(json.loads(first.to_json()), first.to_dict())

    def test_invalid_plan_rejection(self):
        for field, value in (("proposal_id", ""), ("objective", ""),
                             ("ordered_steps", ()), ("affected_files", ()),
                             ("validation_steps", ()), ("acceptance_criteria", ())):
            values = self.plan().to_dict()
            values[field] = value
            with self.subTest(field=field), self.assertRaises(EngineeringPlanValidationError):
                ImplementationPlan(**values)

    def test_acceptance_criteria_preserved(self):
        self.assertEqual(self.plan().acceptance_criteria, ("Tests pass", "Review approves"))

    def test_no_execution_side_effects(self):
        plan = self.plan()
        self.assertFalse(any(name in dir(plan) for name in ("execute", "invoke", "dispatch")))
        self.assertEqual(plan, self.plan())


if __name__ == "__main__":
    unittest.main()
