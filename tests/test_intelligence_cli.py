import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nexus.cli.app import main


class IntelligenceCliTests(unittest.TestCase):
    def test_analysis_is_read_only_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.argv", ["nexus", "intelligence", "analyze", directory]), \
                    patch("nexus.cli.app.IntelligenceOrchestrator") as orchestrator:
                orchestrator.return_value.analyze.return_value = (type("R", (), {
                    "findings": (), "risks": (), "recommendations": ()})(), ())
                main()
                orchestrator.return_value.mission_bridge.materialize_intelligence_mission.assert_not_called()

    def test_create_mission_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            proposal = object()
            bridge = type("B", (), {"materialize_intelligence_mission": lambda *args, **kwargs: type("M", (), {"mission_id": "MISSION-X"})()})()
            with patch("sys.argv", ["nexus", "intelligence", "analyze", directory, "--create-mission"]), \
                    patch("nexus.cli.app.IntelligenceOrchestrator") as orchestrator:
                orchestrator.return_value.analyze.return_value = (type("R", (), {"findings": (), "risks": (), "recommendations": ()})(), (proposal,))
                orchestrator.return_value.mission_bridge = bridge
                main()

    def test_invalid_repository_fails(self):
        with patch("sys.argv", ["nexus", "intelligence", "analyze", "missing-repository"]):
            with self.assertRaises(SystemExit) as result:
                main()
        self.assertEqual(result.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
