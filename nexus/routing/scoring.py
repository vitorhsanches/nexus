"""Deterministic scoring for the Adaptive Capability Router Core.

Hard gates are evaluated before scoring.

Ranking priorities after eligibility:

    1. resource band
    2. exact known/override headroom inside the band
    3. cost/free preference
    4. quality tier
    5. deterministic model-id tie breaker
"""

from __future__ import annotations

from nexus.routing.models import (
    HeadroomBand,
    ModelRoute,
    ResourceSnapshot,
)


_BAND_SCORE = {
    HeadroomBand.PREFERRED: 50,
    HeadroomBand.NORMAL: 40,
    HeadroomBand.NEUTRAL: 30,
    HeadroomBand.CONSERVE: 20,
    HeadroomBand.RESERVE: 10,
    HeadroomBand.EXHAUSTED: 0,
}

_COST_SCORE = {
    "free": 3,
    "standard": 2,
    "premium": 1,
}

_QUALITY_SCORE = {
    "high": 3,
    "standard": 2,
    "basic": 1,
}


def band_score(
    band: HeadroomBand,
) -> int:
    return _BAND_SCORE[band]


def cost_score(
    route: ModelRoute,
) -> int:
    return _COST_SCORE.get(
        route.cost_class or "",
        0,
    )


def quality_score(
    route: ModelRoute,
) -> int:
    return _QUALITY_SCORE.get(
        route.quality_tier or "",
        0,
    )


def exact_headroom_score(
    snapshot: ResourceSnapshot,
) -> float:
    """Return exact pct only when headroom is genuinely known/overridden."""

    if not snapshot.is_known:
        return 0.0

    if snapshot.headroom_pct is None:
        return 0.0

    return float(snapshot.headroom_pct)


def score_route(
    route: ModelRoute,
    band: HeadroomBand,
    snapshot: ResourceSnapshot,
) -> float:
    """Compute deterministic composite score.

    Band separation dominates all secondary factors.

    Exact headroom is then used inside the band, which guarantees:

        80% > 51%
        51% > 50%

    when routes are otherwise equivalent.
    """

    return (
        band_score(band) * 100_000
        + exact_headroom_score(snapshot) * 100
        + cost_score(route) * 10
        + quality_score(route)
    )


_EFFORT_RANK = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "xhigh": 3,
    "max": 4,
    "ultra": 5,
}


def effort_rank(
    route: ModelRoute,
) -> int:
    """Prefer the lowest reasoning effort when scored routes are equivalent.

    Capability/risk and resource scoring remain dominant. Effort is only a
    deterministic tie-breaker so Nexus avoids spending more reasoning budget
    than necessary.
    """

    return _EFFORT_RANK.get(
        (route.effort or "").strip().lower(),
        99,
    )


def sort_key(
    route: ModelRoute,
    score: float,
):
    return (
        -score,
        effort_rank(route),
        route.model_id,
    )


def explain(
    route: ModelRoute,
    capability: str,
    risk_level: str,
    band: HeadroomBand,
    snapshot: ResourceSnapshot,
) -> str:
    if snapshot.state.value == "unknown":
        headroom_desc = "unknown resource headroom"

    elif band == HeadroomBand.EXHAUSTED:
        headroom_desc = "exhausted resource headroom"

    else:
        pct = snapshot.headroom_pct
        headroom_desc = (
            f"{band.value} resource headroom "
            f"({pct:.0f}%)"
        )

    return (
        f"{route.model_id} ({route.provider}) satisfied "
        f"'{capability}' capability at '{risk_level}' risk "
        f"with {headroom_desc}."
    )
