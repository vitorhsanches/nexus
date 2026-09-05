"""Adaptive Capability Router Core (Nexus v2.0-A).

Deterministic, capability-first, resource-aware model selection foundation.

This package is intentionally standalone in this milestone: it is not yet
wired into AgentExecutor, web execution, Mission execution, review execution,
or the legacy Nexus execution path. It makes no network calls and does not
depend on OmniRoute being reachable.
"""

from nexus.routing.models import (
    CapabilityClass,
    HeadroomBand,
    ModelRoute,
    QuotaState,
    ResourceSnapshot,
    RiskLevel,
    RoutingCandidate,
    RoutingRequest,
    SelectedRoute,
)
from nexus.routing.router import (
    InvalidRoutingRequestError,
    NoEligibleRouteError,
    select_best_route,
)

__all__ = [
    "CapabilityClass",
    "HeadroomBand",
    "ModelRoute",
    "QuotaState",
    "ResourceSnapshot",
    "RiskLevel",
    "RoutingCandidate",
    "RoutingRequest",
    "SelectedRoute",
    "InvalidRoutingRequestError",
    "NoEligibleRouteError",
    "select_best_route",
]
