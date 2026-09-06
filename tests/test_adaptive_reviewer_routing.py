"""Tests for Nexus v2.0-D Adaptive Operational Reviewer Routing."""

import unittest
from unittest.mock import patch

from nexus.routing.catalog import APPROVED_CATALOG
from nexus.routing.models import (
    CapabilityClass,
    ModelRoute,
    QuotaState,
    ResourceSnapshot,
)
from nexus.routing.resources import ProviderOverrides
from nexus.routing.router import NoEligibleRouteError
from nexus.routing.service import (
    AdaptiveRoutingService,
    AdaptiveRoutingUnavailableError,
    InvalidRiskLevelError,
    RoutingDecision,
)
from nexus.routing.telemetry import (
    DiscoveredModel,
    OmniRouteTelemetrySnapshot,
    ProviderTelemetry,
)

from nexus.dispatchers.review import (
    ReviewRoutingError,
    review_worker,
)


TERRA_MODEL_ID = "gpt-5.6-terra"


class _FakeRoutingService:
    def __init__(self, decision=None, error=None):
        self._decision = decision
        self._error = error
        self.calls = []

    def select_route_for_capability(self, capability, risk_level=None):
        self.calls.append((capability, risk_level))
        if self._error is not None:
            raise self._error
        return self._decision


class _FakeProcess:
    def __init__(self, output="", returncode=0):
        self.stdout = iter(output.splitlines(keepends=True))
        self._returncode = returncode

    def wait(self):
        return self._returncode


PASS_OUTPUT = (
    "NEXUS_REVIEW_BEGIN\n"
    '{"verdict": "PASS", "failure_class": null, "summary": "ok", '
    '"evidence": ["checked git diff"]}\n'
    "NEXUS_REVIEW_END\n"
)


class AdaptiveRoutingServiceCapabilitySelectionTestCase(unittest.TestCase):
    """Covers explicit review-capability selection primitives."""

    def test_review_capability_selects_only_review_capable_route(self):
        snapshot = OmniRouteTelemetrySnapshot()
        service = AdaptiveRoutingService(telemetry_client=snapshot)

        decision = service.select_route_for_capability(
            CapabilityClass.REVIEW.value,
            risk_level="low",
        )

        self.assertEqual(decision.model, "cc/claude-sonnet-5-low")
        self.assertEqual(decision.provider, "claude")
        self.assertEqual(decision.execution_path, "OMNIROUTE")

    def test_low_review_selects_approved_claude_route(self):
        snapshot = OmniRouteTelemetrySnapshot()
        service = AdaptiveRoutingService(telemetry_client=snapshot)

        decision = service.select_route_for_capability(
            CapabilityClass.REVIEW.value,
            risk_level="low",
        )
        self.assertEqual(decision.model, "cc/claude-sonnet-5-low")

    def test_medium_review_remains_eligible(self):
        snapshot = OmniRouteTelemetrySnapshot()
        service = AdaptiveRoutingService(telemetry_client=snapshot)

        decision = service.select_route_for_capability(
            CapabilityClass.REVIEW.value,
            risk_level="medium",
        )
        self.assertEqual(decision.model, "cc/claude-sonnet-5-low")

    def test_high_review_fails_no_eligible_route(self):
        snapshot = OmniRouteTelemetrySnapshot()
        service = AdaptiveRoutingService(telemetry_client=snapshot)

        with self.assertRaises(NoEligibleRouteError):
            service.select_route_for_capability(
                CapabilityClass.REVIEW.value,
                risk_level="high",
            )

    def test_critical_review_fails_no_eligible_route(self):
        snapshot = OmniRouteTelemetrySnapshot()
        service = AdaptiveRoutingService(telemetry_client=snapshot)

        with self.assertRaises(NoEligibleRouteError):
            service.select_route_for_capability(
                CapabilityClass.REVIEW.value,
                risk_level="critical",
            )

    def test_terra_never_selected(self):
        terra_route = ModelRoute(
            model_id=TERRA_MODEL_ID,
            provider="openai",
            execution_path="OMNIROUTE",
            effort="low",
            capabilities=frozenset({"review"}),
            max_risk_level="critical",
            approved=True,
            enabled=True,
            experimental=False,
        )
        catalog = APPROVED_CATALOG + (terra_route,)
        snapshot = OmniRouteTelemetrySnapshot()
        service = AdaptiveRoutingService(
            telemetry_client=snapshot,
            approved_catalog=catalog,
        )

        decision = service.select_route_for_capability(
            CapabilityClass.REVIEW.value,
            risk_level="low",
        )

        self.assertNotEqual(decision.model, TERRA_MODEL_ID)

    def test_unhealthy_reviewer_provider_rejected(self):
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
        service = AdaptiveRoutingService(telemetry_client=snapshot)

        with self.assertRaises(NoEligibleRouteError):
            service.select_route_for_capability(
                CapabilityClass.REVIEW.value,
                risk_level="low",
            )

    def test_unusable_reviewer_credential_rejected(self):
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
        service = AdaptiveRoutingService(telemetry_client=snapshot)

        with self.assertRaises(NoEligibleRouteError):
            service.select_route_for_capability(
                CapabilityClass.REVIEW.value,
                risk_level="low",
            )

    def test_exhausted_reviewer_provider_rejected(self):
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
                        state=QuotaState.EXHAUSTED,
                        headroom_pct=0,
                        healthy=True,
                    ),
                )
            },
        )
        service = AdaptiveRoutingService(telemetry_client=snapshot)

        with self.assertRaises(NoEligibleRouteError):
            service.select_route_for_capability(
                CapabilityClass.REVIEW.value,
                risk_level="low",
            )

    def test_runtime_discovery_missing_approved_reviewer_disables_it(self):
        # /v1/models succeeded authoritatively but does not list the
        # approved reviewer model -> it must be disabled.
        snapshot = OmniRouteTelemetrySnapshot(
            discovered_models=(
                DiscoveredModel(
                    model_id="oc/big-pickle",
                    provider="opencode",
                ),
            ),
        )
        service = AdaptiveRoutingService(telemetry_client=snapshot)

        with self.assertRaises(NoEligibleRouteError):
            service.select_route_for_capability(
                CapabilityClass.REVIEW.value,
                risk_level="low",
            )

    def test_v1_models_failure_retains_static_approved_review_catalog(self):
        snapshot = OmniRouteTelemetrySnapshot(
            errors=("/v1/models connection failed",),
            provider_telemetry={
                "claude": ProviderTelemetry(
                    provider="claude",
                    resource=ResourceSnapshot(
                        provider="claude",
                        state=QuotaState.KNOWN,
                        headroom_pct=80,
                        healthy=True,
                    ),
                    healthy=True,
                    credential_usable=True,
                )
            },
        )
        service = AdaptiveRoutingService(telemetry_client=snapshot)

        decision = service.select_route_for_capability(
            CapabilityClass.REVIEW.value,
            risk_level="low",
        )
        self.assertEqual(decision.model, "cc/claude-sonnet-5-low")

    def test_total_outage_uses_approved_route_and_marks_degraded(self):
        snapshot = OmniRouteTelemetrySnapshot(
            errors=(
                "/v1/models connection failed",
                "/api/usage/quota connection failed",
                "/api/monitoring/health connection failed",
            )
        )
        service = AdaptiveRoutingService(telemetry_client=snapshot)

        decision = service.select_route_for_capability(
            CapabilityClass.REVIEW.value,
            risk_level="low",
        )
        self.assertTrue(decision.degraded)
        self.assertEqual(decision.model, "cc/claude-sonnet-5-low")

    def test_total_outage_high_risk_fails_closed(self):
        snapshot = OmniRouteTelemetrySnapshot(
            errors=(
                "/v1/models connection failed",
                "/api/usage/quota connection failed",
                "/api/monitoring/health connection failed",
            )
        )
        service = AdaptiveRoutingService(telemetry_client=snapshot)

        with self.assertRaises(NoEligibleRouteError):
            service.select_route_for_capability(
                CapabilityClass.REVIEW.value,
                risk_level="critical",
            )

    def test_constructing_service_performs_no_network_io(self):
        # Constructing without an injected telemetry client/snapshot must
        # not perform any network I/O; only lazy collection would.
        AdaptiveRoutingService()


class ReviewWorkerAdaptiveRoutingTestCase(unittest.TestCase):
    """Covers the operational reviewer bridge in nexus.dispatchers.review."""

    def _decision(self, **overrides):
        defaults = dict(
            model="cc/claude-sonnet-5-low",
            provider="claude",
            effort="low",
            execution_path="OMNIROUTE",
            reason="test decision",
            degraded=False,
        )
        defaults.update(overrides)
        return RoutingDecision(**defaults)

    def test_default_reviewer_requests_adaptive_routing(self):
        fake_service = _FakeRoutingService(self._decision())

        with patch(
            "nexus.dispatchers.review._find_codex",
            return_value="C:/fake/codex.exe",
        ), patch(
            "nexus.dispatchers.review.create_agent",
            return_value="AGENT-1",
        ), patch(
            "nexus.dispatchers.review.update_agent_status",
        ), patch(
            "nexus.dispatchers.review.update_agent_execution",
        ):
            result = review_worker(
                run_id="RUN-1",
                worker_id="WORKER-1",
                worktree="C:/fake/worktree",
                original_task="do something",
                worker_scope="fix things",
                routing_service=fake_service,
                process_launcher=lambda command: _FakeProcess(PASS_OUTPUT),
            )

        self.assertEqual(len(fake_service.calls), 1)
        capability, risk_level = fake_service.calls[0]
        self.assertEqual(capability, "review")
        self.assertEqual(risk_level, "low")
        self.assertEqual(result["status"], "COMPLETED")

    def test_reviewer_command_explicitly_selects_model_provider_omniroute(self):
        fake_service = _FakeRoutingService(self._decision())
        captured_commands = []

        def launcher(command):
            captured_commands.append(command)
            return _FakeProcess(PASS_OUTPUT)

        with patch(
            "nexus.dispatchers.review._find_codex",
            return_value="C:/fake/codex.exe",
        ), patch(
            "nexus.dispatchers.review.create_agent",
            return_value="AGENT-1",
        ), patch(
            "nexus.dispatchers.review.update_agent_status",
        ), patch(
            "nexus.dispatchers.review.update_agent_execution",
        ):
            review_worker(
                run_id="RUN-1",
                worker_id="WORKER-1",
                worktree="C:/fake/worktree",
                original_task="do something",
                worker_scope="fix things",
                routing_service=fake_service,
                process_launcher=launcher,
            )

        command = captured_commands[0]
        self.assertIn('model_provider="omniroute"', command)

    def test_selected_reviewer_model_is_exact_model_sent_to_codex(self):
        fake_service = _FakeRoutingService(
            self._decision(model="cc/claude-sonnet-5-low", effort="low")
        )
        captured_commands = []

        def launcher(command):
            captured_commands.append(command)
            return _FakeProcess(PASS_OUTPUT)

        with patch(
            "nexus.dispatchers.review._find_codex",
            return_value="C:/fake/codex.exe",
        ), patch(
            "nexus.dispatchers.review.create_agent",
            return_value="AGENT-1",
        ), patch(
            "nexus.dispatchers.review.update_agent_status",
        ), patch(
            "nexus.dispatchers.review.update_agent_execution",
        ):
            review_worker(
                run_id="RUN-1",
                worker_id="WORKER-1",
                worktree="C:/fake/worktree",
                original_task="do something",
                worker_scope="fix things",
                routing_service=fake_service,
                process_launcher=launcher,
            )

        command = captured_commands[0]
        self.assertIn('model="cc/claude-sonnet-5-low"', command)
        self.assertIn('model_reasoning_effort="low"', command)

    def test_plan_risk_is_propagated_to_reviewer_route_selection(self):
        fake_service = _FakeRoutingService(self._decision())

        with patch(
            "nexus.dispatchers.review._find_codex",
            return_value="C:/fake/codex.exe",
        ), patch(
            "nexus.dispatchers.review.create_agent",
            return_value="AGENT-1",
        ), patch(
            "nexus.dispatchers.review.update_agent_status",
        ), patch(
            "nexus.dispatchers.review.update_agent_execution",
        ):
            review_worker(
                run_id="RUN-1",
                worker_id="WORKER-1",
                worktree="C:/fake/worktree",
                original_task="do something",
                worker_scope="fix things",
                risk_level="MEDIUM",
                routing_service=fake_service,
                process_launcher=lambda command: _FakeProcess(PASS_OUTPUT),
            )

        capability, risk_level = fake_service.calls[0]
        self.assertEqual(risk_level, "medium")

    def test_routing_metadata_recorded_in_result(self):
        fake_service = _FakeRoutingService(self._decision())

        with patch(
            "nexus.dispatchers.review._find_codex",
            return_value="C:/fake/codex.exe",
        ), patch(
            "nexus.dispatchers.review.create_agent",
            return_value="AGENT-1",
        ), patch(
            "nexus.dispatchers.review.update_agent_status",
        ), patch(
            "nexus.dispatchers.review.update_agent_execution",
        ):
            result = review_worker(
                run_id="RUN-1",
                worker_id="WORKER-1",
                worktree="C:/fake/worktree",
                original_task="do something",
                worker_scope="fix things",
                routing_service=fake_service,
                process_launcher=lambda command: _FakeProcess(PASS_OUTPUT),
            )

        routing = result["routing"]
        self.assertEqual(routing["model"], "cc/claude-sonnet-5-low")
        self.assertEqual(routing["provider"], "claude")
        self.assertEqual(routing["effort"], "low")
        self.assertEqual(routing["execution_path"], "OMNIROUTE")
        self.assertFalse(routing["degraded"])

    def test_malformed_reviewer_result_fails_closed(self):
        fake_service = _FakeRoutingService(self._decision())

        with patch(
            "nexus.dispatchers.review._find_codex",
            return_value="C:/fake/codex.exe",
        ), patch(
            "nexus.dispatchers.review.create_agent",
            return_value="AGENT-1",
        ), patch(
            "nexus.dispatchers.review.update_agent_status",
        ), patch(
            "nexus.dispatchers.review.update_agent_execution",
        ):
            result = review_worker(
                run_id="RUN-1",
                worker_id="WORKER-1",
                worktree="C:/fake/worktree",
                original_task="do something",
                worker_scope="fix things",
                routing_service=fake_service,
                process_launcher=lambda command: _FakeProcess("no envelope here"),
            )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertIsNone(result["review"])

    def test_launch_failure_fails_closed(self):
        fake_service = _FakeRoutingService(self._decision())

        def raising_launcher(command):
            raise OSError("cannot launch")

        with patch(
            "nexus.dispatchers.review._find_codex",
            return_value="C:/fake/codex.exe",
        ), patch(
            "nexus.dispatchers.review.create_agent",
            return_value="AGENT-1",
        ), patch(
            "nexus.dispatchers.review.update_agent_status",
        ), patch(
            "nexus.dispatchers.review.update_agent_execution",
        ):
            result = review_worker(
                run_id="RUN-1",
                worker_id="WORKER-1",
                worktree="C:/fake/worktree",
                original_task="do something",
                worker_scope="fix things",
                routing_service=fake_service,
                process_launcher=raising_launcher,
            )

        self.assertEqual(result["status"], "LAUNCH_FAILED")

    def test_no_eligible_route_fails_closed_without_launching_codex(self):
        fake_service = _FakeRoutingService(error=NoEligibleRouteError("none"))

        with patch(
            "nexus.dispatchers.review._find_codex",
        ) as mock_find_codex:
            result = review_worker(
                run_id="RUN-1",
                worker_id="WORKER-1",
                worktree="C:/fake/worktree",
                original_task="do something",
                worker_scope="fix things",
                risk_level="high",
                routing_service=fake_service,
            )

        mock_find_codex.assert_not_called()
        self.assertEqual(result["status"], "BLOCKED")

    def test_unknown_explicit_risk_fails_closed(self):
        fake_service = _FakeRoutingService(self._decision())

        result = review_worker(
            run_id="RUN-1",
            worker_id="WORKER-1",
            worktree="C:/fake/worktree",
            original_task="do something",
            worker_scope="fix things",
            risk_level="nonsense",
            routing_service=fake_service,
        )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(len(fake_service.calls), 0)

    def test_explicit_override_validated_against_approved_catalog(self):
        with patch(
            "nexus.dispatchers.review._find_codex",
            return_value="C:/fake/codex.exe",
        ), patch(
            "nexus.dispatchers.review.create_agent",
            return_value="AGENT-1",
        ), patch(
            "nexus.dispatchers.review.update_agent_status",
        ), patch(
            "nexus.dispatchers.review.update_agent_execution",
        ):
            result = review_worker(
                run_id="RUN-1",
                worker_id="WORKER-1",
                worktree="C:/fake/worktree",
                original_task="do something",
                worker_scope="fix things",
                model="cc/claude-sonnet-5-low",
                effort="low",
                process_launcher=lambda command: _FakeProcess(PASS_OUTPUT),
            )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["routing"]["model"], "cc/claude-sonnet-5-low")

    def test_explicit_override_rejects_non_approved_model(self):
        result = review_worker(
            run_id="RUN-1",
            worker_id="WORKER-1",
            worktree="C:/fake/worktree",
            original_task="do something",
            worker_scope="fix things",
            model="gpt-5.6-luna",
        )

        self.assertEqual(result["status"], "BLOCKED")

    def test_explicit_override_rejects_terra(self):
        result = review_worker(
            run_id="RUN-1",
            worker_id="WORKER-1",
            worktree="C:/fake/worktree",
            original_task="do something",
            worker_scope="fix things",
            model=TERRA_MODEL_ID,
        )

        self.assertEqual(result["status"], "BLOCKED")

    def test_explicit_override_rejects_high_risk(self):
        result = review_worker(
            run_id="RUN-1",
            worker_id="WORKER-1",
            worktree="C:/fake/worktree",
            original_task="do something",
            worker_scope="fix things",
            model="cc/claude-sonnet-5-low",
            risk_level="high",
        )

        self.assertEqual(result["status"], "BLOCKED")

    def test_explicit_override_effort_must_match_approved_route(self):
        """An explicit override must never manufacture an unapproved effort.

        ``cc/claude-sonnet-5-low`` is only approved at effort="low"; an
        explicit effort="high" override must fail closed rather than being
        silently substituted into the RoutingDecision.
        """

        result = review_worker(
            run_id="RUN-1",
            worker_id="WORKER-1",
            worktree="C:/fake/worktree",
            original_task="do something",
            worker_scope="fix things",
            model="cc/claude-sonnet-5-low",
            effort="high",
        )

        self.assertEqual(result["status"], "BLOCKED")

    def test_explicit_override_exact_effort_still_valid(self):
        with patch(
            "nexus.dispatchers.review._find_codex",
            return_value="C:/fake/codex.exe",
        ), patch(
            "nexus.dispatchers.review.create_agent",
            return_value="AGENT-1",
        ), patch(
            "nexus.dispatchers.review.update_agent_status",
        ), patch(
            "nexus.dispatchers.review.update_agent_execution",
        ):
            result = review_worker(
                run_id="RUN-1",
                worker_id="WORKER-1",
                worktree="C:/fake/worktree",
                original_task="do something",
                worker_scope="fix things",
                model="cc/claude-sonnet-5-low",
                effort="low",
                process_launcher=lambda command: _FakeProcess(PASS_OUTPUT),
            )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["routing"]["effort"], "low")


class AdaptiveRoutingUnavailableFailsClosedTests(unittest.TestCase):
    def test_adaptive_routing_unavailable_blocks_without_launching_codex(self):
        service = _FakeRoutingService(
            error=AdaptiveRoutingUnavailableError(
                "complete telemetry outage; no approved fallback route"
            )
        )

        with patch(
            "nexus.dispatchers.review._find_codex"
        ) as find_codex, patch(
            "nexus.dispatchers.review.create_agent",
            return_value="AGENT-1",
        ), patch(
            "nexus.dispatchers.review.update_agent_status",
        ), patch(
            "nexus.dispatchers.review.update_agent_execution",
        ):
            launcher_calls = []

            def _launcher(command):
                launcher_calls.append(command)
                return _FakeProcess(PASS_OUTPUT)

            result = review_worker(
                run_id="RUN-1",
                worker_id="WORKER-1",
                worktree="C:/fake/worktree",
                original_task="do something",
                worker_scope="fix things",
                routing_service=service,
                process_launcher=_launcher,
            )

        self.assertEqual(result["status"], "BLOCKED")
        find_codex.assert_not_called()
        self.assertEqual(launcher_calls, [])


if __name__ == "__main__":
    unittest.main()
