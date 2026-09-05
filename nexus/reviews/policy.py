"""Same-tier retry / escalation policy for Nexus Real Review Gate V1.

Pure/testable decision layer over a ReviewDecision plus the current Attempt
count for a Task. Reuses the approved route ladder and MAX_SAME_TIER_RETRIES
bound already proven by the legacy progressive orchestrator
(nexus.policies.escalation, nexus.orchestration.progressive) instead of
inventing a second retry budget or a second ladder."""

from dataclasses import dataclass
from typing import Optional

from nexus.policies.escalation import (
    MAX_SAME_TIER_RETRIES,
    EscalationUnavailable,
    next_route,
)


@dataclass(slots=True, frozen=True)
class NextAction:
    """What the reviewed execution service should do after a verdict."""

    action: str  # "COMPLETE" | "RETRY_SAME_TIER" | "ESCALATE" | "STOP"
    route: Optional[dict] = None
    reason: Optional[str] = None


def decide_next_action(decision, current_route, same_tier_retries):
    """Return the NextAction implied by a ReviewDecision.

    ``current_route`` is the route dict actually used for the Attempt being
    reviewed (must carry ``route_class`` and the fields consumed by
    ``nexus.policies.escalation.next_route``). ``same_tier_retries`` is how
    many same-tier retries have already been consumed for the *current*
    route tier.

    PASS completes immediately. BLOCKED stops immediately. RETRY consumes
    the same-tier retry budget (MAX_SAME_TIER_RETRIES) before requesting
    escalation. ESCALATE always requests escalation immediately. When
    escalation is requested but no approved stronger route exists, the
    action fails closed to STOP with an explicit reason -- this policy
    never invents an unapproved model.
    """
    verdict = decision.verdict

    if verdict == "PASS":
        return NextAction(action="COMPLETE")

    if verdict == "BLOCKED":
        return NextAction(action="STOP", reason="BLOCKED")

    if verdict == "RETRY" and same_tier_retries < MAX_SAME_TIER_RETRIES:
        return NextAction(action="RETRY_SAME_TIER", route=current_route)

    if verdict in ("RETRY", "ESCALATE"):
        try:
            escalated = next_route(current_route)
        except EscalationUnavailable as error:
            return NextAction(
                action="STOP",
                reason=f"ESCALATION_UNAVAILABLE: {error}",
            )
        return NextAction(action="ESCALATE", route=escalated)

    return NextAction(action="STOP", reason=f"UNKNOWN_VERDICT: {verdict!r}")
