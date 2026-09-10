import tempfile
import unittest
from pathlib import Path
from nexus.intelligence import IntelligenceService


class IntelligenceServiceTest(unittest.TestCase):
    def test_invalid_repository_is_rejected(self):
        with self.assertRaises(ValueError):
            IntelligenceService().analyze_repository(Path(tempfile.gettempdir()) / "missing-nexus-repository")

    def test_pipeline_returns_report(self):
        report = IntelligenceService().analyze_repository(".")
        self.assertEqual(report.snapshot.repository_id, report.snapshot.repository_id)
        self.assertEqual(len(report.risks), len(report.recommendations))


if __name__ == "__main__":
    unittest.main()
