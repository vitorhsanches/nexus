"""Deterministic agent state transitions for the Nexus Agent Execution Loop V1."""

from nexus.agents.models import AGENT_STATUSES

# Valid next states for each current agent state.
TRANSITIONS = {
    "AVAILABLE": {"BUSY"},
    "BUSY": {"AVAILABLE", "FAILED"},
    "FAILED": {"OFFLINE"},
    "OFFLINE": set(),
}


class InvalidTransitionError(ValueError):
    """Raised when an agent status transition is not allowed."""


def validate_status(status):
    if status not in AGENT_STATUSES:
        raise InvalidTransitionError(f"Unknown agent status: {status!r}")


def can_transition(from_status, to_status):
    validate_status(from_status)
    validate_status(to_status)
    return to_status in TRANSITIONS[from_status]


def transition(from_status, to_status):
    """Return to_status if allowed, otherwise raise InvalidTransitionError."""
    if not can_transition(from_status, to_status):
        allowed = sorted(TRANSITIONS[from_status]) or "none"
        raise InvalidTransitionError(
            f"Invalid agent status transition: {from_status!r} -> {to_status!r} "
            f"(allowed: {allowed})"
        )
    return to_status
