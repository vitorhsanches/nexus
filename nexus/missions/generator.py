"""Deterministic mission generation from a Manager plan for Mission Engine V1."""

from uuid import uuid4

from nexus.missions.models import Mission
from nexus.tasks import registry


def _new_mission_id() -> str:
    return f"MISSION-{uuid4().hex[:8].upper()}"


def generate_mission_from_plan(plan: dict, run_id: str) -> Mission:
    """Build a Mission from a Manager plan, creating one Task per worker.

    If the plan has no workers, a Mission with no tasks is returned.
    Tasks are created through the existing Task Registry.
    """
    title = plan.get("title")
    if not isinstance(title, str) or not title.strip():
        title = plan.get("summary") or "Nexus Mission"

    description = plan.get("description")
    if not isinstance(description, str):
        description = None

    mission = Mission(
        mission_id=plan.get("mission_id") or _new_mission_id(),
        run_id=run_id,
        title=title,
        description=description,
        status="CREATED",
        tasks=[],
    )

    workers = plan.get("workers") or []
    for index, worker in enumerate(workers, start=1):
        if not isinstance(worker, dict):
            continue
        task = registry.create_task(
            mission_id=mission.mission_id,
            title=str(worker.get("scope") or f"Worker {index}"),
            description=str(worker.get("reason") or "Generated from plan."),
            status="CREATED",
            priority=str(worker.get("priority") or "MEDIUM"),
            execution_policy=_execution_policy(worker),
        )
        mission.tasks.append(task)

    return mission


def _execution_policy(worker: dict) -> dict:
    """Build an execution policy from a worker while preserving provider/model."""
    policy = {}

    for key in ("provider", "model", "effort", "route_class", "execution_path"):
        value = worker.get(key)
        if isinstance(value, str) and value:
            policy[key] = value

    return policy
