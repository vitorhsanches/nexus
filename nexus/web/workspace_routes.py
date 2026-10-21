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
    description: str | None = Field(default=None, max_length=100_000)
    confirm_model: str | None = Field(default=None, min_length=1, max_length=128)
    test_profile: str = Field(min_length=1, max_length=128)
    timeout_seconds: int | None = Field(default=None, ge=5, le=3600)


class PaneMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=100_000)
    confirm_model: str | None = Field(default=None, min_length=1, max_length=128)


class MaestroAuthorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=128)
    confirm_cost: bool = False


class MaestroMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=20_000)


class PlanApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_models: dict[str, str] = Field(min_length=1, max_length=8)
    max_turns_per_agent: int = Field(ge=1, le=6)
    mission_timeout_seconds: int = Field(ge=60, le=3600)
    confirm_costs: bool = False


class PlanExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_keys: list[str] = Field(min_length=1, max_length=8)
    test_profiles: dict[str, str] = Field(min_length=1, max_length=8)
    max_turns_per_agent: int = Field(ge=1, le=6)
    mission_timeout_seconds: int = Field(ge=60, le=3600)
    confirm_costs: bool = False


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


@router.post("/panes/{pane_id}/messages")
def send_pane_message(pane_id: str, request: PaneMessageRequest):
    from nexus.workspace import service

    return _call(service.send_pane_message, pane_id, request.text, confirm_model=request.confirm_model)


@router.get("/maestro")
def maestro_state(project_id: str):
    from nexus.workspace import maestro

    return _call(maestro.conversation_state, project_id)


@router.post("/maestro/authorize")
def maestro_authorize(request: MaestroAuthorizeRequest):
    from nexus.workspace import maestro

    return _call(maestro.authorize_planning, request.project_id, request.model, confirm_cost=request.confirm_cost)


@router.post("/maestro/messages")
def maestro_message(request: MaestroMessageRequest):
    from nexus.workspace import maestro

    return _call(maestro.send_message, request.project_id, request.text)


@router.post("/plans/{plan_id}/approve")
def approve_plan(plan_id: str, request: PlanApproveRequest):
    from nexus.workspace import service

    return _call(
        service.approve_plan,
        plan_id,
        agent_models=request.agent_models,
        max_turns_per_agent=request.max_turns_per_agent,
        mission_timeout_seconds=request.mission_timeout_seconds,
        confirm_costs=request.confirm_costs,
    )


@router.post("/plans/{plan_id}/execute")
def execute_plan(plan_id: str, request: PlanExecuteRequest):
    from nexus.workspace import service

    return _call(
        service.authorize_plan_execution,
        plan_id,
        agent_keys=request.agent_keys,
        test_profiles=request.test_profiles,
        max_turns_per_agent=request.max_turns_per_agent,
        mission_timeout_seconds=request.mission_timeout_seconds,
        confirm_costs=request.confirm_costs,
    )


@router.get("/plans/{plan_id}/execution")
def plan_execution(plan_id: str):
    from nexus.workspace import service

    return _call(service.plan_execution_state, plan_id)


@router.post("/plans/{plan_id}/interrupt")
def interrupt_plan(plan_id: str):
    from nexus.workspace import service

    return _call(service.interrupt_plan_execution, plan_id)


@router.post("/plans/{plan_id}/recover")
def recover_plan(plan_id: str):
    """Reattach only to a known durable execution; never replay a mission."""
    from nexus.workspace import coordinator

    return _call(coordinator.recover, plan_id)
