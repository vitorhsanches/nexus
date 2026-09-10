import unittest
from unittest.mock import patch

from nexus.intelligence.mission_bridge import (
    IntelligenceMissionBridge, IntelligenceMissionProposal,
)
from nexus.intelligence.errors import IntelligenceValidationError
from nexus.intelligence.models import FindingSeverity, MissionRecommendation, RepositorySnapshot
from nexus.intelligence.report import IntelligenceReport


def report(recommendations):
    snapshot = RepositorySnapshot("repo", "rev", "2025-01-01T00:00:00+00:00")
    return IntelligenceReport(snapshot, snapshot.captured_at, (), (), tuple(recommendations), {})


def recommendation():
    return MissionRecommendation(
        "REC-1", "INTELLIGENCE-F-1", "Fix dependency drift", FindingSeverity.HIGH,
        .9, ("pyproject.toml", "evidence:42"), "2025-01-01T00:00:00+00:00",
        title="Fix dependency drift", objective="Bring dependencies within policy.",
        priority="HIGH", rationale="Outdated dependency", affected_modules=("nexus.intelligence",))


class IntelligenceMissionBridgeTests(unittest.TestCase):
    def test_recommendation_creates_mission_proposal(self):
        proposal = IntelligenceMissionBridge().proposals_from_report(report((recommendation(),)))[0]
        self.assertIsInstance(proposal, IntelligenceMissionProposal)
        self.assertEqual(proposal.title, "Fix dependency drift")
        self.assertEqual(proposal.priority, "HIGH")

    def test_evidence_is_preserved(self):
        proposal = IntelligenceMissionBridge().proposal_from_recommendation(recommendation())
        self.assertEqual(proposal.source_findings, ("evidence:42", "pyproject.toml"))

    def test_output_is_deterministic(self):
        bridge = IntelligenceMissionBridge()
        first = bridge.proposals_from_report(report((recommendation(),)))[0]
        second = bridge.proposals_from_report(report((recommendation(),)))[0]
        self.assertEqual(first, second)
        self.assertEqual(first.to_json(), second.to_json())

    def test_invalid_recommendation_is_rejected(self):
        with self.assertRaises(IntelligenceValidationError):
            IntelligenceMissionBridge().proposal_from_recommendation(object())

    def test_bridge_has_no_execution_side_effects(self):
        with patch("nexus.tasks.registry.create_task") as create_task:
            IntelligenceMissionBridge().proposals_from_report(report((recommendation(),)))
        create_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
