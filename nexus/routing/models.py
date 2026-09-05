"""Typed domain models for the Adaptive Capability Router Core."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from numbers import Real


class CapabilityClass(str, Enum):
    """Task capability classes supported by the router."""

    MECHANICAL = "mechanical"
    STANDARD_CODING = "standard-coding"
    ADVANCED_CODING = "advanced-coding"
    REVIEW = "review"
    PLANNING = "planning"
    HIGH_RISK = "high-risk"


class RiskLevel(str, Enum):
    """Risk levels understood by the deterministic routing policy."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class QuotaState(str, Enum):
    """Provenance/state of a provider resource reading."""

    KNOWN = "known"
    OVERRIDE = "override"
    UNKNOWN = "unknown"
    EXHAUSTED = "exhausted"


class HeadroomBand(str, Enum):
    """Deterministic resource bands used during ranking."""

    PREFERRED = "preferred"
    NORMAL = "normal"
    CONSERVE = "conserve"
    RESERVE = "reserve"
    EXHAUSTED = "exhausted"
    NEUTRAL = "neutral"


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """A discovered, approved, or experimental execution route.

    IMPORTANT:
    Discovery is NOT approval.

    A newly constructed route defaults to ``approved=False`` so runtime
    OmniRoute discovery can never silently grant production eligibility.
    """

    model_id: str
    provider: str
    execution_path: str
    effort: str = "low"
    capabilities: frozenset[str] = field(default_factory=frozenset)

    # Maximum Task risk this model/route has been explicitly approved to
    # handle. This is a hard gate, not a scoring preference.
    max_risk_level: str = RiskLevel.LOW.value

    approved: bool = False
    enabled: bool = True
    experimental: bool = False

    cost_class: str | None = None
    quality_tier: str | None = None

    def __post_init__(self):
        if not self.model_id or not self.model_id.strip():
            raise ValueError("model_id must be non-empty.")
        if not self.provider or not self.provider.strip():
            raise ValueError("provider must be non-empty.")
        if not self.execution_path or not self.execution_path.strip():
            raise ValueError("execution_path must be non-empty.")

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    @property
    def is_production_eligible(self) -> bool:
        return (
            self.approved
            and self.enabled
            and not self.experimental
        )


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    """A provider's resource/headroom reading at a point in time."""

    provider: str
    state: QuotaState
    headroom_pct: float | None = None
    healthy: bool = True

    def __post_init__(self):
        if not self.provider or not self.provider.strip():
            raise ValueError("provider must be non-empty.")

        if self.state in (
            QuotaState.KNOWN,
            QuotaState.OVERRIDE,
        ):
            if (
                self.headroom_pct is None
                or isinstance(self.headroom_pct, bool)
                or not isinstance(self.headroom_pct, Real)
                or not math.isfinite(float(self.headroom_pct))
                or not 0 <= float(self.headroom_pct) <= 100
            ):
                raise ValueError(
                    "KNOWN/OVERRIDE headroom_pct must be a finite "
                    "number between 0 and 100."
                )

        elif self.state == QuotaState.UNKNOWN:
            if self.headroom_pct is not None:
                raise ValueError(
                    "UNKNOWN resource snapshots cannot carry "
                    "a headroom percentage."
                )

        elif self.state == QuotaState.EXHAUSTED:
            if (
                self.headroom_pct is not None
                and float(self.headroom_pct) != 0.0
            ):
                raise ValueError(
                    "EXHAUSTED resource snapshots may only carry "
                    "0% or None headroom."
                )

    @property
    def is_known(self) -> bool:
        return self.state in (
            QuotaState.KNOWN,
            QuotaState.OVERRIDE,
        )


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    """A request to route a Task to the best eligible model."""

    capability: str
    risk_level: str = RiskLevel.LOW.value
    blocked_models: frozenset[str] = field(default_factory=frozenset)
    blocked_providers: frozenset[str] = field(default_factory=frozenset)
    allow_experimental: bool = False


@dataclass(frozen=True, slots=True)
class RoutingCandidate:
    """A scored candidate produced during route selection."""

    route: ModelRoute
    score: float
    headroom_band: HeadroomBand
    reason: str


@dataclass(frozen=True, slots=True)
class SelectedRoute:
    """Final routing decision plus ordered fallbacks."""

    route: ModelRoute
    score: float
    reason: str
    fallbacks: tuple[RoutingCandidate, ...] = field(default_factory=tuple)
