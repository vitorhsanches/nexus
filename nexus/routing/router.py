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
from nexus.routing.resources import capacity_sufficient, classify_headroom
from nexus.routing.scoring import (
    cost_tier,
    explain,
    score_route,
    sort_key,
)


class InvalidRoutingRequestError(ValueError):
    """Raised when routing input uses an unknown capability/risk policy."""


class NoEligibleRouteError(RuntimeError):
    """Raised when no route survives all hard eligibility gates."""

    def __init__(self, message: str, *, rejections: tuple[dict, ...] = ()):
        super().__init__(message)
        self.rejections = rejections


MAX_REPORTED_REJECTIONS = 20


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
) -> tuple[bool, ResourceSnapshot | None, str | None]:

    if is_blocked_model(route.model_id):
        return False, None, "blocked_model"

    if route.model_id in request.blocked_models:
        return False, None, "blocked_model"

    if route.provider in request.blocked_providers:
        return False, None, "blocked_provider"

    if not capability_eligible(
        route,
        request.capability,
    ):
        return False, None, "capability"

    if not risk_eligible(
        route,
        request.risk_level,
    ):
        return False, None, "risk"

    if not route.approved or not route.enabled:
        return False, None, "not_approved"

    if (
        route.experimental
        and not request.allow_experimental
    ):
        return False, None, "experimental"

    snapshot = _resource_for(
        route.provider,
        resources,
    )

    if not snapshot.healthy:
        return False, None, "unhealthy"

    band = classify_headroom(snapshot)

    if band.value == "exhausted":
        return False, None, "exhausted"
    if capacity_sufficient(snapshot, request.capability, request.risk_level) is False:
        return False, None, "capacity_insufficient"

    return True, snapshot, None


def select_best_route(
    request: RoutingRequest,
    catalog: tuple[ModelRoute, ...] | None = None,
    resources: dict[str, ResourceSnapshot] | None = None,
) -> SelectedRoute:
    """Select the cheapest capable route with defensible capacity.

    Capability/risk/policy, health and known capacity are hard gates.
    Known sufficient quota wins over UNKNOWN; cost ranks within each group.
    """

    _validate_request(request)

    source_catalog = (
        catalog
        if catalog is not None
        else default_catalog()
    )

    resources = resources or {}

    candidates: list[RoutingCandidate] = []
    rejections: list[dict] = []
    for route in source_catalog:
        eligible, snapshot, reject_reason = _eligible(
            route,
            request,
            resources,
        )

        if not eligible:
            rejections.append(
                {
                    "model": route.model_id,
                    "provider": route.provider,
                    "effort": route.effort,
                    "reason": reject_reason,
                }
            )
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
        reported = tuple(rejections[:MAX_REPORTED_REJECTIONS])
        summary = "; ".join(
            f"{item['model']} ({item['provider']}/{item['effort']}): "
            f"{item['reason']}"
            for item in reported
        )
        omitted = len(rejections) - len(reported)
        if omitted:
            summary += f"; {omitted} additional candidate(s) omitted"
        raise NoEligibleRouteError(
            "No eligible route found for "
            f"capability={request.capability!r}, "
            f"risk_level={request.risk_level!r}. "
            f"Candidate rejections: {summary or 'catalog_empty'}.",
            rejections=reported,
        )

    # Known sufficient capacity outranks UNKNOWN; within either group,
    # cost is the first preference after all eligibility gates.
    cost_order = {"free": 0, "low_cost": 1, "standard": 2, "premium": 3}
    candidates.sort(key=lambda candidate: (
        candidate.headroom_band.value == "neutral",
        cost_order.get(cost_tier(candidate.route), 4),
        *sort_key(candidate.route, candidate.score),
    ))

    winner = candidates[0]
    fallbacks = tuple(candidates[1:])

    reason = winner.reason
    cost_order = {"free": 0, "low_cost": 1, "standard": 2, "premium": 3}
    known_costs = {
        cost_tier(candidate.route)
        for candidate in candidates
        if cost_tier(candidate.route) in cost_order
    }
    if known_costs and cost_tier(winner.route) == min(
        known_costs, key=cost_order.__getitem__
    ):
        reason = f"CHEAPEST_CAPABLE: {reason}"

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
