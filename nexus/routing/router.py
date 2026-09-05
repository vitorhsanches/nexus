"""Deterministic entry point for the Adaptive Capability Router Core."""

from __future__ import annotations

from nexus.routing.capabilities import (
    capability_eligible,
    is_known_capability,
    is_known_risk_level,
    risk_eligible,
)
from nexus.routing.catalog import (
    default_catalog,
    is_blocked_model,
)
from nexus.routing.models import (
    ModelRoute,
    QuotaState,
    ResourceSnapshot,
    RoutingCandidate,
    RoutingRequest,
    SelectedRoute,
)
from nexus.routing.resources import classify_headroom
from nexus.routing.scoring import (
    explain,
    score_route,
    sort_key,
)


class InvalidRoutingRequestError(ValueError):
    """Raised when routing input uses an unknown capability/risk policy."""


class NoEligibleRouteError(RuntimeError):
    """Raised when no route survives all hard eligibility gates."""


def _resource_for(
    provider: str,
    resources: dict[str, ResourceSnapshot],
) -> ResourceSnapshot:
    snapshot = resources.get(provider)

    if snapshot is None:
        return ResourceSnapshot(
            provider=provider,
            state=QuotaState.UNKNOWN,
            headroom_pct=None,
            healthy=True,
        )

    return snapshot


def _validate_request(
    request: RoutingRequest,
) -> None:
    if not is_known_capability(request.capability):
        raise InvalidRoutingRequestError(
            f"Unknown routing capability: "
            f"{request.capability!r}."
        )

    if not is_known_risk_level(request.risk_level):
        raise InvalidRoutingRequestError(
            f"Unknown routing risk level: "
            f"{request.risk_level!r}."
        )


def _eligible(
    route: ModelRoute,
    request: RoutingRequest,
    resources: dict[str, ResourceSnapshot],
) -> tuple[bool, ResourceSnapshot | None]:

    if is_blocked_model(route.model_id):
        return False, None

    if route.model_id in request.blocked_models:
        return False, None

    if route.provider in request.blocked_providers:
        return False, None

    if not capability_eligible(
        route,
        request.capability,
    ):
        return False, None

    if not risk_eligible(
        route,
        request.risk_level,
    ):
        return False, None

    if not route.approved or not route.enabled:
        return False, None

    if (
        route.experimental
        and not request.allow_experimental
    ):
        return False, None

    snapshot = _resource_for(
        route.provider,
        resources,
    )

    if not snapshot.healthy:
        return False, None

    band = classify_headroom(snapshot)

    if band.value == "exhausted":
        return False, None

    return True, snapshot


def select_best_route(
    request: RoutingRequest,
    catalog: tuple[ModelRoute, ...] | None = None,
    resources: dict[str, ResourceSnapshot] | None = None,
) -> SelectedRoute:
    """Select the highest ranked eligible route.

    Capability, risk, approval, health, blocked-route policy and exhaustion
    are hard gates. Resource availability only ranks candidates that already
    satisfy those requirements.
    """

    _validate_request(request)

    source_catalog = (
        catalog
        if catalog is not None
        else default_catalog()
    )

    resources = resources or {}

    candidates: list[RoutingCandidate] = []

    for route in source_catalog:
        eligible, snapshot = _eligible(
            route,
            request,
            resources,
        )

        if not eligible:
            continue

        band = classify_headroom(snapshot)

        score = score_route(
            route,
            band,
            snapshot,
        )

        reason = explain(
            route,
            request.capability,
            request.risk_level,
            band,
            snapshot,
        )

        candidates.append(
            RoutingCandidate(
                route=route,
                score=score,
                headroom_band=band,
                reason=reason,
            )
        )

    if not candidates:
        raise NoEligibleRouteError(
            "No eligible route found for "
            f"capability={request.capability!r}, "
            f"risk_level={request.risk_level!r}."
        )

    candidates.sort(
        key=lambda candidate: sort_key(
            candidate.route,
            candidate.score,
        )
    )

    winner = candidates[0]
    fallbacks = tuple(candidates[1:])

    reason = winner.reason

    if fallbacks:
        first_fallback = fallbacks[0]

        reason += (
            f" Ranked above fallback "
            f"{first_fallback.route.model_id} "
            f"(score={first_fallback.score:.0f}) "
            f"with selected score={winner.score:.0f}."
        )

    return SelectedRoute(
        route=winner.route,
        score=winner.score,
        reason=reason,
        fallbacks=fallbacks,
    )
