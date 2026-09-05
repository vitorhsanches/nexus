"""Service layer for Nexus Mission Engine V1."""

from datetime import datetime, timezone
from uuid import uuid4

from nexus.missions.lifecycle import transition
from nexus.missions.models import Mission, MISSION_STATUSES
from nexus.tasks import registry


class MissionNotFoundError(KeyError):
    pass


def _new_mission_id() -> str:
    return f"MISSION-{uuid4().hex[:8].upper()}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_mission(
    run_id: str,
    title: str,
    description: str | None = None,
    status: str = "CREATED",
    project_id: str | None = None,
    execution_path: str | None = None,
) -> Mission:
    """Create an empty Mission with no tasks."""
    if status not in MISSION_STATUSES:
        raise ValueError(f"Invalid mission status: {status!r}")

    mission = Mission(
        mission_id=_new_mission_id(),
        run_id=run_id,
        title=title,
        description=description,
        status=status,
        tasks=[],
        created_at=_now(),
        project_id=project_id,
        execution_path=execution_path,
    )
    _missions[mission.mission_id] = mission
    return mission


def create_mission_from_plan(plan: dict, run_id: str) -> Mission:
    """Create a Mission and materialize its tasks from a Manager plan.

    Uses generate_mission_from_plan so worker-derived tasks are created
    through the existing Task Registry.
    """
    from nexus.missions.generator import generate_mission_from_plan

    mission = generate_mission_from_plan(plan, run_id=run_id)
    mission.created_at = _now()
    _missions[mission.mission_id] = mission
    return mission


def get_mission(mission_id: str) -> Mission:
    try:
        return _missions[mission_id]
    except KeyError:
        raise MissionNotFoundError(mission_id)


def list_missions() -> list:
    return list(_missions.values())


def update_mission_status(mission_id, status):
    mission = get_mission(mission_id)
    mission.status = transition(mission.status, status)
    return mission


# Internal in-memory store.
_missions = {}
