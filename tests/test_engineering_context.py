import json
import unittest

from nexus.engineering import EngineeringContext, EngineeringContextValidationError


class EngineeringContextTests(unittest.TestCase):
    def make_context(self):
        return EngineeringContext.build(
            mission_id="MISSION-1", task_id="TASK-1", objective="Reduce parser risk",
            affected_files=("src/parser.py", "tests/test_parser.py"),
            evidence=("finding:F-1", "src/parser.py:10"), risks=("regression",),
            constraints=("preserve API",), validation_requirements=("unit tests",),
            created_at="2026-01-01T00:00:00Z")

    def test_creation_and_serialization(self):
        context = self.make_context()
        self.assertEqual(context.mission_id, "MISSION-1")
        self.assertEqual(json.loads(context.to_json()), context.to_dict())

    def test_validation_failures(self):
        with self.assertRaises(EngineeringContextValidationError):
            self.make_context().build(mission_id="M", task_id="T", objective="", affected_files=("a",), evidence=("e",), created_at="2026-01-01T00:00:00Z")
        with self.assertRaises(EngineeringContextValidationError):
            EngineeringContext.build(mission_id="M", task_id="T", objective="x", affected_files=(), evidence=(), created_at="2026-01-01T00:00:00Z")

    def test_evidence_preserved_and_deterministic(self):
        first = self.make_context()
        second = EngineeringContext.build(mission_id="MISSION-1", task_id="TASK-1", objective="Reduce parser risk", affected_files=("tests/test_parser.py", "src/parser.py"), evidence=("src/parser.py:10", "finding:F-1"), risks=("regression",), constraints=("preserve API",), validation_requirements=("unit tests",), created_at="2026-01-01T00:00:00Z")
        self.assertEqual(first.evidence, ("finding:F-1", "src/parser.py:10"))
        self.assertEqual(first, second)

    def test_no_execution_side_effects(self):
        before = self.make_context().to_json()
        self.assertEqual(before, self.make_context().to_json())
        self.assertFalse(any(name in dir(EngineeringContext) for name in ("execute", "invoke", "dispatch")))


if __name__ == "__main__":
    unittest.main()
