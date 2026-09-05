"""Capability and risk hard-gate policies."""

from __future__ import annotations

from nexus.routing.models import (
    CapabilityClass,
    ModelRoute,
    RiskLevel,
)


VALID_CAPABILITIES: frozenset[str] = frozenset(
    member.value for member in CapabilityClass
)

VALID_RISK_LEVELS: frozenset[str] = frozenset(
    member.value for member in RiskLevel
)

_RISK_RANK = {
    RiskLevel.LOW.value: 0,
    RiskLevel.MEDIUM.value: 1,
    RiskLevel.HIGH.value: 2,
    RiskLevel.CRITICAL.value: 3,
}


def is_known_capability(capability: str) -> bool:
    return capability in VALID_CAPABILITIES


def is_known_risk_level(risk_level: str) -> bool:
    return risk_level in VALID_RISK_LEVELS


def capability_eligible(
    route: ModelRoute,
    capability: str,
) -> bool:
    """Hard gate: the route must explicitly declare the capability."""
    return route.supports(capability)


def risk_eligible(
    route: ModelRoute,
    requested_risk: str,
) -> bool:
    """Hard gate: Task risk cannot exceed route approval level."""

    requested_rank = _RISK_RANK.get(requested_risk)
    route_rank = _RISK_RANK.get(route.max_risk_level)

    if requested_rank is None or route_rank is None:
        return False

    return requested_rank <= route_rank
