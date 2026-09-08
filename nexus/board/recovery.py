"""Deterministic Board rehydration from durable Mission/Task/Attempt state.

Recovery is idempotent, does not invoke Manager/Worker/Reviewer/OmniRoute/resume_run/network,
and does not fabricate historical BoardEvent/TaskAssignment entries.
It rebuilds current Board columns from CURRENT Task.status.
"""
from nexus.board.models import BoardColumn
import nexus.missions.service as mission_service
import nexus.tasks.registry as task_registry
import nexus.board.service as board_service

_STATUS_TO_COLUMN = {
    "CREATED": BoardColumn.TODO,
    "READY": BoardColumn.TODO,
    "CLAIMED": BoardColumn.TODO,
    "RUNNING": BoardColumn.IN_PROGRESS,
    "REVIEW": BoardColumn.REVIEW,
    "COMPLETED": BoardColumn.DONE,
    "FAILED": BoardColumn.TODO,
}

def _column_for(status: str) -> BoardColumn:
    if status not in _STATUS_TO_COLUMN:
        raise ValueError(f"Corrupt task status for board mapping: {status!r}")
    return _STATUS_TO_COLUMN[status]

def rehydrate_missions():
    from nexus.missions.persistence import load_missions
    missions = load_missions()
    for m in missions:
        if m.mission_id not in mission_service._missions:
            mission_service._missions[m.mission_id] = m
    # Rebuild mission.tasks deterministically from recovered tasks ordered by task_id
    for m in mission_service._missions.values():
        tasks_for_mission = [t for t in task_registry._tasks.values() if t.mission_id == m.mission_id]
        # deterministic order by task_id
        tasks_for_mission.sort(key=lambda t: t.task_id)
        # ensure no duplicates and mission.tasks reconstructed
        seen = set()
        deduped = []
        for t in tasks_for_mission:
            if t.task_id not in seen:
                seen.add(t.task_id)
                deduped.append(t)
        m.tasks = deduped
    # Also handle missions just loaded that had tasks not yet linked (tasks loaded later)
    # So we also link tasks that will be loaded after; caller must ensure tasks rehydrated first or call again.
    return missions

def rehydrate_tasks_and_attempts():
    from nexus.tasks.persistence import load_tasks, load_attempts
    tasks = load_tasks()
    # Validate referential integrity: every Task must reference existing Mission
    mission_ids = set(mission_service._missions.keys())
    # Also include missions that exist durably but not yet in memory? They should be in memory after rehydrate_missions
    for t in tasks:
        if t.mission_id not in mission_ids:
            raise ValueError(f"Orphan task {t.task_id!r} references missing mission {t.mission_id!r}")
    for t in tasks:
        if t.task_id not in task_registry._tasks:
            task_registry._tasks[t.task_id] = t
    # Rebuild mission.tasks after tasks loaded
    for m in mission_service._missions.values():
        tasks_for_mission = [t for t in task_registry._tasks.values() if t.mission_id == m.mission_id]
        tasks_for_mission.sort(key=lambda t: t.task_id)
        seen = set()
        deduped = []
        for t in tasks_for_mission:
            if t.task_id not in seen:
                seen.add(t.task_id)
                deduped.append(t)
        m.tasks = deduped
    attempts = load_attempts()
    # Validate orphan attempts
    task_ids = set(task_registry._tasks.keys())
    for a in attempts:
        if a.task_id not in task_ids:
            raise ValueError(f"Orphan attempt {a.attempt_id!r} references missing task {a.task_id!r}")
    grouped={}
    for a in attempts:
        grouped.setdefault(a.task_id, []).append(a)
    for tid, lst in grouped.items():
        if tid not in task_registry._attempts:
            task_registry._attempts[tid] = lst
        else:
            existing_ids = {x.attempt_id for x in task_registry._attempts[tid]}
            for a in lst:
                if a.attempt_id not in existing_ids:
                    task_registry._attempts[tid].append(a)
    for a in attempts:
        try:
            seq = int(a.attempt_id.rsplit("-",1)[-1])
        except Exception:
            raise ValueError(f"Corrupt attempt_id format: {a.attempt_id!r}")
        cur = task_registry._next_attempt.get(a.task_id, 0)
        if seq > cur:
            task_registry._next_attempt[a.task_id] = seq
    return tasks, attempts

def rebuild_boards():
    from nexus.board.models import Board, _now
    board_service._boards.clear()
    for mission in mission_service._missions.values():
        board = Board(mission_id=mission.mission_id, board_id="BOARD-" + mission.mission_id, created_at=_now())
        board_service._boards[mission.mission_id] = board
        for task in sorted([t for t in task_registry._tasks.values() if t.mission_id == mission.mission_id], key=lambda t: t.task_id):
            col = _column_for(task.status)
            board.columns[col].append(task.task_id)
    return board_service._boards

def recover_all():
    rehydrate_missions()
    rehydrate_tasks_and_attempts()
    rebuild_boards()
    return True

def clear_memory():
    mission_service._missions.clear()
    task_registry._tasks.clear()
    task_registry._attempts.clear()
    task_registry._next_attempt.clear()
    board_service._boards.clear()
