"""Approved model catalog for the Adaptive Capability Router Core.

Runtime discovery and Nexus production approval are deliberately separate.

A model appearing in OmniRoute does NOT become production eligible merely by
existing in the runtime model catalog.
"""

from __future__ import annotations

import re

from nexus.routing.models import ModelRoute


def _normalized_model_identity(model_id: str) -> str:
    """Normalize aliases for global deny-list checks.

    Examples all normalize to a comparable form:

        gpt-5.6-terra
        GPT_5_6_TERRA
        gpt/5/6/terra
        gpt.5.6.terra

    -> gpt56terra
    """

    return re.sub(
        r"[^a-z0-9]+",
        "",
        (model_id or "").lower(),
    )


def is_blocked_model(model_id: str) -> bool:
    """Return True for globally prohibited models/aliases."""

    normalized = _normalized_model_identity(model_id)

    # Global invariant: GPT-5.6 Terra is never an eligible Nexus route.
    return "gpt56terra" in normalized


APPROVED_CATALOG: tuple[ModelRoute, ...] = (
    ModelRoute(
        model_id="oc/big-pickle",

        # Resource/provider identity comes from OmniRoute's actual catalog:
        # owned_by = opencode.
        provider="opencode",

        # The route is still executed through OmniRoute infrastructure.
        execution_path="OMNIROUTE",
        effort="low",

        capabilities=frozenset(
            {
                "mechanical",
            }
        ),

        max_risk_level="low",

        approved=True,
        enabled=True,
        experimental=False,

        cost_class="free",
        quality_tier="basic",
    ),

    ModelRoute(
        model_id="cc/claude-sonnet-5-low",
        provider="claude",
        execution_path="OMNIROUTE",
        effort="low",

        capabilities=frozenset(
            {
                "mechanical",
                "standard-coding",
                "advanced-coding",
                "review",
                "planning",
            }
        ),

        # High/critical risk is deliberately NOT silently approved yet.
        max_risk_level="medium",

        approved=True,
        enabled=True,
        experimental=False,

        cost_class="standard",
        quality_tier="high",
    ),

    # Explicitly qualified for HIGH-risk Manager Review on 2026-09-06.
    #
    # Qualification was performed against the real v2.0-E Worker diff using
    # the exact route below through OmniRoute. The semantic reviewer returned
    # PASS with concrete architecture/lifecycle evidence and the target
    # worktree remained unchanged.
    #
    # Approval is intentionally REVIEW-only and capped at HIGH.
    # CRITICAL remains fail-closed.
    ModelRoute(
        model_id="cc/claude-sonnet-5-high",
        provider="claude",
        execution_path="OMNIROUTE",
        effort="high",

        capabilities=frozenset(
            {
                "review",
            }
        ),

        max_risk_level="high",

        approved=True,
        enabled=True,
        experimental=False,

        cost_class="standard",
        quality_tier="high",
    ),
)


def default_catalog() -> tuple[ModelRoute, ...]:
    return APPROVED_CATALOG


def production_eligible(
    catalog: tuple[ModelRoute, ...],
    allow_experimental: bool = False,
) -> tuple[ModelRoute, ...]:
    """Filter catalog to explicitly approved selectable routes."""

    result = []

    for route in catalog:
        if is_blocked_model(route.model_id):
            continue

        if not route.approved or not route.enabled:
            continue

        if route.experimental and not allow_experimental:
            continue

        result.append(route)

    return tuple(result)
