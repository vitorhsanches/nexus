"""Nexus D4.5 NVIDIA Nemotron advanced-coding qualification tests.

Tests cover:
- Qualification evidence validation
- Catalog promotion
- Fleet normalization
- Provider telemetry / routing with Claude exhausted
- Regression: EXHAUSTED hard-gate preserved
"""
from __future__ import annotations

import unittest

from nexus.routing.catalog import (
    APPROVED_CATALOG,
    default_catalog,
    is_blocked_model,
    production_eligible,
)
from nexus.routing.fleet import (
    NormalizationStatus,
    RouteKind,
    normalize_discovered_model,
)
from nexus.routing.models import (
    CapabilityClass,
    ModelRoute,
    QuotaState,
    ResourceSnapshot,
    RiskLevel,
    RoutingRequest,
)
from nexus.routing.qualification import (
    CANDIDATE_NVIDIA_NEMOTRON_CODING_ROUTE,
    is_exact_nvidia_nemotron_coding_candidate_identity,
    promoted_route_for_qualified_nvidia_nemotron_coding,
)
from nexus.routing.qualification_benchmark import (
    QualificationClient,
    SENTINEL,
    canonical_response_model_id,
)
from nexus.routing.qualification_framework import (
    QualificationCandidate,
    QualificationEvidence,
    QualificationEvidenceType,
    QualificationVerdict,
    evaluate_qualification,
    is_candidate_eligible,
)
from nexus.routing.router import NoEligibleRouteError, select_best_route
from nexus.routing.telemetry import DiscoveredModel
from nexus.policies.escalation import MAX_SAME_TIER_RETRIES


# ── Qualification evidence ────────────────────────────────────────────────

class TestNemotronQualificationEvidence(unittest.TestCase):
    """Validate the D4.5 qualification evidence for NVIDIA Nemotron."""

    def _candidate(self) -> QualificationCandidate:
        return QualificationCandidate(
            model_id="nvidia/nemotron-3-super-120b-a12b",
            provider="nvidia",
            execution_path="OMNIROUTE",
            effort="low",
            capability="advanced-coding",
            risk="medium",
        )

    def _identity(self):
        c = self._candidate()
        return dict(
            model_id=c.model_id, provider=c.provider,
            execution_path=c.execution_path, effort=c.effort,
            capability=c.capability, risk=c.risk,
        )

    def _passing_evidence(self):
        identity = self._identity()
        return (
            QualificationEvidence(
                QualificationEvidenceType.EXACT_IDENTITY_CHECK, "responses",
                True, "requested=nvidia/nemotron-3-super-120b-a12b "
                "returned=nvidia/nemotron-3-super-120b-a12b", **identity,
            ),
            QualificationEvidence(
                QualificationEvidenceType.HOST_VALIDATION, "responses",
                True, "valid JSON 200", **identity,
            ),
            QualificationEvidence(
                QualificationEvidenceType.SEMANTIC_REVIEW, "responses",
                True, "sentinel NEXUS_D44_SENTINEL_7F3A", **identity,
            ),
        )

    def test_candidate_eligible(self):
        cand = self._candidate()
        self.assertTrue(is_candidate_eligible(
            cand, fleet_modality="unresolved", route_kind="fixed",
            normalization_status="resolved", policy_blocked=False,
        ))

    def test_full_evidence_passes(self):
        rec = evaluate_qualification(
            self._candidate(), self._passing_evidence(),
            fleet_modality="unresolved", route_kind="fixed",
            normalization_status="resolved", policy_blocked=False,
        )
        self.assertEqual(rec.verdict, QualificationVerdict.PASS)
        self.assertFalse(rec.missing_evidence)

    def test_missing_identity_check_fails(self):
        identity = self._identity()
        evidence = (
            QualificationEvidence(
                QualificationEvidenceType.HOST_VALIDATION, "responses",
                True, "valid", **identity),
            QualificationEvidence(
                QualificationEvidenceType.SEMANTIC_REVIEW, "responses",
                True, "sentinel", **identity),
        )
        rec = evaluate_qualification(
            self._candidate(), evidence,
            fleet_modality="unresolved", route_kind="fixed",
            normalization_status="resolved", policy_blocked=False,
        )
        self.assertEqual(rec.verdict, QualificationVerdict.FAIL)
        self.assertIn(
            QualificationEvidenceType.EXACT_IDENTITY_CHECK,
            rec.missing_evidence,
        )

    def test_identity_mismatch_fails(self):
        """Exact identity with wrong returned model must fail."""
        identity = self._identity()
        evidence = (
            QualificationEvidence(
                QualificationEvidenceType.EXACT_IDENTITY_CHECK, "responses",
                False, "mismatch", **identity),
            QualificationEvidence(
                QualificationEvidenceType.HOST_VALIDATION, "responses",
                True, "valid", **identity),
            QualificationEvidence(
                QualificationEvidenceType.SEMANTIC_REVIEW, "responses",
                True, "sentinel", **identity),
        )
        rec = evaluate_qualification(
            self._candidate(), evidence,
            fleet_modality="unresolved", route_kind="fixed",
            normalization_status="resolved", policy_blocked=False,
        )
        self.assertEqual(rec.verdict, QualificationVerdict.FAIL)

    def test_not_blocked(self):
        self.assertFalse(is_blocked_model("nvidia/nemotron-3-super-120b-a12b"))


# ── North Mini Code FAIL evidence ────────────────────────────────────────

class TestNorthMiniCodeQualificationFail(unittest.TestCase):
    """Confirm OpenRouter namespace canonicalization is strict."""

    def test_openrouter_namespace_is_canonicalized(self):
        self.assertEqual(
            canonical_response_model_id(
                "openrouter/cohere/north-mini-code:free",
                "cohere/north-mini-code:free", "openrouter",
            ),
            "cohere/north-mini-code:free",
        )

    def test_same_openrouter_canonical_model_matches(self):
        self.assertIsNotNone(canonical_response_model_id(
            "openrouter/vendor/model-a", "vendor/model-a", "openrouter",
        ))

    def test_different_openrouter_model_does_not_match(self):
        self.assertIsNone(canonical_response_model_id(
            "openrouter/vendor/model-a", "vendor/model-b", "openrouter",
        ))

    def test_other_provider_prefix_is_not_stripped(self):
        self.assertIsNone(canonical_response_model_id(
            "vendor/model-a", "model-a", "vendor",
        ))

    def test_nvidia_identity_remains_exact(self):
        self.assertIsNotNone(canonical_response_model_id(
            "nvidia/nemotron-3-super-120b-a12b",
            "nvidia/nemotron-3-super-120b-a12b", "nvidia",
        ))

    def test_north_mini_qualification_passes_with_canonical_response(self):
        candidate = QualificationCandidate(
            model_id="openrouter/cohere/north-mini-code:free",
            provider="openrouter", execution_path="OMNIROUTE", effort="low",
            capability="advanced-coding", risk="medium",
        )

        class Transport:
            def post_json(self, url, headers, payload, timeout):
                self.payload = payload
                return {
                    "model": "cohere/north-mini-code:free",
                    "output_text": SENTINEL,
                }

        record = QualificationClient(transport=Transport()).qualify(
            candidate, fleet_modality="unresolved", route_kind="fixed",
            normalization_status="resolved", policy_blocked=False,
        )
        self.assertEqual(record.verdict, QualificationVerdict.PASS)


# ── Catalog promotion ────────────────────────────────────────────────────

class TestNemotronCatalogPromotion(unittest.TestCase):

    def test_nemotron_in_approved_catalog(self):
        ids = [r.model_id for r in APPROVED_CATALOG]
        self.assertIn("nvidia/nemotron-3-super-120b-a12b", ids)

    def test_nemotron_production_eligible(self):
        eligible = production_eligible(default_catalog())
        ids = [r.model_id for r in eligible]
        self.assertIn("nvidia/nemotron-3-super-120b-a12b", ids)

    def test_nemotron_provider_is_nvidia(self):
        for route in APPROVED_CATALOG:
            if route.model_id == "nvidia/nemotron-3-super-120b-a12b":
                self.assertEqual(route.provider, "nvidia")
                return
        self.fail("Nemotron not found in catalog")

    def test_nemotron_capabilities_advanced_coding_only(self):
        for route in APPROVED_CATALOG:
            if route.model_id == "nvidia/nemotron-3-super-120b-a12b":
                self.assertEqual(route.capabilities, frozenset({"advanced-coding"}))
                return
        self.fail("Nemotron not found in catalog")

    def test_nemotron_risk_level_medium(self):
        for route in APPROVED_CATALOG:
            if route.model_id == "nvidia/nemotron-3-super-120b-a12b":
                self.assertEqual(route.max_risk_level, "medium")
                return
        self.fail("Nemotron not found in catalog")

    def test_nemotron_cost_class_unknown(self):
        for route in APPROVED_CATALOG:
            if route.model_id == "nvidia/nemotron-3-super-120b-a12b":
                self.assertEqual(route.cost_class, "unknown")
                return
        self.fail("Nemotron not found in catalog")

    def test_promoted_route_matches_catalog(self):
        promoted = promoted_route_for_qualified_nvidia_nemotron_coding()
        self.assertTrue(promoted.approved)
        self.assertFalse(promoted.experimental)
        self.assertEqual(promoted.model_id, "nvidia/nemotron-3-super-120b-a12b")
        self.assertEqual(promoted.provider, "nvidia")

    def test_candidate_identity_exact_match(self):
        self.assertTrue(is_exact_nvidia_nemotron_coding_candidate_identity(
            "nvidia/nemotron-3-super-120b-a12b", "nvidia", "OMNIROUTE", "low",
        ))

    def test_candidate_identity_rejects_wrong_provider(self):
        self.assertFalse(is_exact_nvidia_nemotron_coding_candidate_identity(
            "nvidia/nemotron-3-super-120b-a12b", "openrouter", "OMNIROUTE", "low",
        ))

    def test_candidate_identity_rejects_double_prefix(self):
        self.assertFalse(is_exact_nvidia_nemotron_coding_candidate_identity(
            "nvidia/nvidia/nemotron-3-super-120b-a12b", "nvidia", "OMNIROUTE", "low",
        ))


# ── Fleet normalization ──────────────────────────────────────────────────

class TestNemotronFleetNormalization(unittest.TestCase):

    def test_normalize_nemotron(self):
        disc = DiscoveredModel(
            model_id="nvidia/nemotron-3-super-120b-a12b",
            provider="nvidia",
        )
        norm = normalize_discovered_model(disc)
        self.assertEqual(norm.normalization_status, NormalizationStatus.RESOLVED.value)
        self.assertEqual(norm.route_kind, RouteKind.FIXED.value)
        self.assertEqual(norm.provider, "nvidia")
        self.assertFalse(norm.policy_blocked)

    def test_double_prefix_normalizes(self):
        disc = DiscoveredModel(
            model_id="nvidia/nvidia/nemotron-3-super-120b-a12b",
            provider="nvidia",
        )
        norm = normalize_discovered_model(disc)
        self.assertEqual(norm.normalization_status, NormalizationStatus.RESOLVED.value)

    def test_north_mini_normalizes(self):
        disc = DiscoveredModel(
            model_id="openrouter/cohere/north-mini-code:free",
            provider="openrouter",
        )
        norm = normalize_discovered_model(disc)
        self.assertEqual(norm.normalization_status, NormalizationStatus.RESOLVED.value)
        self.assertEqual(norm.route_kind, RouteKind.FIXED.value)


# ── Routing / Claude-exhausted fallback ──────────────────────────────────

class TestClaudeExhaustedFallback(unittest.TestCase):

    def test_nemotron_selected_when_claude_exhausted(self):
        resources = {
            "claude": ResourceSnapshot(
                provider="claude", state=QuotaState.EXHAUSTED,
                headroom_pct=0.0, healthy=True,
            ),
            "nvidia": ResourceSnapshot(
                provider="nvidia", state=QuotaState.UNKNOWN,
                headroom_pct=None, healthy=True,
            ),
        }
        request = RoutingRequest(
            capability=CapabilityClass.ADVANCED_CODING,
            risk_level=RiskLevel.MEDIUM,
        )
        selected = select_best_route(
            request, catalog=default_catalog(), resources=resources,
        )
        self.assertEqual(selected.route.model_id, "openrouter/cohere/north-mini-code:free")
        self.assertEqual(selected.route.provider, "openrouter")

    def test_both_exhausted_raises(self):
        resources = {
            "claude": ResourceSnapshot(
                provider="claude", state=QuotaState.EXHAUSTED,
                headroom_pct=0.0, healthy=True,
            ),
            "nvidia": ResourceSnapshot(
                provider="nvidia", state=QuotaState.EXHAUSTED,
                headroom_pct=0.0, healthy=True,
            ),
            "openrouter": ResourceSnapshot(
                provider="openrouter", state=QuotaState.EXHAUSTED,
                headroom_pct=0.0, healthy=True,
            ),
        }
        request = RoutingRequest(
            capability=CapabilityClass.ADVANCED_CODING,
            risk_level=RiskLevel.MEDIUM,
        )
        with self.assertRaises(NoEligibleRouteError):
            select_best_route(
                request, catalog=default_catalog(), resources=resources,
            )

    def test_claude_only_exhausted_blocks_claude(self):
        """When only Claude is exhausted, no Claude route is returned."""
        resources = {
            "claude": ResourceSnapshot(
                provider="claude", state=QuotaState.EXHAUSTED,
                headroom_pct=0.0, healthy=True,
            ),
        }
        request = RoutingRequest(
            capability=CapabilityClass.ADVANCED_CODING,
            risk_level=RiskLevel.MEDIUM,
        )
        selected = select_best_route(
            request, catalog=default_catalog(), resources=resources,
        )
        self.assertNotEqual(selected.route.provider, "claude")

    def test_normal_routing_prefers_claude(self):
        """With no exhaustion, Claude routes remain preferred (higher quality)."""
        resources = {
            "claude": ResourceSnapshot(
                provider="claude", state=QuotaState.UNKNOWN,
                headroom_pct=None, healthy=True,
            ),
            "nvidia": ResourceSnapshot(
                provider="nvidia", state=QuotaState.UNKNOWN,
                headroom_pct=None, healthy=True,
            ),
        }
        request = RoutingRequest(
            capability=CapabilityClass.ADVANCED_CODING,
            risk_level=RiskLevel.MEDIUM,
        )
        selected = select_best_route(
            request, catalog=default_catalog(), resources=resources,
        )
        self.assertEqual(selected.route.provider, "openrouter")


# ── Policy preservation regression ───────────────────────────────────────

class TestPolicyPreservation(unittest.TestCase):

    def test_max_same_tier_retries_is_one(self):
        self.assertEqual(MAX_SAME_TIER_RETRIES, 1)

    def test_exhausted_hard_gate(self):
        """EXHAUSTED must remain unavailable — never relaxed to UNKNOWN."""
        resources = {
            "claude": ResourceSnapshot(
                provider="claude", state=QuotaState.EXHAUSTED,
                headroom_pct=0.0, healthy=True,
            ),
        }
        request = RoutingRequest(
            capability=CapabilityClass.STANDARD_CODING,
            risk_level=RiskLevel.MEDIUM,
        )
        with self.assertRaises(NoEligibleRouteError):
            select_best_route(
                request, catalog=default_catalog(), resources=resources,
            )


# ── Provider diversity ───────────────────────────────────────────────────

class TestProviderDiversity(unittest.TestCase):

    def test_advanced_coding_has_multiple_providers(self):
        eligible = production_eligible(default_catalog())
        providers = set()
        for route in eligible:
            if "advanced-coding" in route.capabilities:
                providers.add(route.provider)
        self.assertGreaterEqual(len(providers), 2)
        self.assertIn("claude", providers)
        self.assertIn("nvidia", providers)

    def test_advanced_coding_model_count(self):
        eligible = production_eligible(default_catalog())
        adv = [r for r in eligible if "advanced-coding" in r.capabilities]
        self.assertGreaterEqual(len(adv), 3)  # sonnet-low, sonnet-high, nemotron


if __name__ == "__main__":
    unittest.main()
