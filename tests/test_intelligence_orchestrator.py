import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nexus.missions.service as mission_service
import nexus.tasks.registry as task_registry
from nexus.intelligence.errors import IntelligenceValidationError
from nexus.intelligence.orchestrator import IntelligenceOrchestrator


class IntelligenceOrchestratorTests(unittest.TestCase):
    def setUp(self):
        mission_service._missions.clear()
        task_registry._tasks.clear()

    def test_complete_pipeline_is_non_executing_and_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("module.py", "other.py", "third.py"):
                Path(directory, name).write_text("print('ok')\n", encoding="utf-8")
            with patch("nexus.missions.persistence.persist_mission_bundle"):
                result = IntelligenceOrchestrator().analyze_and_prepare_mission(directory, "RUN-1")
        self.assertEqual(len(result.tasks), 5)
        self.assertEqual(result.tasks, tuple(result.mission.tasks))
        self.assertTrue(set(result.proposal.source_findings).issubset(
            {e for finding in result.report.findings for e in finding.evidence_references}
        ) or not result.report.findings)
        self.assertEqual(result.mission.status, "CREATED")
        self.assertFalse(task_registry.list_attempts())

    def test_invalid_repository_and_non_single_recommendation_leave_registries_empty(self):
        with self.assertRaises(IntelligenceValidationError):
            IntelligenceOrchestrator().analyze_and_prepare_mission("does-not-exist", "RUN-1")
        self.assertFalse(mission_service._missions)
        self.assertFalse(task_registry._tasks)

        service = type("Service", (), {"analyze_repository": lambda self, path: object()})()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(IntelligenceValidationError):
                IntelligenceOrchestrator(service).analyze_and_prepare_mission(directory, "RUN-1")
        self.assertFalse(mission_service._missions)
        self.assertFalse(task_registry._tasks)


if __name__ == "__main__":
    unittest.main()
