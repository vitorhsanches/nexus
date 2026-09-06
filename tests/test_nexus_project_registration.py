import json
import unittest
from pathlib import Path

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.router import resolve_project


REAL_PROJECTS_FILE = (
    Path(__file__).resolve().parents[1] / "config" / "projects.json"
)


class RealProjectsRegistryTestCase(unittest.TestCase):
    """Verifies Nexus itself is registered in the real config/projects.json."""

    def setUp(self) -> None:
        with REAL_PROJECTS_FILE.open("r", encoding="utf-8-sig") as file:
            self._payload = json.load(file)

        self._original_db_path = database.DATABASE_PATH
        self._original_storage_dir = database.STORAGE_DIR
        self._original_projects_file = projects.PROJECTS_FILE

        import tempfile

        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp_dir.name)

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus-test.db"
        projects.PROJECTS_FILE = REAL_PROJECTS_FILE

        database.initialize_database()
        projects.sync_projects()

    def tearDown(self) -> None:
        database.DATABASE_PATH = self._original_db_path
        database.STORAGE_DIR = self._original_storage_dir
        projects.PROJECTS_FILE = self._original_projects_file
        self._tmp_dir.cleanup()

    def _find_nexus_entry(self):
        for project in self._payload["projects"]:
            if project["id"] == "nexus":
                return project
        return None

    def test_nexus_entry_present_in_config_file(self):
        entry = self._find_nexus_entry()
        self.assertIsNotNone(
            entry, "Nexus must be registered in config/projects.json"
        )
        self.assertEqual(entry["name"], "Nexus")
        self.assertTrue(entry["enabled"])
        self.assertTrue(entry["path"].endswith("nexus"))

    def test_nexus_generic_aliases_are_not_present(self):
        entry = self._find_nexus_entry()
        forbidden = {"task", "tasks", "project", "code", "app", "development"}
        aliases = {alias.lower() for alias in entry.get("aliases", [])}
        self.assertTrue(
            forbidden.isdisjoint(aliases),
            f"Nexus must not use generic aliases, found: {aliases & forbidden}",
        )

    def test_nexus_resolves_explicitly_via_router(self):
        result = resolve_project("nexus")
        self.assertEqual(result.id, "nexus")
        self.assertEqual(result.name, "Nexus")
        self.assertTrue(result.path.endswith("nexus"))

    def test_nexus_resolves_via_name(self):
        result = resolve_project("Nexus")
        self.assertEqual(result.id, "nexus")

    def test_norte_and_poc_still_present(self):
        ids = {project["id"] for project in self._payload["projects"]}
        self.assertIn("norte", ids)
        self.assertIn("orchestrator-poc", ids)


if __name__ == "__main__":
    unittest.main()
