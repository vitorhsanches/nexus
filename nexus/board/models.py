"""Board domain models for Nexus Mission Board Core V1.

The board is an internal functional layer (not UI) that presents a
task-board view inspired by tools such as Overclick. It is a read-focused
visualization over the real state maintained by the Task Registry; the source
of truth for task lifecycle remains the Task Registry.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class BoardColumn(Enum):
    """Columns available on a Nexus mission board."""

    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    REVIEW = "REVIEW"
    DONE = "DONE"


@dataclass(slots=True)
class TaskAssignment:
    """A record of an agent being assigned to a task on the board."""

    task_id: str
    agent: str
    column: BoardColumn = BoardColumn.TODO
    assigned_at: str | None = None


@dataclass(slots=True)
class Board:
    """Aggregate board state for a single mission.

    ``columns`` maps each BoardColumn to the ordered list of task ids
    currently placed in it. ``assignments`` and ``events`` are append-only
    ledgers capturing the mission's board history.
    """

    mission_id: str
    board_id: str | None = None
    columns: dict = field(
        default_factory=lambda: {column: [] for column in BoardColumn}
    )
    assignments: list = field(default_factory=list)
    events: list = field(default_factory=list)
    created_at: str | None = None


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()
