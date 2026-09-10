import unittest

from nexus.intelligence import (
    FindingSeverity, IntelligenceFinding, IntelligenceScanResult,
    IntelligenceValidationError, MissionRecommendation, RepositorySnapshot, ScanStatus,
)


STAMP = "2026-09-09T12:00:00+00:00"


def finding():
    return IntelligenceFinding("F-1", "Issue", "Details", FindingSeverity.HIGH, .8, ("src/a.py:4",), STAMP)


class IntelligenceModelsTest(unittest.TestCase):
    def test_models_serialize_deterministically_and_enums_as_values(self):
        snapshot = RepositorySnapshot("repo", "abc123", STAMP, ("src/a.py",))
        scan = IntelligenceScanResult("S-1", snapshot, ScanStatus.COMPLETED, (finding(),), STAMP, STAMP)
        self.assertEqual(scan.to_json(), scan.to_json())
        self.assertIn('"status":"COMPLETED"', scan.to_json())
        self.assertEqual(list(scan.to_dict()), sorted(scan.to_dict()))

    def test_invalid_states_are_rejected(self):
        with self.assertRaises(IntelligenceValidationError):
            IntelligenceFinding("F", "title", "description", FindingSeverity.LOW, 1.1, ("e",), STAMP)
        with self.assertRaises(IntelligenceValidationError):
            IntelligenceFinding("F", "title", "description", FindingSeverity.LOW, .5, (), STAMP)
        with self.assertRaises(IntelligenceValidationError):
            IntelligenceScanResult("S", RepositorySnapshot("r", "rev", STAMP), ScanStatus.COMPLETED, (), STAMP)
        with self.assertRaises(IntelligenceValidationError):
            RepositorySnapshot(None, "rev", STAMP)

    def test_recommendations_require_evidence_and_support_confidence(self):
        recommendation = MissionRecommendation("R-1", "M-1", "Prioritize review", FindingSeverity.MEDIUM, .9, ("F-1",), STAMP)
        self.assertEqual(recommendation.to_dict()["confidence"], .9)
        with self.assertRaises(IntelligenceValidationError):
            MissionRecommendation("R-1", "M-1", "Prioritize review", FindingSeverity.MEDIUM, .9, (), STAMP)


if __name__ == "__main__":
    unittest.main()
