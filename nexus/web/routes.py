"""JSON API routes for the Nexus Local Mission Board UI V1."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from nexus.agents.policy import ExecutionPolicyError
from nexus.tasks.registry import TaskNotFoundError
from nexus.web import execution, services


router = APIRouter(prefix="/api")


@router.get("/missions")
def missions():
    """Return all missions with their per-column board breakdown."""
    return {"missions": services.get_missions()}


@router.get("/tasks")
def tasks():
    """Return all tasks with their board column and attempt data."""
    return {"tasks": services.get_tasks()}


@router.get("/agents")
def agents():
    """Return all registered agents with their execution sessions."""
    return {"agents": services.get_agents()}


@router.get("/sessions")
def sessions():
    """Return all execution sessions enriched with agent/task labels."""
    return {"sessions": services.get_sessions()}


@router.get("/board")
def board():
    """Return the aggregated kanban board grouped into UI columns."""
    return services.get_board()


@router.get("/summary")
def summary():
    """Return aggregate counters for the dashboard header."""
    return services.get_summary()


@router.post("/tasks/{task_id}/execute")
def execute_task(task_id: str):
    """Execute a task through the Nexus agent execution pipeline.

    Loads the task, selects an agent via the capability router, opens a
    workspace session, and advances the lifecycle CREATED -> RUNNING ->
    COMPLETED. Returns an execution summary.
    """
    try:
        summary = execution.execute_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    except ExecutionPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"execution": summary}


class MissionCreateRequest(BaseModel):
    """Request body for creating a mission through the UI."""

    title: str
    description: str = ""


@router.post("/missions")
def create_mission(request: MissionCreateRequest):
    """Create an in-memory Mission through the Mission Engine.

    The mission lives only in memory and is not persisted. Returns the
    created mission with its board breakdown.
    """
    title = request.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="title must not be empty")

    description = request.description.strip() or None
    mission = services.create_mission(title=title, description=description)
    return {"mission": services._to_dict(mission)}


@router.post("/demo/mission")
def create_demo_mission():
    """Create a deterministic demo mission through the Mission Engine.

    Temporarily used to exercise the whole mission -> task -> board flow from
    the UI. The mission is held only in memory and is not persisted.
    """
    mission = services.create_demo_mission(run_id="RUN-DEMO")
    return {"mission": services._to_dict(mission), "tasks": services.get_tasks()}
