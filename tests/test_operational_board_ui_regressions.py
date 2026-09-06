"""Regression guards for Nexus v2.0-E.1 Operational Board UI."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (
    ROOT / "nexus" / "web" / "templates" / "board.html"
).read_text(encoding="utf-8")
JS = (
    ROOT / "nexus" / "web" / "static" / "js" / "board.js"
).read_text(encoding="utf-8")


class OperationalBoardUiRegressionTestCase(unittest.TestCase):

    def test_operational_runs_precedes_task_board(self):
        self.assertGreaterEqual(HTML.find("Operational Runs"), 0)
        self.assertGreaterEqual(HTML.find("Task Board"), 0)
        self.assertLess(
            HTML.find("Operational Runs"),
            HTML.find("Task Board"),
        )

    def test_existing_board_sections_are_preserved(self):
        for label in (
            "Missions",
            "Task Board",
            "Active Agents",
            "Execution Sessions",
            "CREATE MISSION",
        ):
            self.assertIn(label, HTML)

    def test_operational_project_id_is_supported(self):
        self.assertIn("run.project_id", JS)

    def test_operational_run_auto_selection_and_preservation_exist(self):
        self.assertIn("var selectedRunId = null", JS)
        self.assertIn("var stillPresent = orderedRuns.some", JS)
        self.assertIn("selectedRunId = orderedRuns[0].id", JS)

    def test_active_agent_capabilities_are_dom_nodes_not_stringified_array(self):
        self.assertIn('var caps = el("div", "task-caps")', JS)
        self.assertIn('caps.appendChild(el("span", "cap", c))', JS)
        self.assertNotIn(
            'card.appendChild(el("div", "task-caps", a.capabilities.map',
            JS,
        )

    def test_legacy_reviewer_routing_is_safe(self):
        self.assertIn(
            "legacy / routing metadata unavailable",
            JS,
        )

    def test_operational_endpoints_are_still_consumed(self):
        self.assertIn('/api/operational/runs', JS)


    def test_operational_project_name_uses_project_id_fallback(self):
        self.assertIn(
            'run.project_name || run.project_id || "-"',
            JS,
        )

    def test_lineage_arrow_uses_encoding_safe_unicode_escape(self):
        self.assertIn(
            r'"\u2193"',
            JS,
        )

    def test_control_board_branding_is_present(self):
        self.assertIn("Nexus Control Board", HTML)


if __name__ == "__main__":
    unittest.main()
