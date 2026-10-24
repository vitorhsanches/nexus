"""Adaptive reasoning effort policy for NEXUS (Phase B).

Implements the MINIMUM ADEQUATE REASONING DEPTH principle: no reasoning
effort is spent beyond what the role, task complexity, and risk warrant.

The four orthogonal input axes are:
    role         : which NEXUS agent role is being routed
    complexity   : LOW / MEDIUM / HIGH -- cognitive/structural task difficulty
    risk         : LOW / MEDIUM / HIGH / CRITICAL -- cost of a wrong output
    failure_class: previous failure class (for retry effort escalation only)

EFFORT MATRIX
=============
MANAGER:
    LOW   complexity -> "low"
    MEDIUM complexity -> "medium"
    HIGH  complexity -> "medium"   (no "high" for planning; reviewers own CRITICAL)

SCOUT:
    LOW   complexity -> "low"
    MEDIUM/HIGH      -> "medium"

WORKER:
    LOW   complexity (any risk)        -> "low"
    MEDIUM complexity + LOW risk       -> "low"
    MEDIUM complexity + MEDIUM/HIGH/CRITICAL risk -> "medium"
    HIGH  complexity (any risk)        -> "medium"

REVIEWER:
    LOW  risk                          -> "low"
    MEDIUM risk                        -> "medium"
    HIGH risk                          -> "medium"
    CRITICAL risk OR security-critical -> "high"

RETRY EFFORT ESCALATION (separate dimension from model escalation)
==========================
    CAPABILITY_FAILURE  -> reasoning failure -> low→medium, medium→high, high→high
    REQUIREMENT_FAILURE -> reasoning failure -> low→medium, medium→high, high→high
    VALIDATION_FAILURE  -> reasoning failure -> low→medium, medium→high, high→high
    PROVIDER_FAILURE    -> non-reasoning     -> effort UNCHANGED
    TRANSIENT           -> non-reasoning     -> effort UNCHANGED
    TOOL_FAILURE        -> non-reasoning     -> effort UNCHANGED
    SCOPE_VIOLATION     -> non-reasoning     -> effort UNCHANGED
    UNKNOWN or absent   -> conservative      -> effort UNCHANGED
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Effort ordering (used for comparison and clamping)
# ---------------------------------------------------------------------------

_EFFORT_RANK: dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
}


# ---------------------------------------------------------------------------
# Reasoning failure classification
# ---------------------------------------------------------------------------

# Failure classes that indicate the model's reasoning depth was insufficient.
# Escalating effort may help the model think more carefully on the next attempt.
REASONING_FAILURE_CLASSES: frozenset[str] = frozenset({
    "CAPABILITY_FAILURE",   # model couldn't perform the required capability
    "REQUIREMENT_FAILURE",  # model failed to meet explicit requirements
    "VALIDATION_FAILURE",   # model's output failed correctness validation
})

# Failure classes caused by infrastructure, tooling, or policy -- NOT by
# insufficient reasoning.  Escalating effort will NOT help; must never be
# elevated on their account.
NON_REASONING_FAILURE_CLASSES: frozenset[str] = frozenset({
    "PROVIDER_FAILURE",  # provider outage / rate limit / infrastructure
    "TRANSIENT",         # transient errors (network, timeout, etc.)
    "TOOL_FAILURE",      # tool invocation failure
    "SCOPE_VIOLATION",   # worker exceeded its authorized scope boundary
    "UNKNOWN",           # unknown failure class -- conservative
})


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EffortDecision:
    """The result of the adaptive reasoning effort policy calculation.

    Immutable, hashable, and serialization-friendly.  The ``reason`` field
    is deterministic structural prose -- never model output, never derived
    from external sources.
    """

    effort: str   # one of "low" | "medium" | "high"
    reason: str   # deterministic human-readable explanation


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_reasoning_failure(failure_class: Optional[str]) -> bool:
    """Return True iff ``failure_class`` indicates insufficient reasoning depth."""
    return failure_class in REASONING_FAILURE_CLASSES


def _escalate_effort(current: str) -> str:
    """Advance effort by one step: low→medium, medium→high, high→high."""
    if current == "low":
        return "medium"
    if current == "medium":
        return "high"
    return "high"  # already at maximum; no further escalation


# ---------------------------------------------------------------------------
# Public policy function
# ---------------------------------------------------------------------------


def select_reasoning_effort(
    *,
    role: str,
    complexity: Optional[str] = None,
    risk: Optional[str] = None,
    route_class: Optional[str] = None,
    has_dependencies: bool = False,
    previous_effort: Optional[str] = None,
    failure_class: Optional[str] = None,
) -> EffortDecision:
    """Select the minimum adequate reasoning effort for a NEXUS agent role.

    Pure and deterministic: no filesystem, database, network, or model
    access of any kind.  Same inputs always produce the same output.

    Parameters
    ----------
    role:
        The NEXUS agent role: ``"MANAGER"``, ``"SCOUT"``, ``"WORKER"``,
        or ``"REVIEWER"``.  Case-insensitive.
    complexity:
        Cognitive/structural task complexity: ``"LOW"``, ``"MEDIUM"``,
        or ``"HIGH"``.  Defaults to ``"LOW"`` when absent.
        For MANAGER: inferred from mission triage scope (BROAD→MEDIUM,
        BOUNDED→LOW) before the plan exists.
        For WORKER: inferred from route_class in the caller.
        For SCOUT: derived from mission scope/evidence complexity.
        For REVIEWER: ignored -- risk is the primary REVIEWER axis.
    risk:
        Output risk level: ``"LOW"``, ``"MEDIUM"``, ``"HIGH"``, or
        ``"CRITICAL"``.  Defaults to ``"LOW"`` when absent.
        Primary axis for REVIEWER; secondary axis for WORKER.
    route_class:
        Worker route class (e.g. ``"security-critical"``).  Used by the
        REVIEWER path to detect security-critical routes which warrant
        ``"high"`` effort regardless of the nominal risk value.
    has_dependencies:
        Whether this WORKER depends on outputs from other workers.
        Currently informational; recorded in the reason string.
    previous_effort:
        The effort level used on the previous attempt (``"low"``,
        ``"medium"``, or ``"high"``).  When provided *together* with
        ``failure_class``, triggers the retry escalation path instead of
        the baseline matrix.
    failure_class:
        The failure class recorded by the previous review.  Only
        ``CAPABILITY_FAILURE``, ``REQUIREMENT_FAILURE``, and
        ``VALIDATION_FAILURE`` (reasoning failures) trigger effort
        escalation.  All other values leave effort unchanged.

    Returns
    -------
    EffortDecision
        Selected effort (``"low"``, ``"medium"``, or ``"high"``) and a
        deterministic reason string.
    """

    role_upper = (role or "").upper()
    complexity_upper = (complexity or "LOW").upper()
    risk_upper = (risk or "LOW").upper()

    # ------------------------------------------------------------------
    # RETRY ESCALATION PATH (takes priority over the baseline matrix)
    # ------------------------------------------------------------------
    # When both previous_effort and failure_class are given, this is a
    # retry/escalation call; apply effort escalation and return immediately.
    if previous_effort is not None and failure_class is not None:
        if _is_reasoning_failure(failure_class):
            escalated = _escalate_effort(previous_effort)
            return EffortDecision(
                effort=escalated,
                reason=(
                    f"Effort escalated {previous_effort!r} → {escalated!r} "
                    f"after reasoning failure ({failure_class}); "
                    "deeper reasoning may address the root cause."
                ),
            )
        # Non-reasoning failure: effort is never elevated
        return EffortDecision(
            effort=previous_effort,
            reason=(
                f"Effort unchanged at {previous_effort!r}: "
                f"{failure_class} is a non-reasoning failure; "
                "escalating effort would not address the root cause."
            ),
        )

    # ------------------------------------------------------------------
    # BASELINE MATRIX (no retry context)
    # ------------------------------------------------------------------

    if role_upper == "MANAGER":
        # Manager effort is driven by mission complexity, inferred from
        # triage scope before the plan exists.  BROAD missions (MEDIUM
        # complexity) require more careful planning decomposition.
        if complexity_upper in ("MEDIUM", "HIGH"):
            return EffortDecision(
                effort="medium",
                reason=(
                    f"Manager: {complexity_upper} complexity mission "
                    "warrants medium effort for careful planning."
                ),
            )
        return EffortDecision(
            effort="low",
            reason="Manager: LOW complexity (BOUNDED scope); low effort is adequate.",
        )

    if role_upper == "SCOUT":
        # Scouts gather repository evidence; default is low effort.
        # MEDIUM/HIGH complexity missions (broad scope) benefit from
        # deeper evidence gathering.
        if complexity_upper in ("MEDIUM", "HIGH"):
            return EffortDecision(
                effort="medium",
                reason=(
                    f"Scout: {complexity_upper} complexity; "
                    "medium effort improves evidence quality."
                ),
            )
        return EffortDecision(
            effort="low",
            reason="Scout: LOW complexity; low effort is adequate for evidence gathering.",
        )

    if role_upper == "WORKER":
        # Worker effort is the intersection of complexity and risk.
        # has_dependencies is noted in the reason string but does not
        # by itself add a full tier (it ensures at least "medium" at HIGH).
        dep_note = " (dependent worker)" if has_dependencies else ""

        if complexity_upper == "LOW":
            return EffortDecision(
                effort="low",
                reason=f"Worker: LOW complexity{dep_note}; low effort is adequate.",
            )

        if complexity_upper == "MEDIUM":
            if risk_upper in ("MEDIUM", "HIGH", "CRITICAL"):
                return EffortDecision(
                    effort="medium",
                    reason=(
                        f"Worker: MEDIUM complexity + {risk_upper} risk"
                        f"{dep_note} warrants medium effort."
                    ),
                )
            # LOW risk, MEDIUM complexity
            return EffortDecision(
                effort="low",
                reason=(
                    f"Worker: MEDIUM complexity + LOW risk{dep_note}; "
                    "low effort is adequate."
                ),
            )

        # HIGH complexity
        return EffortDecision(
            effort="medium",
            reason=(
                f"Worker: HIGH complexity{dep_note} warrants medium effort."
            ),
        )

    if role_upper == "REVIEWER":
        # Reviewer effort is driven primarily by risk.
        # security-critical route_class always warrants high effort,
        # even when the nominal risk field is not CRITICAL.
        if route_class == "security-critical" or risk_upper == "CRITICAL":
            return EffortDecision(
                effort="high",
                reason=(
                    "Reviewer: CRITICAL risk or security-critical route; "
                    "high effort is required."
                ),
            )
        if risk_upper in ("HIGH", "MEDIUM"):
            return EffortDecision(
                effort="medium",
                reason=f"Reviewer: {risk_upper} risk warrants medium effort.",
            )
        # LOW risk
        return EffortDecision(
            effort="low",
            reason="Reviewer: LOW risk; low effort is adequate.",
        )

    # Unknown role -- fail safely with low effort (never raises)
    return EffortDecision(
        effort="low",
        reason=f"Unknown role {role!r}; defaulting to low effort conservatively.",
    )
