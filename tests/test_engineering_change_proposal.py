import json
import unittest

from nexus.engineering import (ChangeProposal, ChangeStatus, EngineeringProposalValidationError,
                                ImplementationPlan, generate_change_proposal)


class ChangeProposalTests(unittest.TestCase):
    def plan(self):
        return ImplementationPlan("IP-1", "EP-1", "Improve parser", ("inspect", "test"),
            ("src/parser.py",), ("Python 3.12",), ("unit tests",), "Restore prior state",
            ("Tests pass", "Review approves"), "low", "2026-01-01T00:00:00Z")

    def test_creation_and_plan_linkage(self):
        proposal = generate_change_proposal(self.plan())
        self.assertEqual(proposal.plan_id, "IP-1")
        self.assertEqual(proposal.status, ChangeStatus.CREATED)

    def test_deterministic_serialization(self):
        first = generate_change_proposal(self.plan())
        second = ChangeProposal(**first.to_dict())
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(json.loads(first.to_json()), first.to_dict())

    def test_validation_failures(self):
        with self.assertRaises(EngineeringProposalValidationError):
            generate_change_proposal(None)
        proposal = generate_change_proposal(self.plan()).to_dict()
        for field in ("target_files", "proposed_changes", "expected_tests", "rollback_strategy"):
            values = dict(proposal)
            values[field] = ()
            with self.subTest(field=field), self.assertRaises(EngineeringProposalValidationError):
                ChangeProposal(**values)

    def test_approval_required_and_no_side_effects(self):
        proposal = generate_change_proposal(self.plan())
        self.assertTrue(proposal.approval_required)
        self.assertFalse(any(name in dir(proposal) for name in ("execute", "invoke", "dispatch")))


if __name__ == "__main__":
    unittest.main()
