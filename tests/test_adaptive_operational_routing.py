"""Tests for Nexus v2.0-C Adaptive Operational Routing."""

import os
import unittest
from unittest.mock import patch

from nexus.agents.adapters.base import AdapterResult, ExecutionContext
from nexus.agents.adapters.omniroute import (
    MECHANICAL_MODEL,
    STANDARD_CODING_MODEL,
    OmniRouteAdapter,
    build_command,
    select_route,
)
from nexus.policies.escalation import (
    ROUTE_LADDERS,
    next_route,
    validate_route_for_class,
)
from nexus.routing.catalog import APPROVED_CATALOG
from nexus.routing.models import ModelRoute, QuotaState, ResourceSnapshot
from nexus.routing.resources import ProviderOverrides
from nexus.routing.router import NoEligibleRouteError
from nexus.routing.service import (
    HEADROOM_OVERRIDE_ENV_VAR,
    AdaptiveRoutingService,
    InvalidRiskLevelError,
    RoutingDecision,
    capability_class_for,
    load_headroom_overrides_from_env,
    normalize_risk_level,
    parse_headroom_overrides_env,
)
from nexus.routing.telemetry import (
    DiscoveredModel,
    OmniRouteTelemetrySnapshot,
    ProviderTelemetry,
    TelemetryCollectionError,
)


TERRA_MODEL_ID = "gpt-5.6-terra"


def _catalog(extra=()):
    return APPROVED_CATALOG + tuple(extra)


class CapabilityRiskNormalizationTestCase(unittest.TestCase):
    def test_mechanical_capabilities_map_to_mechanical(self):
        self.assertEqual(capability_class_for(["mechanical"]), "mechanical")
        self.assertEqual(capability_class_for(["formatting", "cleanup"]), "mechanical")

    def test_non_mechanical_capabilities_map_to_standard_coding(self):
        self.assertEqual(capability_class_for(["coding"]), "standard-coding")
        self.assertEqual(capability_class_for([]), "standard-coding")
        self.assertEqual(capability_class_for(None), "standard-coding")

    def test_mixed_capabilities_map_to_standard_coding(self):
        self.assertEqual(
            capability_class_for(["mechanical", "coding"]),
            "standard-coding",
        )

    def test_missing_risk_defaults_to_low(self):
        self.assertEqual(normalize_risk_level(None), "low")

    def test_risk_case_normalization(self):
        self.assertEqual(normalize_risk_level("LOW"), "low")
        self.assertEqual(normalize_risk_level("Medium"), "medium")
        self.assertEqual(normalize_risk_level("HIGH"), "high")
        self.assertEqual(normalize_risk_level("critical"), "critical")

    def test_unknown_risk_fails_closed(self):
        with self.assertRaises(InvalidRiskLevelError):
            normalize_risk_level("nonsense")

    def test_non_string_risk_fails_closed(self):
        with self.assertRaises(InvalidRiskLevelError):
            normalize_risk_level(123)


class HeadroomOverrideEnvParsingTestCase(unittest.TestCase):
    def test_valid_config_parses_correctly(self):
        values = parse_headroom_overrides_env("claude=80,codex=3,opencode=55")
        self.assertEqual(values, {"claude": 80.0, "codex": 3.0, "opencode": 55.0})

    def test_whitespace_tolerant(self):
        values = parse_headroom_overrides_env(" claude = 80 , opencode = 3 ")
        self.assertEqual(values, {"claude": 80.0, "opencode": 3.0})

    def test_missing_config_is_allowed(self):
        self.assertEqual(parse_headroom_overrides_env(None), {})
        self.assertEqual(parse_headroom_overrides_env(""), {})

    def test_malformed_config_fails_safely(self):
        with self.assertRaises(ValueError):
            parse_headroom_overrides_env("claude=notanumber")

        with self.assertRaises(ValueError):
            parse_headroom_overrides_env("claude")

        with self.assertRaises(ValueError):
            parse_headroom_overrides_env("claude=200")

    def test_no_eval_used_for_malicious_input(self):
        with self.assertRaises(ValueError):
            parse_headroom_overrides_env("claude=__import__('os')")

    def test_load_from_env_missing_var_is_allowed(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(HEADROOM_OVERRIDE_ENV_VAR, None)
            overrides = load_headroom_overrides_from_env()
            self.assertIsNone(overrides.get("claude"))

    def test_load_from_env_valid_config(self):
        with patch.dict(
            os.environ, {HEADROOM_OVERRIDE_ENV_VAR: "claude=80,opencode=3"}
        ):
            overrides = load_headroom_overrides_from_env()
            self.assertEqual(overrides.get("claude"), 80.0)
            self.assertEqual(overrides.get("opencode"), 3.0)

    def test_load_from_env_malformed_config_fails_safely(self):
        with patch.dict(os.environ, {HEADROOM_OVERRIDE_ENV_VAR: "claude=broken"}):
            overrides = load_headroom_overrides_from_env()
            self.assertIsNone(overrides.get("claude"))


class AdaptiveRoutingServiceSelectionTestCase(unittest.TestCase):
    def test_mechanical_no_overrides_selects_big_pickle(self):
        service = AdaptiveRoutingService(
            telemetry_client=OmniRouteTelemetrySnapshot(),
            overrides=ProviderOverrides({}),
        )
        decision = service.select_route_for_task(
            required_capabilities=["mechanical"]
        )
        self.assertEqual(decision.model, "oc/big-pickle")
        self.assertFalse(decision.degraded)

    def test_standard_coding_selects_sonnet(self):
        service = AdaptiveRoutingService(
            telemetry_client=OmniRouteTelemetrySnapshot(),
            overrides=ProviderOverrides({}),
        )
        decision = service.select_route_for_task(required_capabilities=["coding"])
        self.assertEqual(decision.model, "cc/claude-sonnet-5-low")

    def test_mechanical_claude_high_opencode_low_selects_claude(self):
        service = AdaptiveRoutingService(
            telemetry_client=OmniRouteTelemetrySnapshot(),
            overrides=ProviderOverrides({"claude": 80, "opencode": 3}),
        )
        decision = service.select_route_for_task(
            required_capabilities=["mechanical"]
        )
        self.assertEqual(decision.model, "cc/claude-sonnet-5-low")

    def test_mechanical_opencode_high_claude_low_selects_big_pickle(self):
        service = AdaptiveRoutingService(
            telemetry_client=OmniRouteTelemetrySnapshot(),
            overrides=ProviderOverrides({"opencode": 80, "claude": 3}),
        )
        decision = service.select_route_for_task(
            required_capabilities=["mechanical"]
        )
        self.assertEqual(decision.model, "oc/big-pickle")

    def test_capability_hard_gate_is_authoritative(self):
        # Even with huge headroom for a provider whose only route lacks the
        # capability, that provider's route must never be selected.
        restricted_catalog = (
            ModelRoute(
                model_id="oc/big-pickle",
                provider="opencode",
                execution_path="OMNIROUTE",
                capabilities=frozenset({"mechanical"}),
                max_risk_level="low",
                approved=True,
                enabled=True,
            ),
            ModelRoute(
                model_id="cc/claude-sonnet-5-low",
                provider="claude",
                execution_path="OMNIROUTE",
                capabilities=frozenset({"standard-coding"}),
                max_risk_level="medium",
                approved=True,
                enabled=True,
            ),
        )
        service = AdaptiveRoutingService(
            telemetry_client=OmniRouteTelemetrySnapshot(),
            overrides=ProviderOverrides({"opencode": 99}),
            approved_catalog=restricted_catalog,
        )
        decision = service.select_route_for_task(required_capabilities=["coding"])
        self.assertEqual(decision.model, "cc/claude-sonnet-5-low")

    def test_risk_hard_gate_is_authoritative(self):
        service = AdaptiveRoutingService(
            telemetry_client=OmniRouteTelemetrySnapshot(),
            overrides=ProviderOverrides({"claude": 99}),
        )
        with self.assertRaises(NoEligibleRouteError):
            service.select_route_for_task(
                required_capabilities=["coding"],
                execution_policy={"risk_level": "critical"},
            )

    def test_high_quota_cannot_resurrect_unhealthy_provider(self):
        snapshot = OmniRouteTelemetrySnapshot(
            provider_telemetry={
                "claude": ProviderTelemetry(
                    provider="claude",
                    resource=ResourceSnapshot(
                        provider="claude",
                        state=QuotaState.UNKNOWN,
                        headroom_pct=None,
                        healthy=False,
                    ),
                    healthy=False,
                )
            }
        )
        restrictive_catalog = (
            ModelRoute(
                model_id="cc/claude-sonnet-5-low",
                provider="claude",
                execution_path="OMNIROUTE",
                capabilities=frozenset({"standard-coding"}),
                max_risk_level="medium",
                approved=True,
                enabled=True,
            ),
        )
        service = AdaptiveRoutingService(
            telemetry_client=snapshot,
            overrides=ProviderOverrides({"claude": 99}),
            approved_catalog=restrictive_catalog,
        )
        with self.assertRaises(NoEligibleRouteError):
            service.select_route_for_task(required_capabilities=["coding"])

    def test_high_quota_cannot_resurrect_invalid_credential(self):
        snapshot = OmniRouteTelemetrySnapshot(
            provider_telemetry={
                "claude": ProviderTelemetry(
                    provider="claude",
                    resource=ResourceSnapshot(
                        provider="claude",
                        state=QuotaState.UNKNOWN,
                        headroom_pct=None,
                        healthy=False,
                    ),
                    credential_usable=False,
                )
            }
        )
        restrictive_catalog = (
            ModelRoute(
                model_id="cc/claude-sonnet-5-low",
                provider="claude",
                execution_path="OMNIROUTE",
                capabilities=frozenset({"standard-coding"}),
                max_risk_level="medium",
                approved=True,
                enabled=True,
            ),
        )
        service = AdaptiveRoutingService(
            telemetry_client=snapshot,
            overrides=ProviderOverrides({"claude": 99}),
            approved_catalog=restrictive_catalog,
        )
        with self.assertRaises(NoEligibleRouteError):
            service.select_route_for_task(required_capabilities=["coding"])

    def test_exhausted_provider_is_excluded(self):
        service = AdaptiveRoutingService(
            telemetry_client=OmniRouteTelemetrySnapshot(),
            overrides=ProviderOverrides({"opencode": 0, "claude": 50}),
        )
        decision = service.select_route_for_task(
            required_capabilities=["mechanical"]
        )
        self.assertEqual(decision.model, "cc/claude-sonnet-5-low")

    def test_runtime_missing_approved_model_is_excluded(self):
        snapshot = OmniRouteTelemetrySnapshot(
            discovered_models=(
                DiscoveredModel(model_id="cc/claude-sonnet-5-low", provider="claude"),
            )
        )
        service = AdaptiveRoutingService(telemetry_client=snapshot)
        decision = service.select_route_for_task(
            required_capabilities=["mechanical"]
        )
        # big-pickle missing from discovery -> disabled -> excluded; falls
        # through to claude sonnet since it also supports mechanical.
        self.assertEqual(decision.model, "cc/claude-sonnet-5-low")

    def test_unknown_discovered_model_never_becomes_eligible(self):
        snapshot = OmniRouteTelemetrySnapshot(
            discovered_models=(
                DiscoveredModel(model_id="oc/big-pickle", provider="opencode"),
                DiscoveredModel(model_id="cc/claude-sonnet-5-low", provider="claude"),
                DiscoveredModel(model_id="some/unknown-model", provider="mystery"),
            )
        )
        service = AdaptiveRoutingService(telemetry_client=snapshot)
        decision = service.select_route_for_task(
            required_capabilities=["mechanical"]
        )
        self.assertNotEqual(decision.provider, "mystery")

    def test_terra_remains_impossible(self):
        catalog = _catalog(
            (
                ModelRoute(
                    model_id=TERRA_MODEL_ID,
                    provider="terra",
                    execution_path="OMNIROUTE",
                    capabilities=frozenset({"mechanical", "standard-coding"}),
                    max_risk_level="critical",
                    approved=True,
                    enabled=True,
                ),
            )
        )
        service = AdaptiveRoutingService(
            telemetry_client=OmniRouteTelemetrySnapshot(),
            overrides=ProviderOverrides({"terra": 100}),
            approved_catalog=catalog,
        )
        decision = service.select_route_for_task(
            required_capabilities=["mechanical"]
        )
        self.assertNotEqual(decision.provider, "terra")
        self.assertNotIn("terra", decision.model.lower())

    def test_route_override_bypasses_telemetry(self):
        calls = {"count": 0}

        class CountingSnapshotSource:
            def collect(self):
                calls["count"] += 1
                return OmniRouteTelemetrySnapshot()

        adapter = OmniRouteAdapter(
            script_path="worker.ps1",
            shell="powershell",
            runner=lambda command: type(
                "Completed", (), {"returncode": 0, "stdout": "ok", "stderr": ""}
            )(),
        )
        adapter.routing_service = AdaptiveRoutingService(
            telemetry_client=CountingSnapshotSource()
        )
        context = ExecutionContext(
            task_id="T-OVR",
            task_title="Retry",
            required_capabilities=["mechanical"],
            route_override={
                "route_class": "mechanical",
                "model": "cc/claude-sonnet-5-low",
                "effort": "low",
            },
        )
        result = adapter.run(context)
        self.assertEqual(result.routed_model, "cc/claude-sonnet-5-low")
        self.assertEqual(calls["count"], 0)

    def test_route_override_still_validates_against_ladders(self):
        with self.assertRaises(Exception):
            validate_route_for_class(
                "mechanical",
                {"model": "unapproved/model", "effort": "low"},
            )

    def test_retry_escalation_behavior_unchanged(self):
        worker = {
            "route_class": "mechanical",
            "execution_path": "OMNIROUTE",
            "provider": "omniroute",
            "model": "oc/big-pickle",
            "effort": "low",
        }
        escalated = next_route(worker)
        self.assertEqual(escalated["model"], "cc/claude-sonnet-5-low")

    def test_telemetry_collected_at_most_once_per_selection(self):
        calls = {"count": 0}

        class CountingSnapshotSource:
            def collect(self):
                calls["count"] += 1
                return OmniRouteTelemetrySnapshot()

        service = AdaptiveRoutingService(telemetry_client=CountingSnapshotSource())
        service.select_route_for_task(required_capabilities=["mechanical"])
        self.assertEqual(calls["count"], 1)

    def test_selected_command_model_equals_adapter_result_routed_model(self):
        captured = {}

        def fake_runner(command):
            captured["command"] = command
            return type(
                "Completed", (), {"returncode": 0, "stdout": "ok", "stderr": ""}
            )()

        adapter = OmniRouteAdapter(
            script_path="worker.ps1", shell="powershell", runner=fake_runner
        )
        adapter.routing_service = AdaptiveRoutingService(
            telemetry_client=OmniRouteTelemetrySnapshot(),
            overrides=ProviderOverrides({}),
        )
        context = ExecutionContext(
            task_id="T-1", task_title="Do work", required_capabilities=["mechanical"]
        )
        result = adapter.run(context)
        self.assertIn(result.routed_model, captured["command"])

    def test_total_telemetry_outage_uses_safe_legacy_fallback(self):
        class FailingClient:
            def collect(self):
                return OmniRouteTelemetrySnapshot(
                    errors=(
                        "/v1/models connection failed",
                        "/api/usage/quota connection failed",
                        "/api/monitoring/health connection failed",
                    ),
                )

        restrictive_catalog = (
            ModelRoute(
                model_id="oc/big-pickle",
                provider="opencode",
                execution_path="OMNIROUTE",
                capabilities=frozenset({"mechanical"}),
                max_risk_level="low",
                approved=True,
                enabled=True,
            ),
        )

        # Force ineligibility by requesting an unsupported capability so
        # select_best_route raises, forcing outage fallback logic to run.
        service = AdaptiveRoutingService(
            telemetry_client=FailingClient(),
            approved_catalog=restrictive_catalog,
        )
        decision = service.select_route_for_task(required_capabilities=["mechanical"])
        self.assertTrue(decision.degraded)
        self.assertEqual(decision.model, "oc/big-pickle")

    def test_explicit_unhealthy_evidence_must_not_use_legacy_fallback(self):
        snapshot = OmniRouteTelemetrySnapshot(
            provider_telemetry={
                "opencode": ProviderTelemetry(
                    provider="opencode",
                    resource=__import__(
                        "nexus.routing.models", fromlist=["ResourceSnapshot"]
                    ).ResourceSnapshot(
                        provider="opencode",
                        state=__import__(
                            "nexus.routing.models", fromlist=["QuotaState"]
                        ).QuotaState.UNKNOWN,
                        headroom_pct=None,
                        healthy=False,
                    ),
                    healthy=False,
                )
            },
            errors=("quota fetch failed",),
        )
        restrictive_catalog = (
            ModelRoute(
                model_id="oc/big-pickle",
                provider="opencode",
                execution_path="OMNIROUTE",
                capabilities=frozenset({"mechanical"}),
                max_risk_level="low",
                approved=True,
                enabled=True,
            ),
        )
        service = AdaptiveRoutingService(
            telemetry_client=snapshot,
            approved_catalog=restrictive_catalog,
        )
        with self.assertRaises(NoEligibleRouteError):
            service.select_route_for_task(required_capabilities=["mechanical"])

    def test_default_service_constructs_live_client_lazily(self):
        snapshot = OmniRouteTelemetrySnapshot(
            discovered_models=(
                DiscoveredModel(
                    model_id="oc/big-pickle",
                    provider="opencode",
                ),
                DiscoveredModel(
                    model_id="cc/claude-sonnet-5-low",
                    provider="claude",
                ),
            )
        )

        with patch(
            "nexus.routing.service.OmniRouteTelemetryClient"
        ) as client_cls:
            client_cls.return_value.collect.return_value = snapshot

            service = AdaptiveRoutingService(
                overrides=ProviderOverrides({}),
            )

            client_cls.assert_not_called()

            decision = service.select_route_for_task(
                required_capabilities=["mechanical"]
            )

            client_cls.assert_called_once_with()
            client_cls.return_value.collect.assert_called_once_with()
            self.assertEqual(
                decision.model,
                "oc/big-pickle",
            )

    def test_default_adapter_uses_live_telemetry_path(self):
        snapshot = OmniRouteTelemetrySnapshot(
            discovered_models=(
                DiscoveredModel(
                    model_id="oc/big-pickle",
                    provider="opencode",
                ),
                DiscoveredModel(
                    model_id="cc/claude-sonnet-5-low",
                    provider="claude",
                ),
            )
        )

        captured = {}

        def fake_runner(command):
            captured["command"] = command
            return type(
                "Completed",
                (),
                {
                    "returncode": 0,
                    "stdout": "ok",
                    "stderr": "",
                },
            )()

        with patch(
            "nexus.routing.service.OmniRouteTelemetryClient"
        ) as client_cls:
            client_cls.return_value.collect.return_value = snapshot

            adapter = OmniRouteAdapter(
                script_path="worker.ps1",
                shell="powershell",
                runner=fake_runner,
            )

            result = adapter.run(
                ExecutionContext(
                    task_id="T-LIVE-DEFAULT",
                    task_title="Adaptive initial execution",
                    required_capabilities=["mechanical"],
                )
            )

            client_cls.assert_called_once_with()
            client_cls.return_value.collect.assert_called_once_with()
            self.assertEqual(
                result.routed_model,
                "oc/big-pickle",
            )
            self.assertIn(
                "oc/big-pickle",
                captured["command"],
            )

    def test_models_failure_keeps_static_catalog_and_uses_resource_evidence(self):
        snapshot = OmniRouteTelemetrySnapshot(
            provider_telemetry={
                "opencode": ProviderTelemetry(
                    provider="opencode",
                    resource=ResourceSnapshot(
                        provider="opencode",
                        state=QuotaState.UNKNOWN,
                        headroom_pct=None,
                        healthy=False,
                    ),
                    healthy=False,
                ),
                "claude": ProviderTelemetry(
                    provider="claude",
                    resource=ResourceSnapshot(
                        provider="claude",
                        state=QuotaState.UNKNOWN,
                        headroom_pct=None,
                        healthy=True,
                    ),
                    credential_usable=True,
                ),
            },
            errors=(
                "/v1/models connection failed",
            ),
        )

        service = AdaptiveRoutingService(
            telemetry_client=snapshot,
            overrides=ProviderOverrides(
                {
                    "opencode": 99,
                    "claude": 50,
                }
            ),
        )

        decision = service.select_route_for_task(
            required_capabilities=["mechanical"]
        )

        # Model discovery is UNKNOWN, not an authoritative empty catalog.
        # The explicit unhealthy OpenCode signal remains a hard blocker,
        # while Claude can still serve mechanical work.
        self.assertEqual(
            decision.model,
            "cc/claude-sonnet-5-low",
        )

    def test_successful_empty_runtime_catalog_does_not_legacy_fallback(self):
        snapshot = OmniRouteTelemetrySnapshot(
            # No /v1/models error means model discovery succeeded.
            # Empty discovered_models is therefore authoritative.
            errors=(
                "/api/usage/quota connection failed",
                "/api/monitoring/health connection failed",
            ),
        )

        service = AdaptiveRoutingService(
            telemetry_client=snapshot,
        )

        with self.assertRaises(NoEligibleRouteError):
            service.select_route_for_task(
                required_capabilities=["mechanical"]
            )

    def test_total_outage_high_risk_cannot_bypass_risk_gate(self):
        snapshot = OmniRouteTelemetrySnapshot(
            errors=(
                "/v1/models connection failed",
                "/api/usage/quota connection failed",
                "/api/monitoring/health connection failed",
            )
        )

        service = AdaptiveRoutingService(
            telemetry_client=snapshot,
        )

        with self.assertRaises(NoEligibleRouteError):
            service.select_route_for_task(
                required_capabilities=["coding"],
                execution_policy={
                    "risk_level": "critical",
                },
            )

    def test_total_outage_ignores_headroom_overrides_and_preserves_legacy_route(self):
        snapshot = OmniRouteTelemetrySnapshot(
            errors=(
                "/v1/models connection failed",
                "/api/usage/quota connection failed",
                "/api/monitoring/health connection failed",
            )
        )

        service = AdaptiveRoutingService(
            telemetry_client=snapshot,
            overrides=ProviderOverrides(
                {
                    "claude": 100,
                    "opencode": 0,
                }
            ),
        )

        decision = service.select_route_for_task(
            required_capabilities=["mechanical"]
        )

        self.assertTrue(decision.degraded)
        self.assertEqual(
            decision.model,
            "oc/big-pickle",
        )

    def test_unhealthy_provider_gate_is_proven_with_runtime_model_present(self):
        snapshot = OmniRouteTelemetrySnapshot(
            discovered_models=(
                DiscoveredModel(
                    model_id="cc/claude-sonnet-5-low",
                    provider="claude",
                ),
            ),
            provider_telemetry={
                "claude": ProviderTelemetry(
                    provider="claude",
                    resource=ResourceSnapshot(
                        provider="claude",
                        state=QuotaState.UNKNOWN,
                        headroom_pct=None,
                        healthy=False,
                    ),
                    healthy=False,
                )
            },
        )

        service = AdaptiveRoutingService(
            telemetry_client=snapshot,
            overrides=ProviderOverrides(
                {"claude": 99}
            ),
        )

        with self.assertRaises(NoEligibleRouteError):
            service.select_route_for_task(
                required_capabilities=["coding"]
            )

    def test_invalid_credential_gate_is_proven_with_runtime_model_present(self):
        snapshot = OmniRouteTelemetrySnapshot(
            discovered_models=(
                DiscoveredModel(
                    model_id="cc/claude-sonnet-5-low",
                    provider="claude",
                ),
            ),
            provider_telemetry={
                "claude": ProviderTelemetry(
                    provider="claude",
                    resource=ResourceSnapshot(
                        provider="claude",
                        state=QuotaState.UNKNOWN,
                        headroom_pct=None,
                        healthy=False,
                    ),
                    credential_usable=False,
                )
            },
        )

        service = AdaptiveRoutingService(
            telemetry_client=snapshot,
            overrides=ProviderOverrides(
                {"claude": 99}
            ),
        )

        with self.assertRaises(NoEligibleRouteError):
            service.select_route_for_task(
                required_capabilities=["coding"]
            )


class StandaloneAdapterBackwardCompatibilityTestCase(unittest.TestCase):
    def test_select_route_remains_network_free(self):
        model, effort = select_route(["mechanical"])
        self.assertEqual(model, MECHANICAL_MODEL)
        self.assertEqual(effort, "low")

    def test_build_command_remains_network_free(self):
        context = ExecutionContext(
            task_id="TASK-1", task_title="Any", required_capabilities=["coding"]
        )
        command = build_command(context, script_path="worker.ps1", shell="powershell")
        self.assertIn(STANDARD_CODING_MODEL, command)


if __name__ == "__main__":
    unittest.main()