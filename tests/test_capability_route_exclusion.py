"""Phase 4.1 hardening: exclude an already-insufficient route structurally.

The natural-language Manager hint (build_phase_mission) is supporting
context only. The actual constraint must be enforced through the existing
routing abstraction (``RoutingRequest.blocked_models`` -> the ``_eligible``
hard gate in nexus.routing.router, already used by every other caller of
``select_best_route``): a model a Manager Review already judged
CAPABILITY_INSUFFICIENT must not be selectable again for the same
capability, and the remaining eligible routes are still ranked by the
existing cost-aware scoring -- never a hardcoded model.

Two layers are exercised:
  1. nexus.routing.service.AdaptiveRoutingService / resolve_initial_worker_route
     directly (offline telemetry, real scoring/eligibility code).
  2. The real production chain run_go -> execute_progressively ->
     resolve_initial_worker_route, with only the two leaf model-calling
     functions (execute_worker, review_worker) mocked, proving `exclude_models`
     passed into run_go actually changes the real routing decision, not
     just prompt text.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.orchestration.go import GoError, run_go
from nexus.orchestration.progressive import resolve_initial_worker_route
from nexus.registry.runs import get_run
from nexus.routing.resources import ProviderOverrides
from nexus.routing.router import NoEligibleRouteError
from nexus.routing.service import AdaptiveRoutingService
from nexus.routing.telemetry import OmniRouteTelemetrySnapshot


def _offline_service() -> AdaptiveRoutingService:
    # Empty snapshot -> no live network telemetry; deterministic scoring
    # over the real, static APPROVED_CATALOG only.
    return AdaptiveRoutingService(
        telemetry_client=OmniRouteTelemetrySnapshot(),
        overrides=ProviderOverrides({}),
    )


class AdaptiveRoutingServiceExclusionTestCase(unittest.TestCase):
    """Layer 1: the routing primitive itself."""

    def test_blocked_model_is_never_selected_again(self):
        service = _offline_service()
        first = service.select_route_for_capability("advanced-coding", risk_level="medium")

        second = service.select_route_for_capability(
            "advanced-coding", risk_level="medium",
            blocked_models=frozenset({first.model}),
        )

        self.assertNotEqual(second.model, first.model)

    def test_remaining_eligible_routes_still_cost_aware_ranked(self):
        service = _offline_service()
        full_set = {
            "cc/claude-sonnet-5-low", "cc/claude-sonnet-5-high",
            "nvidia/nemotron-3-super-120b-a12b", "openrouter/cohere/north-mini-code:free",
        }
        for allowed_model in full_set:
            # Blocking every route except one leaves exactly that one --
            # selection still goes through the same scoring/eligibility
            # function on a narrowed candidate set, never a hardcoded pick.
            blocked = frozenset(full_set - {allowed_model})
            narrowed = service.select_route_for_capability(
                "advanced-coding", risk_level="medium", blocked_models=blocked
            )
            self.assertEqual(narrowed.model, allowed_model)

    def test_no_stronger_eligible_route_fails_closed_not_silently(self):
        service = _offline_service()
        all_advanced_coding_models = frozenset(
            {"cc/claude-sonnet-5-low", "cc/claude-sonnet-5-high", "nvidia/nemotron-3-super-120b-a12b",
             "openrouter/cohere/north-mini-code:free"}
        )
        with self.assertRaises(NoEligibleRouteError):
            service.select_route_for_capability(
                "advanced-coding", risk_level="medium",
                blocked_models=all_advanced_coding_models,
            )

    def test_single_route_capability_has_nothing_stronger(self):
        # standard-coding maps to exactly one approved route in the real
        # catalog -- excluding it must fail closed, not loop or invent one.
        service = _offline_service()
        with self.assertRaises(NoEligibleRouteError):
            service.select_route_for_capability(
                "standard-coding", risk_level="low",
                blocked_models=frozenset({"cc/claude-sonnet-5-low"}),
            )


class ResolveInitialWorkerRouteExclusionTestCase(unittest.TestCase):
    """Layer 2: the exact function execute_progressively calls."""

    def test_exclude_models_changes_the_resolved_route(self):
        service = _offline_service()
        first_worker, _ = resolve_initial_worker_route(
            planned_worker={"route_class": "complex-coding", "model": "irrelevant",
                             "provider": "irrelevant", "effort": "low",
                             "execution_path": "OMNIROUTE", "scope": "x", "reason": "x"},
            plan_risk="medium",
            routing_service=service,
        )

        second_worker, _ = resolve_initial_worker_route(
            planned_worker={"route_class": "complex-coding", "model": "irrelevant",
                             "provider": "irrelevant", "effort": "low",
                             "execution_path": "OMNIROUTE", "scope": "x", "reason": "x"},
            plan_risk="medium",
            routing_service=service,
            exclude_models=frozenset({first_worker["model"]}),
        )

        self.assertNotEqual(second_worker["model"], first_worker["model"])
        # route_class (the Manager's own decomposition decision) is
        # untouched; only the concrete model selection changed.
        self.assertEqual(second_worker["route_class"], "complex-coding")


# ---------------------------------------------------------------------------
# Layer 3: production-equivalent -- run_go with only the two leaf
# model-calling functions mocked, so resolve_initial_worker_route and the
# whole routing/eligibility/scoring pipeline run for real.
# ---------------------------------------------------------------------------

FIXTURE_PROJECTS = {"projects": [{"id": "norte", "name": "Norte", "path": "", "aliases": ["norte"], "enabled": True}]}

VALID_PLAN = {
    "complexity": "HIGH",
    "risk": "MEDIUM",
    "parallelism": 1,
    "summary": "Do a complex fix.",
    "workers": [
        {
            "route_class": "complex-coding",
            "execution_path": "OMNIROUTE",
            "provider": "irrelevant",
            "model": "irrelevant",
            "effort": "low",
            "scope": "Fix the thing.",
            "reason": "Complex change.",
            "authority": "WORKSPACE_WRITE",
        }
    ],
}


def _git(repo, *args):
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"},
    )


def _make_repo(tmp_dir: str) -> str:
    repo = os.path.join(tmp_dir, "primary")
    os.makedirs(repo, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "Test")
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--no-verify", "-m", "init")
    return repo


def _blocked_review(failure_class: str) -> dict:
    return {
        "status": "COMPLETED",
        "reviewer_id": "rev-1",
        "routing": {},
        "review": {
            "verdict": "BLOCKED",
            "failure_class": failure_class,
            "summary": "capability tier was insufficient",
            "evidence": [],
        },
    }


class RealPathCapabilityEscalationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp.name)
        self._repo = _make_repo(self._tmp.name)

        self._orig_db_path = database.DATABASE_PATH
        self._orig_storage_dir = database.STORAGE_DIR
        self._orig_projects_file = projects.PROJECTS_FILE

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus-test.db"

        fixture_path = tmp_path / "projects.json"
        payload = json.loads(json.dumps(FIXTURE_PROJECTS))
        payload["projects"][0]["path"] = self._repo
        fixture_path.write_text(json.dumps(payload), encoding="utf-8")
        projects.PROJECTS_FILE = fixture_path

        database.initialize_database()
        projects.sync_projects()
        # External fleet/quota is variable; keep the real routing chain deterministic.
        from nexus.routing.telemetry import DiscoveredModel, OmniRouteTelemetrySnapshot
        ids = (("cc/claude-sonnet-5-low", "claude"),
               ("cc/claude-sonnet-5-high", "claude"),
               ("nvidia/nemotron-3-super-120b-a12b", "nvidia"),
               ("openrouter/cohere/north-mini-code:free", "openrouter"))
        snapshot = OmniRouteTelemetrySnapshot(discovered_models=tuple(
            DiscoveredModel(model_id=model, provider=provider) for model, provider in ids))
        self._telemetry_patch = patch("nexus.routing.telemetry.OmniRouteTelemetryClient.collect", return_value=snapshot)
        self._telemetry_patch.start()
    def tearDown(self) -> None:
        self._telemetry_patch.stop()
        database.DATABASE_PATH = self._orig_db_path
        database.STORAGE_DIR = self._orig_storage_dir
        projects.PROJECTS_FILE = self._orig_projects_file
        self._tmp.cleanup()

    def _run_and_capture_selected_model(self, exclude_models=frozenset()) -> tuple[GoError, str]:
        """Run the REAL run_go -> run_integrated_plan -> execute_progressively
        -> resolve_initial_worker_route chain; only execute_worker/review_worker
        (the two leaf model-calling functions) are mocked. Returns the
        raised GoError and the model resolve_initial_worker_route actually
        chose for the Worker (captured from the real call args)."""
        captured: dict = {}

        def fake_execute_worker(**kwargs):
            captured["model"] = kwargs["worker"]["model"]
            return {
                "agent_id": "agent-1", "worktree": "/fake/wt",
                "status": "COMPLETED", "output": "done", "branch": "b1",
            }

        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={"manager_id": "AGT-1", "status": "COMPLETED", "exit_code": 0, "plan": VALID_PLAN},
        ), patch(
            "nexus.orchestration.progressive.execute_worker", side_effect=fake_execute_worker,
        ), patch(
            "nexus.orchestration.progressive.review_worker",
            return_value=_blocked_review("CAPABILITY_FAILURE"),
        ), patch("nexus.orchestration.integration.record_checkpoint"):
            with self.assertRaises(GoError) as ctx:
                run_go("Norte: fix a complex bug", project_query="norte", exclude_models=exclude_models)

        return ctx.exception, captured["model"]

    def test_first_attempt_selects_a_real_eligible_route(self):
        error, model = self._run_and_capture_selected_model()
        self.assertEqual(error.code, "REVIEW_BLOCKED")
        self.assertIn(
            model,
            {"cc/claude-sonnet-5-low", "cc/claude-sonnet-5-high", "nvidia/nemotron-3-super-120b-a12b",
             "openrouter/cohere/north-mini-code:free"},
        )

    def test_exclude_models_prevents_reselecting_the_insufficient_route(self):
        """CASE B, real path: route/model A initially selected; a stronger
        eligible route B exists; passing exclude_models={A} to the retry's
        run_go must NOT select A again -- it must select an eligible B via
        the existing routing policy."""
        first_error, model_a = self._run_and_capture_selected_model()
        self.assertEqual(fc_classify(first_error), "CAPABILITY_INSUFFICIENT")

        second_error, model_b = self._run_and_capture_selected_model(
            exclude_models=frozenset({model_a})
        )

        self.assertNotEqual(model_b, model_a)
        self.assertIn(
            model_b,
            {"cc/claude-sonnet-5-low", "cc/claude-sonnet-5-high", "nvidia/nemotron-3-super-120b-a12b",
             "openrouter/cohere/north-mini-code:free"} - {model_a},
        )

    def test_no_stronger_eligible_route_blocks_cleanly_without_looping(self):
        """When every eligible route for the capability is already
        excluded, run_go must fail closed with the existing
        INITIAL_ROUTING_UNAVAILABLE blocker -- not loop, not invent a route."""
        all_eligible = frozenset(
            {"cc/claude-sonnet-5-low", "cc/claude-sonnet-5-high", "nvidia/nemotron-3-super-120b-a12b",
             "openrouter/cohere/north-mini-code:free"}
        )
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={"manager_id": "AGT-1", "status": "COMPLETED", "exit_code": 0, "plan": VALID_PLAN},
        ):
            with self.assertRaises(GoError) as ctx:
                run_go("Norte: fix a complex bug", project_query="norte", exclude_models=all_eligible)

        self.assertEqual(ctx.exception.code, "INITIAL_ROUTING_UNAVAILABLE")
        run = get_run(ctx.exception.run_id)
        self.assertEqual(run["status"], "BLOCKED")


def fc_classify(error):
    from nexus import failure_classification as fc
    return fc.classify_go_error(error)


if __name__ == "__main__":
    unittest.main()
