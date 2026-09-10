import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nexus.intelligence.audit import (IntelligenceAuditRecord, IntelligenceAuditRegistry,
                                       record_analysis, record_mission_creation)
from nexus.intelligence.errors import IntelligenceValidationError


class IntelligenceAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.patches = patch.multiple("nexus.registry.database", STORAGE_DIR=root,
                                     DATABASE_PATH=root / "audit.db")
        self.patches.start()

    def tearDown(self):
        self.patches.stop()
        self.tmp.cleanup()

    def test_creation_and_deterministic_serialization(self):
        record = record_analysis("repo", "SCAN-1", 2, 1, timestamp="2026-01-01T00:00:00+00:00", audit_id="A-1")
        self.assertEqual(record, IntelligenceAuditRegistry().list()[0])
        self.assertEqual(record.to_json(), '{"audit_id":"A-1","findings_count":2,"mission_id":null,"recommendation_count":1,"repository":"repo","scan_reference":"SCAN-1","status":"ANALYZED","timestamp":"2026-01-01T00:00:00+00:00"}')

    def test_mission_linkage_does_not_persist_mission(self):
        linked = record_mission_creation(record_analysis("repo", "SCAN-1", audit_id="A-1", timestamp="2026-01-01T00:00:00Z"), "MISSION-1")
        self.assertEqual(linked.mission_id, "MISSION-1")
        from nexus.registry.database import get_connection
        with get_connection() as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("missions", tables)

    def test_invalid_records(self):
        with self.assertRaises(IntelligenceValidationError):
            IntelligenceAuditRecord("A", "bad", "repo", "scan", 0, 0)
        with self.assertRaises(IntelligenceValidationError):
            IntelligenceAuditRecord("A", "2026-01-01T00:00:00Z", "repo", "scan", -1, 0)


if __name__ == "__main__":
    unittest.main()
