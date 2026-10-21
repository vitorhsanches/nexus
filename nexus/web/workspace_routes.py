"""HTTP surface of the multi-pane workspace (P1).

Thin layer over nexus.workspace.service; errors reuse the Development
error shape ({"detail": {"code", "message"}}). Cross-origin protection is
applied in nexus.web.app's middleware for the whole /api/workspace/ prefix.
There is intentionally no route that applies, merges, pushes or deletes.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/api/workspace")


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=128)
    task: str = Field(min_length=1, max_length=20_000)


class PaneCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=128)
    role: str = Field(min_length=1, max_length=32)
    title: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    plan_id: str | None = Field(default=None, min_length=1, max_length=64)


class PaneLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: int | None = Field(default=None, ge=0, le=10_000)
    collapsed: bool | None = None
    expanded: bool | None = None
    hidden: bool | None = None


class PaneUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    layout: PaneLayout | None = None


class PaneMissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=1, max_length=100_000)
    confirm_model: str = Field(min_length=1, max_length=128)
    test_profile: str = Field(min_length=1, max_length=128)
    timeout_seconds: int = Field(default=900, ge=5, le=3600)


def _error(error) -> HTTPException:
    return HTTPException(status_code=error.http_status, detail={"code": error.code, "message": str(error)})


def _call(function, *args, **kwargs):
    from nexus.dev.service import DevServiceError

    try:
        return function(*args, **kwargs)
    except DevServiceError as error:
        raise _error(error) from error


@router.get("/state")
def workspace_state(project_id: str):
    from nexus.workspace import service

    return _call(service.workspace_state, project_id)


@router.post("/plans")
def create_plan(request: PlanRequest):
    from nexus.workspace import service

    return _call(service.plan_task, request.project_id, request.task)


@router.get("/plans")
def list_plans(project_id: str):
    from nexus.workspace import service

    return {"plans": _call(service.list_plans, project_id)}


@router.post("/panes")
def create_pane(request: PaneCreateRequest):
    from nexus.workspace import service

    return _call(
        service.create_pane,
        request.project_id,
        role=request.role,
        title=request.title,
        model=request.model,
        plan_id=request.plan_id,
    )


@router.get("/panes")
def list_panes(project_id: str, include_hidden: bool = False):
    from nexus.workspace import service

    return {"panes": _call(service.list_panes, project_id, include_hidden=include_hidden)}


@router.get("/panes/{pane_id}")
def get_pane(pane_id: str):
    from nexus.workspace import service

    return _call(service.get_pane, pane_id)


@router.patch("/panes/{pane_id}")
def update_pane(pane_id: str, request: PaneUpdateRequest):
    from nexus.workspace import service

    layout = request.layout.model_dump(exclude_none=True) if request.layout else None
    return _call(service.update_pane, pane_id, title=request.title, model=request.model, layout=layout)


@router.get("/panes/{pane_id}/history")
def pane_history(pane_id: str):
    from nexus.workspace import service

    return _call(service.pane_history, pane_id)


@router.post("/panes/{pane_id}/missions")
def start_pane_mission(pane_id: str, request: PaneMissionRequest):
    from nexus.workspace import service

    return _call(
        service.start_pane_mission,
        pane_id,
        request.description,
        confirm_model=request.confirm_model,
        test_profile=request.test_profile,
        timeout_seconds=request.timeout_seconds,
    )
