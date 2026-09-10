import json
import unittest

from nexus.engineering import (ChangeProposal, EngineeringAgent, EngineeringExecutionProposal,
                                EngineeringProposalValidationError, ImplementationPlan,
                                generate_change_proposal)


class EngineeringAgentTests(unittest.TestCase):
    def change(self):
        plan = ImplementationPlan("IP-1", "EP-1", "Improve parser", ("inspect", "test"),
            ("src/parser.py",), ("Python 3.12",), ("unit tests",), "Restore prior state",
            ("Tests pass", "Review approves"), "low", "2026-01-01T00:00:00Z")
        return generate_change_proposal(plan)

    def test_generation_and_linkage(self):
        result = EngineeringAgent().prepare_execution(self.change())
        self.assertIsInstance(result, EngineeringExecutionProposal)
        self.assertEqual(result.change_id, "CP-" + self.change().change_id[3:])
        self.assertTrue(result.approval_required)

    def test_deterministic_serialization(self):
        first = EngineeringAgent().prepare_execution(self.change())
        second = EngineeringExecutionProposal(**first.to_dict())
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(json.loads(first.to_json()), first.to_dict())

    def test_invalid_and_no_side_effects(self):
        with self.assertRaises(EngineeringProposalValidationError):
            EngineeringAgent().prepare_execution(None)
        proposal = self.change().to_dict()
        for field in ("target_files", "expected_tests", "rollback_strategy"):
            values = dict(proposal)
            values[field] = ()
            with self.subTest(field=field), self.assertRaises(EngineeringProposalValidationError):
                ChangeProposal(**values)
        self.assertFalse(any(name in dir(EngineeringAgent()) for name in ("execute", "apply", "run", "commit")))


if __name__ == "__main__":
    unittest.main()
