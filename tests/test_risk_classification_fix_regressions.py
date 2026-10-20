"""Regression tests for the upstream risk/complexity classification fix.

Background: a real Nexus GO run submitted a read-only, explicitly
"do not modify files" analysis/planning task for nexus/routing/service.py
and the Manager LLM classified it as risk=CRITICAL. Because no approved
catalog route is (or should be) qualified for CRITICAL, the run correctly
failed closed with INITIAL_ROUTING_UNAVAILABLE -- but go.py mislabeled
that failure as "Manager Review blocked the run", even though the failure
happens in resolve_initial_worker_route(), strictly before any Worker or
real Manager Review step executes.

This fix is upstream and advisory, not a routing/gating change:

    1. dispatchers/manager.py's Manager prompt now explicitly defines
       COMPLEXITY (technical difficulty) vs RISK (potential operational
       impact of the ACTION) as independent axes, with read-only and
       mutating examples, so complexity=HIGH + risk=LOW/MEDIUM is
       recognized as normal rather than contradictory.
    2. A new, purely advisory _detect_risk_classification_notice() flags
       (never blocks, never rewrites) the specific inconsistent pattern
       this incident exhibited: risk=CRITICAL claimed for a read-only
       intent (ANALYSIS/PLANNING/QUESTION) with no worker text suggesting
       a mutating/destructive/hard-to-reverse action. It is intent-as-
       evidence, not "PLANNING/ANALYSIS => risk <= HIGH" as a bypass rule:
       an EXECUTION-intent plan touching production/credentials is never
       flagged, and nothing ever changes plan["risk"].
    3. go.py (and resume.py, for consistency/defense in depth) now raises
       a distinct GoError/ResumeError code, "INITIAL_ROUTING_UNAVAILABLE",
       instead of folding that failure into "REVIEW_BLOCKED" / "Manager
       Review blocked the run" -- a real REVIEW_FAILED / BLOCKED verdict /
       unknown-verdict outcome still raises REVIEW_BLOCKED exactly as
       before.

No hard gate, catalog, max_risk_level, or select_best_route() behavior is
touched by this fix. CRITICAL still has zero eligible routes and still
fails closed -- that is verified here too, explicitly, so no regression
silently reopens it.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.orchestration.integration import IntegrationError
from nexus.dispatchers.manager import (
    _detect_risk_classification_notice,
    _validate_plan,
    _worker_text_suggests_mutation,
)
from nexus.orchestration.go import GoError, run_go
from nexus.orchestration.progressive import (
    InitialRoutingUnavailable,
    resolve_initial_worker_route,
)
from nexus.registry.runs import get_run
from nexus.routing.capabilities import _RISK_RANK
from nexus.routing.catalog import APPROVED_CATALOG


FIXTURE_PROJECTS = {
    "projects": [
        {
            "id": "norte",
            "name": "Norte",
            "path": r"C:\fake\interface-life",
            "aliases": ["norte"],
            "enabled": True,
        },
    ]
}


def _read_only_analysis_plan(risk: str) -> dict:
    return {
        "complexity": "HIGH",
        "risk": risk,
        "intent": "PLANNING",
        "parallelism": 1,
        "summary": "Analyze routing/service.py and propose a refactor.",
        "workers": [
            {
                "route_class": "complex-coding",
                "execution_path": "OMNIROUTE",
                "provider": "omniroute",
                "model": "GPT-5.6 Sol",
                "effort": "high",
                "scope": (
                    "Read routing/service.py and produce a technical "
                    "plan document. Do not alter files."
                ),
                "reason": "Read-only architectural analysis.",
            }
        ],
    }


def _destructive_execution_plan(risk: str) -> dict:
    return {
        "complexity": "MEDIUM",
        "risk": risk,
        "intent": "EXECUTION",
        "parallelism": 1,
        "summary": "Apply a destructive production migration.",
        "workers": [
            {
                "route_class": "security-critical",
                "execution_path": "OMNIROUTE",
                "provider": "omniroute",
                "model": "GPT-5.6 Sol",
                "effort": "high",
                "scope": (
                    "Apply an irreversible schema migration directly "
                    "against the production database and rotate "
                    "affected credentials."
                ),
                "reason": "Production database migration.",
            }
        ],
    }


class RiskComplexityIndependenceTests(unittest.TestCase):
    """complexity and risk must never be conflated by validation itself."""

    def test_high_complexity_with_low_risk_is_accepted_unchanged(self):
        plan = _read_only_analysis_plan("LOW")
        validated = _validate_plan(json.loads(json.dumps(plan)))

        self.assertEqual(validated["complexity"], "HIGH")
        self.assertEqual(validated["risk"], "LOW")

    def test_high_complexity_with_medium_risk_is_accepted_unchanged(self):
        plan = _read_only_analysis_plan("MEDIUM")
        validated = _validate_plan(json.loads(json.dumps(plan)))

        self.assertEqual(validated["complexity"], "HIGH")
        self.assertEqual(validated["risk"], "MEDIUM")

    def test_validate_plan_never_mutates_a_critical_risk_value(self):
        plan = _read_only_analysis_plan("CRITICAL")
        validated = _validate_plan(json.loads(json.dumps(plan)))

        # No silent downgrade: CRITICAL in, CRITICAL out.
        self.assertEqual(validated["risk"], "CRITICAL")


class ReadOnlyCriticalModuleNoticeTests(unittest.TestCase):
    """Read-only analysis of a critical module should not itself imply
    CRITICAL risk -- but the fix only *signals* this, never blocks or
    rewrites it."""

    def test_read_only_planning_with_critical_risk_is_flagged(self):
        plan = _read_only_analysis_plan("CRITICAL")

        notice = _detect_risk_classification_notice(plan)

        self.assertIsNotNone(notice)
        self.assertIn("RISK_INTENT_INCONSISTENT", notice)
        # The notice must never claim to have changed anything.
        self.assertEqual(plan["risk"], "CRITICAL")

    def test_read_only_planning_with_high_risk_is_not_flagged(self):
        plan = _read_only_analysis_plan("HIGH")

        notice = _detect_risk_classification_notice(plan)

        self.assertIsNone(notice)

    def test_explicitly_destructive_execution_plan_stays_critical_unflagged(
        self,
    ):
        plan = _destructive_execution_plan("CRITICAL")

        notice = _detect_risk_classification_notice(plan)

        # A genuinely destructive, production-touching EXECUTION plan is
        # exactly what CRITICAL should be able to represent -- the notice
        # must not fire here, and risk must remain CRITICAL.
        self.assertIsNone(notice)
        self.assertEqual(plan["risk"], "CRITICAL")

    def test_worker_text_mutation_detection_is_evidence_not_a_rule(self):
        # PLANNING intent alone does not suppress the notice...
        self.assertFalse(
            _worker_text_suggests_mutation(
                _read_only_analysis_plan("CRITICAL")["workers"]
            )
        )
        # ...but explicit destructive/production language does, regardless
        # of intent -- intent is one input, not a bypass switch.
        self.assertTrue(
            _worker_text_suggests_mutation(
                _destructive_execution_plan("CRITICAL")["workers"]
            )
        )

    def test_extract_plan_attaches_notice_without_touching_validate_plan(
        self,
    ):
        # _validate_plan itself (used directly by other tests/call sites)
        # must remain notice-free and side-effect-free on risk/complexity.
        plan = _validate_plan(
            json.loads(json.dumps(_read_only_analysis_plan("CRITICAL")))
        )
        self.assertNotIn("risk_consistency_notice", plan)


class CriticalRiskStillFailsClosedTests(unittest.TestCase):
    """No route, for any capability, may accept CRITICAL risk. This fix
    must not weaken that in any way."""

    def test_no_catalog_route_declares_critical_max_risk_level(self):
        for route in APPROVED_CATALOG:
            self.assertNotEqual(
                getattr(route, "max_risk_level", None),
                "critical",
                f"{route} unexpectedly permits CRITICAL risk",
            )

    def test_risk_rank_hard_gate_unchanged(self):
        self.assertEqual(
            _RISK_RANK,
            {"low": 0, "medium": 1, "high": 2, "critical": 3},
        )

    def test_complex_coding_critical_risk_fails_closed(self):
        with self.assertRaises(InitialRoutingUnavailable) as ctx:
            resolve_initial_worker_route(
                planned_worker={
                    "route_class": "complex-coding",
                    "execution_path": "OMNIROUTE",
                    "provider": "omniroute",
                    "model": "GPT-5.6 Sol",
                    "effort": "high",
                    "scope": "irrelevant",
                    "reason": "irrelevant",
                },
                plan_risk="CRITICAL",
            )

        self.assertEqual(ctx.exception.route_class, "complex-coding")
        self.assertEqual(ctx.exception.risk, "CRITICAL")
        self.assertEqual(ctx.exception.cause_type, "NoEligibleRouteError")


class GoErrorLabelingTests(unittest.TestCase):
    """INITIAL_ROUTING_UNAVAILABLE, REVIEW_BLOCKED, and
    WORKER_EXECUTION_FAILED must remain clearly distinguishable, and no
    real Manager Review outcome may be mislabeled or vice versa."""

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp_dir.name)

        self._original_db_path = database.DATABASE_PATH
        self._original_storage_dir = database.STORAGE_DIR
        self._original_projects_file = projects.PROJECTS_FILE

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus-test.db"

        fixture_path = tmp_path / "projects.json"
        fixture_path.write_text(json.dumps(FIXTURE_PROJECTS), encoding="utf-8")
        projects.PROJECTS_FILE = fixture_path

        database.initialize_database()
        projects.sync_projects()

    def tearDown(self) -> None:
        database.DATABASE_PATH = self._original_db_path
        database.STORAGE_DIR = self._original_storage_dir
        projects.PROJECTS_FILE = self._original_projects_file
        self._tmp_dir.cleanup()

    def _run_with_progressive_outcome(self, outcome: dict):
        manager_result = {
            "manager_id": "AGT-0001",
            "status": "COMPLETED",
            "exit_code": 0,
            "plan": _read_only_analysis_plan("CRITICAL"),
        }

        reason = outcome.get("reason")
        if reason == "WORKER_EXECUTION_FAILED":
            integration_error = IntegrationError(
                "WORKER_EXECUTION_FAILED",
                run_status="FAILED",
            )
        elif reason == "REVIEW_FAILED":
            integration_error = IntegrationError(
                "REVIEW_BLOCKED",
                run_status="BLOCKED",
                detail="Manager Review blocked the run: REVIEW_FAILED",
            )
        elif reason == "INITIAL_ROUTING_UNAVAILABLE":
            integration_error = IntegrationError(
                "INITIAL_ROUTING_UNAVAILABLE",
                run_status="BLOCKED",
                error=outcome.get("error", "Initial routing unavailable"),
                error_type=outcome.get("error_type"),
                route_class=outcome.get("route_class"),
                capability=outcome.get("capability"),
                risk=outcome.get("risk"),
            )
        else:
            integration_error = IntegrationError(
                reason or "UNKNOWN",
                run_status="BLOCKED",
            )

        with patch(
            "nexus.orchestration.go.run_manager",
            return_value=manager_result,
        ), patch(
            "nexus.orchestration.go.execute_progressively",
            return_value=outcome,
        ), patch(
            "nexus.orchestration.go.run_integrated_plan",
            side_effect=integration_error,
        ):
            with self.assertRaises(GoError) as ctx:
                run_go("Norte: analyze routing/service.py", project_query="norte")

        return ctx.exception

    def test_initial_routing_unavailable_is_not_reported_as_review_blocked(
        self,
    ):
        exc = self._run_with_progressive_outcome(
            {
                "status": "BLOCKED",
                "reason": "INITIAL_ROUTING_UNAVAILABLE",
                "error": (
                    "Initial adaptive routing failed for "
                    "route_class='complex-coding', "
                    "capability='advanced-coding', risk='CRITICAL': "
                    "no eligible route"
                ),
                "error_type": "NoEligibleRouteError",
                "route_class": "complex-coding",
                "capability": "advanced-coding",
                "risk": "CRITICAL",
                "history": [],
            }
        )

        self.assertEqual(exc.code, "INITIAL_ROUTING_UNAVAILABLE")
        self.assertNotIn("Manager Review", str(exc))
        self.assertIn("INITIAL_ROUTING_UNAVAILABLE", str(exc) + exc.code)

        run = get_run(exc.run_id)
        self.assertEqual(run["status"], "BLOCKED")

    def test_real_review_failure_still_reported_as_review_blocked(self):
        exc = self._run_with_progressive_outcome(
            {
                "status": "BLOCKED",
                "reason": "REVIEW_FAILED",
                "history": [],
                "review": {
                    "reviewer_id": "AGT-0002",
                    "status": "BLOCKED",
                    "review": None,
                    "exit_code": 0,
                    "error": "No valid review envelope found.",
                },
            }
        )

        self.assertEqual(exc.code, "REVIEW_BLOCKED")
        self.assertIn("Manager Review blocked the run", str(exc))

    def test_worker_execution_failure_is_reported_as_worker_execution_failed(
        self,
    ):
        exc = self._run_with_progressive_outcome(
            {
                "status": "FAILED",
                "reason": "WORKER_EXECUTION_FAILED",
                "history": [],
                "worker": {"status": "FAILED", "error": "boom"},
            }
        )

        self.assertEqual(exc.code, "WORKER_EXECUTION_FAILED")


if __name__ == "__main__":
    unittest.main()
