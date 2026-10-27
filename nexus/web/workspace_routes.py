"""HTTP surface of the multi-pane workspace (P1).

Thin layer over nexus.workspace.service; errors reuse the Development
error shape ({"detail": {"code", "message"}}). Cross-origin protection is
applied in nexus.web.app's middleware for the whole /api/workspace/ prefix.
There is intentionally no route that applies, merges, pushes or deletes.
"""

from __future__ import annotations

from typing import Literal

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
    backend: Literal["omniroute", "codex_native"] = "omniroute"
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
    confirm_cost: bool = False
    effort: str | None = Field(default=None, min_length=1, max_length=32)
    test_profile: str = Field(min_length=1, max_length=128)
    timeout_seconds: int | None = Field(default=None, ge=5, le=3600)


class PaneMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=100_000)
    confirm_model: str | None = Field(default=None, min_length=1, max_length=128)
    confirm_cost: bool = False
    effort: str | None = Field(default=None, min_length=1, max_length=32)


class MaestroAuthorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=128)
    confirm_cost: bool = False
    effort: str | None = Field(default=None, min_length=1, max_length=32)


class CostAuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=128)
    confirm_cost: bool = False
    max_uses: int = Field(ge=1, le=10000)
    validity_seconds: int = Field(ge=60, le=31536000)
    max_concurrent: int = Field(ge=1, le=100)


class MaestroMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=20_000)
    effort: str | None = Field(default=None, min_length=1, max_length=32)


class PlanApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_models: dict[str, str] = Field(min_length=1, max_length=8)
    backend: Literal["omniroute", "codex_native"] = "omniroute"
    agent_backends: dict[str, Literal["omniroute", "codex_native"]] | None = Field(
        default=None, max_length=8,
    )
    agent_efforts: dict[str, str] | None = Field(default=None, max_length=8)
    max_turns_per_agent: int = Field(ge=1, le=6)
    mission_timeout_seconds: int = Field(ge=60, le=3600)
    confirm_costs: bool = False


class PlanExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_keys: list[str] = Field(min_length=1, max_length=8)
    test_profiles: dict[str, str] = Field(max_length=8)
    max_turns_per_agent: int = Field(ge=1, le=6)
    mission_timeout_seconds: int = Field(ge=60, le=3600)
    confirm_costs: bool = False


class HandoffPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_pane_id: str = Field(min_length=1, max_length=64)
    target_pane_id: str = Field(min_length=1, max_length=64)
    source_mission_id: str = Field(min_length=1, max_length=64)


class HandoffApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_pane_id: str = Field(min_length=1, max_length=64)
    target_pane_id: str = Field(min_length=1, max_length=64)


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


@router.get("/cost-authorizations")
def list_cost_authorizations(project_id: str):
    from nexus.workspace import cost_authorizations

    return _call(cost_authorizations.list_authorizations, project_id)


@router.post("/cost-authorizations")
def create_cost_authorization(request: CostAuthorizationRequest):
    from nexus.workspace import cost_authorizations

    return _call(cost_authorizations.authorize, request.project_id, request.model,
                 confirm_cost=request.confirm_cost, max_uses=request.max_uses,
                 validity_seconds=request.validity_seconds, max_concurrent=request.max_concurrent)


@router.delete("/cost-authorizations/{authorization_id}")
def revoke_cost_authorization(authorization_id: str, project_id: str):
    from nexus.workspace import cost_authorizations

    return _call(cost_authorizations.revoke, project_id, authorization_id)


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
        backend=request.backend,
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
        confirm_cost=request.confirm_cost,
        effort=request.effort,
        test_profile=request.test_profile,
        timeout_seconds=request.timeout_seconds,
    )


@router.post("/panes/{pane_id}/messages")
def send_pane_message(pane_id: str, request: PaneMessageRequest):
    from nexus.workspace import service

    return _call(
        service.send_pane_message, pane_id, request.text,
        confirm_model=request.confirm_model, confirm_cost=request.confirm_cost, effort=request.effort,
    )


@router.get("/maestro")
def maestro_state(project_id: str):
    from nexus.workspace import maestro

    return _call(maestro.conversation_state, project_id)


@router.post("/maestro/authorize")
def maestro_authorize(request: MaestroAuthorizeRequest):
    from nexus.workspace import maestro

    return _call(maestro.authorize_planning, request.project_id, request.model,
                 confirm_cost=request.confirm_cost, effort=request.effort)


@router.post("/maestro/messages")
def maestro_message(request: MaestroMessageRequest):
    from nexus.workspace import maestro

    return _call(maestro.send_message, request.project_id, request.text, effort=request.effort)


@router.post("/plans/{plan_id}/approve")
def approve_plan(plan_id: str, request: PlanApproveRequest):
    from nexus.workspace import service

    return _call(
        service.approve_plan,
        plan_id,
        agent_models=request.agent_models,
        backend=request.backend,
        agent_backends=request.agent_backends,
        agent_efforts=request.agent_efforts,
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


@router.post("/plans/{plan_id}/resume")
def resume_plan(plan_id: str):
    """Explicit operator action; GET execution remains strictly read-only."""
    from nexus.workspace import coordinator

    return _call(coordinator.resume, plan_id)


@router.post("/handoffs/preview")
def preview_handoff(request: HandoffPreviewRequest):
    from nexus.workspace import handoff

    return _call(handoff.preview, request.source_pane_id, request.target_pane_id,
                 request.source_mission_id)


@router.post("/handoffs/{handoff_id}/approve")
def approve_handoff(handoff_id: str, request: HandoffApproveRequest):
    from nexus.workspace import handoff

    return _call(handoff.approve, handoff_id, request.fingerprint,
                 request.source_pane_id, request.target_pane_id)


@router.post("/handoffs/{handoff_id}/transfer")
def transfer_handoff(handoff_id: str):
    from nexus.workspace import handoff

    return _call(handoff.transfer, handoff_id)


@router.get("/handoffs/{handoff_id}")
def handoff_status(handoff_id: str):
    from nexus.workspace import handoff

    return _call(handoff.status, handoff_id)


@router.get("/plans/{plan_id}/handoffs")
def handoff_history(plan_id: str):
    from nexus.workspace import handoff

    return {"handoffs": _call(handoff.history, plan_id)}
