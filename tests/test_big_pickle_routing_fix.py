"""Tests for the oc/big-pickle routing fix.

Verifies that oc/big-pickle is present in APPROVED_CATALOG with enabled=False
and is therefore not selectable for any routing request, while the rest of the
catalog continues to function correctly.

Ref: NEXUS_EXECUTION_ROUTE_ADMISSION_AUDIT — RUN-24E46FCA
Root cause: oc/big-pickle (opencode provider) returns HTTP 403 outside the
OpenCode application. Fix: enabled=False in catalog until execution-context
qualification is implemented.
"""

import unittest

from nexus.routing.catalog import (
    APPROVED_CATALOG,
    default_catalog,
    production_eligible,
)
from nexus.routing.models import (
    ModelRoute,
    RoutingRequest,
)
from nexus.routing.router import (
    NoEligibleRouteError,
    select_best_route,
)
from nexus.routing.service import AdaptiveRoutingService
from nexus.routing.telemetry import OmniRouteTelemetrySnapshot


BIG_PICKLE_ID = "oc/big-pickle"


def _find_big_pickle(catalog=None) -> ModelRoute | None:
    src = catalog if catalog is not None else APPROVED_CATALOG
    for route in src:
        if route.model_id == BIG_PICKLE_ID:
            return route
    return None


class TestBigPicklePresence(unittest.TestCase):
    """Catalog presence and enabled=False."""

    def test_big_pickle_present_in_approved_catalog(self):
        """oc/big-pickle must remain in APPROVED_CATALOG (knowledge preserved)."""
        route = _find_big_pickle()
        self.assertIsNotNone(
            route,
            "oc/big-pickle must stay in APPROVED_CATALOG even when disabled.",
        )

    def test_big_pickle_enabled_is_false(self):
        """oc/big-pickle must have enabled=False (the fix)."""
        route = _find_big_pickle()
        self.assertIsNotNone(route)
        self.assertFalse(
            route.enabled,
            "oc/big-pickle must have enabled=False until execution-context "
            "qualification is implemented.",
        )

    def test_big_pickle_approved_unchanged(self):
        """approved field must not have been changed (only enabled changed)."""
        route = _find_big_pickle()
        self.assertIsNotNone(route)
        self.assertTrue(
            route.approved,
            "approved must remain True — only enabled was changed.",
        )

    def test_big_pickle_capabilities_unchanged(self):
        """capabilities=frozenset({'mechanical'}) must be intact."""
        route = _find_big_pickle()
        self.assertIsNotNone(route)
        self.assertEqual(route.capabilities, frozenset({"mechanical"}))

    def test_big_pickle_cost_class_unchanged(self):
        """cost_class must still be 'free'."""
        route = _find_big_pickle()
        self.assertIsNotNone(route)
        self.assertEqual(route.cost_class, "free")

    def test_big_pickle_provider_unchanged(self):
        """provider must still be 'opencode'."""
        route = _find_big_pickle()
        self.assertIsNotNone(route)
        self.assertEqual(route.provider, "opencode")

    def test_big_pickle_not_in_production_eligible(self):
        """production_eligible() must exclude disabled routes."""
        eligible = production_eligible(default_catalog())
        ids = [r.model_id for r in eligible]
        self.assertNotIn(
            BIG_PICKLE_ID,
            ids,
            "oc/big-pickle must not appear in production_eligible() when disabled.",
        )


class TestBigPickleNotSelectedForMechanicalLow(unittest.TestCase):
    """select_best_route() must not select oc/big-pickle for mechanical/low."""

    def test_big_pickle_not_selected_mechanical_low(self):
        """select_best_route(mechanical, low) must not return oc/big-pickle."""
        request = RoutingRequest(capability="mechanical", risk_level="low")
        result = select_best_route(request)
        self.assertNotEqual(
            result.route.model_id,
            BIG_PICKLE_ID,
            f"oc/big-pickle must not be selected for mechanical/low "
            f"while enabled=False. Got: {result.route.model_id}",
        )

    def test_mechanical_low_selects_executable_route(self):
        """An eligible, executable route must be selected for mechanical/low."""
        request = RoutingRequest(capability="mechanical", risk_level="low")
        # Must not raise — there must be at least one eligible route.
        result = select_best_route(request)
        self.assertIsNotNone(result.route.model_id)
        self.assertNotEqual(result.route.model_id, "")
        # The selected route must not be big-pickle (belt-and-suspenders).
        self.assertNotEqual(result.route.model_id, BIG_PICKLE_ID)

    def test_big_pickle_explicitly_in_blocked_models(self):
        """If oc/big-pickle is passed as blocked_models it is still excluded."""
        request = RoutingRequest(
            capability="mechanical",
            risk_level="low",
            blocked_models=frozenset({BIG_PICKLE_ID}),
        )
        result = select_best_route(request)
        self.assertNotEqual(result.route.model_id, BIG_PICKLE_ID)


class TestHardGatesStillWork(unittest.TestCase):
    """Existing safety gates must be unaffected by the catalog change."""

    def test_terra_blocked_model(self):
        """Terra models must still be blocked by is_blocked_model."""
        from nexus.routing.catalog import is_blocked_model
        self.assertTrue(is_blocked_model("gpt-5.6-terra"))
        self.assertTrue(is_blocked_model("GPT_5_6_TERRA"))
        self.assertTrue(is_blocked_model("gpt/5/6/terra"))
        self.assertFalse(is_blocked_model("cc/claude-sonnet-5-low"))
        self.assertFalse(is_blocked_model("oc/big-pickle"))

    def test_terra_not_selected(self):
        """Terra must never be selected even if injected into catalog."""
        terra_route = ModelRoute(
            model_id="gpt-5.6-terra",
            provider="openai",
            execution_path="OMNIROUTE",
            effort="low",
            capabilities=frozenset({"mechanical"}),
            max_risk_level="low",
            approved=True,
            enabled=True,
            experimental=False,
            cost_class="free",
            quality_tier="high",
        )
        normal = ModelRoute(
            model_id="cc/claude-sonnet-5-low",
            provider="claude",
            execution_path="OMNIROUTE",
            effort="low",
            capabilities=frozenset({"mechanical"}),
            max_risk_level="medium",
            approved=True,
            enabled=True,
            experimental=False,
            cost_class="standard",
            quality_tier="high",
        )
        request = RoutingRequest(capability="mechanical", risk_level="low")
        result = select_best_route(request, catalog=(terra_route, normal))
        self.assertNotEqual(result.route.model_id, "gpt-5.6-terra")
        self.assertEqual(result.route.model_id, "cc/claude-sonnet-5-low")

    def test_no_eligible_route_raises(self):
        """NoEligibleRouteError must still be raised with empty catalog."""
        from nexus.routing.router import NoEligibleRouteError
        request = RoutingRequest(capability="mechanical", risk_level="low")
        with self.assertRaises(NoEligibleRouteError):
            select_best_route(request, catalog=())

    def test_disabled_only_catalog_raises(self):
        """NoEligibleRouteError must be raised when only disabled routes exist."""
        disabled = ModelRoute(
            model_id="oc/big-pickle",
            provider="opencode",
            execution_path="OMNIROUTE",
            effort="low",
            capabilities=frozenset({"mechanical"}),
            max_risk_level="low",
            approved=True,
            enabled=False,
            experimental=False,
            cost_class="free",
            quality_tier="basic",
        )
        request = RoutingRequest(capability="mechanical", risk_level="low")
        with self.assertRaises(NoEligibleRouteError):
            select_best_route(request, catalog=(disabled,))


class TestHighCriticalRoutingUnaffected(unittest.TestCase):
    """HIGH and CRITICAL routing must not regress."""

    def test_review_high_risk_eligible(self):
        """cc/claude-sonnet-5-high must still be selectable for review/high."""
        request = RoutingRequest(capability="review", risk_level="high")
        result = select_best_route(request)
        self.assertNotEqual(result.route.model_id, BIG_PICKLE_ID)
        # Must succeed — there is an approved review/high route.
        self.assertIsNotNone(result.route.model_id)

    def test_advanced_coding_high_risk_eligible(self):
        """cc/claude-sonnet-5-high must still be selectable for advanced-coding/high."""
        request = RoutingRequest(capability="advanced-coding", risk_level="high")
        result = select_best_route(request)
        self.assertNotEqual(result.route.model_id, BIG_PICKLE_ID)
        self.assertIsNotNone(result.route.model_id)

    def test_critical_risk_mechanical_raises(self):
        """No route is approved for mechanical/critical — must fail-closed."""
        request = RoutingRequest(capability="mechanical", risk_level="critical")
        with self.assertRaises(NoEligibleRouteError):
            select_best_route(request)


class _TotalOutage:
    """Simulates complete OmniRoute telemetry unavailability."""
    def collect(self):
        return OmniRouteTelemetrySnapshot(
            errors=(
                "/v1/models connection failed",
                "/api/usage/quota connection failed",
                "/api/monitoring/health connection failed",
            ),
        )


class TestOutageFallbackMechanical(unittest.TestCase):
    """Outage-path behavior for mechanical routing with big-pickle disabled."""

    def test_outage_mechanical_low_resolves_to_cc_sonnet_low(self):
        """Under total telemetry outage, mechanical/LOW must resolve to
        cc/claude-sonnet-5-low (the second, enabled entry in the fallback)."""
        svc = AdaptiveRoutingService(telemetry_client=_TotalOutage())
        decision = svc.select_route_for_capability("mechanical", risk_level="low")
        self.assertEqual(decision.model, "cc/claude-sonnet-5-low")
        self.assertEqual(decision.provider, "claude")
        self.assertEqual(decision.execution_path, "OMNIROUTE")
        self.assertTrue(decision.degraded)

    def test_outage_big_pickle_not_selected(self):
        """oc/big-pickle must never appear in the outage fallback decision."""
        svc = AdaptiveRoutingService(telemetry_client=_TotalOutage())
        decision = svc.select_route_for_capability("mechanical", risk_level="low")
        self.assertNotEqual(decision.model, "oc/big-pickle")

    def test_outage_fallback_revalidated_by_hard_gates(self):
        """The outage fallback passes through select_best_route() which enforces
        enabled, approved, capability, and risk hard gates.  Inject a catalog
        where the only mechanical fallback candidate is disabled and confirm
        NoEligibleRouteError propagates — proving hard gates are active."""
        from nexus.routing.models import ModelRoute

        disabled_only_fallback = {
            "mechanical": (
                ("oc/big-pickle", "opencode", "low", "OMNIROUTE"),
            ),
        }
        disabled_route = _find_big_pickle()
        self.assertIsNotNone(disabled_route)
        svc = AdaptiveRoutingService(
            telemetry_client=_TotalOutage(),
            approved_catalog=(disabled_route,),
            legacy_fallback=disabled_only_fallback,
        )
        with self.assertRaises(NoEligibleRouteError):
            svc.select_route_for_capability("mechanical", risk_level="low")

    def test_outage_all_fallbacks_ineligible_fail_closed(self):
        """When every mechanical fallback entry is absent from the approved
        catalog (unknown model_id), AdaptiveRoutingUnavailableError is raised
        — the system fails closed rather than routing to an unknown route."""
        from nexus.routing.service import AdaptiveRoutingUnavailableError

        unknown_fallback = {
            "mechanical": (
                ("xx/nonexistent-model", "unknown", "low", "OMNIROUTE"),
            ),
        }
        svc = AdaptiveRoutingService(
            telemetry_client=_TotalOutage(),
            legacy_fallback=unknown_fallback,
        )
        with self.assertRaises((NoEligibleRouteError, AdaptiveRoutingUnavailableError)):
            svc.select_route_for_capability("mechanical", risk_level="low")


if __name__ == "__main__":
    unittest.main()
