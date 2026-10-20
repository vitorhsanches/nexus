"""Tests for the Nexus v2.0-D.4.2 fleet/inventory normalization layer.

Covers deterministic normalization of discovered models into
NormalizedFleetRoute/ModelFamily structures, effort/alias/route-kind/
modality classification, unresolved handling, policy-blocked markers,
family deduplication, inventory summaries, and regression assertions
that catalog/routing behavior is unaffected by this additive module.
"""

import unittest

from nexus.routing.catalog import APPROVED_CATALOG, is_blocked_model
from nexus.routing.fleet import (
    DEFAULT_EXECUTION_PATH,
    Effort,
    Modality,
    ModelFamily,
    NormalizationStatus,
    NormalizedFleetRoute,
    Provenance,
    RouteKind,
    build_families,
    canonical_provider_namespace,
    family_key_for,
    has_no_think_marker,
    infer_effort_from_model_id,
    infer_modality_from_model_id,
    infer_route_kind,
    normalize_discovered_fleet,
    normalize_discovered_model,
    normalize_effort,
    normalize_modality,
    summarize_fleet,
    unresolved_route,
)
from nexus.routing.models import ModelRoute, RoutingRequest
from nexus.routing.router import select_best_route
from nexus.routing.telemetry import DiscoveredModel


def _discovered(model_id, provider="unknown"):
    return DiscoveredModel(model_id=model_id, provider=provider)


class FamilyAliasNormalizationTests(unittest.TestCase):
    """Test 1: same family, distinct namespace aliases collapse together."""

    def test_cc_and_claude_namespace_aliases_share_family(self):
        cc_key = family_key_for("cc/claude-haiku-4-5-20251001-low")
        claude_key = family_key_for("claude/claude-haiku-4-5-20251001-low")

        self.assertEqual(cc_key, claude_key)

    def test_family_dedup_marks_both_entries_as_alias(self):
        routes = normalize_discovered_fleet(
            (
                _discovered("cc/claude-haiku-4-5-20251001-low", "claude"),
                _discovered("claude/claude-haiku-4-5-20251001-low", "claude"),
            )
        )

        self.assertEqual(len(routes), 2)
        for route in routes:
            self.assertEqual(route.route_kind, RouteKind.ALIAS.value)

    def test_single_unique_identity_is_fixed_not_alias(self):
        routes = normalize_discovered_fleet(
            (_discovered("cc/claude-sonnet-5-high", "claude"),)
        )

        self.assertEqual(routes[0].route_kind, RouteKind.FIXED.value)


class EffortNormalizationTests(unittest.TestCase):
    """Test 2/9: effort variants, including xhigh/minimal/none."""

    def test_standard_effort_labels(self):
        self.assertEqual(normalize_effort("low"), Effort.LOW.value)
        self.assertEqual(normalize_effort("medium"), Effort.MEDIUM.value)
        self.assertEqual(normalize_effort("high"), Effort.HIGH.value)

    def test_xhigh_variants(self):
        self.assertEqual(normalize_effort("xhigh"), Effort.XHIGH.value)
        self.assertEqual(normalize_effort("x-high"), Effort.XHIGH.value)
        self.assertEqual(normalize_effort("ultra"), Effort.XHIGH.value)

    def test_minimal_variant(self):
        self.assertEqual(normalize_effort("minimal"), Effort.MINIMAL.value)

    def test_none_variant(self):
        self.assertEqual(normalize_effort("none"), Effort.NONE.value)

    def test_unresolved_for_unknown_effort(self):
        self.assertEqual(
            normalize_effort("banana"),
            Effort.UNRESOLVED.value,
        )

    def test_unresolved_for_empty_effort(self):
        self.assertEqual(normalize_effort(""), Effort.UNRESOLVED.value)
        self.assertEqual(normalize_effort(None), Effort.UNRESOLVED.value)


class NoThinkNamespaceTests(unittest.TestCase):
    """Test 3: no-think namespace/effort handling."""

    def test_no_think_marker_detected(self):
        self.assertTrue(has_no_think_marker("glm-4.6-no-think"))
        self.assertTrue(has_no_think_marker("glm-4.6-nothink"))

    def test_no_think_maps_to_none_effort(self):
        self.assertEqual(
            infer_effort_from_model_id("glm-4.6-no-think"),
            Effort.NONE.value,
        )

    def test_no_marker_present_normal_effort(self):
        self.assertEqual(
            infer_effort_from_model_id("glm-4.6-high"),
            Effort.HIGH.value,
        )


class RouteKindPoolClassificationTests(unittest.TestCase):
    """Test 4: auto/*, :free, and pool-style identities classify as POOL."""

    def test_auto_best_free_is_pool(self):
        self.assertEqual(
            infer_route_kind("auto/best-free"),
            RouteKind.POOL.value,
        )

    def test_auto_coding_free_is_pool(self):
        self.assertEqual(
            infer_route_kind("auto/coding:free"),
            RouteKind.POOL.value,
        )

    def test_openrouter_openrouter_free_is_pool(self):
        self.assertEqual(
            infer_route_kind("openrouter/openrouter/free"),
            RouteKind.POOL.value,
        )

    def test_fixed_identity_is_not_pool(self):
        self.assertEqual(
            infer_route_kind("cc/claude-sonnet-5-high"),
            RouteKind.FIXED.value,
        )

    def test_fixed_free_opencode_identities_are_not_pools(self):
        for model_id in (
            "oc/mimo-v2.5-free",
            "oc/nemotron-3-ultra-free",
            "oc/north-mini-code-free",
            "oc/deepseek-v4-flash-free",
        ):
            with self.subTest(model_id=model_id):
                self.assertEqual(
                    infer_route_kind(model_id), RouteKind.FIXED.value
                )

    def test_fixed_free_identity_preserves_raw_route(self):
        model_id = "oc/mimo-v2.5-free"
        route = normalize_discovered_model(_discovered(model_id, "opencode"))
        self.assertEqual(route.model_id, model_id)
        self.assertEqual(route.alias, model_id)
        self.assertEqual(route.route_kind, RouteKind.FIXED.value)

    def test_empty_identity_is_unknown(self):
        self.assertEqual(infer_route_kind(""), RouteKind.UNKNOWN.value)


class ModalityInferenceTests(unittest.TestCase):
    """Tests 5-8: model-ID-driven modality inference."""

    def test_embedding_model_id(self):
        self.assertEqual(
            infer_modality_from_model_id("text-embedding-3-large"),
            Modality.EMBEDDING.value,
        )

    def test_whisper_audio_model_id(self):
        self.assertEqual(
            infer_modality_from_model_id("whisper-1"),
            Modality.AUDIO.value,
        )

    def test_flux_image_model_id(self):
        self.assertEqual(
            infer_modality_from_model_id("flux-1.1-pro"),
            Modality.IMAGE.value,
        )

    def test_tts_audio_model_id(self):
        self.assertEqual(
            infer_modality_from_model_id("tts-1"),
            Modality.AUDIO.value,
        )

    def test_rerank_model_id(self):
        self.assertEqual(
            infer_modality_from_model_id("bge-reranker-v2"),
            Modality.RERANK.value,
        )

    def test_no_signal_is_unresolved(self):
        self.assertEqual(
            infer_modality_from_model_id("cc/claude-sonnet-5-high"),
            Modality.UNRESOLVED.value,
        )

    def test_normalize_modality_explicit_literal(self):
        self.assertEqual(normalize_modality("vision"), Modality.VISION.value)
        self.assertEqual(normalize_modality("chat"), Modality.TEXT.value)
        self.assertEqual(
            normalize_modality("unknown-thing"),
            Modality.UNRESOLVED.value,
        )


class RepresentativeProviderRouteTests(unittest.TestCase):
    """Tests 10-13: representative Gemini/GLM/Nemotron/OpenCode-free routes."""

    def test_gemini_route_normalizes(self):
        route = normalize_discovered_model(
            _discovered("google/gemini-2.5-pro-high", "google")
        )
        self.assertEqual(route.effort, Effort.HIGH.value)
        self.assertEqual(route.provenance, Provenance.DISCOVERED.value)
        self.assertFalse(route.approved)

    def test_glm_no_think_route_normalizes(self):
        route = normalize_discovered_model(
            _discovered("zhipu/glm-4.6-no-think", "zhipu")
        )
        self.assertEqual(route.effort, Effort.NONE.value)

    def test_nemotron_route_normalizes(self):
        route = normalize_discovered_model(
            _discovered("nvidia/nemotron-70b-instruct", "nvidia")
        )
        self.assertEqual(route.provider, "nvidia")
        self.assertEqual(route.route_kind, RouteKind.FIXED.value)

    def test_opencode_free_pool_route_normalizes(self):
        route = normalize_discovered_model(
            _discovered("oc/opencode-free", "opencode")
        )
        self.assertEqual(route.route_kind, RouteKind.POOL.value)


class PolicyBlockedMarkerTests(unittest.TestCase):
    """Tests 14-15: Terra blocking is reused, not re-implemented."""

    def test_terra_identity_is_policy_blocked(self):
        route = normalize_discovered_model(
            _discovered("gpt-5.6-terra", "openai")
        )
        self.assertTrue(route.policy_blocked)
        self.assertFalse(route.enabled)

    def test_terra_blocking_matches_catalog_policy(self):
        model_id = "gpt/5/6/terra"
        route = normalize_discovered_model(_discovered(model_id, "openai"))

        self.assertEqual(route.policy_blocked, is_blocked_model(model_id))

    def test_non_blocked_identity_is_not_flagged(self):
        route = normalize_discovered_model(
            _discovered("cc/claude-sonnet-5-high", "claude")
        )
        self.assertFalse(route.policy_blocked)
        self.assertTrue(route.enabled)


class UnresolvedHandlingTests(unittest.TestCase):
    """Test 16: unresolved placeholder route semantics."""

    def test_unresolved_route_defaults(self):
        route = unresolved_route()

        self.assertEqual(route.provenance, Provenance.UNRESOLVED.value)
        self.assertEqual(route.effort, Effort.UNRESOLVED.value)
        self.assertEqual(route.modality, Modality.UNRESOLVED.value)
        self.assertEqual(route.route_kind, RouteKind.UNKNOWN.value)
        self.assertFalse(route.approved)
        self.assertFalse(route.enabled)

    def test_unresolved_route_execution_path_default(self):
        route = unresolved_route()
        self.assertEqual(route.execution_path, DEFAULT_EXECUTION_PATH)

    def test_unknown_identity_stays_unresolved_and_auditable(self):
        route = normalize_discovered_model(_discovered("vendor/unknown-model", "vendor"))
        self.assertEqual(route.model_id, "vendor/unknown-model")
        self.assertEqual(route.normalization_status, NormalizationStatus.UNRESOLVED.value)
        self.assertEqual(route.route_kind, RouteKind.UNKNOWN.value)
        self.assertEqual(route.family, "unresolved")
        self.assertFalse(route.approved)
        self.assertFalse(route.enabled)

    def test_unknown_identity_is_not_invented_as_a_family(self):
        route = normalize_discovered_model(_discovered("arbitrary-new-id", "vendor"))
        self.assertEqual(build_families((route,)), ())

    def test_arbitrary_gpt_identity_stays_unresolved(self):
        route = normalize_discovered_model(
            _discovered("gpt-completely-unknown-model", "openai")
        )
        self.assertEqual(
            route.normalization_status, NormalizationStatus.UNRESOLVED.value
        )

    def test_terra_remains_blocked_when_unresolved(self):
        route = normalize_discovered_model(_discovered("gpt-5.6-terra", "openai"))
        self.assertEqual(
            route.normalization_status, NormalizationStatus.UNRESOLVED.value
        )
        self.assertTrue(route.policy_blocked)
        self.assertFalse(route.approved)
        self.assertFalse(route.enabled)


class FamilyDeduplicationAndSummaryTests(unittest.TestCase):
    """Test 17: family dedup + inventory summary."""

    def test_build_families_deduplicates_aliases(self):
        routes = normalize_discovered_fleet(
            (
                _discovered("cc/claude-haiku-4-5-20251001-low", "claude"),
                _discovered("claude/claude-haiku-4-5-20251001-low", "claude"),
                _discovered("cc/claude-sonnet-5-high", "claude"),
            )
        )

        families = build_families(routes)
        self.assertEqual(len(families), 2)

        haiku_family = next(
            f for f in families if "haiku" in f.family_key
        )
        self.assertEqual(len(haiku_family.aliases), 2)

    def test_summarize_fleet_counts(self):
        routes = normalize_discovered_fleet(
            (
                _discovered("cc/claude-haiku-4-5-20251001-low", "claude"),
                _discovered("claude/claude-haiku-4-5-20251001-low", "claude"),
                _discovered("auto/best-free", "openrouter"),
                _discovered("gpt-5.6-terra", "openai"),
            )
        )

        summary = summarize_fleet(routes)

        self.assertEqual(summary.total_routes, 4)
        self.assertEqual(summary.total_families, 2)
        self.assertEqual(summary.policy_blocked_count, 1)
        self.assertIn(RouteKind.ALIAS.value, summary.by_route_kind)
        self.assertIn(RouteKind.POOL.value, summary.by_route_kind)

    def test_repeated_normalization_is_identical(self):
        discovery = (
            _discovered("cc/claude-haiku-4-5-20251001-low", "claude"),
            _discovered("claude/claude-haiku-4-5-20251001-high", "claude"),
            _discovered("google/gemini-2.5-pro-high", "google"),
            _discovered("oc/opencode-free", "opencode"),
        )
        first = normalize_discovered_fleet(discovery)
        second = normalize_discovered_fleet(discovery)
        self.assertEqual(first, second)
        self.assertEqual(build_families(first), build_families(second))
        self.assertEqual(summarize_fleet(first), summarize_fleet(second))


class NamespaceCanonicalizationTests(unittest.TestCase):
    def test_known_short_namespace_aliases(self):
        self.assertEqual(canonical_provider_namespace("cc"), "claude")
        self.assertEqual(canonical_provider_namespace("oc"), "opencode")

    def test_unknown_namespace_passthrough(self):
        self.assertEqual(
            canonical_provider_namespace("google"),
            "google",
        )


class NormalizedFleetRouteValidationTests(unittest.TestCase):
    def test_requires_non_empty_model_id(self):
        with self.assertRaises(ValueError):
            NormalizedFleetRoute(
                model_id="",
                alias="",
                namespace="",
                provider="claude",
                execution_path="OMNIROUTE",
                family="claude:x",
                effort=Effort.LOW.value,
                route_kind=RouteKind.FIXED.value,
                modality=Modality.TEXT.value,
                provenance=Provenance.DISCOVERED.value,
            )

    def test_requires_non_empty_execution_path(self):
        with self.assertRaises(ValueError):
            NormalizedFleetRoute(
                model_id="cc/claude-sonnet-5-high",
                alias="cc/claude-sonnet-5-high",
                namespace="cc",
                provider="claude",
                execution_path="",
                family="claude:claude-sonnet-5",
                effort=Effort.LOW.value,
                route_kind=RouteKind.FIXED.value,
                modality=Modality.TEXT.value,
                provenance=Provenance.DISCOVERED.value,
            )


class ModelFamilyValidationTests(unittest.TestCase):
    def test_requires_non_empty_family_key(self):
        with self.assertRaises(ValueError):
            ModelFamily(
                family_key="",
                canonical_provider="claude",
                base_model="claude-sonnet-5",
            )


class RegressionAssertionsTests(unittest.TestCase):
    """Ensures catalog/routing behavior is unchanged by this additive module."""

    def test_approved_catalog_untouched(self):
        self.assertTrue(len(APPROVED_CATALOG) >= 3)
        for route in APPROVED_CATALOG:
            self.assertIsInstance(route, ModelRoute)

    def test_terra_still_globally_blocked_via_catalog(self):
        self.assertTrue(is_blocked_model("gpt-5.6-terra"))
        self.assertTrue(is_blocked_model("GPT_5_6_TERRA"))

    def test_routing_policy_still_selects_expected_route(self):
        request = RoutingRequest(
            capability="advanced-coding",
            risk_level="low",
        )
        selected = select_best_route(request)

        self.assertEqual(selected.route.model_id, "openrouter/cohere/north-mini-code:free")
        self.assertTrue(selected.route.approved)

    def test_fleet_module_does_not_mutate_catalog_routes(self):
        before = APPROVED_CATALOG
        normalize_discovered_fleet(
            (_discovered("cc/claude-sonnet-5-high", "claude"),)
        )
        after = APPROVED_CATALOG

        self.assertIs(before, after)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
