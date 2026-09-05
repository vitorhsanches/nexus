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

    plan_project_id = plan.get("project_id")
    plan_project_id = plan_project_id if isinstance(plan_project_id, str) and plan_project_id else None

    plan_execution_path = plan.get("execution_path")
    plan_execution_path = (
        plan_execution_path if isinstance(plan_execution_path, str) and plan_execution_path else None
    )

    mission = Mission(
        mission_id=plan.get("mission_id") or _new_mission_id(),
        run_id=run_id,
        title=title,
        description=description,
        status="CREATED",
        tasks=[],
        project_id=plan_project_id,
        execution_path=plan_execution_path,
    )

    workers = plan.get("workers") or []
    previous_task_id = None
    for index, worker in enumerate(workers, start=1):
        if not isinstance(worker, dict):
            continue
        dependencies = [previous_task_id] if previous_task_id is not None else []
        task = registry.create_task(
            mission_id=mission.mission_id,
            title=str(worker.get("scope") or f"Worker {index}"),
            description=str(worker.get("reason") or "Generated from plan."),
            status="CREATED",
            priority=str(worker.get("priority") or "MEDIUM"),
            dependencies=dependencies,
            execution_policy=_execution_policy(
                worker, plan_project_id, plan_execution_path
            ),
        )
        mission.tasks.append(task)
        previous_task_id = task.task_id

    return mission


def _execution_policy(worker: dict, plan_project_id=None, plan_execution_path=None) -> dict:
    """Build an execution policy from a worker while preserving provider/model.

    Plan-level ``project_id``/``execution_path`` are inherited into every
    generated task unless the worker explicitly overrides them.
    """
    policy = {}

    for key in ("provider", "model", "effort", "route_class", "execution_path"):
        value = worker.get(key)
        if isinstance(value, str) and value:
            policy[key] = value

    required = worker.get("required_capabilities")
    if isinstance(required, (list, tuple)) and required:
        policy["required_capabilities"] = list(required)

    worker_project_id = worker.get("project_id")
    if isinstance(worker_project_id, str) and worker_project_id:
        policy["project_id"] = worker_project_id
    elif plan_project_id:
        policy["project_id"] = plan_project_id

    if "execution_path" not in policy and plan_execution_path:
        policy["execution_path"] = plan_execution_path

    return policy
