import json
import unittest

from nexus.engineering import (EngineeringContext, EngineeringProposalValidationError,
                               ProposalEngine, ProposalStatus)


class EngineeringProposalTests(unittest.TestCase):
    def context(self):
        return EngineeringContext.build(mission_id="M-1", task_id="T-1", objective="Reduce parser risk",
            affected_files=("src/parser.py",), evidence=("finding:F-1",), risks=("regression",),
            validation_requirements=("unit tests",), created_at="2026-01-01T00:00:00Z")

    def test_creation_and_context_linkage(self):
        proposal = ProposalEngine().generate(self.context())
        self.assertTrue(proposal.proposal_id.startswith("EP-"))
        self.assertEqual(proposal.context_id, self.context().context_id)
        self.assertEqual(proposal.status, ProposalStatus.CREATED)

    def test_deterministic_serialization(self):
        engine = ProposalEngine()
        self.assertEqual(engine.generate(self.context()).to_json(), engine.generate(self.context()).to_json())
        self.assertEqual(json.loads(engine.generate(self.context()).to_json()), engine.generate(self.context()).to_dict())

    def test_invalid_proposal_rejection(self):
        with self.assertRaises(EngineeringProposalValidationError):
            engine = ProposalEngine()
            engine.generate(None)

    def test_no_side_effects_or_execution(self):
        proposal = ProposalEngine().generate(self.context())
        self.assertFalse(any(name in dir(proposal) for name in ("execute", "invoke", "dispatch")))
        self.assertEqual(proposal, ProposalEngine().generate(self.context()))


if __name__ == "__main__":
    unittest.main()
