"""Nexus v2.0-D.3 HIGH advanced-coding qualification regressions."""

import unittest

from nexus.routing.catalog import default_catalog
from nexus.routing.models import RiskLevel, RoutingRequest
from nexus.routing.qualification import (
    CANDIDATE_HIGH_CODING_ROUTE,
    is_exact_high_coding_candidate_identity,
    promoted_route_for_qualified_high_coding,
)
from nexus.routing.router import NoEligibleRouteError, select_best_route


class HighCodingCandidateIdentityTests(unittest.TestCase):
    def test_candidate_exact_identity(self):
        route = CANDIDATE_HIGH_CODING_ROUTE

        self.assertEqual(
            route.model_id,
            "cc/claude-sonnet-5-high",
        )
        self.assertEqual(route.provider, "claude")
        self.assertEqual(
            route.execution_path,
            "OMNIROUTE",
        )
        self.assertEqual(route.effort, "low")
        self.assertEqual(
            route.capabilities,
            frozenset({"advanced-coding"}),
        )
        self.assertEqual(
            route.max_risk_level,
            RiskLevel.HIGH.value,
        )

    def test_candidate_remains_unapproved_definition(self):
        self.assertFalse(
            CANDIDATE_HIGH_CODING_ROUTE.approved
        )
        self.assertTrue(
            CANDIDATE_HIGH_CODING_ROUTE.experimental
        )

    def test_exact_identity_matches(self):
        self.assertTrue(
            is_exact_high_coding_candidate_identity(
                "cc/claude-sonnet-5-high",
                "claude",
                "OMNIROUTE",
                "low",
            )
        )

    def test_wrong_effort_does_not_match(self):
        self.assertFalse(
            is_exact_high_coding_candidate_identity(
                "cc/claude-sonnet-5-high",
                "claude",
                "OMNIROUTE",
                "high",
            )
        )


class HighCodingProductionRouteTests(unittest.TestCase):
    def production_route(self):
        matches = [
            route
            for route in default_catalog()
            if route.model_id == "cc/claude-sonnet-5-high"
            and route.provider == "claude"
            and route.execution_path == "OMNIROUTE"
            and route.effort == "low"
            and route.capabilities
                == frozenset({"advanced-coding"})
        ]

        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_exact_route_is_production_approved(self):
        route = self.production_route()

        self.assertTrue(route.approved)
        self.assertTrue(route.enabled)
        self.assertFalse(route.experimental)
        self.assertEqual(
            route.max_risk_level,
            RiskLevel.HIGH.value,
        )

    def test_high_advanced_coding_selects_exact_route(self):
        selected = select_best_route(
            RoutingRequest(
                capability="advanced-coding",
                risk_level="high",
            ),
            catalog=default_catalog(),
            resources={},
        )

        route = selected.route

        self.assertEqual(
            route.model_id,
            "cc/claude-sonnet-5-high",
        )
        self.assertEqual(route.provider, "claude")
        self.assertEqual(
            route.execution_path,
            "OMNIROUTE",
        )
        self.assertEqual(route.effort, "low")

    def test_critical_advanced_coding_remains_blocked(self):
        with self.assertRaises(NoEligibleRouteError):
            select_best_route(
                RoutingRequest(
                    capability="advanced-coding",
                    risk_level="critical",
                ),
                catalog=default_catalog(),
                resources={},
            )

    def test_low_effort_high_coding_route_is_not_reviewer(self):
        route = self.production_route()

        self.assertFalse(route.supports("review"))
        self.assertFalse(route.supports("planning"))
        self.assertFalse(
            route.supports("standard-coding")
        )

    def test_existing_high_reviewer_remains_separate(self):
        matches = [
            route
            for route in default_catalog()
            if route.model_id == "cc/claude-sonnet-5-high"
            and route.provider == "claude"
            and route.execution_path == "OMNIROUTE"
            and route.effort == "high"
            and route.capabilities
                == frozenset({"review"})
        ]

        self.assertEqual(len(matches), 1)
        self.assertEqual(
            matches[0].max_risk_level,
            RiskLevel.HIGH.value,
        )

    def test_promoted_helper_matches_production_route(self):
        self.assertEqual(
            promoted_route_for_qualified_high_coding(),
            self.production_route(),
        )


if __name__ == "__main__":
    unittest.main()
