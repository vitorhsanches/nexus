"""JSON API routes for the Nexus Local Mission Board UI V1."""

from fastapi import APIRouter

from nexus.web import services


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