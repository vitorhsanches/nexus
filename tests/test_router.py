import json
import tempfile
import unittest
from pathlib import Path

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.router import (
    ProjectAmbiguousError,
    ProjectNotFoundError,
    resolve_project,
    resolve_project_from_text,
)


FIXTURE_PROJECTS = {
    "projects": [
        {
            "id": "norte",
            "name": "Norte",
            "path": r"C:\fake\interface-life",
            "aliases": ["norte", "interface life", "today"],
            "enabled": True,
        },
        {
            "id": "orchestrator-poc",
            "name": "Orchestrator POC",
            "path": r"C:\fake\codex-omniroute-poc",
            "aliases": ["poc", "orchestrator poc", "codex poc"],
            "enabled": True,
        },
        {
            "id": "sul",
            "name": "Sul",
            "path": r"C:\fake\sul",
            "aliases": ["today"],
            "enabled": True,
        },
    ]
}


class RouterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp_dir.name)

        self._original_db_path = database.DATABASE_PATH
        self._original_storage_dir = database.STORAGE_DIR
        self._original_projects_file = projects.PROJECTS_FILE

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus-test.db"

        fixture_path = tmp_path / "projects.json"
        fixture_path.write_text(
            json.dumps(FIXTURE_PROJECTS), encoding="utf-8"
        )
        projects.PROJECTS_FILE = fixture_path

        database.initialize_database()
        projects.sync_projects()

    def tearDown(self) -> None:
        database.DATABASE_PATH = self._original_db_path
        database.STORAGE_DIR = self._original_storage_dir
        projects.PROJECTS_FILE = self._original_projects_file
        self._tmp_dir.cleanup()

    def test_norte_explicit_selection(self):
        result = resolve_project("norte")
        self.assertEqual(result.id, "norte")

    def test_norte_explicit_name_selection(self):
        result = resolve_project("Norte")
        self.assertEqual(result.id, "norte")

    def test_alias_selection(self):
        result = resolve_project("interface life")
        self.assertEqual(result.id, "norte")

    def test_case_insensitive_matching(self):
        result = resolve_project("NORTE")
        self.assertEqual(result.id, "norte")

    def test_normalized_matching_extra_whitespace(self):
        result = resolve_project("  Orchestrator   POC  ")
        self.assertEqual(result.id, "orchestrator-poc")

    def test_orchestrator_poc_selection(self):
        result = resolve_project("poc")
        self.assertEqual(result.id, "orchestrator-poc")

    def test_no_match_raises_not_found(self):
        with self.assertRaises(ProjectNotFoundError):
            resolve_project("does-not-exist")

    def test_ambiguous_alias_raises_ambiguous(self):
        with self.assertRaises(ProjectAmbiguousError):
            resolve_project("today")

    def test_resolve_from_text_norte(self):
        result = resolve_project_from_text(
            "No Norte corrija o problema X"
        )
        self.assertEqual(result.id, "norte")

    def test_resolve_from_text_poc(self):
        result = resolve_project_from_text(
            "Run this on the codex poc repository"
        )
        self.assertEqual(result.id, "orchestrator-poc")

    def test_resolve_from_text_no_match(self):
        with self.assertRaises(ProjectNotFoundError):
            resolve_project_from_text(
                "Do something unrelated to any project"
            )

    def test_resolve_from_text_ambiguous(self):
        with self.assertRaises(ProjectAmbiguousError):
            resolve_project_from_text(
                "today we need to check something"
            )


if __name__ == "__main__":
    unittest.main()
