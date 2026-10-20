"""Read-only routing and OmniRoute status projection for the Control Center.

The projection deliberately keeps four different facts separate:

* OmniRoute reachability / server health;
* credential-store cryptography;
* provider authentication and observed health;
* Nexus route approval and runtime callability.

Discovery is never treated as approval, aggregate health is never promoted to
per-provider health, and unknown signals remain ``UNKNOWN``.
"""

from __future__ import annotations

from nexus.routing.catalog import APPROVED_CATALOG, is_blocked_model
from nexus.routing.telemetry import OmniRouteTelemetryClient


def _upper(value: object, default: str = "UNKNOWN") -> str:
    text = str(value or "").strip()
    return text.upper() if text else default


def _runtime_state(snapshot) -> dict:
    health = snapshot.server_health
    if health is None:
        status = "UNAVAILABLE" if snapshot.errors else "UNKNOWN"
        return {
            "status": status,
            "version": None,
            "credential_store": "UNKNOWN",
            "cryptography_provider": None,
            "providers": "UNKNOWN",
            "configured_providers": None,
            "credential_healthy": None,
            "credential_failed": None,
        }

    raw_status = _upper(health.status)
    status = "ONLINE" if raw_status in {"HEALTHY", "OK", "UP"} else raw_status
    crypto_status = _upper(health.cryptography_status)
    credential_store = "HEALTHY" if crypto_status == "HEALTHY" else crypto_status

    configured = health.configured_count
    if configured == 0:
        providers = "AUTH REQUIRED"
    elif health.credential_failed and health.credential_failed > 0:
        providers = "DEGRADED"
    elif health.credential_healthy and health.credential_healthy > 0:
        # This is aggregate credential state, not per-provider health.
        providers = "CONFIGURED"
    else:
        providers = "UNKNOWN"

    return {
        "status": status,
        "version": None,
        "credential_store": credential_store,
        "cryptography_provider": health.cryptography_provider,
        "providers": providers,
        "configured_providers": configured,
        "credential_healthy": health.credential_healthy,
        "credential_failed": health.credential_failed,
    }


def _route_view(route, snapshot, configured_count: int | None) -> dict:
    discovered_ids = {item.model_id for item in snapshot.discovered_models}
    discovered = route.model_id in discovered_ids
    static_eligible = (
        route.approved
        and route.enabled
        and not route.experimental
        and not is_blocked_model(route.model_id)
    )
    provider = snapshot.provider_telemetry.get(route.provider)

    if not static_eligible:
        health = "DISABLED"
        eligibility = "NO"
        callable_now = False
    elif configured_count == 0:
        health = "AUTH REQUIRED"
        eligibility = "NO"
        callable_now = False
    elif not discovered:
        health = "UNAVAILABLE"
        eligibility = "NO"
        callable_now = False
    elif provider is None:
        health = "UNKNOWN"
        eligibility = "UNKNOWN"
        callable_now = False
    elif provider.credential_usable is False:
        health = "AUTH REQUIRED"
        eligibility = "NO"
        callable_now = False
    elif provider.healthy is False or not provider.resource.healthy:
        health = "DEGRADED"
        eligibility = "NO"
        callable_now = False
    elif provider.credential_usable is True:
        health = "HEALTHY" if provider.healthy is True else "AVAILABLE"
        eligibility = "YES"
        callable_now = True
    else:
        health = "UNKNOWN"
        eligibility = "UNKNOWN"
        callable_now = False

    quota = provider.resource if provider is not None else None
    return {
        "model": route.model_id,
        "provider": route.provider,
        "effort": route.effort,
        "capabilities": sorted(route.capabilities),
        "max_risk_level": route.max_risk_level,
        "cost_class": route.cost_class or "unknown",
        "quality_tier": route.quality_tier or "unknown",
        "approved": route.approved,
        "enabled": route.enabled,
        "discovered": discovered,
        "health": health,
        "eligibility": eligibility,
        "callable": callable_now,
        "quota_state": quota.state.value if quota is not None else "unknown",
        "headroom_pct": quota.headroom_pct if quota is not None else None,
    }


def get_routing_status(client=None) -> dict:
    """Collect and serialize a secret-free operational routing snapshot."""

    telemetry_client = client if client is not None else OmniRouteTelemetryClient()
    snapshot = telemetry_client.collect()
    runtime = _runtime_state(snapshot)
    configured_count = runtime["configured_providers"]
    routes = [
        _route_view(route, snapshot, configured_count)
        for route in APPROVED_CATALOG
    ]
    callable_count = sum(1 for route in routes if route["callable"])

    return {
        "runtime": runtime,
        "summary": {
            "approved_routes": sum(1 for route in routes if route["approved"]),
            "enabled_routes": sum(1 for route in routes if route["enabled"]),
            "callable_routes": callable_count,
            "actions_available": callable_count > 0,
        },
        "routes": routes,
        "policy": [
            "capability + risk hard gates",
            "quota / availability",
            "cost",
            "quality + minimum effort",
            "approved fallback",
        ],
        "errors": list(snapshot.errors),
        "warnings": list(snapshot.warnings),
    }
