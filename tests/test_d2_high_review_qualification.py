"""Nexus v2.0-D.2 High-Risk Reviewer Qualification regression tests.

All tests are fully offline/deterministic: no network, no OmniRoute, no
Codex subprocess calls. They exercise nexus.routing.qualification plus the
existing router/catalog/dispatcher hard gates to prove:

    1. exact route identity for cc/claude-sonnet-5-high;
    2. HIGH review eligibility only when explicitly approved;
    3. CRITICAL remains rejected even for the candidate route;
    4. cc/claude-sonnet-5-low remains capped at MEDIUM;
    5. GPT-5.6 Terra remains forbidden;
    6/7/8. wrong effort/provider/execution_path never match;
    9. the unapproved (default) candidate route stays unavailable;
    10. explicit reviewer override still passes through validation;
    11. adaptive routing respects the risk ceiling;
    12. existing LOW/MEDIUM behavior remains valid.
"""

import unittest

from nexus.dispatchers.review import (
    ReviewRoutingError,
    _explicit_override_decision,
)
from nexus.routing.capabilities import risk_eligible
from nexus.routing.catalog import default_catalog, is_blocked_model
from nexus.routing.models import (
    ModelRoute,
    QuotaState,
    ResourceSnapshot,
    RiskLevel,
    RoutingRequest,
)
from nexus.routing.qualification import (
    CANDIDATE_HIGH_REVIEW_ROUTE,
    is_exact_candidate_identity,
    promoted_route_for_qualified_high_review,
)
from nexus.routing.router import NoEligibleRouteError, select_best_route


class ExactIdentityTestCase(unittest.TestCase):
    def test_candidate_identity_fields(self):
        route = CANDIDATE_HIGH_REVIEW_ROUTE
        self.assertEqual(route.model_id, "cc/claude-sonnet-5-high")
        self.assertEqual(route.provider, "claude")
        self.assertEqual(route.execution_path, "OMNIROUTE")
        self.assertEqual(route.effort, "high")
        self.assertEqual(route.capabilities, frozenset({"review"}))
        self.assertEqual(route.max_risk_level, RiskLevel.HIGH.value)

    def test_candidate_is_not_approved_by_default(self):
        self.assertFalse(CANDIDATE_HIGH_REVIEW_ROUTE.approved)
        self.assertTrue(CANDIDATE_HIGH_REVIEW_ROUTE.experimental)

    def test_exact_match_true(self):
        self.assertTrue(
            is_exact_candidate_identity(
                "cc/claude-sonnet-5-high", "claude", "OMNIROUTE", "high"
            )
        )

    def test_wrong_effort_rejected(self):
        self.assertFalse(
            is_exact_candidate_identity(
                "cc/claude-sonnet-5-high", "claude", "OMNIROUTE", "low"
            )
        )

    def test_wrong_provider_rejected(self):
        self.assertFalse(
            is_exact_candidate_identity(
                "cc/claude-sonnet-5-high", "codex", "OMNIROUTE", "high"
            )
        )

    def test_wrong_execution_path_rejected(self):
        self.assertFalse(
            is_exact_candidate_identity(
                "cc/claude-sonnet-5-high", "claude", "DIRECT", "high"
            )
        )

    def test_wrong_model_rejected(self):
        self.assertFalse(
            is_exact_candidate_identity(
                "cc/claude-sonnet-5-low", "claude", "OMNIROUTE", "high"
            )
        )

    def test_similar_but_different_models_do_not_match(self):
        for model_id in (
            "cc/claude-opus-5-high",
            "cc/claude-sonnet-5-high-preview",
            "cc/claude-sonnet-5",
        ):
            with self.subTest(model_id=model_id):
                self.assertFalse(
                    is_exact_candidate_identity(
                        model_id, "claude", "OMNIROUTE", "high"
                    )
                )


class UnapprovedRouteUnavailableTestCase(unittest.TestCase):
    """Discovery/definition is never approval: the candidate route must be
    unavailable through select_best_route until explicitly promoted."""

    def test_candidate_route_not_eligible_for_high_review(self):
        with self.assertRaises(NoEligibleRouteError):
            select_best_route(
                RoutingRequest(
                    capability="review",
                    risk_level="high",
                ),
                catalog=(CANDIDATE_HIGH_REVIEW_ROUTE,),
                resources={},
            )

    def test_qualified_route_is_in_default_catalog(self):
        route = next(
            route
            for route in default_catalog()
            if route.model_id == "cc/claude-sonnet-5-high"
        )

        self.assertTrue(route.approved)
        self.assertTrue(route.enabled)
        self.assertFalse(route.experimental)
        self.assertEqual(route.provider, "claude")
        self.assertEqual(route.execution_path, "OMNIROUTE")
        self.assertEqual(route.effort, "high")
        self.assertEqual(route.capabilities, frozenset({"review"}))
        self.assertEqual(route.max_risk_level, RiskLevel.HIGH.value)

    def test_explicit_override_allows_qualified_high_route(self):
        decision = _explicit_override_decision(
            "cc/claude-sonnet-5-high",
            "high",
            "high",
        )

        self.assertEqual(decision.model, "cc/claude-sonnet-5-high")
        self.assertEqual(decision.effort, "high")
        self.assertEqual(decision.provider, "claude")
        self.assertEqual(decision.execution_path, "OMNIROUTE")


class PromotedRouteTestCase(unittest.TestCase):
    """Only exercises the shape of a hypothetical promoted route; this test
    never mutates the real production catalog."""

    def setUp(self):
        self.promoted = promoted_route_for_qualified_high_review()

    def test_promoted_route_is_approved_and_enabled(self):
        self.assertTrue(self.promoted.approved)
        self.assertTrue(self.promoted.enabled)
        self.assertFalse(self.promoted.experimental)

    def test_promoted_route_max_risk_is_high_not_critical(self):
        self.assertEqual(self.promoted.max_risk_level, RiskLevel.HIGH.value)
        self.assertNotEqual(self.promoted.max_risk_level, RiskLevel.CRITICAL.value)

    def test_promoted_route_capability_is_review_only(self):
        self.assertEqual(self.promoted.capabilities, frozenset({"review"}))

    def test_promoted_route_high_review_eligible(self):
        selected = select_best_route(
            RoutingRequest(capability="review", risk_level="high"),
            catalog=(self.promoted,),
            resources={},
        )
        self.assertEqual(selected.route.model_id, "cc/claude-sonnet-5-high")

    def test_promoted_route_critical_review_still_rejected(self):
        with self.assertRaises(NoEligibleRouteError):
            select_best_route(
                RoutingRequest(capability="review", risk_level="critical"),
                catalog=(self.promoted,),
                resources={},
            )

    def test_promoted_route_does_not_grant_coding_capability(self):
        self.assertFalse(self.promoted.supports("standard-coding"))
        self.assertFalse(self.promoted.supports("advanced-coding"))
        self.assertFalse(self.promoted.supports("planning"))


class CriticalRemainsRejectedTestCase(unittest.TestCase):
    def test_critical_review_rejected_against_full_default_catalog(self):
        with self.assertRaises(NoEligibleRouteError):
            select_best_route(
                RoutingRequest(capability="review", risk_level="critical"),
                resources={},
            )

    def test_critical_review_rejected_even_with_candidate_and_promoted(self):
        promoted = promoted_route_for_qualified_high_review()
        with self.assertRaises(NoEligibleRouteError):
            select_best_route(
                RoutingRequest(capability="review", risk_level="critical"),
                catalog=default_catalog() + (promoted,),
                resources={},
            )

    def test_risk_eligible_hard_gate_denies_critical_for_high_route(self):
        promoted = promoted_route_for_qualified_high_review()
        self.assertFalse(risk_eligible(promoted, RiskLevel.CRITICAL.value))
        self.assertTrue(risk_eligible(promoted, RiskLevel.HIGH.value))


class SonnetLowUnchangedTestCase(unittest.TestCase):
    def test_sonnet_low_remains_medium_ceiling(self):
        catalog = default_catalog()
        sonnet_low = next(
            route
            for route in catalog
            if route.model_id == "cc/claude-sonnet-5-low"
        )
        self.assertEqual(sonnet_low.max_risk_level, RiskLevel.MEDIUM.value)

    def test_sonnet_low_rejected_for_high_review(self):
        catalog = default_catalog()
        sonnet_low = next(
            route
            for route in catalog
            if route.model_id == "cc/claude-sonnet-5-low"
        )
        with self.assertRaises(NoEligibleRouteError):
            select_best_route(
                RoutingRequest(capability="review", risk_level="high"),
                catalog=(sonnet_low,),
                resources={},
            )

    def test_sonnet_low_still_eligible_for_medium_review(self):
        catalog = default_catalog()
        sonnet_low = next(
            route
            for route in catalog
            if route.model_id == "cc/claude-sonnet-5-low"
        )
        selected = select_best_route(
            RoutingRequest(capability="review", risk_level="medium"),
            catalog=(sonnet_low,),
            resources={},
        )
        self.assertEqual(selected.route.model_id, "cc/claude-sonnet-5-low")


class TerraRemainsForbiddenTestCase(unittest.TestCase):
    def test_terra_blocked_regardless_of_other_fields(self):
        for alias in (
            "gpt-5.6-terra",
            "GPT_5_6_TERRA",
            "gpt/5/6/terra",
            "gpt.5.6.terra",
        ):
            with self.subTest(alias=alias):
                self.assertTrue(is_blocked_model(alias))

    def test_terra_never_selectable_even_if_hypothetically_approved(self):
        terra_route = ModelRoute(
            model_id="gpt-5.6-terra",
            provider="openai",
            execution_path="OMNIROUTE",
            effort="high",
            capabilities=frozenset({"review"}),
            max_risk_level="high",
            approved=True,
            enabled=True,
            experimental=False,
        )
        with self.assertRaises(NoEligibleRouteError):
            select_best_route(
                RoutingRequest(capability="review", risk_level="high"),
                catalog=(terra_route,),
                resources={},
            )


class ExplicitOverrideHardGateTestCase(unittest.TestCase):
    def test_override_respects_risk_ceiling(self):
        with self.assertRaises(ReviewRoutingError):
            _explicit_override_decision(
                "cc/claude-sonnet-5-low",
                "low",
                "high",
            )

    def test_override_allows_approved_route_within_ceiling(self):
        decision = _explicit_override_decision(
            "cc/claude-sonnet-5-low",
            "low",
            "medium",
        )
        self.assertEqual(decision.model, "cc/claude-sonnet-5-low")

    def test_override_rejects_unknown_model(self):
        with self.assertRaises(ReviewRoutingError):
            _explicit_override_decision(
                "nonexistent/model",
                "low",
                "low",
            )


class AdaptiveRoutingRiskCeilingTestCase(unittest.TestCase):
    def test_adaptive_default_catalog_high_review_available(self):
        selected = select_best_route(
            RoutingRequest(capability="review", risk_level="high"),
            resources={},
        )

        self.assertEqual(
            selected.route.model_id,
            "cc/claude-sonnet-5-high",
        )
        self.assertEqual(selected.route.effort, "high")

    def test_adaptive_default_catalog_medium_review_available(self):
        selected = select_best_route(
            RoutingRequest(capability="review", risk_level="medium"),
            resources={},
        )
        self.assertEqual(selected.route.model_id, "cc/claude-sonnet-5-low")

    def test_adaptive_default_catalog_low_review_available(self):
        selected = select_best_route(
            RoutingRequest(capability="review", risk_level="low"),
            resources={},
        )
        self.assertIsNotNone(selected.route.model_id)


class LowEffortPreferenceTestCase(unittest.TestCase):
    """Reasoning effort is minimized only after stronger routing priorities."""

    @staticmethod
    def _route(model_id, provider, effort):
        return ModelRoute(
            model_id=model_id,
            provider=provider,
            execution_path="OMNIROUTE",
            effort=effort,
            capabilities=frozenset({"review"}),
            max_risk_level=RiskLevel.MEDIUM.value,
            approved=True,
            enabled=True,
            experimental=False,
            cost_class="standard",
            quality_tier="high",
        )

    def test_low_effort_wins_when_routes_are_otherwise_equivalent(self):
        low = self._route(
            "test/reviewer-low",
            "provider-low",
            "low",
        )
        high = self._route(
            "test/reviewer-high",
            "provider-high",
            "high",
        )

        selected = select_best_route(
            RoutingRequest(
                capability="review",
                risk_level="medium",
            ),
            catalog=(high, low),
            resources={},
        )

        self.assertEqual(selected.route.model_id, "test/reviewer-low")
        self.assertEqual(selected.route.effort, "low")

    def test_resource_headroom_remains_above_effort_preference(self):
        low = self._route(
            "test/reviewer-low",
            "provider-low",
            "low",
        )
        high = self._route(
            "test/reviewer-high",
            "provider-high",
            "high",
        )

        resources = {
            "provider-low": ResourceSnapshot(
                provider="provider-low",
                state=QuotaState.KNOWN,
                headroom_pct=25,
                healthy=True,
            ),
            "provider-high": ResourceSnapshot(
                provider="provider-high",
                state=QuotaState.KNOWN,
                headroom_pct=80,
                healthy=True,
            ),
        }

        selected = select_best_route(
            RoutingRequest(
                capability="review",
                risk_level="medium",
            ),
            catalog=(low, high),
            resources=resources,
        )

        self.assertEqual(selected.route.model_id, "test/reviewer-high")
        self.assertEqual(selected.route.effort, "high")


if __name__ == "__main__":
    unittest.main()
