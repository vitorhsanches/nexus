"""Adaptive Operational Routing bridge (Nexus v2.0-C).

Bridges the existing operational OmniRouteAdapter to the v2.0 Adaptive
Capability Router by orchestrating, at actual execution-selection time:

    1. runtime OmniRoute telemetry collection;
    2. runtime-aware approved catalog overlay;
    3. resource/headroom context (telemetry + injected overrides);
    4. Task capability/risk normalization;
    5. select_best_route();
    6. a structured routing decision consumable by the adapter.

This module intentionally contains only orchestration. Telemetry
normalization lives in nexus.routing.telemetry; hard-gate/scoring policy
lives in nexus.routing.router/scoring/capabilities.

No network calls happen at import time, during object construction, or
during repr(). Telemetry is only collected lazily, inside
select_route_for_task, and at most once per call.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from nexus.routing.catalog import default_catalog
from nexus.routing.models import (
    CapabilityClass,
    ModelRoute,
    RiskLevel,
    RoutingRequest,
)
from nexus.routing.resources import ProviderOverrides
from nexus.routing.router import (
    NoEligibleRouteError,
    select_best_route,
)
from nexus.routing.telemetry import (
    OmniRouteTelemetryClient,
    OmniRouteTelemetrySnapshot,
    build_routing_resources,
    build_runtime_catalog,
)


HEADROOM_OVERRIDE_ENV_VAR = "NEXUS_PROVIDER_HEADROOM_OVERRIDES"

MECHANICAL_CAPABILITIES = frozenset(
    {
        "mechanical",
        "formatting",
        "cleanup",
    }
)

_RISK_ALIASES = {
    "low": RiskLevel.LOW.value,
    "medium": RiskLevel.MEDIUM.value,
    "high": RiskLevel.HIGH.value,
    "critical": RiskLevel.CRITICAL.value,
}


class InvalidRiskLevelError(ValueError):
    """Raised when an explicit risk level is present but unrecognized.

    Fails closed rather than silently normalizing to LOW.
    """


class AdaptiveRoutingUnavailableError(RuntimeError):
    """Raised when no route can be safely selected, even via fallback."""


def capability_class_for(required_capabilities) -> str:
    """Map a Task's required capabilities to a router capability class.

    Preserves current operational behavior exactly:

        non-empty capabilities, all mechanical -> mechanical
        otherwise                              -> standard-coding

    Advanced-coding/review/planning routing is intentionally not introduced
    into the operational Adapter yet.
    """

    capabilities = set(required_capabilities or [])

    if capabilities and capabilities.issubset(MECHANICAL_CAPABILITIES):
        return CapabilityClass.MECHANICAL.value

    return CapabilityClass.STANDARD_CODING.value


def normalize_risk_level(explicit_risk) -> str:
    """Normalize an explicit risk level, failing closed on unknown values.

    None/absent risk preserves backward compatibility with LOW. Case is
    normalized safely (LOW -> low, etc). An explicit but unrecognized risk
    level raises rather than silently becoming LOW.
    """

    if explicit_risk is None:
        return RiskLevel.LOW.value

    if not isinstance(explicit_risk, str):
        raise InvalidRiskLevelError(
            f"Unrecognized explicit risk level: {explicit_risk!r}."
        )

    normalized = explicit_risk.strip().lower()
    resolved = _RISK_ALIASES.get(normalized)

    if resolved is None:
        raise InvalidRiskLevelError(
            f"Unrecognized explicit risk level: {explicit_risk!r}."
        )

    return resolved


def risk_level_for_policy(execution_policy) -> str:
    """Extract and normalize an explicit risk level from an execution policy."""

    policy = execution_policy if isinstance(execution_policy, dict) else {}
    return normalize_risk_level(policy.get("risk_level"))


def _parse_headroom_override_entry(entry: str) -> tuple[str, float]:
    if "=" not in entry:
        raise ValueError(f"Malformed headroom override entry: {entry!r}.")

    provider, _, raw_value = entry.partition("=")
    provider = provider.strip()
    raw_value = raw_value.strip()

    if not provider:
        raise ValueError(f"Malformed headroom override entry: {entry!r}.")

    if not re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+|\d+)", raw_value):
        raise ValueError(
            f"Malformed headroom override value for {provider!r}: {raw_value!r}."
        )

    value = float(raw_value)

    if not (0 <= value <= 100):
        raise ValueError(
            f"Headroom override value for {provider!r} out of range: {value!r}. "
            "Expected a finite percentage from 0 to 100."
        )

    return provider, value


def parse_headroom_overrides_env(raw):
    """Parse NEXUS_PROVIDER_HEADROOM_OVERRIDES deterministically.

    Format: provider=percent pairs separated by commas, e.g.
    claude=80,codex=3,opencode=55.

    Whitespace tolerant. No percentages are hardcoded here; values come
    solely from the environment. No eval is used. Malformed configuration
    fails safely by raising ValueError; callers decide how to degrade (e.g.
    ignore overrides entirely).
    """

    if raw is None or not raw.strip():
        return {}

    values = {}

    for entry in raw.split(","):
        entry = entry.strip()

        if not entry:
            continue

        provider, value = _parse_headroom_override_entry(entry)
        values[provider] = value

    return values


def load_headroom_overrides_from_env(
    env_var: str = HEADROOM_OVERRIDE_ENV_VAR,
) -> ProviderOverrides:
    """Build a ProviderOverrides from the environment, failing safely.

    Absence of the variable is fully supported and returns empty overrides.
    Malformed configuration also degrades to empty overrides rather than
    raising into the caller: a manual convenience knob must never crash
    routing.
    """

    raw = os.environ.get(env_var)

    try:
        values = parse_headroom_overrides_env(raw)
        return ProviderOverrides(values)
    except (ValueError, TypeError):
        return ProviderOverrides({})


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """A structured adaptive routing decision for a single execution."""

    model: str
    provider: str
    effort: str
    execution_path: str
    reason: str
    fallbacks: tuple = field(default_factory=tuple)
    degraded: bool = False


class AdaptiveRoutingService:
    """Bridges Task routing requests to the v2.0 Adaptive Capability Router.

    Supports dependency injection of the telemetry client/snapshot source,
    ProviderOverrides, and the approved catalog so tests never perform real
    network calls. Telemetry is collected lazily, only inside
    select_route_for_task, and at most once per call.
    """

    def __init__(
        self,
        telemetry_client=None,
        overrides: ProviderOverrides | None = None,
        approved_catalog: tuple[ModelRoute, ...] | None = None,
        legacy_fallback: dict | None = None,
    ):
        # Constructor performs no network I/O: the client is only stored,
        # never invoked, until select_route_for_task() runs.
        self._telemetry_client = telemetry_client
        self._overrides = (
            overrides
            if overrides is not None
            else load_headroom_overrides_from_env()
        )
        self._approved_catalog = (
            approved_catalog
            if approved_catalog is not None
            else default_catalog()
        )

        # Legacy static routes, used only for total-telemetry-outage
        # fallback. (model, provider, effort, execution_path).
        self._legacy_fallback = legacy_fallback or {
            CapabilityClass.MECHANICAL.value: (
                "oc/big-pickle",
                "opencode",
                "low",
                "OMNIROUTE",
            ),
            CapabilityClass.STANDARD_CODING.value: (
                "cc/claude-sonnet-5-low",
                "claude",
                "low",
                "OMNIROUTE",
            ),
        }

    def _collect_telemetry(self) -> OmniRouteTelemetrySnapshot:
        """Collect runtime telemetry lazily.

        When no source was injected, the operational default is the real
        OmniRouteTelemetryClient. Constructing the service itself remains
        network-free; the client is created and collected only when an
        initial route is actually selected.
        """

        if self._telemetry_client is None:
            self._telemetry_client = OmniRouteTelemetryClient()

        if isinstance(
            self._telemetry_client,
            OmniRouteTelemetrySnapshot,
        ):
            return self._telemetry_client

        return self._telemetry_client.collect()

    @staticmethod
    def _models_discovery_failed(
        snapshot: OmniRouteTelemetrySnapshot,
    ) -> bool:
        """Return True only when /v1/models explicitly failed.

        OmniRouteTelemetryClient.collect() records endpoint-specific errors.
        This distinction is important: an empty catalog returned
        successfully is authoritative negative evidence, while an empty
        discovered_models tuple caused by a failed endpoint is UNKNOWN.
        """

        return any(
            "/v1/models" in str(error)
            for error in snapshot.errors
        )

    def _legacy_fallback_decision(
        self,
        capability: str,
        risk_level: str,
    ) -> RoutingDecision:
        """Return the legacy route only after normal router hard gates.

        Resource overrides are deliberately ignored during complete telemetry
        outage so degraded operation preserves the existing static routing
        behavior. Capability, risk, approval, enabled state and Terra blocking
        remain authoritative because the candidate is revalidated through
        select_best_route().
        """

        fallback = self._legacy_fallback.get(capability)

        if fallback is None:
            raise AdaptiveRoutingUnavailableError(
                "No approved legacy fallback exists for "
                f"capability={capability!r}."
            )

        model, provider, effort, execution_path = fallback

        fallback_catalog = tuple(
            route
            for route in self._approved_catalog
            if route.model_id == model
            and route.provider == provider
            and route.effort == effort
            and route.execution_path == execution_path
        )

        if not fallback_catalog:
            raise AdaptiveRoutingUnavailableError(
                "Configured legacy fallback is not present in the "
                "approved catalog."
            )

        request = RoutingRequest(
            capability=capability,
            risk_level=risk_level,
        )

        # Empty resource mapping means UNKNOWN/NEUTRAL headroom.
        # No manual headroom override may influence complete-outage fallback.
        selected = select_best_route(
            request,
            catalog=fallback_catalog,
            resources={},
        )

        route = selected.route

        return RoutingDecision(
            model=route.model_id,
            provider=route.provider,
            effort=route.effort,
            execution_path=route.execution_path,
            reason=(
                "OmniRoute telemetry was completely unavailable; "
                "using the existing approved legacy route after "
                "capability/risk safety validation. "
                + selected.reason
            ),
            fallbacks=(),
            degraded=True,
        )

    def select_route_for_task(
        self,
        required_capabilities=None,
        execution_policy=None,
    ) -> RoutingDecision:
        """Select the best route for one NEW execution.

        Explicit retry/escalation route_override never reaches this method;
        OmniRouteAdapter handles that path through the existing v1.9 ladder.

        Runtime discovery is authoritative only when /v1/models did not
        fail. A successfully fetched empty runtime catalog is explicit
        negative evidence and therefore disables approved models. When
        /v1/models itself failed but other telemetry sections succeeded,
        the static approved catalog is retained and trustworthy resource /
        health evidence is still applied.

        Complete telemetry outage uses the existing legacy route only after
        normal capability/risk hard-gate validation.
        """

        capability = capability_class_for(
            required_capabilities
        )
        risk_level = risk_level_for_policy(
            execution_policy
        )

        snapshot = self._collect_telemetry()

        models_discovery_failed = (
            self._models_discovery_failed(snapshot)
        )

        total_outage = (
            models_discovery_failed
            and not snapshot.discovered_models
            and not snapshot.provider_telemetry
            and snapshot.server_health is None
        )

        if total_outage:
            return self._legacy_fallback_decision(
                capability,
                risk_level,
            )

        telemetry_attempted = (
            bool(snapshot.discovered_models)
            or bool(snapshot.provider_telemetry)
            or snapshot.server_health is not None
            or bool(snapshot.errors)
            or bool(snapshot.warnings)
        )

        # If /v1/models failed, absence from discovered_models is not
        # authoritative. Keep the approved catalog and apply whichever
        # quota/credential/provider-health sections did succeed.
        models_discovery_authoritative = (
            telemetry_attempted
            and not models_discovery_failed
        )

        if models_discovery_authoritative:
            runtime_catalog = build_runtime_catalog(
                self._approved_catalog,
                snapshot,
            )
        else:
            runtime_catalog = self._approved_catalog

        resources = build_routing_resources(
            self._approved_catalog,
            snapshot,
            overrides=self._overrides,
        )

        request = RoutingRequest(
            capability=capability,
            risk_level=risk_level,
        )

        selected = select_best_route(
            request,
            catalog=runtime_catalog,
            resources=resources,
        )

        route = selected.route

        return RoutingDecision(
            model=route.model_id,
            provider=route.provider,
            effort=route.effort,
            execution_path=route.execution_path,
            reason=selected.reason,
            fallbacks=selected.fallbacks,
            degraded=False,
        )
