import unittest

from nexus.engineering.errors import EngineeringProposalValidationError
from nexus.engineering.review import (
    EngineeringReviewDecision, ReviewDecisionStatus, approve_execution,
    create_pending_review, reject_execution,
)


class EngineeringReviewTests(unittest.TestCase):
    def test_pending_review_creation(self):
        review = create_pending_review("EE-1", "RV-1", "2026-01-01T00:00:00Z")
        self.assertEqual(review.decision, ReviewDecisionStatus.PENDING)
        self.assertEqual(review.execution_id, "EE-1")

    def test_approval_flow(self):
        review = approve_execution(create_pending_review("EE-1", "RV-1", "now"),
                                   "alice", ("files:a", "scope:tests"), reviewed_at="later")
        self.assertEqual(review.decision, ReviewDecisionStatus.APPROVED)

    def test_rejection_flow(self):
        review = reject_execution(create_pending_review("EE-1", "RV-1", "now"), "alice", "unsafe")
        self.assertEqual(review.decision, ReviewDecisionStatus.REJECTED)

    def test_invalid_approvals(self):
        with self.assertRaises(EngineeringProposalValidationError):
            EngineeringReviewDecision("RV-1", "EE-1", "APPROVED", "", "", ("scope",), "now", "")
        with self.assertRaises(EngineeringProposalValidationError):
            EngineeringReviewDecision("RV-1", "", ReviewDecisionStatus.PENDING, "", "", (), "now", "")

    def test_deterministic_serialization(self):
        review = create_pending_review("EE-1", "RV-1", "now")
        self.assertEqual(review.to_json(), review.to_json())
        self.assertEqual(review.to_dict()["approval_scope"], [])

    def test_no_execution_side_effects(self):
        review = create_pending_review("EE-1", "RV-1", "now")
        self.assertEqual(review.execution_id, "EE-1")
        self.assertEqual(review.decision, ReviewDecisionStatus.PENDING)


if __name__ == "__main__":
    unittest.main()
