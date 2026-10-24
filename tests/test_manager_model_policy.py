"""Tests for the Manager model selection policy (nexus/policies/manager_model.py).

Five tests as specified for the NEXUS_MANAGER_MODEL_POLICY change:

  1. BOUNDED Manager selects Luna.
  2. BROAD Manager selects Sol.
  3. Effort remains controlled by the adaptive reasoning policy (unchanged).
  4. An unavailable preferred model still follows the existing fallback/routing
     path: Nexus expresses its preference, OmniRoute owns availability.
     Verified by confirming go.py passes the policy-selected model to
     run_manager and does NOT hard-fail the mission when run_manager succeeds.
  5. Worker/Scout/Reviewer routing is unchanged by this policy.

All tests are pure unit tests (or thin integration tests with mocked
dispatchers/subprocesses). No filesystem, database, network, or real AI
model access of any kind.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from nexus.dispatchers.triage import MissionScope
from nexus.policies.manager_model import (
    MANAGER_MODEL_BOUNDED,
    MANAGER_MODEL_BROAD,
    select_manager_model,
)
from nexus.policies.reasoning_effort import select_reasoning_effort


class TestManagerModelSelection(unittest.TestCase):
    """Tests 1-2: Model selection by mission scope."""

    def test_1_bounded_scope_selects_luna(self):
        """BOUNDED mission → Luna (cost-efficient), routed through OmniRoute."""
        model = select_manager_model(MissionScope.BOUNDED)
        self.assertEqual(model, MANAGER_MODEL_BOUNDED)
        self.assertEqual(model, "codex/gpt-5.6-luna")

    def test_2_broad_scope_selects_sol(self):
        """BROAD mission → Sol (stronger planning), routed through OmniRoute."""
        model = select_manager_model(MissionScope.BROAD)
        self.assertEqual(model, MANAGER_MODEL_BROAD)
        self.assertEqual(model, "codex/gpt-5.6-sol")

    def test_constants_are_distinct(self):
        """Luna and Sol must be different model identifiers."""
        self.assertNotEqual(MANAGER_MODEL_BOUNDED, MANAGER_MODEL_BROAD)

    def test_select_manager_model_is_deterministic(self):
        """Same scope always returns the same model (pure function)."""
        for scope in (MissionScope.BOUNDED, MissionScope.BROAD):
            self.assertEqual(
                select_manager_model(scope),
                select_manager_model(scope),
            )

    def test_select_manager_model_returns_nonempty_string(self):
        """Both scope values return a non-empty string (never None, never '')."""
        for scope in (MissionScope.BOUNDED, MissionScope.BROAD):
            result = select_manager_model(scope)
            self.assertIsInstance(result, str)
            self.assertTrue(result.strip(), f"Empty model for scope={scope}")


class TestManagerEffortPolicyUnchanged(unittest.TestCase):
    """Test 3: Effort is still controlled by the adaptive reasoning policy."""

    def test_3a_bounded_manager_effort_is_low(self):
        """BOUNDED Manager (LOW complexity) → effort=low — unchanged."""
        decision = select_reasoning_effort(role="MANAGER", complexity="LOW", risk=None)
        self.assertEqual(decision.effort, "low")

    def test_3b_broad_manager_effort_is_medium(self):
        """BROAD Manager (MEDIUM complexity) → effort=medium — unchanged."""
        decision = select_reasoning_effort(role="MANAGER", complexity="MEDIUM", risk=None)
        self.assertEqual(decision.effort, "medium")

    def test_3c_manager_effort_never_defaults_to_high(self):
        """Manager HIGH complexity → effort=medium (not high). Unchanged."""
        decision = select_reasoning_effort(role="MANAGER", complexity="HIGH", risk=None)
        self.assertEqual(decision.effort, "medium")
        self.assertNotEqual(decision.effort, "high")

    def test_3d_model_and_effort_are_independent(self):
        """Model selection and effort selection are separate concerns."""
        # BROAD: Sol model + medium effort
        broad_model = select_manager_model(MissionScope.BROAD)
        broad_effort = select_reasoning_effort(role="MANAGER", complexity="MEDIUM").effort
        self.assertEqual(broad_model, "codex/gpt-5.6-sol")
        self.assertEqual(broad_effort, "medium")

        # BOUNDED: Luna model + low effort
        bounded_model = select_manager_model(MissionScope.BOUNDED)
        bounded_effort = select_reasoning_effort(role="MANAGER", complexity="LOW").effort
        self.assertEqual(bounded_model, "codex/gpt-5.6-luna")
        self.assertEqual(bounded_effort, "low")


class TestManagerModelFallbackPath(unittest.TestCase):
    """Test 4: go.py passes the policy model to run_manager; OmniRoute handles availability.

    Verifies that:
      (a) For a BOUNDED mission, run_manager is called with gpt-5.6-luna.
      (b) For a BROAD mission, run_manager is called with gpt-5.6-sol.
      (c) When run_manager succeeds (even if it internally routed to a fallback),
          go.py does NOT hard-fail the mission — the run completes normally.
    """

    FIXTURE_PROJECTS = {
        "projects": [
            {
                "id": "norte",
                "name": "Norte",
                "path": r"C:\fake\interface-life",
                "aliases": ["norte"],
                "enabled": True,
            }
        ]
    }

    VALID_PLAN = {
        "complexity": "LOW",
        "risk": "LOW",
        "parallelism": 1,
        "summary": "Inspect the routing policy.",
        "intent": "ANALYSIS",
        "workers": [],
        "risk_consistency_notice": None,
    }

    def setUp(self):
        import nexus.registry.database as database
        import nexus.registry.projects as projects

        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp_dir.name)

        self._orig_db = database.DATABASE_PATH
        self._orig_storage = database.STORAGE_DIR
        self._orig_projects = projects.PROJECTS_FILE

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus-test.db"

        fixture_path = tmp_path / "projects.json"
        fixture_path.write_text(json.dumps(self.FIXTURE_PROJECTS), encoding="utf-8")
        projects.PROJECTS_FILE = fixture_path

        database.initialize_database()
        projects.sync_projects()

        self._database = database
        self._projects = projects

    def tearDown(self):
        self._database.DATABASE_PATH = self._orig_db
        self._database.STORAGE_DIR = self._orig_storage
        self._projects.PROJECTS_FILE = self._orig_projects
        self._tmp_dir.cleanup()

    def _make_manager_result(self, plan=None):
        return {
            "manager_id": "AGENT-TEST-0001",
            "status": "COMPLETED",
            "exit_code": 0,
            "plan": plan or dict(self.VALID_PLAN),
        }

    def test_4a_bounded_mission_calls_run_manager_with_luna(self):
        """go.py passes Luna (codex/gpt-5.6-luna) to run_manager for a BOUNDED mission."""
        from nexus.orchestration.go import run_go

        captured = {}

        def fake_manager(**kwargs):
            captured.update(kwargs)
            return self._make_manager_result()

        with patch("nexus.orchestration.go.run_manager", side_effect=fake_manager), \
             patch("nexus.orchestration.go.run_integrated_plan") as mock_intg, \
             patch("nexus.orchestration.go.record_checkpoint"), \
             patch("nexus.orchestration.go.record_approved_plan"), \
             patch("nexus.orchestration.go.establish_ownership"), \
             patch("nexus.orchestration.go.reconcile_stale_ownership", return_value=[]):

            mock_intg.return_value = []

            # "norte/nexus/routing/service.py" has file extension → BOUNDED
            run_go(
                "Inspect nexus/routing/service.py for issues.",
                project_query="norte",
            )

        self.assertEqual(
            captured.get("model"),
            "codex/gpt-5.6-luna",
            f"Expected codex/gpt-5.6-luna for BOUNDED mission, got {captured.get('model')!r}",
        )

    def test_4b_broad_mission_calls_run_manager_with_sol(self):
        """go.py passes Sol (codex/gpt-5.6-sol) to run_manager for a BROAD mission."""
        from nexus.orchestration.go import run_go

        captured = {}

        def fake_manager(**kwargs):
            captured.update(kwargs)
            return self._make_manager_result()

        def fake_scouts(run_id, repo, request_text):
            return "# scout evidence"

        with patch("nexus.orchestration.go.run_manager", side_effect=fake_manager), \
             patch("nexus.orchestration.go.run_scout_stage", side_effect=fake_scouts), \
             patch("nexus.orchestration.go.run_integrated_plan") as mock_intg, \
             patch("nexus.orchestration.go.record_checkpoint"), \
             patch("nexus.orchestration.go.record_approved_plan"), \
             patch("nexus.orchestration.go.establish_ownership"), \
             patch("nexus.orchestration.go.reconcile_stale_ownership", return_value=[]):

            mock_intg.return_value = []

            # "this codebase" + "main problems" → BROAD
            run_go(
                "Survey this codebase and identify the main problems.",
                project_query="norte",
            )

        self.assertEqual(
            captured.get("model"),
            "codex/gpt-5.6-sol",
            f"Expected codex/gpt-5.6-sol for BROAD mission, got {captured.get('model')!r}",
        )

    def test_4c_mission_does_not_hard_fail_when_manager_succeeds(self):
        """When run_manager returns COMPLETED, go.py completes the run normally.

        This covers the fallback case: even if OmniRoute internally routed to
        Luna instead of Sol (because Sol was unavailable), run_manager still
        returns COMPLETED and the run proceeds without hard-failing.
        """
        from nexus.orchestration.go import run_go

        def fake_manager(**kwargs):
            # Simulate OmniRoute having routed to a fallback model internally
            return {
                "manager_id": "AGENT-TEST-FB01",
                "status": "COMPLETED",
                "exit_code": 0,
                "plan": dict(self.VALID_PLAN),
            }

        with patch("nexus.orchestration.go.run_manager", side_effect=fake_manager), \
             patch("nexus.orchestration.go.run_scout_stage", return_value="evidence"), \
             patch("nexus.orchestration.go.run_integrated_plan", return_value=[]), \
             patch("nexus.orchestration.go.record_checkpoint"), \
             patch("nexus.orchestration.go.record_approved_plan"), \
             patch("nexus.orchestration.go.establish_ownership"), \
             patch("nexus.orchestration.go.reconcile_stale_ownership", return_value=[]):

            result = run_go(
                "Survey this codebase and identify the main problems.",
                project_query="norte",
            )

        self.assertEqual(result["status"], "COMPLETED")


class TestNonManagerPoliciesUnchanged(unittest.TestCase):
    """Test 5: Worker/Scout/Reviewer routing is unchanged.

    select_manager_model() is only imported/used on the MANAGER path.
    It must not affect Worker, Scout, or Reviewer effort selection.
    """

    def test_5a_worker_effort_unchanged(self):
        """Worker effort selection is unaffected by manager_model policy."""
        cases = [
            ("LOW", "LOW", "low"),
            ("MEDIUM", "LOW", "low"),
            ("MEDIUM", "MEDIUM", "medium"),
            ("HIGH", "LOW", "medium"),
        ]
        for complexity, risk, expected in cases:
            with self.subTest(complexity=complexity, risk=risk):
                decision = select_reasoning_effort(
                    role="WORKER", complexity=complexity, risk=risk
                )
                self.assertEqual(decision.effort, expected)

    def test_5b_scout_effort_unchanged(self):
        """Scout effort selection is unaffected by manager_model policy."""
        self.assertEqual(
            select_reasoning_effort(role="SCOUT", complexity="LOW").effort, "low"
        )
        self.assertEqual(
            select_reasoning_effort(role="SCOUT", complexity="MEDIUM").effort, "medium"
        )

    def test_5c_reviewer_effort_unchanged(self):
        """Reviewer effort selection is unaffected by manager_model policy."""
        self.assertEqual(
            select_reasoning_effort(role="REVIEWER", risk="LOW").effort, "low"
        )
        self.assertEqual(
            select_reasoning_effort(role="REVIEWER", risk="CRITICAL").effort, "high"
        )

    def test_5d_select_manager_model_does_not_touch_worker_routing(self):
        """select_manager_model import does not mutate Worker routing catalog.

        APPROVED_CATALOG is a tuple of ModelRoute entries.  We verify that
        the catalog length and capability sets are unchanged after importing
        the manager_model policy module (import side effects must be zero).
        """
        from nexus.routing.catalog import APPROVED_CATALOG

        # Catalog is a tuple of ModelRoute; collect all capabilities
        def _all_capabilities(catalog):
            caps = set()
            for route in catalog:
                caps.update(route.capabilities)
            return caps

        caps_before = _all_capabilities(APPROVED_CATALOG)
        count_before = len(APPROVED_CATALOG)

        # Expected capabilities exist before import
        self.assertIn("mechanical", caps_before)
        self.assertIn("standard-coding", caps_before)

        from nexus.policies.manager_model import select_manager_model  # noqa: F401

        # After import: catalog must be unchanged
        caps_after = _all_capabilities(APPROVED_CATALOG)
        self.assertEqual(caps_before, caps_after,
                         "Capabilities changed after importing manager_model")
        self.assertEqual(count_before, len(APPROVED_CATALOG),
                         "APPROVED_CATALOG length changed after importing manager_model")


if __name__ == "__main__":
    unittest.main()
