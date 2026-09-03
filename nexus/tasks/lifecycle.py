"""Deterministic task state transitions for Nexus Task Registry V1."""

from nexus.tasks.models import TASK_STATUSES

# Valid next states for each current task state.
TRANSITIONS = {
    "CREATED": {"READY"},
    "READY": {"CLAIMED"},
    "CLAIMED": {"RUNNING"},
    "RUNNING": {"REVIEW", "FAILED"},
    "REVIEW": {"COMPLETED"},
    "COMPLETED": set(),
    "FAILED": set(),
}


class InvalidTransitionError(ValueError):
    """Raised when a task status transition is not allowed."""


def validate_status(status):
    if status not in TASK_STATUSES:
        raise InvalidTransitionError(f"Unknown task status: {status!r}")


def can_transition(from_status, to_status):
    validate_status(from_status)
    validate_status(to_status)
    return to_status in TRANSITIONS[from_status]


def transition(from_status, to_status):
    """Return to_status if allowed, otherwise raise InvalidTransitionError."""
    if not can_transition(from_status, to_status):
        allowed = sorted(TRANSITIONS[from_status]) or "none"
        raise InvalidTransitionError(
            f"Invalid task status transition: {from_status!r} -> {to_status!r} "
            f"(allowed: {allowed})"
        )
    return to_status
