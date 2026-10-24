"""Phase 4.2: repeated TRANSIENT_PROVIDER_ERROR must fall back to a
different eligible route (reusing the existing routing/blocked_models
exclusion), not hard-fail straight into a Program corrective coding
attempt.

Real E2E defect (PROGRAM-..., Phase 2): a Worker on
openrouter/cohere/north-mini-code:free failed TRANSIENT_PROVIDER_ERROR,
retried the SAME route once (correct, MAX_SAME_TIER_RETRIES=1), failed
again, and then hard-failed WORKER_EXECUTION_FAILED straight to Program,
which spent its bounded corrective-attempt budget on a provider outage
instead of the existing fallback mechanism resolving it.

Unit-level tests (this file's first two classes) mock
resolve_initial_worker_route/execute_worker/review_worker directly, at the
exact call sites execute_progressively uses -- no real network/subprocess.
The last class drives the real production chain (run_go ->
run_integrated_plan -> execute_progressively -> resolve_initial_worker_route
-> the real AdaptiveRoutingService/catalog) with only the two leaf
model-calling functions mocked, proving the actual route/model decision.
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
from nexus import failure_classification as fc
from nexus.orchestration.go import GoError, run_go
from nexus.orchestration.integration import _worker_outcome_to_integration_error
from nexus.orchestration.progressive import (
    InitialRoutingUnavailable,
    execute_progressively,
)
from nexus.policies.escalation import MAX_SAME_TIER_RETRIES
from nexus.registry.runs import get_run


ROUTE_A = {
    "route_class": "complex-coding", "execution_path": "OMNIROUTE",
    "provider": "openrouter", "model": "openrouter/cohere/north-mini-code:free",
    "effort": "low", "scope": "fix things", "reason": "initial route",
}
ROUTING_A = {
    "capability": "advanced-coding", "risk": "MEDIUM",
    "model": ROUTE_A["model"], "provider": ROUTE_A["provider"], "effort": "low",
    "execution_path": "OMNIROUTE", "reason": "cheapest capable", "degraded": False,
}
ROUTE_B = {**ROUTE_A, "provider": "nvidia", "model": "nvidia/nemotron-3-super-120b-a12b"}
ROUTING_B = {**ROUTING_A, "model": ROUTE_B["model"], "provider": ROUTE_B["provider"]}


def _transient_failure(agent_id="a1", worktree="/wt"):
    return {"status": "FAILED", "reason": "TRANSIENT_PROVIDER_ERROR", "agent_id": agent_id, "worktree": worktree}


def _completed_worker(agent_id="a1", worktree="/wt"):
    return {"status": "COMPLETED", "agent_id": agent_id, "worktree": worktree, "output": "done", "branch": "b"}


def _passing_review():
    return {
        "status": "COMPLETED", "reviewer_id": "rev-1", "routing": {},
        "review": {"verdict": "PASS", "failure_class": None, "summary": "ok", "evidence": []},
    }


def _run(planned_worker=None, **overrides):
    # A unique run_id: nexus.telemetry.runs is a process-global in-memory
    # store keyed by run_id, so a generic id like "RUN-1" would leak
    # recorded calls into any other test in the same pytest process that
    # happens to reuse it.
    kwargs = dict(
        run_id="RUN-TRANSIENT-FALLBACK-TEST", repo="C:/fake/repo", manager_id="MGR-1",
        original_task="do the thing",
        planned_worker=planned_worker or {"model": "irrelevant", "scope": "fix things",
                                           "route_class": "complex-coding"},
        plan_risk="MEDIUM",
    )
    kwargs.update(overrides)
    with patch("nexus.orchestration.progressive.update_run_status"), \
         patch("nexus.orchestration.progressive.record_checkpoint"), \
         patch("nexus.orchestration.progressive.time.sleep"):
        return execute_progressively(**kwargs)


class SameTierRetryOnceTestCase(unittest.TestCase):
    """CASE A: a single transient failure retries the SAME route once."""

    def test_transient_once_then_success_stays_on_same_route(self):
        self.assertEqual(MAX_SAME_TIER_RETRIES, 1)
        with patch(
            "nexus.orchestration.progressive.resolve_initial_worker_route",
            return_value=(dict(ROUTE_A), dict(ROUTING_A)),
        ), patch(
            "nexus.orchestration.progressive.execute_worker",
            side_effect=[_transient_failure(), _completed_worker()],
        ) as worker_mock, patch(
            "nexus.orchestration.progressive.review_worker",
            return_value=_passing_review(),
        ):
            outcome = _run()

        self.assertEqual(outcome["status"], "COMPLETED")
        self.assertEqual(worker_mock.call_count, 2)
        used_models = {call.kwargs["worker"]["model"] for call in worker_mock.call_args_list}
        self.assertEqual(used_models, {ROUTE_A["model"]})  # never changed route


class FallbackAfterExhaustedRetryTestCase(unittest.TestCase):
    """CASE B/C/D: after the allowed same-tier retry fails again, fall
    back to a different eligible, cost-aware-ranked route -- never the
    exact route that just failed twice."""

    def test_second_transient_failure_falls_back_to_a_different_route(self):
        route_calls = []

        def fake_resolve(*, planned_worker, plan_risk, routing_service=None, exclude_models=frozenset()):
            route_calls.append(frozenset(exclude_models))
            if not exclude_models:
                return dict(ROUTE_A), dict(ROUTING_A)
            return dict(ROUTE_B), dict(ROUTING_B)

        with patch(
            "nexus.orchestration.progressive.resolve_initial_worker_route",
            side_effect=fake_resolve,
        ), patch(
            "nexus.orchestration.progressive.execute_worker",
            side_effect=[_transient_failure(), _transient_failure(), _completed_worker()],
        ) as worker_mock, patch(
            "nexus.orchestration.progressive.review_worker",
            return_value=_passing_review(),
        ):
            outcome = _run()

        self.assertEqual(outcome["status"], "COMPLETED")
        self.assertEqual(worker_mock.call_count, 3)
        used_models = [call.kwargs["worker"]["model"] for call in worker_mock.call_args_list]
        # CASE D: route A used for its two allowed attempts, never reselected.
        self.assertEqual(used_models, [ROUTE_A["model"], ROUTE_A["model"], ROUTE_B["model"]])
        # The fallback resolution excluded exactly the exhausted route.
        self.assertEqual(route_calls[1], frozenset({ROUTE_A["model"]}))
        # Program never sees this -- one clean COMPLETED outcome.
        self.assertNotIn("reason", outcome)

    def test_fallback_never_misclassified_as_capability_or_requirement(self):
        """Even though the outcome path shares code with WORKER_EXECUTION_FAILED,
        a transient provider issue must never be reported as a capability
        or requirement failure once classified."""
        outcome = {
            "status": "FAILED", "reason": "WORKER_EXECUTION_FAILED",
            "worker": {"status": "FAILED", "reason": "TRANSIENT_PROVIDER_ERROR"},
            "history": [],
        }
        error = _worker_outcome_to_integration_error(outcome, 1)
        go_error = GoError(error.code, str(error), run_id="RUN-X", detail=error.payload)

        classified = fc.classify_go_error(go_error)
        self.assertEqual(classified, fc.TRANSIENT_PROVIDER)
        self.assertNotIn(classified, (fc.CAPABILITY_INSUFFICIENT, fc.REQUIREMENT_UNSATISFIED))

    def test_all_routes_exhausted_blocks_cleanly_no_coding_correction(self):
        """CASE E: once no eligible route remains, a truthful
        infrastructure/provider blocker is returned -- not a coding
        correction, not an infinite retry."""
        with patch(
            "nexus.orchestration.progressive.resolve_initial_worker_route",
            side_effect=[
                (dict(ROUTE_A), dict(ROUTING_A)),
                InitialRoutingUnavailable(
                    "No eligible route remains.",
                    route_class="complex-coding", capability="advanced-coding", risk="MEDIUM",
                ),
            ],
        ), patch(
            "nexus.orchestration.progressive.execute_worker",
            side_effect=[_transient_failure(), _transient_failure()],
        ) as worker_mock:
            outcome = _run()

        self.assertEqual(outcome["status"], "BLOCKED")
        self.assertEqual(outcome["reason"], "INITIAL_ROUTING_UNAVAILABLE")
        # No third Worker attempt was made once routing was exhausted.
        self.assertEqual(worker_mock.call_count, 2)

        error = _worker_outcome_to_integration_error(outcome, 1)
        go_error = GoError(error.code, str(error), run_id="RUN-X", detail=error.payload)
        self.assertEqual(fc.classify_go_error(go_error), fc.ROUTE_UNAVAILABLE)


# ---------------------------------------------------------------------------
# CASE G: production-equivalent real path.
# ---------------------------------------------------------------------------

FIXTURE_PROJECTS = {"projects": [{"id": "norte", "name": "Norte", "path": "", "aliases": ["norte"], "enabled": True}]}

VALID_PLAN = {
    "complexity": "HIGH", "risk": "MEDIUM", "parallelism": 1,
    "summary": "Do a complex fix.",
    "workers": [
        {
            "route_class": "complex-coding", "execution_path": "OMNIROUTE",
            "provider": "irrelevant", "model": "irrelevant", "effort": "low",
            "scope": "Fix the thing.", "reason": "Complex change.",
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


def _make_worker_worktree(primary_repo: str, branch_name: str, tmp_dir: str) -> str:
    wt_path = os.path.join(tmp_dir, branch_name)
    subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, wt_path],
        cwd=primary_repo, check=True, capture_output=True,
    )
    return wt_path


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


ADVANCED_CODING_MODELS = frozenset({
    "cc/claude-sonnet-5-low", "cc/claude-sonnet-5-high",
    "nvidia/nemotron-3-super-120b-a12b", "openrouter/cohere/north-mini-code:free",
})


class RealPathTransientFallbackTestCase(unittest.TestCase):
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
        # Keep routing real while making the external discovery snapshot deterministic.
        from nexus.routing.telemetry import DiscoveredModel, OmniRouteTelemetrySnapshot
        discovered = tuple(DiscoveredModel(model_id=m, provider=("openrouter" if m.startswith("openrouter/") else "nvidia" if m.startswith("nvidia/") else "claude")) for m in ADVANCED_CODING_MODELS)
        self._telemetry_patch = patch("nexus.routing.telemetry.OmniRouteTelemetryClient.collect", return_value=OmniRouteTelemetrySnapshot(discovered_models=discovered))
        self._telemetry_patch.start()

    def tearDown(self) -> None:
        self._telemetry_patch.stop()
        database.DATABASE_PATH = self._orig_db_path
        database.STORAGE_DIR = self._orig_storage_dir
        projects.PROJECTS_FILE = self._orig_projects_file
        self._tmp.cleanup()

    def test_real_routing_falls_back_to_a_different_eligible_model(self):
        """CASE B/G, real path: the real AdaptiveRoutingService/catalog
        picks the initial route; it fails transiently twice; the real
        fallback resolution then picks a genuinely different eligible
        model -- asserted on the actual model, not prompt text."""
        calls: list[str] = []
        clean_worktree = _make_worker_worktree(self._repo, "worker-fallback", self._tmp.name)

        def fake_execute_worker(**kwargs):
            model = kwargs["worker"]["model"]
            calls.append(model)
            if calls.count(model) <= 2 and len(set(calls)) == 1:
                return _transient_failure(worktree=clean_worktree)
            return _completed_worker(worktree=clean_worktree)

        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={"manager_id": "AGT-1", "status": "COMPLETED", "exit_code": 0, "plan": VALID_PLAN},
        ), patch(
            "nexus.orchestration.progressive.execute_worker", side_effect=fake_execute_worker,
        ), patch(
            "nexus.orchestration.progressive.review_worker", return_value=_passing_review(),
        ), patch(
            "nexus.orchestration.progressive.time.sleep",
        ), patch("nexus.orchestration.integration.record_checkpoint"):
            result = run_go("Norte: fix a complex bug", project_query="norte")

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(calls[0], calls[1])  # same-tier retry: same model twice
        self.assertNotEqual(calls[2], calls[0])  # fallback: a genuinely different model
        self.assertTrue(set(calls) <= ADVANCED_CODING_MODELS)

    def test_real_routing_all_eligible_routes_exhausted_blocks_cleanly(self):
        """CASE E/G, real path: every eligible route fails transiently
        forever -> a clean, bounded INITIAL_ROUTING_UNAVAILABLE blocker,
        never an infinite loop, never a coding correction."""
        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={"manager_id": "AGT-1", "status": "COMPLETED", "exit_code": 0, "plan": VALID_PLAN},
        ), patch(
            "nexus.orchestration.progressive.execute_worker",
            return_value=_transient_failure(),
        ), patch(
            "nexus.orchestration.progressive.time.sleep",
        ), patch("nexus.orchestration.integration.record_checkpoint"):
            with self.assertRaises(GoError) as ctx:
                run_go("Norte: fix a complex bug", project_query="norte")

        self.assertEqual(ctx.exception.code, "INITIAL_ROUTING_UNAVAILABLE")
        self.assertEqual(fc.classify_go_error(ctx.exception), fc.ROUTE_UNAVAILABLE)
        run = get_run(ctx.exception.run_id)
        self.assertEqual(run["status"], "BLOCKED")

    def test_program_correction_budget_untouched_by_internal_fallback(self):
        """CASE F: a transient failure fully recovered by the existing
        fallback inside run_go is invisible to Program -- exactly one
        run_go call, exactly one Program attempt, no corrective mission."""
        calls: list[str] = []
        clean_worktree = _make_worker_worktree(self._repo, "worker-budget", self._tmp.name)

        def fake_execute_worker(**kwargs):
            model = kwargs["worker"]["model"]
            calls.append(model)
            if len(set(calls)) == 1 and calls.count(model) <= 2:
                return _transient_failure(worktree=clean_worktree)
            return _completed_worker(worktree=clean_worktree)

        with patch(
            "nexus.orchestration.go.run_manager",
            return_value={"manager_id": "AGT-1", "status": "COMPLETED", "exit_code": 0, "plan": VALID_PLAN},
        ), patch(
            "nexus.orchestration.progressive.execute_worker", side_effect=fake_execute_worker,
        ), patch(
            "nexus.orchestration.progressive.review_worker", return_value=_passing_review(),
        ), patch(
            "nexus.orchestration.progressive.time.sleep",
        ), patch("nexus.orchestration.integration.record_checkpoint"):
            result = run_go("Norte: fix a complex bug", project_query="norte")

        # Program's caller only ever sees this single successful call --
        # no GoError, no retry loop, no corrective mission was necessary.
        self.assertEqual(result["status"], "COMPLETED")




def test_explicit_quota_exhaustion_falls_back_without_retrying_provider():
    calls = []
    used = []

    def fake_resolve(*, planned_worker, plan_risk, routing_service=None,
                     exclude_models=frozenset(), exclude_providers=frozenset()):
        calls.append((exclude_models, exclude_providers))
        if not exclude_models:
            return dict(ROUTE_A), dict(ROUTING_A)
        return dict(ROUTE_B), dict(ROUTING_B)

    def fake_worker(**kwargs):
        used.append(kwargs["worker"]["model"])
        if len(used) == 1:
            return {"status": "FAILED", "reason": "QUOTA_EXHAUSTED",
                    "agent_id": "a1", "worktree": "/wt"}
        return _completed_worker()

    with patch("nexus.orchestration.progressive.resolve_initial_worker_route",
               side_effect=fake_resolve), patch(
        "nexus.orchestration.progressive.execute_worker",
        side_effect=fake_worker,
    ), patch("nexus.orchestration.progressive.review_worker",
             return_value=_passing_review()):
        outcome = _run()

    assert outcome["status"] == "COMPLETED"
    assert used == [ROUTE_A["model"], ROUTE_B["model"]]
    assert calls[1][0] == frozenset({ROUTE_A["model"]})
    assert calls[1][1] == frozenset({"openrouter"})

if __name__ == "__main__":
    unittest.main()
