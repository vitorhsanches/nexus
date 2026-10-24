"""
tests/test_plan_vs_actual_execution_identity.py

Proves that planned requirements and actual execution identity are kept
honestly distinct, and that nothing about adaptive routing's actual
behaviour changed while getting there.

Root cause: the Manager's plan requires a concrete `provider`/`model` per
Worker (e.g. "OpenAI"/"GPT-5"), but
nexus.orchestration.progressive.resolve_initial_worker_route() always
replaces provider/model/effort with the Adaptive Router's own decision
before a Worker launches or anything is persisted -- confirmed by its own
docstring ("Its provider/model/effort fields are advisory only ... and
are replaced by the route selected by Nexus policy") and by the
EXECUTION_START checkpoint's `execution_identity_source: "adaptive_router"`
tag. That part of the architecture was already correct. What was missing:
nothing durable recorded route_class (the one planned field that IS real
and never overwritten) next to the actual identity, the Manager prompt
still asked for provider/model as if they were binding, and there was no
test locking in that planned hints never reach the router.

Fix: persist `route_class` on the agent row (additive column, parallel to
the full_result column added previously), surface it through the existing
bounded agent projection, and reframe the Manager prompt's provider/model/
effort fields as explicit hints. No routing/capability/risk code changed.

Cases (per the task's CASE A-G):
  A. Plan requests a capability (route_class), router picks Claude actual.
  B. Router fallback selects a different model; route_class unchanged.
  C. No explicit model-constraint mechanism exists -- confirmed, not invented.
  D. Control Center source renders actual model as primary, route_class as a
     clearly-labeled secondary "planned" line.
  E. Current Models telemetry reflects only actual models, never plan hints.
  F. Historical row (no route_class) degrades honestly, nothing fabricated.
  G. Adaptive routing's own decision is identical regardless of what the
     Manager's advisory provider/model/effort hints said.
"""

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import nexus.registry.database as database
import nexus.registry.projects as projects
import nexus.telemetry.runs as telemetry_runs
from nexus.orchestration.progressive import resolve_initial_worker_route
from nexus.registry.agents import create_agent
from nexus.routing.service import AdaptiveRoutingService, RoutingDecision
from nexus.web import operational


_FIXTURE_PROJECTS = {
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

_PLANNED_WORKER_BASE = {
    "route_class": "complex-coding",
    "execution_path": "OMNIROUTE",
    "scope": "Implement the fix.",
    "reason": "Needs strong reasoning for a tricky bug.",
    "depends_on": [],
}


def _planned_worker(provider_hint: str, model_hint: str, effort_hint: str = "low") -> dict:
    """A Manager-authored planned Worker with an advisory provider/model hint."""
    worker = dict(_PLANNED_WORKER_BASE)
    worker["provider"] = provider_hint
    worker["model"] = model_hint
    worker["effort"] = effort_hint
    return worker


def _fake_routing_service(model: str, provider: str = "omniroute", effort: str = "medium"):
    svc = MagicMock(spec=AdaptiveRoutingService)
    svc.select_route_for_capability.return_value = RoutingDecision(
        model=model,
        provider=provider,
        effort=effort,
        execution_path="OMNIROUTE",
        reason="adaptive router decision",
        degraded=False,
    )
    return svc


class _DbMixin(unittest.TestCase):
    RUN_ID = "RUN-PLANVACTUAL-TEST"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp.name)

        self._orig_db = database.DATABASE_PATH
        self._orig_storage = database.STORAGE_DIR
        self._orig_projects = projects.PROJECTS_FILE

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "test.db"

        fixture = tmp_path / "projects.json"
        fixture.write_text(json.dumps(_FIXTURE_PROJECTS), encoding="utf-8")
        projects.PROJECTS_FILE = fixture

        database.initialize_database()
        projects.sync_projects()

        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO runs (id,project_id,input,intent,status,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (self.RUN_ID, "norte", "do work", "GO", "RUNNING", "now"),
            )

    def tearDown(self):
        database.DATABASE_PATH = self._orig_db
        database.STORAGE_DIR = self._orig_storage
        projects.PROJECTS_FILE = self._orig_projects
        self._tmp.cleanup()


# ─────────────────────────────────────────────────────────────────────────────
# Case A — plan requests a capability, router picks Claude: actual identity
# persisted never matches the Manager's advisory hint.
# ─────────────────────────────────────────────────────────────────────────────

class CaseAPlanVsActualTests(_DbMixin):
    def test_resolved_route_replaces_plan_hint_with_actual_router_decision(self):
        planned = _planned_worker(provider_hint="OpenAI", model_hint="GPT-5")
        service = _fake_routing_service(model="cc/claude-sonnet-5-low", provider="omniroute")

        current_worker, routing = resolve_initial_worker_route(
            planned_worker=planned,
            plan_risk="LOW",
            routing_service=service,
        )

        # Actual execution identity: the router's decision, never the hint.
        self.assertEqual(current_worker["model"], "cc/claude-sonnet-5-low")
        self.assertEqual(current_worker["provider"], "omniroute")
        self.assertNotEqual(current_worker["model"], "GPT-5")
        self.assertNotEqual(current_worker["provider"], "OpenAI")

        # Planned requirement: route_class, never touched by the router.
        self.assertEqual(current_worker["route_class"], "complex-coding")
        self.assertEqual(routing["capability"], "advanced-coding")

        # The original planned_worker dict itself is untouched (defensive
        # deepcopy) -- the Manager's plan record never gets silently rewritten.
        self.assertEqual(planned["model"], "GPT-5")
        self.assertEqual(planned["provider"], "OpenAI")

    def test_persisted_agent_row_has_actual_identity_and_planned_route_class(self):
        """End-to-end: create_agent() persists the actual router decision as
        model/provider, and route_class as the one honest planned field --
        the Manager's GPT-5/OpenAI hint is never written to the agents table."""
        planned = _planned_worker(provider_hint="OpenAI", model_hint="GPT-5")
        service = _fake_routing_service(model="cc/claude-sonnet-5-low", provider="omniroute")

        current_worker, _routing = resolve_initial_worker_route(
            planned_worker=planned,
            plan_risk="LOW",
            routing_service=service,
        )

        agent_id = create_agent(
            run_id=self.RUN_ID,
            role="Worker",
            provider=current_worker["provider"],
            model=current_worker["model"],
            effort=current_worker["effort"],
            status="RUNNING",
            route_class=current_worker["route_class"],
        )

        agents = operational.get_operational_agents(run_id=self.RUN_ID)
        self.assertEqual(len(agents), 1)
        row = agents[0]
        self.assertEqual(row["id"], agent_id)
        self.assertEqual(row["model"], "cc/claude-sonnet-5-low")
        self.assertEqual(row["provider"], "omniroute")
        self.assertEqual(row["route_class"], "complex-coding")
        self.assertNotIn("GPT-5", json.dumps(row))
        self.assertNotIn("OpenAI", json.dumps(row))


# ─────────────────────────────────────────────────────────────────────────────
# Case B — router fallback selects a different model; route_class (the plan
# requirement) remains unchanged across the Worker's lifecycle.
# ─────────────────────────────────────────────────────────────────────────────

class CaseBFallbackModelTests(_DbMixin):
    def test_fallback_route_changes_model_but_not_the_planned_capability(self):
        planned = _planned_worker(provider_hint="OpenAI", model_hint="GPT-5")

        initial_service = _fake_routing_service(model="cc/claude-sonnet-5-low")
        initial_worker, _ = resolve_initial_worker_route(
            planned_worker=planned, plan_risk="LOW", routing_service=initial_service,
        )

        # Simulate what progressive.py does on a transient failure: resolve
        # again, excluding the model that just failed, for the SAME planned
        # Worker (same route_class) -- the router picks a different actual
        # route, never reconsidering the Manager's original hint.
        fallback_service = _fake_routing_service(model="oc/big-pickle")
        fallback_worker, _ = resolve_initial_worker_route(
            planned_worker=planned,
            plan_risk="LOW",
            routing_service=fallback_service,
            exclude_models=frozenset({initial_worker["model"]}),
        )

        self.assertNotEqual(initial_worker["model"], fallback_worker["model"])
        self.assertEqual(initial_worker["route_class"], fallback_worker["route_class"])
        self.assertEqual(fallback_worker["route_class"], "complex-coding")

        # The exclusion actually reached the router call.
        fallback_service.select_route_for_capability.assert_called_once()
        _, kwargs = fallback_service.select_route_for_capability.call_args
        self.assertEqual(kwargs["blocked_models"], frozenset({"cc/claude-sonnet-5-low"}))


# ─────────────────────────────────────────────────────────────────────────────
# Case C — no explicit model-constraint mechanism exists today; confirmed,
# not fabricated.
# ─────────────────────────────────────────────────────────────────────────────

class CaseCNoExplicitConstraintMechanismTests(unittest.TestCase):
    def test_select_route_for_capability_has_no_model_or_provider_requirement_param(self):
        """Documents the real current contract: the router's capability
        entry point takes capability/risk/blocklists only. There is no way
        today to force a specific model/provider, and this fix does not add
        one -- it only makes the existing hint-vs-actual split honest."""
        params = set(
            inspect.signature(AdaptiveRoutingService.select_route_for_capability).parameters
        )
        self.assertEqual(
            params,
            {"self", "capability", "risk_level", "blocked_models", "blocked_providers"},
        )


# ─────────────────────────────────────────────────────────────────────────────
# Case D — Control Center source: actual model is the primary row; the
# planned route_class is a clearly distinct, separately labeled line.
# ─────────────────────────────────────────────────────────────────────────────

class CaseDControlCenterSourceTests(unittest.TestCase):
    def test_programs_js_renders_actual_model_and_a_distinctly_labeled_planned_line(self):
        js_path = Path(__file__).resolve().parents[1] / "nexus" / "web" / "static" / "js" / "programs.js"
        source = js_path.read_text(encoding="utf-8")

        # Actual execution identity remains the primary row field.
        self.assertIn('el("span", "cp-agent-model", a.model || "UNKNOWN")', source)
        # Planned requirement is rendered separately and explicitly labeled,
        # never merged into the same field as the actual model.
        self.assertIn('"PLANNED: "', source)
        self.assertIn("cp-agent-planned", source)
        self.assertIn("ROUTE_CLASS_LABEL", source)


# ─────────────────────────────────────────────────────────────────────────────
# Case E — Current Models / telemetry reflects only actual models.
# ─────────────────────────────────────────────────────────────────────────────

class CaseECurrentModelsTelemetryTests(unittest.TestCase):
    RUN_ID = "RUN-TELEMETRY-PLANVACTUAL-TEST"

    def tearDown(self):
        telemetry_runs._RUNS.pop(self.RUN_ID, None)

    def test_models_used_reflects_only_actual_models_never_plan_hints(self):
        telemetry_runs.start_run(self.RUN_ID)
        # What actually executed, per record_call()'s real call sites in
        # progressive.py (model=current_worker.get("model"), the router's
        # decision) -- the Manager's "GPT-5" hint is never passed here.
        telemetry_runs.record_call(
            self.RUN_ID,
            role="WORKER",
            model="cc/claude-sonnet-5-low",
            provider="omniroute",
            route_class="complex-coding",
            reason="INITIAL",
            result="COMPLETED",
        )

        summary = telemetry_runs.get_run_summary(self.RUN_ID)
        self.assertEqual(summary["models_used"], ["cc/claude-sonnet-5-low"])
        self.assertNotIn("GPT-5", summary["models_used"])


# ─────────────────────────────────────────────────────────────────────────────
# Case F — historical row without route_class degrades honestly.
# ─────────────────────────────────────────────────────────────────────────────

class CaseFHistoricalRowTests(_DbMixin):
    def test_agent_row_without_route_class_reports_none_not_a_guess(self):
        create_agent(
            run_id=self.RUN_ID,
            role="Worker",
            provider="omniroute",
            model="cc/claude-sonnet-5-low",
            status="RUNNING",
            # route_class intentionally omitted -- mirrors every pre-fix
            # agent row in a real, already-running Nexus database.
        )

        agents = operational.get_operational_agents(run_id=self.RUN_ID)
        self.assertEqual(len(agents), 1)
        self.assertIsNone(agents[0]["route_class"])


# ─────────────────────────────────────────────────────────────────────────────
# Case G — adaptive routing's actual decision is identical regardless of the
# Manager's advisory provider/model/effort hints; routing behavior unchanged.
# ─────────────────────────────────────────────────────────────────────────────

class CaseGRoutingBehaviorUnchangedTests(unittest.TestCase):
    def test_router_call_args_never_include_the_plan_hint_values(self):
        """The router is called with capability/risk/blocklists only --
        never with the Manager's advisory provider/model/effort. Varying
        those hints across two otherwise-identical plans must not change
        what gets passed to the router or what it returns."""
        service_a = _fake_routing_service(model="cc/claude-sonnet-5-low")
        service_b = _fake_routing_service(model="cc/claude-sonnet-5-low")

        worker_gpt_hint = _planned_worker(provider_hint="OpenAI", model_hint="GPT-5", effort_hint="high")
        worker_claude_hint = _planned_worker(provider_hint="Anthropic", model_hint="claude-opus", effort_hint="low")

        resolved_a, _ = resolve_initial_worker_route(
            planned_worker=worker_gpt_hint, plan_risk="LOW", routing_service=service_a,
        )
        resolved_b, _ = resolve_initial_worker_route(
            planned_worker=worker_claude_hint, plan_risk="LOW", routing_service=service_b,
        )

        # Identical route_class -> identical capability -> identical call.
        _, kwargs_a = service_a.select_route_for_capability.call_args
        _, kwargs_b = service_b.select_route_for_capability.call_args
        args_a = service_a.select_route_for_capability.call_args.args
        args_b = service_b.select_route_for_capability.call_args.args
        self.assertEqual(args_a, args_b)
        self.assertEqual(kwargs_a, kwargs_b)

        # Resolved actual identity is identical too -- the hints had zero
        # influence on the outcome.
        self.assertEqual(resolved_a["model"], resolved_b["model"])
        self.assertEqual(resolved_a["provider"], resolved_b["provider"])
        self.assertEqual(resolved_a["effort"], resolved_b["effort"])


if __name__ == "__main__":
    unittest.main()
