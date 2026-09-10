import unittest
from unittest.mock import patch

import nexus.missions.service as mission_service
import nexus.tasks.registry as task_registry
from nexus.intelligence.errors import IntelligenceValidationError
from nexus.intelligence.mission_bridge import (
    IntelligenceMissionProposal,
    materialize_intelligence_mission,
)


class IntelligenceMissionMaterializationTests(unittest.TestCase):
    def setUp(self):
        mission_service._missions.clear()
        task_registry._tasks.clear()

    def proposal(self):
        return IntelligenceMissionProposal(
            "IMP-1", "Drift fix", "Bring dependencies into policy.",
            ("finding:1",), "HIGH", ("nexus.intelligence",), ("Tests pass",)
        )

    def test_materializes_ordered_standard_graph_without_execution(self):
        with patch("nexus.missions.persistence.persist_mission_bundle") as persist:
            mission = materialize_intelligence_mission(
                self.proposal(), "RUN-1", "PROJECT-1", approved=True
            )
        self.assertEqual(len(mission.tasks), 5)
        self.assertEqual([t.title for t in mission.tasks], [
            "Analyze affected area", "Define implementation plan",
            "Implement bounded change", "Run validation", "Review result",
        ])
        self.assertEqual([t.dependencies for t in mission.tasks],
                         [[], [mission.tasks[0].task_id], [mission.tasks[1].task_id],
                          [mission.tasks[2].task_id], [mission.tasks[3].task_id]])
        self.assertEqual(mission.tasks[0].acceptance_criteria, ["Tests pass"])
        self.assertIn("finding:1", mission.tasks[0].description)
        self.assertEqual(
            [t.execution_policy["required_capabilities"] for t in mission.tasks],
            [["analysis"], ["architecture"], ["coding"], ["analysis"], ["review"]],
        )
        persist.assert_called_once()

    def test_requires_explicit_approval_and_is_atomic_on_failure(self):
        with self.assertRaises(IntelligenceValidationError):
            materialize_intelligence_mission(self.proposal(), "RUN-1")
        self.assertEqual(mission_service._missions, {})
        self.assertEqual(task_registry._tasks, {})

        with patch("nexus.missions.persistence.persist_mission_bundle",
                   side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                materialize_intelligence_mission(self.proposal(), "RUN-1", approved=True)
        self.assertEqual(mission_service._missions, {})
        self.assertEqual(task_registry._tasks, {})


if __name__ == "__main__":
    unittest.main()
