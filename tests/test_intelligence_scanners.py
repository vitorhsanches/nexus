import tempfile
import unittest
from pathlib import Path

from nexus.intelligence.scanners import GitScanner, RepositoryScanner


class RepositoryScannerTest(unittest.TestCase):
    def test_scan_is_deterministic_and_generates_findings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "main.py").write_text("import os\n" + "if True:\n    pass\n" * 30, encoding="utf-8")
            first = RepositoryScanner(root).scan("2026-09-09T12:00:00+00:00")
            second = RepositoryScanner(root).scan("2026-09-09T12:00:00+00:00")
            self.assertEqual(first[0].to_json(), second[0].to_json())
            self.assertEqual(first[1].to_json(), second[1].to_json())
            self.assertTrue(first[1].findings)
            self.assertTrue(all(f.evidence_references for f in first[1].findings))

    def test_invalid_repository_handling(self):
        with self.assertRaises(ValueError): RepositoryScanner("does-not-exist").scan()

    def test_snapshot_contains_repository_files_and_git_shape(self):
        root = Path.cwd()
        snapshot, result = RepositoryScanner(root).scan("2026-09-09T12:00:00+00:00")
        self.assertTrue(snapshot.file_references)
        self.assertEqual(result.snapshot, snapshot)
        git = GitScanner(root).scan()
        self.assertEqual(git["HEAD"], git["revision"])
        self.assertIsInstance(git["dirty"], bool)


if __name__ == "__main__": unittest.main()
