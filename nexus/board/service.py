"""Service layer for Nexus Mission Board Core V1.

This is the internal, functional board core only. It keeps an in-memory board
per mission and reuses the existing Mission Engine and Task Registry as the
source of truth for missions and tasks. No database schema or UI is involved.
"""

from nexus.board.events import BoardEvent
from nexus.board.models import Board, BoardColumn, TaskAssignment, _now
from nexus.missions.service import get_mission as _get_mission
from nexus.tasks import registry as task_registry

# Internal in-memory board store, keyed by mission_id.
_boards = {}

# Map existing Task Registry statuses onto board columns.
_STATUS_TO_COLUMN = {
    "CREATED": BoardColumn.TODO,
    "READY": BoardColumn.TODO,
    "CLAIMED": BoardColumn.TODO,
    "RUNNING": BoardColumn.IN_PROGRESS,
    "REVIEW": BoardColumn.REVIEW,
    "COMPLETED": BoardColumn.DONE,
    "FAILED": BoardColumn.TODO,
}


class BoardNotFoundError(KeyError):
    pass


def _column_for_status(status: str) -> BoardColumn:
    return _STATUS_TO_COLUMN.get(status, BoardColumn.TODO)


def _validate_column(column) -> BoardColumn:
    return BoardColumn(column)


def create_board(mission) -> Board:
    """Create a board for a mission, seeding it from existing tasks.

    ``mission`` may be a ``nexus.missions.models.Mission`` instance or a
    mission id string. Tasks registered for the mission are placed into the
    matching board column based on their current Task Registry status.
    """
    mission = _resolve_mission(mission)
    board = Board(
        mission_id=mission.mission_id,
        board_id=f"BOARD-{mission.mission_id}",
        created_at=_now(),
    )

    for task in task_registry.list_tasks():
        if task.mission_id == mission.mission_id:
            column = _column_for_status(task.status)
            board.columns[column].append(task.task_id)

    _boards[mission.mission_id] = board
    return board


def assign_task(task_id, agent) -> TaskAssignment:
    """Assign an agent to a task and record the assignment on its board."""
    task = task_registry.get_task(task_id)
    board = get_board(task.mission_id)

    if task.task_id not in board.columns[BoardColumn.TODO]:
        # Ensure the task card is present on the board before assignment.
        _place(board, task.task_id, BoardColumn.TODO)

    task.assigned_agent = agent
    assignment = TaskAssignment(
        task_id=task.task_id,
        agent=agent,
        column=BoardColumn.TODO,
        assigned_at=_now(),
    )
    board.assignments.append(assignment)

    record_event(
        BoardEvent(
            event_type="TASK_ASSIGNED",
            task_id=task.task_id,
            mission_id=task.mission_id,
            metadata={"agent": agent},
        )
    )
    return assignment


def move_task(task_id, column) -> TaskAssignment:
    """Move a task card to another board column."""
    column = _validate_column(column)
    task = task_registry.get_task(task_id)
    board = get_board(task.mission_id)

    _place(board, task.task_id, column)

    # Keep the assignment view in sync with the latest column.
    assignment = TaskAssignment(
        task_id=task.task_id,
        agent=task.assigned_agent,
        column=column,
        assigned_at=_now(),
    )
    board.assignments.append(assignment)
    return assignment


def record_event(event: BoardEvent) -> BoardEvent:
    """Record a task board event on the owning mission's board ledger."""
    board = get_board(event.mission_id)
    board.events.append(event)
    return event


def get_board(mission_id: str) -> Board:
    """Return the board for a mission id, creating it on demand."""
    try:
        return _boards[mission_id]
    except KeyError:
        raise BoardNotFoundError(mission_id)


def _resolve_mission(mission):
    if isinstance(mission, str):
        return _get_mission(mission)
    return mission


def _place(board: Board, task_id: str, column: BoardColumn) -> None:
    """Move a task id into a column, removing it from any other column first."""
    for other in BoardColumn:
        if task_id in board.columns[other]:
            board.columns[other].remove(task_id)
    if task_id not in board.columns[column]:
        board.columns[column].append(task_id)
