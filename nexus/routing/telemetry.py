"""OmniRoute runtime telemetry acquisition and normalization (Nexus v2.0-B).

This module translates real OmniRoute runtime information (discovered
models, quota/usage, and health) into safe, deterministic Nexus routing
signals.

CRITICAL INVARIANTS
--------------------
- Discovery is never approval. Runtime discovery MUST NEVER set
  ``ModelRoute.approved=True``.
- Missing/ambiguous quota information normalizes to ``QuotaState.UNKNOWN``
  with ``headroom_pct=None``. It never becomes a fabricated 100%.
- Aggregate/global health counts never fabricate per-provider health.
- No credentials are ever logged, printed, persisted, or included in
  exception text or object reprs.
- No network calls happen at import time or during normal test collection;
  all HTTP access goes through an injectable transport boundary.

This module is intentionally NOT wired into AgentExecutor, OmniRouteAdapter,
Mission execution, Review Gate, ROUTE_LADDERS, or the legacy Nexus CLI. It
only prepares routing context.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from urllib.error import URLError

from nexus.routing.models import (
    ModelRoute,
    QuotaState,
    ResourceSnapshot,
)
from nexus.routing.resources import (
    ProviderOverrides,
    snapshot_from_quota_response,
)


DEFAULT_BASE_URL = "http://127.0.0.1:20128"
DEFAULT_TIMEOUT_SECONDS = 2.0

_API_KEY_ENV_VAR = "OMNIROUTE_API_KEY"


class TelemetryCollectionError(RuntimeError):
    """Raised when a specific telemetry fetch cannot be completed safely.

    Never includes secrets. Only the failing endpoint and a sanitized
    description of the failure are included.
    """

    def __init__(self, endpoint: str, detail: str):
        self.endpoint = endpoint
        self.detail = detail
        super().__init__(f"OmniRoute telemetry fetch failed for {endpoint}: {detail}")


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiscoveredModel:
    """A single runtime model entry discovered from /v1/models.

    This is a pure discovery record. It carries no approval semantics.
    """

    model_id: str
    provider: str
    context_length: int | None = None

    def __post_init__(self):
        if not self.model_id or not self.model_id.strip():
            raise ValueError("model_id must be non-empty.")
        if not self.provider or not self.provider.strip():
            raise ValueError("provider must be non-empty.")


@dataclass(frozen=True, slots=True)
class ProviderTelemetry:
    """Normalized, fail-safe telemetry for a single provider/resource owner.

    ``credential_usable`` and ``healthy`` intentionally represent different
    signals:

    - credential_usable=False is a hard routing blocker;
    - healthy=False is a hard provider-health blocker;
    - None means the corresponding state is unknown, not healthy.
    """

    provider: str
    resource: ResourceSnapshot
    token_status: str | None = None
    credential_usable: bool | None = None
    healthy: bool | None = None
    diagnostics: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not self.provider or not self.provider.strip():
            raise ValueError("provider must be non-empty.")


@dataclass(frozen=True, slots=True)
class ServerHealth:
    """Normalized OmniRoute server health.

    Aggregate counters are kept separate from explicit per-provider health.
    Aggregate server/credential health must never be treated as proof that an
    individual provider is healthy.
    """

    status: str | None = None
    credential_total: int | None = None
    credential_healthy: int | None = None
    credential_failed: int | None = None
    catalog_count: int | None = None
    configured_count: int | None = None
    provider_health: dict[str, bool | None] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class OmniRouteTelemetrySnapshot:
    """Aggregate deterministic telemetry snapshot.

    May represent a partial collection: successful sections are preserved
    alongside explicit errors/warnings rather than fabricating missing data.
    """

    discovered_models: tuple[DiscoveredModel, ...] = field(default_factory=tuple)
    provider_telemetry: dict[str, ProviderTelemetry] = field(default_factory=dict)
    server_health: ServerHealth | None = None
    errors: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# Health / credential normalization
# ---------------------------------------------------------------------------


_TOKEN_USABLE = {
    "valid",
    "active",
    "available",
    "healthy",
    "ok",
}

_TOKEN_UNUSABLE = {
    "invalid",
    "expired",
    "revoked",
    "failed",
    "unusable",
    "unavailable",
    "disabled",
    "error",
}


def _token_status_usable(token_status) -> bool | None:
    """Normalize explicit credential usability without inventing health."""

    if token_status is None:
        return None

    if not isinstance(token_status, str):
        return None

    normalized = token_status.strip().lower()

    if normalized in _TOKEN_USABLE:
        return True

    if normalized in _TOKEN_UNUSABLE:
        return False

    return None


def _normalize_health_value(value) -> bool | None:
    """Normalize explicit provider health from common OmniRoute shapes.

    Unknown/unrecognized values remain None rather than being interpreted as
    healthy.
    """

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        normalized = value.strip().lower()

        if normalized in {
            "healthy",
            "available",
            "active",
            "ok",
            "up",
            "valid",
        }:
            return True

        if normalized in {
            "unhealthy",
            "unavailable",
            "failed",
            "error",
            "down",
            "invalid",
            "expired",
            "revoked",
            "disabled",
        }:
            return False

        return None

    if isinstance(value, dict):
        direct = value.get("healthy")

        if isinstance(direct, bool):
            return direct

        for key in ("status", "state", "health"):
            if key in value:
                normalized = _normalize_health_value(value.get(key))

                if normalized is not None:
                    return normalized

    return None


def _parse_provider_health(raw) -> dict[str, bool | None]:
    """Parse explicit providerHealth without using aggregate health counts."""

    result: dict[str, bool | None] = {}

    if isinstance(raw, dict):
        for provider, value in raw.items():
            if not isinstance(provider, str) or not provider.strip():
                continue

            result[provider] = _normalize_health_value(value)

        return result

    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue

            provider = entry.get("provider")

            if not isinstance(provider, str) or not provider.strip():
                continue

            result[provider] = _normalize_health_value(entry)

    return result


def _merge_explicit_health(
    first: bool | None,
    second: bool | None,
) -> bool | None:
    """False dominates, then explicit True, otherwise UNKNOWN."""

    if first is False or second is False:
        return False

    if first is True or second is True:
        return True

    return None


def _merge_provider_health(
    provider_telemetry: dict[str, ProviderTelemetry],
    provider_health: dict[str, bool | None],
) -> dict[str, ProviderTelemetry]:
    """Merge /health provider signals with quota/credential telemetry."""

    result = dict(provider_telemetry)

    for provider, health_signal in provider_health.items():
        existing = result.get(provider)

        if existing is None:
            diagnostics = ()

            if health_signal is False:
                diagnostics = ("providerHealth=unhealthy",)

            result[provider] = ProviderTelemetry(
                provider=provider,
                resource=ResourceSnapshot(
                    provider=provider,
                    state=QuotaState.UNKNOWN,
                    headroom_pct=None,
                    healthy=health_signal is not False,
                ),
                token_status=None,
                credential_usable=None,
                healthy=health_signal,
                diagnostics=diagnostics,
            )
            continue

        merged_health = _merge_explicit_health(
            existing.healthy,
            health_signal,
        )

        diagnostics = list(existing.diagnostics)

        if health_signal is False:
            diagnostics.append("providerHealth=unhealthy")

        resource_healthy = (
            existing.resource.healthy
            and existing.credential_usable is not False
            and merged_health is not False
        )

        result[provider] = ProviderTelemetry(
            provider=provider,
            resource=ResourceSnapshot(
                provider=existing.resource.provider,
                state=existing.resource.state,
                headroom_pct=existing.resource.headroom_pct,
                healthy=resource_healthy,
            ),
            token_status=existing.token_status,
            credential_usable=existing.credential_usable,
            healthy=merged_health,
            diagnostics=tuple(diagnostics),
        )

    return result


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class Transport:
    """Minimal injectable HTTP transport boundary.

    The default implementation uses the standard library. Tests should
    inject a fake transport so no real network calls occur.
    """

    def get_json(
        self,
        url: str,
        headers: dict[str, str],
        timeout: float,
    ):
        request = urllib.request.Request(url, headers=headers, method="GET")

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except URLError as exc:
            raise TelemetryCollectionError(url, "connection failed") from None
        except OSError:
            raise TelemetryCollectionError(url, "connection failed") from None

        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            raise TelemetryCollectionError(url, "malformed JSON response") from None


class OmniRouteTelemetryClient:
    """Fetches and normalizes OmniRoute runtime telemetry.

    Never logs, prints, or exposes the API key. The Authorization header is
    generated only at the transport boundary, and the key itself is never
    stored on any attribute that participates in ``repr()``.
    """

    __slots__ = (
        "_base_url",
        "_timeout",
        "_transport",
        "_OmniRouteTelemetryClient__api_key",
        "__weakref__",
    )

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        api_key: str | None = None,
        transport: Transport | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport if transport is not None else Transport()

        # Deliberately not stored as a plain public attribute: kept private
        # and excluded from repr/diagnostics.
        resolved_key = api_key if api_key is not None else os.environ.get(_API_KEY_ENV_VAR)
        object.__setattr__(self, "_OmniRouteTelemetryClient__api_key", resolved_key)

    def __repr__(self) -> str:
        return (
            f"OmniRouteTelemetryClient(base_url={self._base_url!r}, "
            f"timeout={self._timeout!r}, api_key=<redacted>)"
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        key = getattr(self, "_OmniRouteTelemetryClient__api_key", None)

        if key:
            headers["Authorization"] = f"Bearer {key}"

        return headers

    def _get(self, path: str):
        url = f"{self._base_url}{path}"
        return self._transport.get_json(url, self._headers(), self._timeout)

    def fetch_models(self) -> tuple[DiscoveredModel, ...]:
        payload = self._get("/v1/models")
        entries = payload.get("data") if isinstance(payload, dict) else payload

        if entries is None:
            raise TelemetryCollectionError("/v1/models", "missing 'data' field")

        if not isinstance(entries, list):
            raise TelemetryCollectionError("/v1/models", "unexpected payload shape")

        models = []

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            model_id = entry.get("id")
            provider = entry.get("owned_by")

            if not model_id or not provider:
                continue

            context_length = entry.get("context_length")

            if context_length is not None and not isinstance(context_length, int):
                context_length = None

            models.append(
                DiscoveredModel(
                    model_id=model_id,
                    provider=provider,
                    context_length=context_length,
                )
            )

        return tuple(models)

    def fetch_quota(self) -> dict[str, ProviderTelemetry]:
        payload = self._get("/api/usage/quota")

        if not isinstance(payload, dict):
            raise TelemetryCollectionError(
                "/api/usage/quota", "unexpected payload shape"
            )

        providers = payload.get("providers")

        if providers is None:
            raise TelemetryCollectionError(
                "/api/usage/quota", "missing 'providers' field"
            )

        if not isinstance(providers, list):
            raise TelemetryCollectionError(
                "/api/usage/quota", "unexpected payload shape"
            )

        result: dict[str, ProviderTelemetry] = {}

        for entry in providers:
            if not isinstance(entry, dict):
                continue

            provider = entry.get("provider")

            if not provider:
                continue

            token_status = entry.get("tokenStatus")
            credential_usable = _token_status_usable(token_status)

            resource = snapshot_from_quota_response(
                provider=provider,
                quota_total=entry.get("quotaTotal"),
                quota_used=entry.get("quotaUsed"),
                healthy=credential_usable is not False,
            )

            diagnostics = []

            if token_status is not None and credential_usable is not True:
                diagnostics.append(f"tokenStatus={token_status}")

            result[provider] = ProviderTelemetry(
                provider=provider,
                resource=resource,
                token_status=token_status,
                credential_usable=credential_usable,
                healthy=None,
                diagnostics=tuple(diagnostics),
            )

        return result

    def fetch_health(self) -> ServerHealth:
        payload = self._get("/api/monitoring/health")

        if not isinstance(payload, dict):
            raise TelemetryCollectionError("/api/monitoring/health", "unexpected payload shape")

        credential_health = payload.get("credentialHealth") or {}
        provider_summary = payload.get("providerSummary") or {}

        diagnostics = []

        raw_provider_health = payload.get("providerHealth")
        provider_health = _parse_provider_health(raw_provider_health)

        if not raw_provider_health:
            diagnostics.append(
                "providerHealth was empty/absent; per-provider health is "
                "UNKNOWN, not inferred from aggregate counts."
            )
        elif not provider_health:
            diagnostics.append(
                "providerHealth was present but no recognized provider health "
                "signals could be normalized."
            )

        return ServerHealth(
            status=payload.get("status"),
            credential_total=credential_health.get("total"),
            credential_healthy=credential_health.get("healthy"),
            credential_failed=credential_health.get("failed"),
            catalog_count=provider_summary.get("catalogCount"),
            configured_count=provider_summary.get("configuredCount"),
            provider_health=provider_health,
            diagnostics=tuple(diagnostics),
        )

    def collect(self) -> OmniRouteTelemetrySnapshot:
        """Aggregate collection that fails safe.

        Preserves any successfully collected sections and records errors for
        the rest instead of fabricating data.
        """

        discovered_models: tuple[DiscoveredModel, ...] = ()
        provider_telemetry: dict[str, ProviderTelemetry] = {}
        server_health: ServerHealth | None = None
        errors: list[str] = []
        warnings: list[str] = []

        try:
            discovered_models = self.fetch_models()
        except TelemetryCollectionError as exc:
            errors.append(str(exc))

        try:
            provider_telemetry = self.fetch_quota()
        except TelemetryCollectionError as exc:
            errors.append(str(exc))

        try:
            server_health = self.fetch_health()
            warnings.extend(server_health.diagnostics)

            provider_telemetry = _merge_provider_health(
                provider_telemetry,
                server_health.provider_health,
            )
        except TelemetryCollectionError as exc:
            errors.append(str(exc))

        return OmniRouteTelemetrySnapshot(
            discovered_models=discovered_models,
            provider_telemetry=provider_telemetry,
            server_health=server_health,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )


# ---------------------------------------------------------------------------
# Runtime catalog overlay (discovery correlation, never approval)
# ---------------------------------------------------------------------------


def build_runtime_catalog(
    approved_catalog: tuple[ModelRoute, ...],
    telemetry_snapshot: OmniRouteTelemetrySnapshot,
) -> tuple[ModelRoute, ...]:
    """Build a runtime-aware VIEW of the approved catalog.

    - Never mutates ``approved_catalog``.
    - Never promotes a discovered-but-unapproved model.
    - An approved route missing from the runtime /v1/models discovery
      becomes disabled (``enabled=False``) in the returned view.
    - Approved routes present in the runtime discovery are returned
      unchanged, still subject to existing static policy (e.g. the global
      Terra block continues to apply via ``is_blocked_model`` elsewhere).
    """

    discovered_ids = {
        model.model_id for model in telemetry_snapshot.discovered_models
    }

    result = []

    for route in approved_catalog:
        if route.model_id in discovered_ids:
            result.append(route)
        else:
            result.append(
                ModelRoute(
                    model_id=route.model_id,
                    provider=route.provider,
                    execution_path=route.execution_path,
                    effort=route.effort,
                    capabilities=route.capabilities,
                    max_risk_level=route.max_risk_level,
                    approved=route.approved,
                    enabled=False,
                    experimental=route.experimental,
                    cost_class=route.cost_class,
                    quality_tier=route.quality_tier,
                )
            )

    return tuple(result)


# ---------------------------------------------------------------------------
# Resource building
# ---------------------------------------------------------------------------


def build_routing_resources(
    approved_catalog: tuple[ModelRoute, ...],
    telemetry_snapshot: OmniRouteTelemetrySnapshot,
    overrides: ProviderOverrides | None = None,
) -> dict[str, ResourceSnapshot]:
    """Build a ``dict[str, ResourceSnapshot]`` consumable by select_best_route().

    Precedence for headroom, per provider:

        1. valid explicit ProviderOverrides
        2. reliable live KNOWN quota
        3. UNKNOWN

    Explicit unhealthy/unavailable provider state is a hard gate that an
    override cannot bypass.
    """

    providers = {route.provider for route in approved_catalog}

    result: dict[str, ResourceSnapshot] = {}

    for provider in providers:
        telemetry = telemetry_snapshot.provider_telemetry.get(provider)

        healthy = True

        if telemetry is not None:
            healthy = (
                telemetry.resource.healthy
                and telemetry.credential_usable is not False
                and telemetry.healthy is not False
            )

        override_pct = overrides.get(provider) if overrides is not None else None

        if override_pct is not None:
            result[provider] = overrides.as_snapshot(provider, healthy=healthy)
            continue

        if telemetry is not None:
            base = telemetry.resource
            result[provider] = ResourceSnapshot(
                provider=provider,
                state=base.state,
                headroom_pct=base.headroom_pct,
                healthy=healthy,
            )
            continue

        result[provider] = ResourceSnapshot(
            provider=provider,
            state=QuotaState.UNKNOWN,
            headroom_pct=None,
            healthy=healthy,
        )

    return result


# ---------------------------------------------------------------------------
# Optional live smoke helper (manual use only; never runs during tests)
# ---------------------------------------------------------------------------


def smoke_check(
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> OmniRouteTelemetrySnapshot:
    """Manual diagnostic helper for local smoke testing.

    Not invoked automatically by any test or import. Never prints secrets
    and never mutates routing/execution state; it only returns a snapshot.
    """

    client = OmniRouteTelemetryClient(base_url=base_url, timeout=timeout)
    return client.collect()
