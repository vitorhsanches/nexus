import unittest
from nexus.intelligence import IntelligenceService


class IntelligenceReportTest(unittest.TestCase):
    def test_report_serialization_is_deterministic(self):
        report = IntelligenceService().analyze_repository(".")
        self.assertEqual(report.to_json(), report.to_json())
        self.assertIn("recommendations", report.to_dict())


if __name__ == "__main__":
    unittest.main()
