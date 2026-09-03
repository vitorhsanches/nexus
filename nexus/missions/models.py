"""Mission model for Nexus Mission Engine V1."""

from dataclasses import dataclass, field
from typing import Optional


MISSION_STATUSES = {
    "CREATED",
    "PLANNING",
    "READY",
    "RUNNING",
    "COMPLETED",
    "FAILED",
}


@dataclass(slots=True)
class Mission:
    mission_id: str
    run_id: str
    title: str
    description: Optional[str] = None
    status: str = "CREATED"
    tasks: Optional[list] = field(default=None)
    created_at: Optional[str] = None
