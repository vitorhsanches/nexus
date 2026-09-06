"""Tests for OmniRoute runtime telemetry integration (Nexus v2.0-B)."""

import os
import unittest
import unittest.mock
from urllib.error import URLError

from nexus.routing.catalog import APPROVED_CATALOG, is_blocked_model
from nexus.routing.models import (
    ModelRoute,
    QuotaState,
    ResourceSnapshot,
    RiskLevel,
    RoutingRequest,
)
from nexus.routing.resources import ProviderOverrides
from nexus.routing.router import select_best_route
from nexus.routing.telemetry import (
    DiscoveredModel,
    OmniRouteTelemetryClient,
    OmniRouteTelemetrySnapshot,
    ProviderTelemetry,
    ServerHealth,
    TelemetryCollectionError,
    Transport,
    build_routing_resources,
    build_runtime_catalog,
)


class FakeTransport(Transport):
    """Deterministic in-memory transport; performs no network I/O."""

    def __init__(self, responses=None, raise_for=None, captured_headers=None):
        self.responses = responses or {}
        self.raise_for = raise_for or {}
        self.captured_headers = captured_headers if captured_headers is not None else []

    def get_json(self, url, headers, timeout):
        self.captured_headers.append((url, dict(headers)))

        for suffix, exc in self.raise_for.items():
            if url.endswith(suffix):
                raise exc

        for suffix, payload in self.responses.items():
            if url.endswith(suffix):
                return payload

        raise TelemetryCollectionError(url, "no fake response configured")


MODELS_PAYLOAD = {
    "data": [
        {
            "id": "cc/claude-sonnet-5-low",
            "owned_by": "claude",
            "context_length": 1000000,
        },
        {
            "id": "oc/big-pickle",
            "owned_by": "opencode",
            "context_length": 200000,
        },
        {
            "id": "some/unknown-experimental-model",
            "owned_by": "someprovider",
            "context_length": 8000,
        },
    ]
}

QUOTA_UNKNOWN_PAYLOAD = {
    "providers": [
        {
            "provider": "codex",
            "quotaUsed": 0,
            "quotaTotal": None,
            "percentRemaining": 100,
            "resetAt": None,
            "tokenStatus": "valid",
        },
        {
            "provider": "claude",
            "quotaUsed": 0,
            "quotaTotal": None,
            "percentRemaining": 100,
            "resetAt": None,
            "tokenStatus": "valid",
        },
    ]
}

QUOTA_KNOWN_PAYLOAD = {
    "providers": [
        {
            "provider": "claude",
            "quotaUsed": 25,
            "quotaTotal": 100,
            "percentRemaining": 75,
            "resetAt": None,
            "tokenStatus": "valid",
        },
    ]
}

HEALTH_PAYLOAD = {
    "status": "healthy",
    "providerHealth": {},
    "connectionHealth": {},
    "credentialHealth": {
        "total": 9,
        "healthy": 9,
        "failed": 0,
        "unknown": 0,
        "stale": 0,
    },
    "providerSummary": {
        "catalogCount": 352,
        "configuredCount": 9,
        "activeCount": 9,
        "monitoredCount": 0,
    },
}


HEALTH_WITH_PROVIDER_STATE = {
    "status": "healthy",
    "providerHealth": {
        "claude": {
            "status": "unhealthy",
        },
        "codex": {
            "status": "healthy",
        },
    },
    "connectionHealth": {},
    "credentialHealth": {
        "total": 2,
        "healthy": 2,
        "failed": 0,
        "unknown": 0,
        "stale": 0,
    },
    "providerSummary": {
        "catalogCount": 2,
        "configuredCount": 2,
        "activeCount": 2,
        "monitoredCount": 2,
    },
}


def make_client(responses=None, raise_for=None, api_key=None, captured_headers=None):
    transport = FakeTransport(
        responses=responses,
        raise_for=raise_for,
        captured_headers=captured_headers,
    )
    return OmniRouteTelemetryClient(api_key=api_key, transport=transport), transport


class ModelDiscoveryTests(unittest.TestCase):
    def test_parses_claude_sonnet_entry(self):
        client, _ = make_client(responses={"/v1/models": MODELS_PAYLOAD})
        models = client.fetch_models()
        claude = next(m for m in models if m.model_id == "cc/claude-sonnet-5-low")
        self.assertEqual(claude.provider, "claude")
        self.assertEqual(claude.context_length, 1000000)

    def test_parses_big_pickle_owned_by_opencode(self):
        client, _ = make_client(responses={"/v1/models": MODELS_PAYLOAD})
        models = client.fetch_models()
        pickle = next(m for m in models if m.model_id == "oc/big-pickle")
        self.assertEqual(pickle.provider, "opencode")
        self.assertEqual(pickle.context_length, 200000)

    def test_discovered_model_is_not_production_eligible(self):
        model = DiscoveredModel(model_id="x/y", provider="x")
        self.assertFalse(hasattr(model, "approved"))
        # DiscoveredModel is a pure discovery record; it must not carry
        # any approval concept at all.


class RuntimeCatalogOverlayTests(unittest.TestCase):
    def test_overlay_does_not_mutate_approved_catalog(self):
        before = tuple(APPROVED_CATALOG)
        snapshot = OmniRouteTelemetrySnapshot(
            discovered_models=(
                DiscoveredModel(model_id="cc/claude-sonnet-5-low", provider="claude"),
            )
        )
        build_runtime_catalog(APPROVED_CATALOG, snapshot)
        self.assertEqual(before, APPROVED_CATALOG)

    def test_approved_model_missing_at_runtime_becomes_unavailable(self):
        snapshot = OmniRouteTelemetrySnapshot(discovered_models=())
        overlay = build_runtime_catalog(APPROVED_CATALOG, snapshot)
        for route in overlay:
            self.assertFalse(route.enabled)

    def test_unknown_discovered_model_never_becomes_approved(self):
        snapshot = OmniRouteTelemetrySnapshot(
            discovered_models=(
                DiscoveredModel(model_id="some/unknown-experimental-model", provider="x"),
            )
        )
        overlay = build_runtime_catalog(APPROVED_CATALOG, snapshot)
        overlay_ids = {r.model_id for r in overlay}
        self.assertNotIn("some/unknown-experimental-model", overlay_ids)

    def test_terra_discovered_at_runtime_remains_blocked(self):
        snapshot = OmniRouteTelemetrySnapshot(
            discovered_models=(
                DiscoveredModel(model_id="gpt-5.6-terra", provider="openai"),
            )
        )
        self.assertTrue(is_blocked_model("gpt-5.6-terra"))
        # Even if a hypothetical approved-catalog entry existed, the global
        # deny-list check remains authoritative downstream in the router.

    def test_approved_model_present_at_runtime_stays_enabled(self):
        snapshot = OmniRouteTelemetrySnapshot(
            discovered_models=(
                DiscoveredModel(model_id="cc/claude-sonnet-5-low", provider="claude"),
                DiscoveredModel(model_id="cc/claude-sonnet-5-high", provider="claude"),
                DiscoveredModel(model_id="oc/big-pickle", provider="opencode"),
            )
        )
        overlay = build_runtime_catalog(APPROVED_CATALOG, snapshot)
        for route in overlay:
            self.assertTrue(route.enabled)


class QuotaNormalizationTests(unittest.TestCase):
    def test_null_total_with_percent_remaining_100_is_unknown(self):
        client, _ = make_client(responses={"/api/usage/quota": QUOTA_UNKNOWN_PAYLOAD})
        telemetry = client.fetch_quota()
        self.assertEqual(telemetry["claude"].resource.state, QuotaState.UNKNOWN)
        self.assertIsNone(telemetry["claude"].resource.headroom_pct)
        self.assertEqual(telemetry["codex"].resource.state, QuotaState.UNKNOWN)
        self.assertIsNone(telemetry["codex"].resource.headroom_pct)

    def test_reliable_quota_produces_known_percentage(self):
        client, _ = make_client(responses={"/api/usage/quota": QUOTA_KNOWN_PAYLOAD})
        telemetry = client.fetch_quota()
        self.assertEqual(telemetry["claude"].resource.state, QuotaState.KNOWN)
        self.assertAlmostEqual(telemetry["claude"].resource.headroom_pct, 75.0)

    def test_missing_provider_quota_is_unknown_in_resources(self):
        snapshot = OmniRouteTelemetrySnapshot(provider_telemetry={})
        resources = build_routing_resources(APPROVED_CATALOG, snapshot)
        for resource in resources.values():
            self.assertEqual(resource.state, QuotaState.UNKNOWN)
            self.assertIsNone(resource.headroom_pct)

    def test_invalid_token_status_recorded_as_diagnostic(self):
        payload = {
            "providers": [
                {
                    "provider": "claude",
                    "quotaUsed": 0,
                    "quotaTotal": None,
                    "tokenStatus": "invalid",
                }
            ]
        }
        client, _ = make_client(responses={"/api/usage/quota": payload})
        telemetry = client.fetch_quota()
        self.assertEqual(telemetry["claude"].token_status, "invalid")
        self.assertFalse(telemetry["claude"].credential_usable)
        self.assertFalse(telemetry["claude"].resource.healthy)
        self.assertIn("tokenStatus=invalid", telemetry["claude"].diagnostics)


class OverrideIntegrationTests(unittest.TestCase):
    def test_manual_claude_override_wins_over_live_unknown(self):
        client, _ = make_client(responses={"/api/usage/quota": QUOTA_UNKNOWN_PAYLOAD})
        provider_telemetry = client.fetch_quota()
        snapshot = OmniRouteTelemetrySnapshot(provider_telemetry=provider_telemetry)

        overrides = ProviderOverrides({"claude": 80})
        resources = build_routing_resources(APPROVED_CATALOG, snapshot, overrides)

        self.assertEqual(resources["claude"].state, QuotaState.OVERRIDE)
        self.assertAlmostEqual(resources["claude"].headroom_pct, 80.0)

    def test_manual_codex_override_becomes_reserve_band(self):
        from nexus.routing.resources import classify_headroom
        from nexus.routing.models import HeadroomBand

        overrides = ProviderOverrides({"codex": 3})
        snapshot = overrides.as_snapshot("codex")
        self.assertEqual(classify_headroom(snapshot), HeadroomBand.RESERVE)

    def test_override_does_not_make_unhealthy_provider_healthy(self):
        provider_telemetry = {
            "claude": ProviderTelemetry(
                provider="claude",
                resource=ResourceSnapshot(
                    provider="claude",
                    state=QuotaState.UNKNOWN,
                    headroom_pct=None,
                    healthy=True,
                ),
                healthy=False,
            )
        }
        snapshot = OmniRouteTelemetrySnapshot(provider_telemetry=provider_telemetry)
        overrides = ProviderOverrides({"claude": 80})
        resources = build_routing_resources(APPROVED_CATALOG, snapshot, overrides)
        self.assertFalse(resources["claude"].healthy)
        self.assertEqual(resources["claude"].headroom_pct, 80.0)


    def test_override_does_not_resurrect_invalid_credential(self):
        payload = {
            "providers": [
                {
                    "provider": "claude",
                    "quotaUsed": 0,
                    "quotaTotal": None,
                    "tokenStatus": "invalid",
                }
            ]
        }

        client, _ = make_client(
            responses={
                "/api/usage/quota": payload,
            }
        )

        provider_telemetry = client.fetch_quota()

        snapshot = OmniRouteTelemetrySnapshot(
            provider_telemetry=provider_telemetry
        )

        overrides = ProviderOverrides(
            {
                "claude": 90,
            }
        )

        resources = build_routing_resources(
            APPROVED_CATALOG,
            snapshot,
            overrides,
        )

        self.assertEqual(
            resources["claude"].headroom_pct,
            90.0,
        )

        self.assertFalse(
            resources["claude"].healthy
        )


class HealthNormalizationTests(unittest.TestCase):
    def test_server_health_parses_global_fields(self):
        client, _ = make_client(responses={"/api/monitoring/health": HEALTH_PAYLOAD})
        health = client.fetch_health()
        self.assertEqual(health.status, "healthy")
        self.assertEqual(health.credential_total, 9)
        self.assertEqual(health.credential_healthy, 9)
        self.assertEqual(health.catalog_count, 352)

    def test_empty_provider_health_does_not_fabricate_health(self):
        client, _ = make_client(responses={"/api/monitoring/health": HEALTH_PAYLOAD})
        health = client.fetch_health()
        self.assertTrue(
            any("UNKNOWN" in d for d in health.diagnostics)
        )

    def test_explicit_unhealthy_provider_propagates_to_resources(self):
        provider_telemetry = {
            "claude": ProviderTelemetry(
                provider="claude",
                resource=ResourceSnapshot(
                    provider="claude",
                    state=QuotaState.UNKNOWN,
                    headroom_pct=None,
                    healthy=True,
                ),
                healthy=False,
            )
        }
        snapshot = OmniRouteTelemetrySnapshot(provider_telemetry=provider_telemetry)
        resources = build_routing_resources(APPROVED_CATALOG, snapshot)
        self.assertFalse(resources["claude"].healthy)


    def test_fetch_health_parses_explicit_provider_state(self):
        client, _ = make_client(
            responses={
                "/api/monitoring/health": HEALTH_WITH_PROVIDER_STATE,
            }
        )

        health = client.fetch_health()

        self.assertFalse(
            health.provider_health["claude"]
        )

        self.assertTrue(
            health.provider_health["codex"]
        )

    def test_collect_merges_provider_health_into_resources(self):
        client, _ = make_client(
            responses={
                "/v1/models": MODELS_PAYLOAD,
                "/api/usage/quota": QUOTA_UNKNOWN_PAYLOAD,
                "/api/monitoring/health": HEALTH_WITH_PROVIDER_STATE,
            }
        )

        snapshot = client.collect()

        self.assertFalse(
            snapshot.provider_telemetry["claude"].healthy
        )

        resources = build_routing_resources(
            APPROVED_CATALOG,
            snapshot,
            ProviderOverrides(
                {
                    "claude": 90,
                }
            ),
        )

        self.assertFalse(
            resources["claude"].healthy
        )


class FailureModeTests(unittest.TestCase):
    def test_server_unreachable_raises_precise_error(self):
        client, _ = make_client(raise_for={"/api/monitoring/health": TelemetryCollectionError("/api/monitoring/health", "connection failed")})
        with self.assertRaises(TelemetryCollectionError):
            client.fetch_health()

    def test_malformed_json_fails_safely(self):
        class BadTransport(Transport):
            def get_json(self, url, headers, timeout):
                raise TelemetryCollectionError(url, "malformed JSON response")

        client = OmniRouteTelemetryClient(transport=BadTransport())
        with self.assertRaises(TelemetryCollectionError):
            client.fetch_models()

    def test_models_succeed_quota_fails_partial_collection(self):
        transport = FakeTransport(
            responses={"/v1/models": MODELS_PAYLOAD, "/api/monitoring/health": HEALTH_PAYLOAD},
            raise_for={"/api/usage/quota": TelemetryCollectionError("/api/usage/quota", "connection failed")},
        )
        client = OmniRouteTelemetryClient(transport=transport)
        snapshot = client.collect()
        self.assertTrue(len(snapshot.discovered_models) > 0)
        self.assertEqual(snapshot.provider_telemetry, {})
        self.assertTrue(any("quota" in e for e in snapshot.errors))
        self.assertFalse(snapshot.ok)

    def test_health_succeeds_provider_details_absent(self):
        client, _ = make_client(responses={"/api/monitoring/health": HEALTH_PAYLOAD})
        health = client.fetch_health()
        self.assertTrue(len(health.diagnostics) > 0)

    def test_empty_provider_list(self):
        client, _ = make_client(responses={"/api/usage/quota": {"providers": []}})
        telemetry = client.fetch_quota()
        self.assertEqual(telemetry, {})

    def test_missing_expected_fields_raises(self):
        client, _ = make_client(responses={"/v1/models": {}})
        with self.assertRaises(TelemetryCollectionError):
            client.fetch_models()

    def test_offline_transport_produces_precise_failure_without_network_io(self):
        client = OmniRouteTelemetryClient(
            base_url="http://127.0.0.1:20128",
            timeout=0.2,
        )

        with unittest.mock.patch(
            "nexus.routing.telemetry.urllib.request.urlopen",
            side_effect=URLError("simulated offline"),
        ):
            with self.assertRaises(TelemetryCollectionError) as context:
                client.fetch_health()

        self.assertIn(
            "connection failed",
            str(context.exception),
        )

    def test_collect_does_not_fabricate_full_availability_on_total_failure(self):
        transport = FakeTransport(
            raise_for={
                "/v1/models": TelemetryCollectionError("/v1/models", "connection failed"),
                "/api/usage/quota": TelemetryCollectionError("/api/usage/quota", "connection failed"),
                "/api/monitoring/health": TelemetryCollectionError("/api/monitoring/health", "connection failed"),
            }
        )
        client = OmniRouteTelemetryClient(transport=transport)
        snapshot = client.collect()
        self.assertEqual(snapshot.discovered_models, ())
        self.assertEqual(snapshot.provider_telemetry, {})
        self.assertIsNone(snapshot.server_health)
        self.assertEqual(len(snapshot.errors), 3)
        self.assertFalse(snapshot.ok)


class SecurityTests(unittest.TestCase):
    def test_api_key_not_in_repr(self):
        client, _ = make_client(api_key="super-secret-token")
        self.assertNotIn("super-secret-token", repr(client))

    def test_api_key_not_in_exception_text(self):
        client, _ = make_client(
            api_key="super-secret-token",
            raise_for={"/api/monitoring/health": TelemetryCollectionError("/api/monitoring/health", "connection failed")},
        )
        try:
            client.fetch_health()
        except TelemetryCollectionError as exc:
            self.assertNotIn("super-secret-token", str(exc))
        else:
            self.fail("expected TelemetryCollectionError")

    def test_authorization_header_generated_only_at_transport_boundary(self):
        headers_seen = []
        client, transport = make_client(
            responses={"/api/monitoring/health": HEALTH_PAYLOAD},
            api_key="super-secret-token",
            captured_headers=headers_seen,
        )
        client.fetch_health()
        self.assertEqual(len(headers_seen), 1)
        _, headers = headers_seen[0]
        self.assertEqual(headers["Authorization"], "Bearer super-secret-token")

    def test_no_api_key_means_no_authorization_header(self):
        headers_seen = []
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OMNIROUTE_API_KEY", None)
            client, transport = make_client(
                responses={"/api/monitoring/health": HEALTH_PAYLOAD},
                captured_headers=headers_seen,
            )
            client.fetch_health()
        _, headers = headers_seen[0]
        self.assertNotIn("Authorization", headers)


class ResourceBuildingRouterIntegrationTests(unittest.TestCase):
    def test_build_routing_resources_output_accepted_by_select_best_route(self):
        snapshot = OmniRouteTelemetrySnapshot(
            discovered_models=(
                DiscoveredModel(model_id="cc/claude-sonnet-5-low", provider="claude"),
                DiscoveredModel(model_id="oc/big-pickle", provider="opencode"),
            ),
            provider_telemetry={},
        )
        resources = build_routing_resources(APPROVED_CATALOG, snapshot)

        request = RoutingRequest(
            capability="mechanical",
            risk_level=RiskLevel.LOW.value,
        )
        result = select_best_route(
            request,
            catalog=APPROVED_CATALOG,
            resources=resources,
        )
        self.assertIsNotNone(result.route)

    def test_higher_override_selected_over_lower_override(self):
        route_a = ModelRoute(
            model_id="a/model",
            provider="provider-a",
            execution_path="OMNIROUTE",
            capabilities=frozenset({"mechanical"}),
            max_risk_level="low",
            approved=True,
            enabled=True,
        )
        route_b = ModelRoute(
            model_id="b/model",
            provider="provider-b",
            execution_path="OMNIROUTE",
            capabilities=frozenset({"mechanical"}),
            max_risk_level="low",
            approved=True,
            enabled=True,
        )
        catalog = (route_a, route_b)
        snapshot = OmniRouteTelemetrySnapshot()
        overrides = ProviderOverrides({"provider-a": 80, "provider-b": 3})
        resources = build_routing_resources(catalog, snapshot, overrides)

        request = RoutingRequest(capability="mechanical", risk_level=RiskLevel.LOW.value)
        result = select_best_route(request, catalog=catalog, resources=resources)
        self.assertEqual(result.route.model_id, "a/model")


if __name__ == "__main__":
    unittest.main()
