"""Tests for manual Manager model selection (NEXUS_MANAGER_MODEL_SELECTION).

Covers the scenarios specified for the feature:

  1. Auto preserves original behavior.
  2. Manual eligible selection reaches the real Manager dispatcher.
  3. Invalid or unavailable model is rejected.
  4. Unqualified model cannot bypass policy.
  5. Worker routing remains unchanged.
  6. Requested and actual Manager model are persisted correctly.
  7. Historical runs render correctly.
  8. Running runs are unaffected by changing UI selection (structural check:
     no code path updates manager_mode/manager_requested_model after
     create_run()).
  9. UI/API displays only grounded availability information (no raw
     OmniRoute catalog dump).

All tests are pure unit tests or thin integration tests with mocked
dispatchers/subprocesses. No real AI model access of any kind.
"""

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nexus.policies.manager_model import (
    MANAGER_MODEL_BOUNDED,
    MANAGER_MODEL_BROAD,
    MANAGER_MODEL_CHOICES,
    ManagerModelUnavailableError,
    describe_manager_models,
    validate_manual_manager_model,
)


class TestValidateManualManagerModel(unittest.TestCase):
    """Tests 3-4: invalid/unqualified models are rejected before anything runs."""

    def test_3_known_models_are_valid(self):
        for model in MANAGER_MODEL_CHOICES:
            validate_manual_manager_model(model)  # must not raise

    def test_3_unknown_model_is_rejected(self):
        with self.assertRaises(ManagerModelUnavailableError):
            validate_manual_manager_model("gpt-does-not-exist")

    def test_4_unqualified_catalog_model_cannot_bypass_policy(self):
        """A catalog model with NO "planning" capability is still rejected
        for the Manager role, even though it is a real, approved,
        OmniRoute-routable id for other roles (Reviewer).

        This is the core safety gate: being a valid OmniRoute route for
        SOME role (e.g. review-only) does not make a model qualified for
        the Manager's planning contract specifically.
        """
        with self.assertRaises(ManagerModelUnavailableError) as ctx:
            validate_manual_manager_model("cc/claude-sonnet-5-high")
        self.assertIn("cc/claude-sonnet-5-high", str(ctx.exception))

    def test_error_carries_model_and_reason(self):
        try:
            validate_manual_manager_model("not-a-model")
        except ManagerModelUnavailableError as error:
            self.assertEqual(error.model, "not-a-model")
            self.assertTrue(error.reason)
        else:
            self.fail("expected ManagerModelUnavailableError")


class TestDescribeManagerModels(unittest.TestCase):
    """Test 9: the selector surfaces only grounded, non-fabricated data."""

    def test_auto_default_present(self):
        data = describe_manager_models()
        self.assertIsNone(data["auto"]["model_id"])
        self.assertIn("Auto", data["auto"]["display_name"])

    def test_selectable_is_exactly_the_known_omniroute_models(self):
        data = describe_manager_models()
        ids = {m["model_id"] for m in data["selectable"]}
        self.assertEqual(ids, set(MANAGER_MODEL_CHOICES))
        for m in data["selectable"]:
            self.assertEqual(m["availability"], "SELECTABLE")

    def test_does_not_dump_the_raw_omniroute_catalog(self):
        """Only approved+enabled catalog entries may appear as ineligible,
        never the full Worker catalog, and every entry must be real. A
        catalog entry already promoted to selectable must never also
        appear in ineligible (no model classified twice)."""
        from nexus.routing.catalog import APPROVED_CATALOG

        data = describe_manager_models()
        catalog_ids = {route.model_id for route in APPROVED_CATALOG}
        approved_enabled_ids = {
            route.model_id for route in APPROVED_CATALOG if route.approved and route.enabled
        }

        ineligible_ids = {m["model_id"] for m in data["ineligible"]}
        selectable_ids = {m["model_id"] for m in data["selectable"]}
        self.assertTrue(ineligible_ids.issubset(catalog_ids))
        self.assertTrue(ineligible_ids.issubset(approved_enabled_ids))
        self.assertEqual(ineligible_ids & selectable_ids, set())
        self.assertLess(len(data["ineligible"]), len(APPROVED_CATALOG))

        # No ineligible entry has the "planning" capability -- the one
        # catalog route that does is already selectable above.
        planning_ids = {
            route.model_id for route in APPROVED_CATALOG if "planning" in route.capabilities
        }
        self.assertEqual(ineligible_ids & planning_ids, set())

        for m in data["ineligible"]:
            self.assertIn(m["availability"], {"DISCOVERED", "QUALIFIED_FOR_MANAGER"})
            self.assertTrue(m["reason"], "every ineligible entry must explain why")

    def test_no_fabricated_quota_or_health_fields(self):
        """Manager models have no live telemetry; the payload must not
        pretend otherwise by inventing quota/health-looking fields."""
        data = describe_manager_models()
        forbidden = {"quota_state", "headroom_pct", "health"}
        for m in data["selectable"] + data["ineligible"]:
            self.assertFalse(forbidden & set(m.keys()))


class TestManagerModelRunIntegration(unittest.TestCase):
    """Tests 1, 2, 6: end-to-end run_go() behavior with a real (temp) DB."""

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

    def _make_manager_result(self, model):
        return {
            "manager_id": "AGENT-TEST-0001",
            "status": "COMPLETED",
            "exit_code": 0,
            "plan": dict(self.VALID_PLAN),
        }

    def _patched_run_manager(self, captured):
        from nexus.registry.agents import create_agent

        def fake_manager(**kwargs):
            captured.update(kwargs)
            create_agent(
                run_id=kwargs["run_id"],
                role="Manager",
                provider="omniroute",
                model=kwargs["model"],
                effort=kwargs.get("effort", "low"),
                status="COMPLETED",
            )
            return self._make_manager_result(kwargs["model"])

        return fake_manager

    def test_1_auto_preserves_original_behavior(self):
        """manager_model=None (the default) still uses select_manager_model()."""
        from nexus.orchestration.go import run_go
        from nexus.registry.runs import get_run

        captured = {}
        with patch("nexus.orchestration.go.run_manager", side_effect=self._patched_run_manager(captured)), \
             patch("nexus.orchestration.go.run_integrated_plan", return_value=[]), \
             patch("nexus.orchestration.go.record_checkpoint"), \
             patch("nexus.orchestration.go.record_approved_plan"), \
             patch("nexus.orchestration.go.establish_ownership"), \
             patch("nexus.orchestration.go.reconcile_stale_ownership", return_value=[]):

            result = run_go(
                "Inspect nexus/routing/service.py for issues.",
                project_query="norte",
            )

        self.assertEqual(captured.get("model"), MANAGER_MODEL_BOUNDED)
        run = get_run(result["run_id"])
        self.assertEqual(run["manager_mode"], "auto")
        self.assertIsNone(run["manager_requested_model"])

    def test_2_and_6_manual_selection_reaches_dispatcher_and_persists(self):
        """A manually selected eligible model reaches run_manager() and is
        persisted as both the requested and (via the Manager agent row) the
        actual executed model."""
        from nexus.orchestration.go import run_go
        from nexus.registry.agents import list_agents_for_run
        from nexus.registry.runs import get_run

        captured = {}
        with patch("nexus.orchestration.go.run_manager", side_effect=self._patched_run_manager(captured)), \
             patch("nexus.orchestration.go.run_scout_stage", return_value="evidence"), \
             patch("nexus.orchestration.go.run_integrated_plan", return_value=[]), \
             patch("nexus.orchestration.go.record_checkpoint"), \
             patch("nexus.orchestration.go.record_approved_plan"), \
             patch("nexus.orchestration.go.establish_ownership"), \
             patch("nexus.orchestration.go.reconcile_stale_ownership", return_value=[]):

            # "Survey this codebase" + "main problems" -> BROAD scope, which
            # would auto-select Sol; force Luna manually to prove the
            # override -- not the scope policy -- decided the model.
            result = run_go(
                "Survey this codebase and identify the main problems.",
                project_query="norte",
                manager_model=MANAGER_MODEL_BOUNDED,
            )

        self.assertEqual(captured.get("model"), MANAGER_MODEL_BOUNDED)

        run = get_run(result["run_id"])
        self.assertEqual(run["manager_mode"], "manual")
        self.assertEqual(run["manager_requested_model"], MANAGER_MODEL_BOUNDED)

        agents = [dict(a) for a in list_agents_for_run(result["run_id"])]
        manager_agents = [a for a in agents if a["role"] == "Manager"]
        self.assertEqual(len(manager_agents), 1)
        self.assertEqual(manager_agents[0]["model"], MANAGER_MODEL_BOUNDED)
        self.assertEqual(manager_agents[0]["provider"], "omniroute")

    def test_3_invalid_manual_selection_rejected_before_run_created(self):
        """An invalid manual model must fail before any Run row exists."""
        from nexus.orchestration.go import GoError, run_go
        from nexus.registry.runs import list_runs

        before = len(list_runs())

        with patch("nexus.orchestration.go.run_manager") as mock_manager, \
             patch("nexus.orchestration.go.reconcile_stale_ownership", return_value=[]):
            with self.assertRaises(GoError) as ctx:
                run_go(
                    "Inspect nexus/routing/service.py for issues.",
                    project_query="norte",
                    manager_model="cc/claude-sonnet-5-high",
                )

        self.assertEqual(ctx.exception.code, "MANAGER_MODEL_INVALID")
        self.assertIsNone(ctx.exception.run_id)
        mock_manager.assert_not_called()
        self.assertEqual(len(list_runs()), before, "no Run row should be created")

    def test_5_worker_routing_capabilities_unchanged_by_manual_selection(self):
        """Selecting a manual Manager model must not mutate the Worker
        adaptive-routing catalog (capabilities/approval/enabled state)."""
        from nexus.orchestration.go import run_go
        from nexus.routing.catalog import APPROVED_CATALOG

        def _all_capabilities(catalog):
            caps = set()
            for route in catalog:
                caps.update(route.capabilities)
            return caps

        caps_before = _all_capabilities(APPROVED_CATALOG)
        count_before = len(APPROVED_CATALOG)

        captured = {}
        with patch("nexus.orchestration.go.run_manager", side_effect=self._patched_run_manager(captured)), \
             patch("nexus.orchestration.go.run_integrated_plan", return_value=[]), \
             patch("nexus.orchestration.go.record_checkpoint"), \
             patch("nexus.orchestration.go.record_approved_plan"), \
             patch("nexus.orchestration.go.establish_ownership"), \
             patch("nexus.orchestration.go.reconcile_stale_ownership", return_value=[]):

            run_go(
                "Inspect nexus/routing/service.py for issues.",
                project_query="norte",
                manager_model=MANAGER_MODEL_BROAD,
            )

        self.assertEqual(_all_capabilities(APPROVED_CATALOG), caps_before)
        self.assertEqual(len(APPROVED_CATALOG), count_before)

    def test_7_historical_run_without_manager_columns_renders_as_auto(self):
        """A Run row inserted the pre-migration way (no manager_mode/
        manager_requested_model supplied) must read back as auto/None via
        the column defaults, not error or fabricate a manual selection."""
        from nexus.registry.database import get_connection
        from nexus.registry.runs import get_run

        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO runs (id, project_id, input, intent, status, created_at)
                VALUES ('RUN-HISTORICAL1', 'norte', 'legacy input', 'GO', 'COMPLETED', '2020-01-01T00:00:00+00:00')
                """
            )

        run = get_run("RUN-HISTORICAL1")
        self.assertIsNotNone(run)
        self.assertEqual(run["manager_mode"], "auto")
        self.assertIsNone(run["manager_requested_model"])


class TestRunningRunsUnaffectedByLaterSelection(unittest.TestCase):
    """Test 8: nothing in the registry layer ever updates manager_mode /
    manager_requested_model after create_run() -- so changing the UI
    selector later can structurally never mutate an in-flight Run's
    recorded selection (including RUN-E6B89A65 or any other active run)."""

    def test_no_update_statement_touches_manager_selection_columns(self):
        import nexus.registry.runs as runs_module

        source = inspect.getsource(runs_module)
        # The only mentions of these columns must be in create_run()'s
        # INSERT and get_run()'s SELECT -- never inside an UPDATE.
        for update_fn in ("update_run_status", "update_run_risk", "update_run_result"):
            fn_source = inspect.getsource(getattr(runs_module, update_fn))
            self.assertNotIn("manager_mode", fn_source)
            self.assertNotIn("manager_requested_model", fn_source)
        self.assertIn("manager_mode", source)  # sanity: columns do exist somewhere


if __name__ == "__main__":
    unittest.main()
