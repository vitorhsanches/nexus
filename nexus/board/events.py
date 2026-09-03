"""Event model for Nexus Mission Board Core V1."""

from dataclasses import dataclass, field
from datetime import datetime, timezone


TASK_CREATED = "TASK_CREATED"
TASK_ASSIGNED = "TASK_ASSIGNED"
TASK_STARTED = "TASK_STARTED"
TASK_COMPLETED = "TASK_COMPLETED"
TASK_FAILED = "TASK_FAILED"

TASK_EVENT_TYPES = {
    TASK_CREATED,
    TASK_ASSIGNED,
    TASK_STARTED,
    TASK_COMPLETED,
    TASK_FAILED,
}


@dataclass(slots=True)
class BoardEvent:
    """A single lifecycle event recorded on a mission board."""

    event_type: str
    task_id: str
    mission_id: str
    timestamp: str | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.event_type not in TASK_EVENT_TYPES:
            raise ValueError(f"Unknown board event type: {self.event_type!r}")
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc).isoformat()
