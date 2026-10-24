"""Nexus v2.0-D.2 High-Risk Reviewer Qualification.

This module defines the exact candidate route for the D.2 qualification
effort and the mechanism by which it may be promoted into the production
catalog (``nexus.routing.catalog.APPROVED_CATALOG``).

CANDIDATE_HIGH_REVIEW_ROUTE below is intentionally NOT part of the
production catalog. Runtime discovery of a model in OmniRoute is never
sufficient for production approval (see nexus.routing.catalog module
docstring). A route only becomes production eligible after an explicit,
recorded, passing live semantic qualification attempt (Nexus v2.0-D.2
Phase 5) is reflected by a human/operator explicitly adding an approved
ModelRoute to APPROVED_CATALOG.

Exact identity matters. This module exists to make that identity explicit,
auditable, and impossible to satisfy by accident:

    model_id        = "cc/claude-sonnet-5-high"
    provider        = "claude"
    execution_path  = "OMNIROUTE"
    effort          = "high"
    capability      = "review"
    max_risk_level  = "high"          (never "critical")

No other Claude route, no other Sonnet route, no other effort, and no other
execution path satisfies this qualification. Sonnet-low is unaffected and
remains max_risk_level="medium".
"""

from __future__ import annotations

from nexus.routing.models import ModelRoute, RiskLevel


# The exact route identity under qualification for Nexus v2.0-D.2.
# approved=False until a live semantic qualification attempt (Phase 5)
# genuinely passes AND a human/operator explicitly promotes this route.
CANDIDATE_HIGH_REVIEW_ROUTE = ModelRoute(
    model_id="cc/claude-sonnet-5-high",
    provider="claude",
    execution_path="OMNIROUTE",
    effort="high",
    capabilities=frozenset({"review"}),
    max_risk_level=RiskLevel.HIGH.value,
    approved=False,
    enabled=True,
    experimental=True,
    cost_class="standard",
    quality_tier="high",
)


def is_exact_candidate_identity(
    model_id: str,
    provider: str,
    execution_path: str,
    effort: str,
) -> bool:
    """Return True only for the exact D.2 candidate route identity.

    This is a strict equality check across all four identity fields. It
    deliberately does not perform prefix/alias/family matching: wrong
    model, wrong provider, wrong execution path, or wrong effort must
    never match, even if superficially similar (e.g. Sonnet-low, Opus,
    a discovered-but-different Sonnet-high variant, or a non-OMNIROUTE
    execution path).
    """

    reference = CANDIDATE_HIGH_REVIEW_ROUTE

    return (
        model_id == reference.model_id
        and provider == reference.provider
        and execution_path == reference.execution_path
        and effort == reference.effort
    )


def promoted_route_for_qualified_high_review() -> ModelRoute:
    """Build the production ModelRoute to add to APPROVED_CATALOG.

    Only ever call this after a genuinely passing live semantic
    qualification attempt (Nexus v2.0-D.2 Phase 5) whose evidence has been
    recorded in the overnight report. This function itself performs no
    qualification and grants no approval on its own -- it only defines the
    minimal, exact-identity ModelRoute that a human/operator would add to
    the catalog upon success. Capability is intentionally review-only;
    max_risk_level is intentionally "high", never "critical".
    """

    reference = CANDIDATE_HIGH_REVIEW_ROUTE

    return ModelRoute(
        model_id=reference.model_id,
        provider=reference.provider,
        execution_path=reference.execution_path,
        effort=reference.effort,
        capabilities=reference.capabilities,
        max_risk_level=reference.max_risk_level,
        approved=True,
        enabled=True,
        experimental=False,
        cost_class=reference.cost_class,
        quality_tier=reference.quality_tier,
    )

# ---------------------------------------------------------------------------
# Nexus v2.0-D.3 HIGH advanced-coding qualification
# ---------------------------------------------------------------------------
#
# Independently qualified on 2026-09-06.
#
# Exact identity:
#   model_id        = cc/claude-sonnet-5-high
#   provider        = claude
#   execution_path  = OMNIROUTE
#   effort          = low
#   capability      = advanced-coding
#   max_risk_level  = high
#
# This qualification is independent from the D.2 review+HIGH qualification.
# It grants no review capability and never grants CRITICAL risk.

CANDIDATE_HIGH_CODING_ROUTE = ModelRoute(
    model_id="cc/claude-sonnet-5-high",
    provider="claude",
    execution_path="OMNIROUTE",
    effort="low",
    capabilities=frozenset({"advanced-coding"}),
    max_risk_level=RiskLevel.HIGH.value,
    approved=False,
    enabled=True,
    experimental=True,
    cost_class="standard",
    quality_tier="high",
)


def is_exact_high_coding_candidate_identity(
    model_id: str,
    provider: str,
    execution_path: str,
    effort: str,
) -> bool:
    """Match only the exact D.3 HIGH advanced-coding candidate."""

    reference = CANDIDATE_HIGH_CODING_ROUTE

    return (
        model_id == reference.model_id
        and provider == reference.provider
        and execution_path == reference.execution_path
        and effort == reference.effort
    )


def promoted_route_for_qualified_high_coding() -> ModelRoute:
    """Build the exact production route after successful D.3 qualification."""

    reference = CANDIDATE_HIGH_CODING_ROUTE

    return ModelRoute(
        model_id=reference.model_id,
        provider=reference.provider,
        execution_path=reference.execution_path,
        effort=reference.effort,
        capabilities=reference.capabilities,
        max_risk_level=reference.max_risk_level,
        approved=True,
        enabled=True,
        experimental=False,
        cost_class=reference.cost_class,
        quality_tier=reference.quality_tier,
    )


# ---------------------------------------------------------------------------
# Nexus D4.5 NVIDIA Nemotron advanced-coding qualification
# ---------------------------------------------------------------------------
#
# Qualified on 2026-09-25.
#
# Exact identity:
#   model_id        = nvidia/nemotron-3-super-120b-a12b
#   provider        = nvidia
#   execution_path  = OMNIROUTE
#   effort          = low
#   capability      = advanced-coding
#   max_risk_level  = medium
#
# Qualification evidence (D4.4 framework):
#   exact-identity-check: PASS (returned identity matches requested)
#   host-validation: PASS (valid JSON 200 via OmniRoute /v1/responses)
#   semantic-review: PASS (deterministic sentinel NEXUS_D44_SENTINEL_7F3A)
#
# Provider: NVIDIA direct (not OpenRouter variant). Provides provider
# diversity for the advanced-coding fleet beyond Claude-only.
#
# OpenRouter returns the provider-canonical identity without its routing
# namespace; qualification compares that identity through the generic
# benchmark canonicalization contract.
#
# Cost metadata absent from OmniRoute for the nvidia provider.
# cost_class set to "unknown"; no pricing value invented.

CANDIDATE_NVIDIA_NEMOTRON_CODING_ROUTE = ModelRoute(
    model_id="nvidia/nemotron-3-super-120b-a12b",
    provider="nvidia",
    execution_path="OMNIROUTE",
    effort="low",
    capabilities=frozenset({"advanced-coding"}),
    max_risk_level=RiskLevel.MEDIUM.value,
    approved=False,
    enabled=True,
    experimental=True,
    cost_class="unknown",
    quality_tier="standard",
)


def is_exact_nvidia_nemotron_coding_candidate_identity(
    model_id: str,
    provider: str,
    execution_path: str,
    effort: str,
) -> bool:
    """Match only the exact D4.5 NVIDIA Nemotron advanced-coding candidate."""

    reference = CANDIDATE_NVIDIA_NEMOTRON_CODING_ROUTE

    return (
        model_id == reference.model_id
        and provider == reference.provider
        and execution_path == reference.execution_path
        and effort == reference.effort
    )


def promoted_route_for_qualified_nvidia_nemotron_coding() -> ModelRoute:
    """Build the exact production route after successful D4.5 qualification."""

    reference = CANDIDATE_NVIDIA_NEMOTRON_CODING_ROUTE

    return ModelRoute(
        model_id=reference.model_id,
        provider=reference.provider,
        execution_path=reference.execution_path,
        effort=reference.effort,
        capabilities=reference.capabilities,
        max_risk_level=reference.max_risk_level,
        approved=True,
        enabled=True,
        experimental=False,
        cost_class=reference.cost_class,
        quality_tier=reference.quality_tier,
    )


# ---------------------------------------------------------------------------
# OpenRouter North Mini Code advanced-coding qualification
# ---------------------------------------------------------------------------

CANDIDATE_NORTH_MINI_CODING_ROUTE = ModelRoute(
    model_id="openrouter/cohere/north-mini-code:free",
    provider="openrouter",
    execution_path="OMNIROUTE",
    effort="low",
    capabilities=frozenset({"advanced-coding"}),
    max_risk_level=RiskLevel.MEDIUM.value,
    approved=False,
    enabled=True,
    experimental=True,
    cost_class="free",
    quality_tier="standard",
)


def is_exact_north_mini_coding_candidate_identity(
    model_id: str,
    provider: str,
    execution_path: str,
    effort: str,
) -> bool:
    reference = CANDIDATE_NORTH_MINI_CODING_ROUTE
    return (
        model_id == reference.model_id
        and provider == reference.provider
        and execution_path == reference.execution_path
        and effort == reference.effort
    )


def promoted_route_for_qualified_north_mini_coding() -> ModelRoute:
    reference = CANDIDATE_NORTH_MINI_CODING_ROUTE
    return ModelRoute(
        model_id=reference.model_id,
        provider=reference.provider,
        execution_path=reference.execution_path,
        effort=reference.effort,
        capabilities=reference.capabilities,
        max_risk_level=reference.max_risk_level,
        approved=True,
        enabled=True,
        experimental=False,
        cost_class=reference.cost_class,
        quality_tier=reference.quality_tier,
    )
