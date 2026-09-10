import unittest
from nexus.intelligence import (EvidenceEngine, FindingSeverity, IntelligenceFinding,
    IntelligenceScanResult, RepositorySnapshot, RiskEngine, ScanStatus)

STAMP = "2026-09-09T12:00:00+00:00"

def scan():
    snapshot = RepositorySnapshot("repo", "rev", STAMP)
    findings = (
        IntelligenceFinding("F-low", "change", "working tree change", FindingSeverity.LOW, .9, ("a.py:2",), STAMP),
        IntelligenceFinding("F-high", "complexity hotspot", "complex dependency coupling", FindingSeverity.HIGH, .9, ("src/a.py:4", "src/b.py"), STAMP),
    )
    return IntelligenceScanResult("S-1", snapshot, ScanStatus.COMPLETED, findings, STAMP, STAMP)

class RiskEvidenceTest(unittest.TestCase):
    def test_scores_are_deterministic_and_severity_orders_results(self):
        first = RiskEngine().assess(scan())
        self.assertEqual(first, RiskEngine().assess(scan()))
        self.assertEqual(first[0].finding_id, "F-high")

    def test_evidence_is_structured(self):
        evidence = EvidenceEngine().generate(scan())
        self.assertEqual(evidence[0].source_file, "a.py")
        self.assertEqual(evidence[0].line_reference, "2")
        self.assertEqual(evidence[1].evidence_type, "complexity")

    def test_invalid_input(self):
        with self.assertRaises(ValueError):
            RiskEngine().assess(None)
        with self.assertRaises(ValueError):
            EvidenceEngine().generate(object())

if __name__ == "__main__":
    unittest.main()
