import math
import unittest

from nexus.policies.escalation import ROUTE_LADDERS
from nexus.routing.catalog import (
    APPROVED_CATALOG,
    default_catalog,
    is_blocked_model,
    production_eligible,
)
from nexus.routing.models import (
    HeadroomBand,
    ModelRoute,
    QuotaState,
    ResourceSnapshot,
    RoutingRequest,
)
from nexus.routing.resources import (
    ProviderOverrides,
    classify_headroom,
    snapshot_from_quota_response,
)
from nexus.routing.router import (
    InvalidRoutingRequestError,
    NoEligibleRouteError,
    select_best_route,
)


CLAUDE = ModelRoute(
    model_id="cc/claude-sonnet-5-low",
    provider="claude",
    execution_path="OMNIROUTE",
    capabilities=frozenset(
        {
            "standard-coding",
            "advanced-coding",
        }
    ),
    max_risk_level="medium",
    approved=True,
    cost_class="standard",
    quality_tier="high",
)

CODEX = ModelRoute(
    model_id="cx/gpt-5.6-luna-low",
    provider="codex",
    execution_path="OMNIROUTE",
    capabilities=frozenset(
        {
            "standard-coding",
            "advanced-coding",
        }
    ),
    max_risk_level="medium",
    approved=True,
    cost_class="standard",
    quality_tier="high",
)

MECHANICAL_ONLY = ModelRoute(
    model_id="oc/big-pickle",
    provider="opencode",
    execution_path="OMNIROUTE",
    capabilities=frozenset(
        {
            "mechanical",
        }
    ),
    max_risk_level="low",
    approved=True,
)


def route(
    model_id,
    provider,
    *,
    capabilities=("standard-coding",),
    max_risk_level="medium",
    approved=True,
    experimental=False,
    cost_class="standard",
    quality_tier="high",
):
    return ModelRoute(
        model_id=model_id,
        provider=provider,
        execution_path="OMNIROUTE",
        capabilities=frozenset(capabilities),
        max_risk_level=max_risk_level,
        approved=approved,
        experimental=experimental,
        cost_class=cost_class,
        quality_tier=quality_tier,
    )


def resources(**provider_pct):
    result = {}

    for provider, pct in provider_pct.items():
        if pct is None:
            result[provider] = ResourceSnapshot(
                provider=provider,
                state=QuotaState.UNKNOWN,
                headroom_pct=None,
            )

        elif pct <= 0:
            result[provider] = ResourceSnapshot(
                provider=provider,
                state=QuotaState.EXHAUSTED,
                headroom_pct=0,
            )

        else:
            result[provider] = ResourceSnapshot(
                provider=provider,
                state=QuotaState.KNOWN,
                headroom_pct=pct,
            )

    return result


class HeadroomBandTestCase(unittest.TestCase):

    def test_reserve_band_below_10_pct(self):
        snapshot = ResourceSnapshot(
            provider="codex",
            state=QuotaState.KNOWN,
            headroom_pct=3,
        )

        self.assertEqual(
            classify_headroom(snapshot),
            HeadroomBand.RESERVE,
        )

    def test_preferred_band_at_or_above_50_pct(self):
        snapshot = ResourceSnapshot(
            provider="claude",
            state=QuotaState.KNOWN,
            headroom_pct=80,
        )

        self.assertEqual(
            classify_headroom(snapshot),
            HeadroomBand.PREFERRED,
        )

    def test_normal_band(self):
        snapshot = ResourceSnapshot(
            provider="x",
            state=QuotaState.KNOWN,
            headroom_pct=25,
        )

        self.assertEqual(
            classify_headroom(snapshot),
            HeadroomBand.NORMAL,
        )

    def test_conserve_band(self):
        snapshot = ResourceSnapshot(
            provider="x",
            state=QuotaState.KNOWN,
            headroom_pct=15,
        )

        self.assertEqual(
            classify_headroom(snapshot),
            HeadroomBand.CONSERVE,
        )

    def test_unknown_is_neutral_not_100(self):
        snapshot = ResourceSnapshot(
            provider="x",
            state=QuotaState.UNKNOWN,
            headroom_pct=None,
        )

        self.assertEqual(
            classify_headroom(snapshot),
            HeadroomBand.NEUTRAL,
        )

    def test_quota_total_null_remains_unknown(self):
        snapshot = snapshot_from_quota_response(
            provider="codex",
            quota_total=None,
            quota_used=0,
        )

        self.assertEqual(
            snapshot.state,
            QuotaState.UNKNOWN,
        )

        self.assertIsNone(
            snapshot.headroom_pct
        )

    def test_known_quota_computes_headroom(self):
        snapshot = snapshot_from_quota_response(
            provider="claude",
            quota_total=100,
            quota_used=20,
        )

        self.assertEqual(
            snapshot.state,
            QuotaState.KNOWN,
        )

        self.assertEqual(
            snapshot.headroom_pct,
            80,
        )

    def test_fully_used_quota_becomes_exhausted(self):
        snapshot = snapshot_from_quota_response(
            provider="x",
            quota_total=100,
            quota_used=100,
        )

        self.assertEqual(
            snapshot.state,
            QuotaState.EXHAUSTED,
        )


class ResourceValidationTestCase(unittest.TestCase):

    def test_override_rejects_negative_pct(self):
        with self.assertRaises(ValueError):
            ProviderOverrides(
                {
                    "codex": -10,
                }
            )

    def test_override_rejects_above_100_pct(self):
        with self.assertRaises(ValueError):
            ProviderOverrides(
                {
                    "codex": 130,
                }
            )

    def test_override_rejects_nan(self):
        with self.assertRaises(ValueError):
            ProviderOverrides(
                {
                    "codex": math.nan,
                }
            )

    def test_override_rejects_infinity(self):
        with self.assertRaises(ValueError):
            ProviderOverrides(
                {
                    "codex": math.inf,
                }
            )

    def test_override_is_marked_override(self):
        overrides = ProviderOverrides(
            {
                "claude": 80,
            }
        )

        snapshot = overrides.as_snapshot(
            "claude"
        )

        self.assertEqual(
            snapshot.state,
            QuotaState.OVERRIDE,
        )

        self.assertEqual(
            snapshot.headroom_pct,
            80,
        )

    def test_missing_override_is_unknown(self):
        overrides = ProviderOverrides(
            {
                "claude": 80,
            }
        )

        snapshot = overrides.as_snapshot(
            "codex"
        )

        self.assertEqual(
            snapshot.state,
            QuotaState.UNKNOWN,
        )


class CatalogSafetyTestCase(unittest.TestCase):

    def test_new_model_route_is_not_approved_by_default(self):
        discovered = ModelRoute(
            model_id="some/new-model",
            provider="new-provider",
            execution_path="OMNIROUTE",
            capabilities=frozenset(
                {
                    "standard-coding",
                }
            ),
        )

        self.assertFalse(
            discovered.approved
        )

    def test_discovered_unknown_model_cannot_enter_production(self):
        discovered = ModelRoute(
            model_id="some/new-model",
            provider="new-provider",
            execution_path="OMNIROUTE",
            capabilities=frozenset(
                {
                    "standard-coding",
                }
            ),
        )

        request = RoutingRequest(
            capability="standard-coding"
        )

        with self.assertRaises(
            NoEligibleRouteError
        ):
            select_best_route(
                request,
                catalog=(discovered,),
                resources=resources(
                    **{
                        "new-provider": 100,
                    }
                ),
            )

    def test_seed_catalog_routes_explicitly_approved(self):
        self.assertTrue(
            all(
                item.approved
                for item in APPROVED_CATALOG
            )
        )

    def test_big_pickle_provider_is_opencode(self):
        big_pickle = next(
            item
            for item in default_catalog()
            if item.model_id == "oc/big-pickle"
        )

        self.assertEqual(
            big_pickle.provider,
            "opencode",
        )

        self.assertEqual(
            big_pickle.execution_path,
            "OMNIROUTE",
        )

    def test_experimental_not_production_by_default(self):
        experimental = route(
            "x/experimental",
            "x",
            experimental=True,
        )

        self.assertEqual(
            production_eligible(
                (experimental,)
            ),
            (),
        )

    def test_experimental_can_be_explicitly_allowed(self):
        experimental = route(
            "x/experimental",
            "x",
            experimental=True,
        )

        self.assertEqual(
            production_eligible(
                (experimental,),
                allow_experimental=True,
            ),
            (experimental,),
        )


class TerraSafetyTestCase(unittest.TestCase):

    def test_terra_aliases_are_globally_blocked(self):
        aliases = (
            "gpt-5.6-terra",
            "GPT_5_6_TERRA",
            "gpt/5/6/terra",
            "gpt.5.6.terra",
            "cx/gpt-5.6-terra-low",
            "dva/gpt-5-6-terra-high",
            "codex/GPT_5_6_TERRA_MAX",
        )

        for model_id in aliases:
            with self.subTest(model_id=model_id):
                self.assertTrue(
                    is_blocked_model(
                        model_id
                    )
                )

    def test_terra_cannot_win_even_with_100_pct(self):
        terra = route(
            "cx/gpt-5.6-terra-low",
            "codex",
        )

        request = RoutingRequest(
            capability="standard-coding",
        )

        with self.assertRaises(
            NoEligibleRouteError
        ):
            select_best_route(
                request,
                catalog=(terra,),
                resources=resources(
                    codex=100
                ),
            )


class CapabilityAndRiskGateTestCase(unittest.TestCase):

    def test_high_quota_cannot_bypass_capability(self):
        request = RoutingRequest(
            capability="advanced-coding",
        )

        selected = select_best_route(
            request,
            catalog=(
                MECHANICAL_ONLY,
                CODEX,
            ),
            resources=resources(
                opencode=99,
                codex=1,
            ),
        )

        self.assertEqual(
            selected.route.provider,
            "codex",
        )

    def test_unknown_capability_fails_closed(self):
        request = RoutingRequest(
            capability="whatever",
        )

        with self.assertRaises(
            InvalidRoutingRequestError
        ):
            select_best_route(
                request,
                catalog=(CLAUDE,),
            )

    def test_unknown_risk_level_fails_closed(self):
        request = RoutingRequest(
            capability="standard-coding",
            risk_level="extreme",
        )

        with self.assertRaises(
            InvalidRoutingRequestError
        ):
            select_best_route(
                request,
                catalog=(CLAUDE,),
            )

    def test_low_risk_route_cannot_serve_high_risk_request(self):
        low_route = route(
            "x/low",
            "x",
            max_risk_level="low",
        )

        request = RoutingRequest(
            capability="standard-coding",
            risk_level="high",
        )

        with self.assertRaises(
            NoEligibleRouteError
        ):
            select_best_route(
                request,
                catalog=(low_route,),
                resources=resources(
                    x=100,
                ),
            )

    def test_medium_route_cannot_serve_high_risk_request(self):
        request = RoutingRequest(
            capability="standard-coding",
            risk_level="high",
        )

        with self.assertRaises(
            NoEligibleRouteError
        ):
            select_best_route(
                request,
                catalog=(CLAUDE,),
                resources=resources(
                    claude=100,
                ),
            )

    def test_explicit_high_risk_approved_route_can_serve_high(self):
        strong = route(
            "x/strong",
            "strong-provider",
            max_risk_level="high",
        )

        request = RoutingRequest(
            capability="standard-coding",
            risk_level="high",
        )

        selected = select_best_route(
            request,
            catalog=(strong,),
            resources=resources(
                **{
                    "strong-provider": 50,
                }
            ),
        )

        self.assertEqual(
            selected.route.model_id,
            "x/strong",
        )


class ExactHeadroomRankingTestCase(unittest.TestCase):

    def test_80_beats_51_inside_preferred_band(self):
        high = route(
            "zz/high-resource",
            "high-provider",
        )

        low = route(
            "aa/lower-resource",
            "low-provider",
        )

        request = RoutingRequest(
            capability="standard-coding",
        )

        selected = select_best_route(
            request,
            catalog=(
                low,
                high,
            ),
            resources=resources(
                **{
                    "high-provider": 80,
                    "low-provider": 51,
                }
            ),
        )

        self.assertEqual(
            selected.route.provider,
            "high-provider",
        )

    def test_51_beats_50_inside_preferred_band(self):
        higher = route(
            "zz/51",
            "provider-51",
        )

        lower = route(
            "aa/50",
            "provider-50",
        )

        request = RoutingRequest(
            capability="standard-coding",
        )

        selected = select_best_route(
            request,
            catalog=(
                lower,
                higher,
            ),
            resources=resources(
                **{
                    "provider-51": 51,
                    "provider-50": 50,
                }
            ),
        )

        self.assertEqual(
            selected.route.provider,
            "provider-51",
        )

    def test_known_80_beats_unknown_neutral(self):
        known = route(
            "zz/known",
            "known-provider",
        )

        unknown = route(
            "aa/unknown",
            "unknown-provider",
        )

        request = RoutingRequest(
            capability="standard-coding",
        )

        selected = select_best_route(
            request,
            catalog=(
                unknown,
                known,
            ),
            resources={
                "known-provider": ResourceSnapshot(
                    provider="known-provider",
                    state=QuotaState.KNOWN,
                    headroom_pct=80,
                ),
                "unknown-provider": ResourceSnapshot(
                    provider="unknown-provider",
                    state=QuotaState.UNKNOWN,
                    headroom_pct=None,
                ),
            },
        )

        self.assertEqual(
            selected.route.provider,
            "known-provider",
        )


class RouteSelectionTestCase(unittest.TestCase):

    def test_claude_80_beats_codex_3(self):
        selected = select_best_route(
            RoutingRequest(
                capability="standard-coding"
            ),
            catalog=(
                CLAUDE,
                CODEX,
            ),
            resources=resources(
                claude=80,
                codex=3,
            ),
        )

        self.assertEqual(
            selected.route.provider,
            "claude",
        )

        self.assertEqual(
            selected.fallbacks[0].route.provider,
            "codex",
        )

    def test_exhausted_provider_excluded(self):
        selected = select_best_route(
            RoutingRequest(
                capability="standard-coding"
            ),
            catalog=(
                CLAUDE,
                CODEX,
            ),
            resources=resources(
                claude=0,
                codex=50,
            ),
        )

        self.assertEqual(
            selected.route.provider,
            "codex",
        )

        self.assertEqual(
            len(selected.fallbacks),
            0,
        )

    def test_unhealthy_provider_excluded(self):
        selected = select_best_route(
            RoutingRequest(
                capability="standard-coding"
            ),
            catalog=(
                CLAUDE,
                CODEX,
            ),
            resources={
                "claude": ResourceSnapshot(
                    provider="claude",
                    state=QuotaState.KNOWN,
                    headroom_pct=80,
                    healthy=False,
                ),
                "codex": ResourceSnapshot(
                    provider="codex",
                    state=QuotaState.KNOWN,
                    headroom_pct=50,
                    healthy=True,
                ),
            },
        )

        self.assertEqual(
            selected.route.provider,
            "codex",
        )

    def test_deterministic_tie_breaker(self):
        route_a = route(
            "zz/route",
            "provider-z",
        )

        route_b = route(
            "aa/route",
            "provider-a",
        )

        selected = select_best_route(
            RoutingRequest(
                capability="standard-coding"
            ),
            catalog=(
                route_a,
                route_b,
            ),
            resources=resources(
                **{
                    "provider-z": 50,
                    "provider-a": 50,
                }
            ),
        )

        self.assertEqual(
            selected.route.model_id,
            "aa/route",
        )

    def test_fallbacks_ranked(self):
        high = route(
            "x/high",
            "high",
        )

        mid = route(
            "x/mid",
            "mid",
        )

        low = route(
            "x/low",
            "low",
        )

        selected = select_best_route(
            RoutingRequest(
                capability="standard-coding"
            ),
            catalog=(
                low,
                mid,
                high,
            ),
            resources=resources(
                high=90,
                mid=25,
                low=5,
            ),
        )

        self.assertEqual(
            selected.route.provider,
            "high",
        )

        self.assertEqual(
            [
                item.route.provider
                for item in selected.fallbacks
            ],
            [
                "mid",
                "low",
            ],
        )

    def test_blocked_provider_excluded(self):
        selected = select_best_route(
            RoutingRequest(
                capability="standard-coding",
                blocked_providers=frozenset(
                    {
                        "claude",
                    }
                ),
            ),
            catalog=(
                CLAUDE,
                CODEX,
            ),
            resources=resources(
                claude=80,
                codex=50,
            ),
        )

        self.assertEqual(
            selected.route.provider,
            "codex",
        )

    def test_blocked_model_excluded(self):
        selected = select_best_route(
            RoutingRequest(
                capability="standard-coding",
                blocked_models=frozenset(
                    {
                        CLAUDE.model_id,
                    }
                ),
            ),
            catalog=(
                CLAUDE,
                CODEX,
            ),
            resources=resources(
                claude=80,
                codex=50,
            ),
        )

        self.assertEqual(
            selected.route.provider,
            "codex",
        )

    def test_no_candidate_fails_closed(self):
        with self.assertRaises(
            NoEligibleRouteError
        ):
            select_best_route(
                RoutingRequest(
                    capability="high-risk",
                    risk_level="high",
                ),
                resources={},
            )

    def test_reason_contains_capability_risk_and_resource(self):
        selected = select_best_route(
            RoutingRequest(
                capability="standard-coding",
                risk_level="low",
            ),
            catalog=(
                CLAUDE,
                CODEX,
            ),
            resources=resources(
                claude=80,
                codex=3,
            ),
        )

        self.assertIn(
            "standard-coding",
            selected.reason,
        )

        self.assertIn(
            "low",
            selected.reason,
        )

        self.assertIn(
            "80%",
            selected.reason,
        )


class BackwardsCompatibilityTestCase(unittest.TestCase):

    def test_existing_v19_route_ladders_unchanged(self):
        self.assertIn(
            "mechanical",
            ROUTE_LADDERS,
        )

        self.assertIn(
            "standard-coding",
            ROUTE_LADDERS,
        )

        mechanical = ROUTE_LADDERS[
            "mechanical"
        ]

        self.assertEqual(
            mechanical[0]["model"],
            "oc/big-pickle",
        )

        self.assertEqual(
            mechanical[1]["model"],
            "cc/claude-sonnet-5-low",
        )

        standard = ROUTE_LADDERS[
            "standard-coding"
        ]

        self.assertEqual(
            standard[0]["model"],
            "cc/claude-sonnet-5-low",
        )


if __name__ == "__main__":
    unittest.main()
