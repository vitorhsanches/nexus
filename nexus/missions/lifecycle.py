"""Explicit Mission status lifecycle for Nexus Mission Execution Orchestrator V1."""

from nexus.missions.models import MISSION_STATUSES

TRANSITIONS = {
    "CREATED": {"PLANNING", "READY", "FAILED"},
    "PLANNING": {"READY", "FAILED"},
    "READY": {"RUNNING", "FAILED"},
    "RUNNING": {"COMPLETED", "FAILED"},
    "COMPLETED": set(),
    "FAILED": set(),
}


class InvalidMissionTransitionError(ValueError):
    """Raised when a Mission status transition is not allowed."""


def validate_status(status):
    if status not in MISSION_STATUSES:
        raise InvalidMissionTransitionError(f"Unknown mission status: {status!r}")


def can_transition(from_status, to_status):
    validate_status(from_status)
    validate_status(to_status)
    if from_status == to_status:
        return True
    return to_status in TRANSITIONS[from_status]


def transition(from_status, to_status):
    """Return to_status if allowed (including a same-state no-op).

    Raises InvalidMissionTransitionError otherwise.
    """
    if not can_transition(from_status, to_status):
        allowed = sorted(TRANSITIONS[from_status]) or "none"
        raise InvalidMissionTransitionError(
            f"Invalid mission status transition: {from_status!r} -> {to_status!r} "
            f"(allowed: {allowed})"
        )
    return to_status
