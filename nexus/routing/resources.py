"""Provider resource/headroom policy.

Bands:

    >= 50%   PREFERRED
    20-49%   NORMAL
    10-19%   CONSERVE
    < 10%    RESERVE
    0%       EXHAUSTED
    UNKNOWN  NEUTRAL

The percentage inside a band remains available to scoring so 80% can outrank
51% when all hard gates and secondary attributes are equivalent.
"""

from __future__ import annotations

import math
from numbers import Real

from nexus.routing.models import (
    HeadroomBand,
    QuotaState,
    ResourceSnapshot,
)


def classify_headroom(
    snapshot: ResourceSnapshot,
) -> HeadroomBand:
    if snapshot.state == QuotaState.EXHAUSTED:
        return HeadroomBand.EXHAUSTED

    if snapshot.state == QuotaState.UNKNOWN:
        return HeadroomBand.NEUTRAL

    pct = snapshot.headroom_pct

    # Model validation guarantees KNOWN/OVERRIDE values are valid.
    if pct is None:
        return HeadroomBand.NEUTRAL

    if pct <= 0:
        return HeadroomBand.EXHAUSTED

    if pct < 10:
        return HeadroomBand.RESERVE

    if pct < 20:
        return HeadroomBand.CONSERVE

    if pct < 50:
        return HeadroomBand.NORMAL

    return HeadroomBand.PREFERRED


# Conservative percentage floors plus a 5-point safety reserve. These are
# policy units, not token predictions; UNKNOWN remains usable only as fallback.
_DEMAND_FLOOR = {
    "mechanical": 5.0,
    "standard-coding": 15.0,
    "advanced-coding": 30.0,
    "review": 10.0,
    "planning": 10.0,
    "high-risk": 30.0,
}
SAFETY_RESERVE_PCT = 5.0


def estimated_demand_pct(capability: str, risk_level: str) -> float:
    """Deterministic SMALL/MEDIUM/LARGE demand proxy from existing routing facts."""
    floor = _DEMAND_FLOOR.get(capability, 15.0)
    if risk_level in {"high", "critical"}:
        floor = max(floor, 30.0)
    return floor


def capacity_sufficient(snapshot: ResourceSnapshot, capability: str, risk_level: str) -> bool | None:
    """True/False for known quota, None when no reliable total exists."""
    if snapshot.state == QuotaState.EXHAUSTED:
        return False
    if not snapshot.is_known:
        return None
    return float(snapshot.headroom_pct) >= estimated_demand_pct(capability, risk_level) + SAFETY_RESERVE_PCT

def _validate_override_value(
    provider: str,
    value,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 100
    ):
        raise ValueError(
            f"Invalid provider headroom override for "
            f"{provider!r}: {value!r}. "
            "Expected a finite percentage from 0 to 100."
        )

    return float(value)


class ProviderOverrides:
    """Injectable provider resource headroom overrides.

    Allows Nexus to temporarily know e.g. provider A = 80% while provider B
    = 3% without embedding account-specific percentages into source code.
    """

    def __init__(
        self,
        values: dict[str, float] | None = None,
    ):
        self._values: dict[str, float] = {}

        for provider, value in (values or {}).items():
            self._values[provider] = _validate_override_value(
                provider,
                value,
            )

    def get(
        self,
        provider: str,
    ) -> float | None:
        return self._values.get(provider)

    def as_snapshot(
        self,
        provider: str,
        healthy: bool = True,
    ) -> ResourceSnapshot:
        pct = self._values.get(provider)

        if pct is None:
            return ResourceSnapshot(
                provider=provider,
                state=QuotaState.UNKNOWN,
                headroom_pct=None,
                healthy=healthy,
            )

        if pct <= 0:
            return ResourceSnapshot(
                provider=provider,
                state=QuotaState.EXHAUSTED,
                headroom_pct=0.0,
                healthy=healthy,
            )

        return ResourceSnapshot(
            provider=provider,
            state=QuotaState.OVERRIDE,
            headroom_pct=pct,
            healthy=healthy,
        )


def snapshot_from_quota_response(
    provider: str,
    quota_total: float | None,
    quota_used: float | None,
    healthy: bool = True,
) -> ResourceSnapshot:
    """Convert raw quota information into a fail-safe snapshot.

    OmniRoute responses shaped like:

        quotaTotal = null
        quotaUsed = 0
        percentRemaining = 100

    do NOT establish real known 100% headroom.

    Without a meaningful total, state remains UNKNOWN.
    """

    if quota_total is None or quota_used is None:
        return ResourceSnapshot(
            provider=provider,
            state=QuotaState.UNKNOWN,
            headroom_pct=None,
            healthy=healthy,
        )

    if (
        isinstance(quota_total, bool)
        or isinstance(quota_used, bool)
        or not isinstance(quota_total, Real)
        or not isinstance(quota_used, Real)
        or not math.isfinite(float(quota_total))
        or not math.isfinite(float(quota_used))
    ):
        return ResourceSnapshot(
            provider=provider,
            state=QuotaState.UNKNOWN,
            headroom_pct=None,
            healthy=healthy,
        )

    total = float(quota_total)
    used = float(quota_used)

    if total <= 0:
        return ResourceSnapshot(
            provider=provider,
            state=QuotaState.EXHAUSTED,
            headroom_pct=0.0,
            healthy=healthy,
        )

    if used < 0:
        return ResourceSnapshot(
            provider=provider,
            state=QuotaState.UNKNOWN,
            headroom_pct=None,
            healthy=healthy,
        )

    if used >= total:
        return ResourceSnapshot(
            provider=provider,
            state=QuotaState.EXHAUSTED,
            headroom_pct=0.0,
            healthy=healthy,
        )

    remaining = total - used
    headroom_pct = (remaining / total) * 100.0

    return ResourceSnapshot(
        provider=provider,
        state=QuotaState.KNOWN,
        headroom_pct=headroom_pct,
        healthy=healthy,
    )
