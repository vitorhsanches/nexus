"""Regression tests for the INITIAL_ROUTING_UNAVAILABLE fix.

Background: some GO executions reached planning but failed before Worker
execution with reason=INITIAL_ROUTING_UNAVAILABLE for HIGH-risk
review-critical Workers. Root cause was two structural gaps in the
Adaptive Router bridge, not a hard-gate bug:

    1. AdaptiveRoutingService's total-outage legacy fallback for the
       "review" capability was hardcoded to a single MEDIUM-risk-capped
       route (cc/claude-sonnet-5-low), so a HIGH-risk review request had
       no usable fallback during a full OmniRoute telemetry outage even
       though an approved HIGH-risk review route already exists in the
       catalog (cc/claude-sonnet-5-high).
    2. nexus.orchestration.progressive.INITIAL_ROUTE_CAPABILITY_BY_CLASS
       had no entry at all for "security-critical", so it always failed
       closed regardless of telemetry health.

This suite locks in the fix: review-critical, complex-coding and
security-critical now resolve through the SAME AdaptiveRoutingService /
select_best_route() hard-gate pipeline as every other route class -- no
gate was weakened, no model was invented, and "omniroute" is never
treated as a model provider identity.
"""

import unittest

from nexus.orchestration.progressive import (
    INITIAL_ROUTE_CAPABILITY_BY_CLASS,
    InitialRoutingUnavailable,
    resolve_initial_worker_route,
)
from nexus.policies.escalation import (
    ROUTE_LADDERS,
    EscalationUnavailable,
    next_route,
)
from nexus.routing.catalog import APPROVED_CATALOG, is_blocked_model
from nexus.routing.models import (
    CapabilityClass,
    ModelRoute,
    RoutingRequest,
)
from nexus.routing.router import NoEligibleRouteError, select_best_route
from nexus.routing.service import AdaptiveRoutingService
from nexus.routing.telemetry import DiscoveredModel, OmniRouteTelemetrySnapshot


def _fully_discovered_snapshot():
    """A healthy telemetry snapshot that discovers every approved model.

    Keeps build_runtime_catalog() from disabling any approved route, so
    this suite isolates the fallback/mapping/ladder fix under test rather
    than runtime-discovery correlation (covered by test_routing_telemetry).
    """
    return OmniRouteTelemetrySnapshot(
        discovered_models=tuple(
            DiscoveredModel(model_id=route.model_id, provider=route.provider)
            for route in APPROVED_CATALOG
        ),
    )


def _manager_hint(route_class):
    """The exact shape of the Manager's worker hint from the investigated
    failing plan: a suggested model/provider that does not, and must
    never, exist in the approved catalog. It must never leak into the
    resolved worker."""
    return {
        "route_class": route_class,
        "execution_path": "OMNIROUTE",
        "provider": "omniroute",
        "model": "GPT-5.6 Sol",
        "effort": "high",
        "scope": "Investigate and fix the reported defect.",
        "reason": "Manager planning recommendation.",
    }


class _TotalOutageClient:
    """Injectable telemetry client simulating a complete OmniRoute outage."""

    def collect(self):
        return OmniRouteTelemetrySnapshot(
            errors=(
                "/v1/models connection failed",
                "/api/usage/quota connection failed",
                "/api/monitoring/health connection failed",
            ),
        )


class ReviewCriticalHighRiskResolvesTests(unittest.TestCase):
    def test_review_critical_high_resolves_to_approved_high_review_route(self):
        service = AdaptiveRoutingService(telemetry_client=_fully_discovered_snapshot())

        resolved, routing = resolve_initial_worker_route(
            _manager_hint("review-critical"),
            plan_risk="HIGH",
            routing_service=service,
        )

        self.assertEqual(resolved["model"], "cc/claude-sonnet-5-high")
        self.assertEqual(resolved["provider"], "claude")
        self.assertEqual(resolved["execution_path"], "OMNIROUTE")
        self.assertEqual(routing["capability"], "review")
        self.assertFalse(routing["degraded"])

    def test_review_critical_critical_risk_still_fails_closed(self):
        service = AdaptiveRoutingService(telemetry_client=_fully_discovered_snapshot())

        with self.assertRaises(InitialRoutingUnavailable):
            resolve_initial_worker_route(
                _manager_hint("review-critical"),
                plan_risk="CRITICAL",
                routing_service=service,
            )


class ComplexCodingResolvesTests(unittest.TestCase):
    def test_complex_coding_high_resolves_to_approved_advanced_coding_route(self):
        service = AdaptiveRoutingService(telemetry_client=_fully_discovered_snapshot())

        resolved, routing = resolve_initial_worker_route(
            _manager_hint("complex-coding"),
            plan_risk="HIGH",
            routing_service=service,
        )

        self.assertEqual(resolved["model"], "cc/claude-sonnet-5-high")
        self.assertEqual(resolved["provider"], "claude")
        self.assertEqual(routing["capability"], "advanced-coding")


class SecurityCriticalResolvesTests(unittest.TestCase):
    def test_security_critical_now_has_an_executable_capability_mapping(self):
        self.assertIn("security-critical", INITIAL_ROUTE_CAPABILITY_BY_CLASS)

    def test_security_critical_high_resolves_to_approved_high_review_route(self):
        service = AdaptiveRoutingService(telemetry_client=_fully_discovered_snapshot())

        resolved, routing = resolve_initial_worker_route(
            _manager_hint("security-critical"),
            plan_risk="HIGH",
            routing_service=service,
        )

        self.assertEqual(resolved["model"], "cc/claude-sonnet-5-high")
        self.assertEqual(resolved["provider"], "claude")
        self.assertEqual(routing["capability"], "review")


class ManagerHintNeverBypassesRouterTests(unittest.TestCase):
    def test_manager_model_and_provider_hint_are_discarded(self):
        hint = _manager_hint("review-critical")
        service = AdaptiveRoutingService(telemetry_client=_fully_discovered_snapshot())

        resolved, _routing = resolve_initial_worker_route(
            hint,
            plan_risk="HIGH",
            routing_service=service,
        )

        self.assertNotEqual(resolved["model"], "GPT-5.6 Sol")
        self.assertNotEqual(resolved["provider"], "omniroute")
        self.assertEqual(resolved["model"], "cc/claude-sonnet-5-high")
        self.assertEqual(resolved["provider"], "claude")

        # The Manager's original suggestion dict is never mutated.
        self.assertEqual(hint["model"], "GPT-5.6 Sol")
        self.assertEqual(hint["provider"], "omniroute")

    def test_gpt_5_6_sol_is_not_aliased_to_any_approved_route(self):
        for route in APPROVED_CATALOG:
            self.assertNotEqual(route.model_id, "GPT-5.6 Sol")


class HardGateInvariantsStillHoldTests(unittest.TestCase):
    def test_risk_incompatible_route_stays_blocked(self):
        catalog = (
            ModelRoute(
                model_id="cc/claude-sonnet-5-low",
                provider="claude",
                execution_path="OMNIROUTE",
                effort="low",
                capabilities=frozenset({"review"}),
                max_risk_level="medium",
                approved=True,
                enabled=True,
            ),
        )

        with self.assertRaises(NoEligibleRouteError):
            select_best_route(
                RoutingRequest(capability="review", risk_level="high"),
                catalog=catalog,
                resources={},
            )

    def test_unapproved_route_stays_blocked(self):
        catalog = (
            ModelRoute(
                model_id="cc/claude-sonnet-5-high",
                provider="claude",
                execution_path="OMNIROUTE",
                effort="high",
                capabilities=frozenset({"review"}),
                max_risk_level="high",
                approved=False,
                enabled=True,
                experimental=True,
            ),
        )

        with self.assertRaises(NoEligibleRouteError):
            select_best_route(
                RoutingRequest(capability="review", risk_level="high"),
                catalog=catalog,
                resources={},
            )

    def test_globally_blocked_terra_model_stays_blocked_even_if_marked_approved(self):
        self.assertTrue(is_blocked_model("gpt-5.6-terra"))

        catalog = (
            ModelRoute(
                model_id="gpt-5.6-terra",
                provider="openai",
                execution_path="OMNIROUTE",
                effort="high",
                capabilities=frozenset({"review"}),
                max_risk_level="critical",
                approved=True,
                enabled=True,
                experimental=False,
            ),
        )

        with self.assertRaises(NoEligibleRouteError):
            select_best_route(
                RoutingRequest(capability="review", risk_level="high"),
                catalog=catalog,
                resources={},
            )


class EscalationLaddersTests(unittest.TestCase):
    def test_ladders_exist_for_all_three_route_classes(self):
        for route_class in ("complex-coding", "review-critical", "security-critical"):
            self.assertIn(route_class, ROUTE_LADDERS)
            self.assertTrue(ROUTE_LADDERS[route_class])

    def test_ladders_never_use_omniroute_as_a_provider_identity(self):
        for route_class, ladder in ROUTE_LADDERS.items():
            for rung in ladder:
                self.assertNotEqual(
                    rung["provider"],
                    "omniroute",
                    f"{route_class!r} rung leaked the execution "
                    "gateway into the provider field",
                )

    def test_review_critical_escalates_from_low_to_high(self):
        worker = {
            "route_class": "review-critical",
            "execution_path": "OMNIROUTE",
            "provider": "claude",
            "model": "cc/claude-sonnet-5-low",
            "effort": "low",
        }

        escalated = next_route(worker)

        self.assertEqual(escalated["model"], "cc/claude-sonnet-5-high")
        self.assertEqual(escalated["provider"], "claude")

    def test_security_critical_escalates_from_low_to_high(self):
        worker = {
            "route_class": "security-critical",
            "execution_path": "OMNIROUTE",
            "provider": "claude",
            "model": "cc/claude-sonnet-5-low",
            "effort": "low",
        }

        escalated = next_route(worker)

        self.assertEqual(escalated["model"], "cc/claude-sonnet-5-high")
        self.assertEqual(escalated["provider"], "claude")

    def test_escalation_beyond_the_ladder_top_fails_closed_not_silently(self):
        worker = {
            "route_class": "complex-coding",
            "execution_path": "OMNIROUTE",
            "provider": "claude",
            "model": "cc/claude-sonnet-5-high",
            "effort": "low",
        }

        with self.assertRaises(EscalationUnavailable):
            next_route(worker)

    def test_unknown_route_class_escalation_is_never_silent(self):
        worker = {
            "route_class": "totally-unmapped-class",
            "execution_path": "OMNIROUTE",
            "provider": "claude",
            "model": "cc/claude-sonnet-5-low",
            "effort": "low",
        }

        with self.assertRaises(EscalationUnavailable):
            next_route(worker)


class OutageFallbackStaysFailClosedTests(unittest.TestCase):
    def test_outage_review_low_prefers_cheaper_approved_route(self):
        service = AdaptiveRoutingService(telemetry_client=_TotalOutageClient())

        decision = service.select_route_for_capability(
            CapabilityClass.REVIEW.value,
            risk_level="low",
        )

        self.assertTrue(decision.degraded)
        self.assertEqual(decision.model, "cc/claude-sonnet-5-low")

    def test_outage_review_high_now_resolves_instead_of_failing(self):
        service = AdaptiveRoutingService(telemetry_client=_TotalOutageClient())

        decision = service.select_route_for_capability(
            CapabilityClass.REVIEW.value,
            risk_level="high",
        )

        self.assertTrue(decision.degraded)
        self.assertEqual(decision.model, "cc/claude-sonnet-5-high")
        self.assertEqual(decision.provider, "claude")

    def test_outage_review_critical_risk_still_fails_closed(self):
        # Matches the existing NoEligibleRouteError contract asserted by
        # test_adaptive_reviewer_routing.test_total_outage_high_risk_fails_closed:
        # the fallback candidate revalidation raises NoEligibleRouteError
        # unwrapped, exactly like every other hard-gate rejection.
        service = AdaptiveRoutingService(telemetry_client=_TotalOutageClient())

        with self.assertRaises(NoEligibleRouteError):
            service.select_route_for_capability(
                CapabilityClass.REVIEW.value,
                risk_level="critical",
            )

    def test_outage_complex_coding_high_now_resolves(self):
        service = AdaptiveRoutingService(telemetry_client=_TotalOutageClient())

        decision = service.select_route_for_capability(
            CapabilityClass.ADVANCED_CODING.value,
            risk_level="high",
        )

        self.assertTrue(decision.degraded)
        self.assertEqual(decision.model, "cc/claude-sonnet-5-high")

    def test_outage_mechanical_resolves_to_cc_sonnet_low_with_big_pickle_disabled(self):
        """Under a total OmniRoute telemetry outage the legacy fallback for
        MECHANICAL now carries two candidates:

            1. oc/big-pickle  (cheapest, disabled → excluded by hard gates)
            2. cc/claude-sonnet-5-low  (approved, enabled → selected)

        select_best_route() enforces enabled=True, so oc/big-pickle is never
        chosen even though it appears in the fallback list.  The decision is
        marked degraded=True and carries cc/claude-sonnet-5-low.
        """
        service = AdaptiveRoutingService(telemetry_client=_TotalOutageClient())

        decision = service.select_route_for_capability(
            CapabilityClass.MECHANICAL.value,
            risk_level="low",
        )

        self.assertTrue(decision.degraded)
        self.assertEqual(decision.model, "cc/claude-sonnet-5-low")
        self.assertEqual(decision.provider, "claude")
        self.assertEqual(decision.execution_path, "OMNIROUTE")
        self.assertNotEqual(decision.model, "oc/big-pickle")


class InitialRoutingUnavailableDiagnosticsTests(unittest.TestCase):
    def test_unmapped_route_class_error_carries_route_class(self):
        try:
            resolve_initial_worker_route(
                _manager_hint("still-not-a-real-route-class"),
                plan_risk="HIGH",
            )
            self.fail("expected InitialRoutingUnavailable")
        except InitialRoutingUnavailable as error:
            self.assertEqual(error.route_class, "still-not-a-real-route-class")
            self.assertIsNone(error.capability)

    def test_router_failure_error_carries_capability_and_cause_type(self):
        service = AdaptiveRoutingService(telemetry_client=_TotalOutageClient())

        try:
            resolve_initial_worker_route(
                _manager_hint("review-critical"),
                plan_risk="CRITICAL",
                routing_service=service,
            )
            self.fail("expected InitialRoutingUnavailable")
        except InitialRoutingUnavailable as error:
            self.assertEqual(error.route_class, "review-critical")
            self.assertEqual(error.capability, "review")
            self.assertEqual(error.risk, "CRITICAL")
            self.assertIsNotNone(error.cause_type)
            # Never leaks telemetry/credential details, only the type name.
            self.assertNotIn("Bearer", str(error))
            self.assertNotIn("api_key", str(error).lower())


if __name__ == "__main__":
    unittest.main()
