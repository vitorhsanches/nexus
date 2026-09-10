import tempfile
import unittest
from pathlib import Path

from nexus.intelligence import IntelligenceAnalyzer


class IntelligenceAnalyzerTest(unittest.TestCase):
    def test_analyzer_creates_evidenced_deterministic_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.py").write_text("import os\n" * 600, encoding="utf-8")
            (root / "b.py").write_text("import os\n", encoding="utf-8")
            (root / "c.py").write_text("import os\n", encoding="utf-8")
            first = IntelligenceAnalyzer(root).analyze()
            second = IntelligenceAnalyzer(root).analyze()
            self.assertTrue(first.findings)
            self.assertTrue(all(f.evidence_references for f in first.findings))
            self.assertEqual(first.to_json(), second.to_json())

    def test_empty_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            result = IntelligenceAnalyzer(temp).analyze()
            self.assertEqual(result.findings, ())

    def test_invalid_repository_handling(self):
        with self.assertRaises(ValueError):
            IntelligenceAnalyzer("does-not-exist").analyze()


if __name__ == "__main__":
    unittest.main()
