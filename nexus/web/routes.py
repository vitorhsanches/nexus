"""JSON API routes for the Nexus Local Mission Board UI V1."""

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from nexus.agents.policy import ExecutionPolicyError
from nexus.manager.agent import ManagerError
from nexus.missions.scheduler import MissionDependencyError
from nexus.missions.service import MissionNotFoundError
from nexus.tasks.registry import TaskNotFoundError
from nexus.reviews.reviewer import AlwaysPassReviewer
from nexus.web import execution, mission_execution, operational, programs, services
from nexus.web.mission_execution import MissionConflictError, MissionExecutionError


router = APIRouter(prefix="/api")


@router.get("/operational/runs")
def operational_runs():
    """Return every real Run recorded in the SQLite runs/agents registry.

    Read-only projection over nexus.registry.runs/nexus.registry.agents.
    Never reads or writes nexus.tasks.registry (the in-memory Mission/Task
    Board projection), so Board behavior is unaffected.
    """
    return {"runs": operational.get_operational_runs()}


@router.get("/operational/runs/{run_id}")
def operational_run_detail(run_id: str):
    """Return a single real Run with its full real Agent lineage."""
    run = operational.get_operational_run_detail(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return {"run": run}


@router.get("/operational/agents")
def operational_agents(run_id: str | None = None):
    """Return real Agents (Manager/Worker/ManagerReview) from the registry.

    Optionally scoped to a single Run via the ``run_id`` query parameter.
    """
    return {"agents": operational.get_operational_agents(run_id=run_id)}


@router.get("/operational/agents/{agent_id}/result")
def operational_agent_result(agent_id: str):
    """Return one agent's complete, untruncated result on demand.

    The bulk `/operational/agents` and `/operational/runs/{run_id}` reads
    only ever return the bounded preview plus a `has_full_result` flag; a
    client follows up here -- explicitly, one agent at a time -- to fetch
    the complete deliverable text when it needs it.
    """
    result = operational.get_operational_agent_result(agent_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")
    return {"result": result}


@router.get("/operational/reviewer-routing")
def operational_reviewer_routing(run_id: str | None = None):
    """Return the real ManagerReview routing history from the registry.

    Optionally scoped to a single Run via the ``run_id`` query parameter.
    """
    return {
        "reviewer_routing_history": operational.get_reviewer_routing_history(
            run_id=run_id
        )
    }


@router.get("/programs")
def list_programs():
    """Return every Nexus Program (nexus build/run), newest first.

    Read-only projection over nexus.program.state's JSON files -- these are
    written by whichever process ran ``nexus build``/``nexus run`` (usually
    the CLI, not this web server), so this reflects the real Program state
    on disk regardless of which process is currently live.
    """
    return {"programs": programs.list_program_summaries()}


@router.get("/programs/{program_id}")
def program_detail(program_id: str):
    """Return one Program's full detail: phases, acceptance, active agents,
    routing/failure timeline, bounded file activity, best-effort telemetry."""
    view = programs.get_program_view(program_id)
    if view is None:
        raise HTTPException(status_code=404, detail=f"Program not found: {program_id}")
    return {"program": view}


@router.get("/routing/status")
def routing_status():
    """Return a read-only, fail-safe view of Nexus routes and OmniRoute."""
    from nexus.web.routing_status import get_routing_status  # noqa: PLC0415

    return get_routing_status()


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
    project_id: str | None = None
    execution_path: str | None = None


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
    mission = services.create_mission(
        title=title,
        description=description,
        project_id=request.project_id,
        execution_path=request.execution_path,
    )
    return {"mission": services._to_dict(mission)}


@router.post("/missions/{mission_id}/plan")
def plan_mission(mission_id: str):
    """Run the existing ManagerAgent against a Mission and seed the Board.

    Loads the Mission from the Mission Engine, plans it through the
    deterministic ManagerAgent, and materializes its Tasks through the
    existing Mission Engine + Task Registry so they appear on the existing
    Mission Board. Never executes any Task. Calling this endpoint more than
    once for the same mission is a no-op beyond the first call: it returns
    the already-materialized Mission/Tasks instead of planning again.
    """
    try:
        result = services.plan_mission(mission_id)
    except MissionNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Mission not found: {mission_id}"
        )
    except ManagerError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "mission": services._to_dict(result["mission"]),
        "tasks": [services._to_dict(task) for task in result["tasks"]],
        "intent": result["intent"],
        "manager_id": result["manager_id"],
        "board_seeded": result["board_seeded"],
    }


@router.post("/missions/{mission_id}/execute")
def execute_mission(mission_id: str):
    """Execute a Mission end to end through the Mission Execution Orchestrator.

    Plans the Mission if necessary, validates its dependency graph, then
    executes eligible Tasks sequentially through the existing Task
    execution pipeline, passing predecessor evidence to dependent Tasks.
    Returns a Mission execution summary. Idempotent once COMPLETED.
    """
    try:
        summary = mission_execution.execute_mission(mission_id)
    except MissionNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Mission not found: {mission_id}"
        )
    except MissionDependencyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ExecutionPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except MissionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except MissionExecutionError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except ManagerError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {"execution": summary}


@router.post("/missions/{mission_id}/execute-reviewed")
def execute_mission_reviewed(mission_id: str):
    """Execute a Mission through the reviewed Task execution flow (v1.9).

    Identical Mission Execution Orchestrator as ``POST
    /missions/{mission_id}/execute``, except every eligible Task must pass
    review before it counts as dependency-COMPLETED. This milestone does not
    yet wire a live Manager/Luna reviewer: it uses a deterministic
    always-pass reviewer so the state machine can be exercised safely
    end-to-end without any external model call. A future milestone replaces
    this default with the real Manager reviewer.
    """
    try:
        summary = mission_execution.execute_mission(
            mission_id, review=True, reviewer=AlwaysPassReviewer()
        )
    except MissionNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Mission not found: {mission_id}"
        )
    except MissionDependencyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ExecutionPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except MissionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except MissionExecutionError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except ManagerError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {"execution": summary}


@router.post("/demo/mission")
def create_demo_mission():
    """Create a deterministic demo mission through the Mission Engine.

    Temporarily used to exercise the whole mission -> task -> board flow from
    the UI. The mission is held only in memory and is not persisted.
    """
    mission = services.create_demo_mission(run_id="RUN-DEMO")
    return {"mission": services._to_dict(mission), "tasks": services.get_tasks()}


# ---------------------------------------------------------------------------
# Nexus Interface MVP – Phase C additions
# ---------------------------------------------------------------------------


@router.get("/projects")
def list_projects():
    """Return all registered projects from the SQLite registry."""
    from nexus.registry.projects import list_projects as _list_projects  # noqa: PLC0415

    rows = _list_projects()
    return {"projects": [dict(r) for r in rows]}


@router.get("/runs/{run_id}/checkpoints")
def run_checkpoints(run_id: str):
    """Return durable checkpoints and approved plan for a Run."""
    from nexus.registry.checkpoints import (  # noqa: PLC0415
        get_approved_plan,
        list_checkpoints,
    )

    checkpoints = list_checkpoints(run_id)
    plan = get_approved_plan(run_id)
    return {
        "run_id": run_id,
        "checkpoints": checkpoints,
        "approved_plan": plan,
    }


class GoRequest(BaseModel):
    """Request body for launching a real Nexus GO orchestration run."""

    mission: str
    project_id: str | None = None
    # None (default) preserves existing automatic Manager model selection.
    # When set, it must be one of MANAGER_MODEL_CHOICES; validated below
    # before anything is launched.
    manager_model: str | None = None
    # False (default) preserves existing GO behavior exactly. When True,
    # opts this Run into explicit JavaScript dependency readiness
    # (nexus.orchestration.js_dependency_prep) -- a no-op for non-npm
    # projects, and a hard failure before any Worker launches if requested
    # but not achievable (see run_go()'s docstring).
    prepare_dependencies: bool = False


@router.get("/manager/models")
def manager_models():
    """Describe Manager model choices for the New Mission composer.

    Returns the Auto default, the Codex-CLI-compatible models a user may
    explicitly pick, and discovered-but-ineligible models with a concrete
    reason -- never the raw OmniRoute Worker catalog.
    """
    from nexus.policies.manager_model import describe_manager_models  # noqa: PLC0415

    return describe_manager_models()


@router.post("/go")
def go(request: GoRequest):
    """Launch run_go() in a background thread. Returns a job_id immediately.

    The client should poll GET /api/go/jobs/{job_id} every 2-3 seconds to
    discover the run_id (available within ~1 s) and track final status.
    """
    mission = request.mission.strip()
    if not mission:
        raise HTTPException(status_code=422, detail="mission must not be empty")

    manager_model = request.manager_model.strip() if request.manager_model else None
    if manager_model:
        from nexus.policies.manager_model import (  # noqa: PLC0415
            ManagerModelUnavailableError,
            validate_manual_manager_model,
        )

        try:
            validate_manual_manager_model(manager_model)
        except ManagerModelUnavailableError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    else:
        manager_model = None

    from nexus.web.go_runner import launch_go  # noqa: PLC0415

    job_id = launch_go(
        mission,
        request.project_id,
        manager_model=manager_model,
        prepare_dependencies=request.prepare_dependencies,
    )
    return {"job_id": job_id, "project_id": request.project_id}


@router.get("/go/jobs/{job_id}")
def go_job(job_id: str):
    """Poll a launched GO job for its run_id and terminal status."""
    from nexus.web.go_runner import get_job  # noqa: PLC0415

    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return {"job": job}


@router.get("/go/jobs")
def go_jobs():
    """Return recent GO jobs (newest first)."""
    from nexus.web.go_runner import list_jobs  # noqa: PLC0415

    return {"jobs": list_jobs()}


# ---------------------------------------------------------------------------
# Nexus Chat -- minimal persistent sessions + streaming API (T2)
#
# Every route here is a thin delegation to nexus.chat.service, which owns
# all session/turn state and is the only caller of
# nexus.chat.appserver_client.AppServerClient -- mirrors how /go delegates
# to nexus.web.go_runner / nexus.orchestration.go above. No route blocks on
# a full Codex turn: POST .../turns returns as soon as the turn is
# recorded and its background thread has started.
# ---------------------------------------------------------------------------


class ChatSessionCreateRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    effort: str | None = Field(default=None, min_length=1, max_length=32)
    allow_unknown_cost: bool = False
    backend: Literal["omniroute", "codex_native", "claude_native"] = "omniroute"


class ChatTurnCreateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    # Optional; when supplied, resubmitting the exact same
    # (session_id, idempotency_key) returns the original turn's current
    # state instead of executing it again. Omit only for a caller that
    # genuinely cannot retry.
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)


def _chat_error_response(error) -> HTTPException:
    return HTTPException(status_code=error.http_status, detail={"code": error.code, "message": str(error)})


@router.post("/chat/sessions")
def create_chat_session(request: ChatSessionCreateRequest):
    """Create a Chat session bound to a registered project. The workspace
    path is always that project's own registered path -- never a
    caller-supplied path -- so a session can never target an unapproved
    location."""
    from nexus.chat.service import ChatError, create_session  # noqa: PLC0415

    try:
        if request.backend in {"codex_native", "claude_native"}:
            return create_session(request.project_id, request.model, request.effort, backend=request.backend)
        if request.model is None and request.effort is None and not request.allow_unknown_cost:
            return create_session(request.project_id)
        return create_session(request.project_id, request.model, request.effort, request.allow_unknown_cost)
    except ChatError as error:
        raise _chat_error_response(error) from error


@router.get("/chat/sessions")
def list_chat_sessions(project_id: str | None = None):
    from nexus.chat.service import list_sessions  # noqa: PLC0415

    return {"sessions": list_sessions(project_id)}


@router.get("/chat/models")
def list_chat_models(backend: Literal["omniroute", "codex_native", "claude_native"] = "omniroute", refresh: bool = False):
    from nexus.chat.service import list_models  # noqa: PLC0415
    if backend in {"codex_native", "claude_native"}:
        return list_models(refresh=refresh, backend=backend)
    return list_models()


@router.get("/chat/sessions/{session_id}")
def get_chat_session(session_id: str):
    from nexus.chat.service import ChatError, get_session_detail  # noqa: PLC0415

    try:
        return get_session_detail(session_id)
    except ChatError as error:
        raise _chat_error_response(error) from error


@router.get("/chat/sessions/{session_id}/history")
def get_chat_history(
    session_id: str,
    turn_id: str | None = Query(default=None, min_length=1, max_length=128),
    cursor: str | None = Query(default=None, max_length=4096),
    limit: int = Query(default=50, ge=1, le=100),
):
    """Read an authorized session's persisted Codex conversation page.

    ``turn_id`` is a Codex-native turn ID from a prior history item; a
    Nexus session is always the authority for selecting the thread.
    """
    from nexus.chat.service import ChatError, get_history  # noqa: PLC0415

    try:
        return get_history(session_id, turn_id=turn_id, cursor=cursor, limit=limit)
    except ChatError as error:
        raise _chat_error_response(error) from error


@router.post("/chat/sessions/{session_id}/turns")
def create_chat_turn(session_id: str, request: ChatTurnCreateRequest):
    """Start (or resume) the session's Codex thread and run one turn in
    the background. Returns immediately with the turn's id and current
    status -- poll GET /chat/sessions/{id} or stream
    GET /chat/sessions/{id}/events?turn_id=... for the result."""
    from nexus.chat.service import ChatError, send_turn  # noqa: PLC0415

    if not request.text.strip():
        raise HTTPException(status_code=422, detail="text must not be empty")

    try:
        return send_turn(session_id, request.text, idempotency_key=request.idempotency_key)
    except ChatError as error:
        raise _chat_error_response(error) from error


@router.post("/chat/sessions/{session_id}/interrupt")
def interrupt_chat_turn(session_id: str, turn_id: str | None = None):
    """Request cancellation of the session's active turn (or a specific
    ``turn_id``). A no-op (status=NOT_ACTIVE) if nothing is running --
    never an error, since the turn may simply have already finished."""
    from nexus.chat.service import ChatError, interrupt_turn  # noqa: PLC0415

    try:
        return interrupt_turn(session_id, turn_id)
    except ChatError as error:
        raise _chat_error_response(error) from error


def _sse_line(event_id, event: dict) -> str:
    import json  # noqa: PLC0415

    payload = json.dumps(event, ensure_ascii=False)
    if event_id is None:
        return f"data: {payload}\n\n"
    return f"id: {event_id}\ndata: {payload}\n\n"


def _parse_chat_event_cursor(session_id: str, turn_id: str, cursor: str | None) -> int | None:
    if cursor is None:
        return None
    if len(cursor) > 512:
        raise HTTPException(status_code=400, detail={"code": "INVALID_EVENT_CURSOR", "message": "Event cursor is invalid."})
    prefix = f"{session_id}:{turn_id}:"
    if not cursor.startswith(prefix):
        raise HTTPException(status_code=400, detail={"code": "INVALID_EVENT_CURSOR", "message": "Event cursor does not belong to this session and turn."})
    value = cursor[len(prefix):]
    if not value.isascii() or not value.isdigit() or len(value) > 16:
        raise HTTPException(status_code=400, detail={"code": "INVALID_EVENT_CURSOR", "message": "Event cursor is invalid."})
    return int(value)


async def _chat_event_stream(session_id: str, turn_id: str | None, request: Request, after_seq: int | None = None, raw_cursor: str | None = None):
    """Bounded SSE generator.

    Replays whatever is still buffered for the turn (bounded, in-memory,
    evicted after NEXUS_CHAT event-buffer-limit events or once the turn
    ends and every subscriber has disconnected), then streams live events
    until the turn ends, the client disconnects, or a hard time cap is
    hit. KNOWN LIMITATION, documented rather than solved with a second
    general-purpose event-persistence engine: if a client is not
    subscribed while an event is produced and the turn is no longer
    active by the time it reconnects, the live buffer may be gone -- the
    client always still gets the turn's final, durable
    status/response_text from chat_turns (a plain GET never loses that),
    it just will not get a verbatim replay of every intermediate delta.
    """
    from nexus.chat import service  # noqa: PLC0415

    max_stream_seconds = 600

    resolved_turn_id = turn_id
    if resolved_turn_id is None:
        yield _sse_line(None, {"type": "nexus/noTurn", "sessionId": session_id})
        return

    subscription = service.subscribe_events(resolved_turn_id, after_seq=after_seq)
    if subscription is None:
        # Restart/turn completion can evict all volatile events. State the
        # gap, then provide the durable snapshot and terminal marker.
        yield _sse_line(None, {
            "type": "nexus/replayGap", "sessionId": session_id,
            "turnId": resolved_turn_id, "requested_cursor": raw_cursor,
            "reason": "volatile_events_unavailable",
            "reconcile": f"GET /api/chat/sessions/{session_id}/history",
        })
        snapshot = service.turn_snapshot(resolved_turn_id)
        yield _sse_line(None, {"type": "nexus/turnSnapshot", "turn": snapshot, "sessionId": session_id})
        yield _sse_line(None, {
            "type": "nexus/turnEnded", "sessionId": session_id,
            "turnId": resolved_turn_id,
            "status": snapshot.get("status") if isinstance(snapshot, dict) else None,
        })
        return

    subscriber_queue, catch_up, active_done, replay_gap, oldest, latest = subscription
    expected_seq = (after_seq + 1) if after_seq is not None else latest + 1
    try:
        if after_seq is not None and after_seq > latest:
            yield _sse_line(None, {
                "type": "nexus/replayGap", "sessionId": session_id,
                "turnId": resolved_turn_id, "requested_cursor": raw_cursor,
                "reason": "cursor_ahead_of_stream",
                "reconcile": f"GET /api/chat/sessions/{session_id}/history",
            })
            return
        if replay_gap:
            yield _sse_line(None, {
                "type": "nexus/replayGap", "sessionId": session_id,
                "turnId": resolved_turn_id, "requested_cursor": raw_cursor,
                "oldest_available_seq": oldest, "latest_available_seq": latest,
                "reconcile": f"GET /api/chat/sessions/{session_id}/history",
            })
        for seq, event in catch_up:
            if expected_seq is not None and seq > expected_seq and not replay_gap:
                yield _sse_line(None, {
                    "type": "nexus/replayGap", "sessionId": session_id,
                    "turnId": resolved_turn_id, "requested_cursor": raw_cursor,
                    "oldest_available_seq": seq, "latest_available_seq": latest,
                    "reconcile": f"GET /api/chat/sessions/{session_id}/history",
                })
            yield _sse_line(f"{session_id}:{resolved_turn_id}:{seq}", event)
            expected_seq = seq + 1
        if replay_gap and not catch_up:
            expected_seq = latest + 1

        deadline = asyncio.get_event_loop().time() + max_stream_seconds
        while asyncio.get_event_loop().time() < deadline:
            if await request.is_disconnected():
                break
            try:
                seq, event = await asyncio.to_thread(subscriber_queue.get, True, 0.5)
            except asyncio.TimeoutError:
                continue
            except Exception:
                if active_done.is_set():
                    break
                continue
            if expected_seq is not None and seq > expected_seq:
                dropped_through = getattr(subscriber_queue, "dropped_through", 0)
                yield _sse_line(None, {
                    "type": "nexus/replayGap", "sessionId": session_id,
                    "turnId": resolved_turn_id,
                    "requested_cursor": raw_cursor,
                    "dropped_through_seq": dropped_through,
                    "next_available_seq": seq,
                    "reconcile": f"GET /api/chat/sessions/{session_id}/history",
                })
            yield _sse_line(f"{session_id}:{resolved_turn_id}:{seq}", event)
            expected_seq = seq + 1
            if active_done.is_set() and subscriber_queue.empty():
                break
        if active_done.is_set():
            snapshot = service.turn_snapshot(resolved_turn_id)
            yield _sse_line(None, {
                "type": "nexus/turnEnded", "sessionId": session_id,
                "turnId": resolved_turn_id,
                "status": snapshot.get("status") if isinstance(snapshot, dict) else None,
            })
    finally:
        service.unsubscribe_events(resolved_turn_id, subscriber_queue)


@router.get("/chat/sessions/{session_id}/events")
async def stream_chat_events(
    session_id: str,
    request: Request,
    turn_id: str | None = Query(default=None, min_length=1, max_length=128),
    cursor: str | None = Query(default=None, max_length=512),
):
    """Server-Sent Events stream for one turn (the session's latest turn
    if ``turn_id`` is omitted). Event payloads are the raw App-Server
    notification envelopes -- renderable text fields inside them are
    untrusted plain text and must be escaped by the client, never
    rendered as HTML."""
    from nexus.chat import service  # noqa: PLC0415
    from nexus.chat.service import ChatError  # noqa: PLC0415
    from nexus.registry.chat_sessions import get_session  # noqa: PLC0415

    if get_session(session_id) is None:
        raise _chat_error_response(ChatError("SESSION_NOT_FOUND", f"Unknown session: {session_id!r}", http_status=404))

    resolved_turn_id = turn_id if turn_id is not None else service.latest_turn_id_for_session(session_id)
    header_cursor = request.headers.get("last-event-id") or None
    if cursor is not None and header_cursor is not None and cursor != header_cursor:
        raise HTTPException(status_code=400, detail={"code": "EVENT_CURSOR_CONFLICT", "message": "Query cursor conflicts with Last-Event-ID."})
    raw_cursor = cursor if cursor is not None else header_cursor
    after_seq = _parse_chat_event_cursor(session_id, resolved_turn_id, raw_cursor) if resolved_turn_id is not None else None
    if resolved_turn_id is not None:
        try:
            service.require_session_turn(session_id, resolved_turn_id)
        except ChatError as error:
            raise _chat_error_response(error) from error
        latest_seq = service.active_event_latest_seq(resolved_turn_id)
        if after_seq is not None and latest_seq is not None and after_seq > latest_seq:
            raise HTTPException(status_code=400, detail={"code": "INVALID_EVENT_CURSOR", "message": "Event cursor is ahead of this turn."})

    return StreamingResponse(
        _chat_event_stream(session_id, resolved_turn_id, request, after_seq=after_seq, raw_cursor=raw_cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# == Development Mode (F2.2) ==============================================
#
# Separate API surface, additive only -- nothing above this line is
# modified by it. Reuses nexus.dev.service/executor and
# nexus.registry.dev_sessions exactly as built in F1.2/F2.1/F2.2 Phase 1;
# this section only exposes them over HTTP.


class DevSessionCreateRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    requested_model: str | None = Field(default=None, min_length=1, max_length=128)
    backend: Literal["omniroute", "codex_native"] = "omniroute"


class DevApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["accept", "decline"]


class DevTurnCreateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    effort: str | None = Field(default=None, min_length=1, max_length=32)


class DevMissionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=1, max_length=100_000)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    effort: str | None = Field(default=None, min_length=1, max_length=32)
    allowed_models: list[str] = Field(min_length=1, max_length=50)
    test_profile: str = Field(min_length=1, max_length=128)
    timeout_seconds: int = Field(default=900, ge=5, le=3600)


class ModelActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=128)
    active: bool


class ModelCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=128)
    candidate: bool


class ModelQualificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=128)
    capabilities: list[str] = Field(min_length=1, max_length=5)
    efforts: list[str] = Field(min_length=1, max_length=8)
    cost_class: str = Field(min_length=1, max_length=32)
    max_risk_level: str = Field(min_length=1, max_length=16)


class ModelQualificationRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=128)


class AiPreferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=128)
    role: str = Field(min_length=1, max_length=32)
    executor: str = Field(min_length=1, max_length=32)
    model_id: str = Field(min_length=1, max_length=128)
    effort: str | None = Field(default=None, min_length=1, max_length=32)


class AiProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=32)
    enabled: bool


class AiPreferenceClearRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=128)
    role: str = Field(min_length=1, max_length=32)


class DevIntegrationReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class DevIntegrationApplyRequest(DevIntegrationReviewRequest):
    confirmed: bool = Field(strict=True)


def _dev_error_response(error) -> HTTPException:
    return HTTPException(status_code=error.http_status, detail={"code": error.code, "message": str(error)})


@router.post("/dev/sessions")
def create_dev_session(request: DevSessionCreateRequest):
    """Create a Development session: an isolated Git worktree bound to a
    registered, Development-Mode-authorized project. Never accepts a
    caller-supplied filesystem path -- the worktree always lives under
    the project's own registered path. Cross-origin/DNS-rebinding
    protection for this whole /api/dev/ surface is enforced once, up
    front, by nexus.web.app's protect_chat_localhost middleware -- not
    re-implemented per route."""
    from nexus.dev.service import DevServiceError, create_dev_session as _create_dev_session  # noqa: PLC0415

    try:
        if request.backend == "codex_native":
            return _create_dev_session(request.project_id, requested_model=request.requested_model, backend="codex_native")
        return _create_dev_session(request.project_id, requested_model=request.requested_model)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.get("/dev/sessions")
def list_dev_sessions(project_id: str | None = None):
    from nexus.dev.service import list_sessions  # noqa: PLC0415

    return {"sessions": list_sessions(project_id)}


@router.get("/dev/sessions/{session_id}")
def get_dev_session(session_id: str):
    from nexus.dev.service import DevServiceError, get_session_detail  # noqa: PLC0415

    try:
        return get_session_detail(session_id)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.get("/dev/sessions/{session_id}/turns")
def list_dev_turns(session_id: str):
    from nexus.dev.service import DevServiceError, get_session_detail  # noqa: PLC0415
    from nexus.dev.executor import list_turns  # noqa: PLC0415

    try:
        get_session_detail(session_id)
    except DevServiceError as error:
        raise _dev_error_response(error) from error
    return {"turns": list_turns(session_id)}


@router.get("/dev/sessions/{session_id}/turns/{turn_id}")
def get_dev_turn(session_id: str, turn_id: str):
    from nexus.dev.service import DevServiceError  # noqa: PLC0415
    from nexus.dev.executor import get_turn_detail  # noqa: PLC0415

    try:
        return get_turn_detail(session_id, turn_id)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.get("/dev/sessions/{session_id}/changed-files")
def get_dev_changed_files(session_id: str):
    """Read-only Git status summary for a session's isolated worktree
    (F2.4A) -- added/modified/deleted/renamed paths, never file content
    or a diff. Not gated by the execution feature gate: viewing what a
    session's worktree already contains is not an execution action."""
    from nexus.dev.executor import changed_files  # noqa: PLC0415
    from nexus.dev.service import DevServiceError  # noqa: PLC0415

    try:
        return {"files": changed_files(session_id)}
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.get("/dev/sessions/{session_id}/diff")
def get_dev_session_diff(session_id: str):
    """Read-only diff review against the session's persisted base SHA."""
    from nexus.dev.executor import review_diff  # noqa: PLC0415
    from nexus.dev.service import DevServiceError  # noqa: PLC0415

    try:
        return review_diff(session_id)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.post("/dev/sessions/{session_id}/reviews")
def create_dev_integration_review(session_id: str):
    from nexus.dev.integration import create_review
    from nexus.dev.service import DevServiceError
    try:
        return create_review(session_id)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.get("/dev/sessions/{session_id}/reviews/{review_id}")
def get_dev_integration_review(session_id: str, review_id: str):
    from nexus.dev.integration import get_review
    from nexus.dev.service import DevServiceError
    try:
        return get_review(session_id, review_id)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.get("/dev/sessions/{session_id}/preflight")
def get_dev_integration_preflight(session_id: str, review_id: str = Query(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")):
    from nexus.dev.integration import integration_preflight
    from nexus.dev.service import DevServiceError
    try:
        return integration_preflight(session_id, review_id)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.post("/dev/sessions/{session_id}/apply")
def apply_dev_integration(session_id: str, request: DevIntegrationApplyRequest):
    from nexus.dev.integration import apply_review
    from nexus.dev.service import DevServiceError
    try:
        return apply_review(session_id, request.review_id, confirmed=request.confirmed)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.post("/dev/sessions/{session_id}/reject")
def reject_dev_integration(session_id: str, request: DevIntegrationReviewRequest):
    from nexus.dev.integration import reject_review
    from nexus.dev.service import DevServiceError
    try:
        return reject_review(session_id, request.review_id)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.get("/dev/sessions/{session_id}/integrations")
def list_dev_integrations(session_id: str):
    from nexus.dev.integration import list_history
    from nexus.dev.service import DevServiceError
    try:
        return {"integrations": list_history(session_id)}
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.get("/dev/models")
def list_dev_models_route(refresh: bool = False, backend: Literal["omniroute", "codex_native"] = "omniroute"):
    """Return the models a Development session may use for the given backend:
    live OmniRoute routes approved for coding, or the local Codex account's
    own catalog for Codex Native."""
    if backend == "codex_native":
        from nexus.chat.service import list_models

        return list_models(refresh=refresh, backend="codex_native")
    from nexus.dev.models import list_dev_models

    return list_dev_models(refresh=refresh)


@router.get("/dev/sessions/{session_id}/approvals")
def list_dev_approvals(session_id: str):
    from nexus.dev import approvals
    from nexus.dev.service import DevServiceError, get_session_detail

    try:
        get_session_detail(session_id)
        return {"approvals": approvals.list_for_session(session_id)}
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.post("/dev/sessions/{session_id}/approvals/{approval_id}")
def decide_dev_approval(session_id: str, approval_id: str, request: DevApprovalDecisionRequest):
    """A human answer to one pending Codex approval request. Same loopback and
    same-origin protection as the rest of /api/dev/ (app middleware)."""
    from nexus.dev import approvals
    from nexus.dev.service import DevServiceError

    try:
        return approvals.decide(session_id, approval_id, request.decision)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.get("/dev/model-catalog")
def list_dev_model_catalog_route(refresh: bool = False):
    """Return all discovered models and their non-granting approval status."""
    from nexus.dev.models import list_dev_model_catalog

    return list_dev_model_catalog(refresh=refresh)


@router.get("/dev/model-management")
def model_management_state(refresh: bool = False):
    from nexus.dev.models import list_dev_model_catalog

    return list_dev_model_catalog(refresh=refresh)


@router.get("/dev/model-management/admin-status")
def model_management_admin_status():
    from nexus.dev.model_management import admin_auth_configured
    return {"admin_auth_configured": admin_auth_configured()}


@router.get("/dev/ai-settings")
def ai_settings_state(project_id: str = Query(min_length=1, max_length=128), refresh: bool = False):
    from nexus.workspace import ai_settings
    from nexus.dev.service import DevServiceError

    try:
        return ai_settings.state(project_id, refresh=refresh)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.get("/dev/ai-settings/admin-check")
def ai_settings_admin_check(http_request: Request):
    from nexus.dev import model_management
    from nexus.dev.service import DevServiceError

    try:
        model_management.require_local_operator(http_request, http_request.headers.get("authorization"))
        return {"authenticated": True}
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.post("/dev/ai-settings")
def save_ai_preference(request: AiPreferenceRequest, http_request: Request):
    from nexus.dev import model_management
    from nexus.dev.service import DevServiceError
    from nexus.workspace import ai_settings

    try:
        model_management.require_local_operator(http_request, http_request.headers.get("authorization"))
        return ai_settings.save(request.project_id, request.role, request.executor, request.model_id, request.effort)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.post("/dev/ai-settings/clear")
def clear_ai_preference(request: AiPreferenceClearRequest, http_request: Request):
    from nexus.dev import model_management
    from nexus.dev.service import DevServiceError
    from nexus.workspace import ai_settings

    try:
        model_management.require_local_operator(http_request, http_request.headers.get("authorization"))
        return ai_settings.clear(request.project_id, request.role)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.post("/dev/ai-settings/providers")
def set_ai_provider(request: AiProviderRequest, http_request: Request):
    from nexus.dev import model_management
    from nexus.dev.service import DevServiceError
    from nexus.workspace import ai_settings

    try:
        model_management.require_local_operator(http_request, http_request.headers.get("authorization"))
        return ai_settings.set_provider_enabled(request.project_id, request.provider, request.enabled)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.get("/dev/ai-settings/providers")
def ai_provider_state(project_id: str = Query(min_length=1, max_length=128)):
    from nexus.dev.service import DevServiceError
    from nexus.workspace import ai_settings

    try:
        return ai_settings.provider_state(project_id)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.post("/dev/model-management/activation")
def change_model_activation(request: ModelActivationRequest, http_request: Request):
    from nexus.dev import model_management
    from nexus.dev.service import DevServiceError

    try:
        model_management.require_local_operator(http_request, http_request.headers.get("authorization"))
        return model_management.set_active(request.model_id, request.active)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.post("/dev/model-management/candidate")
def change_model_candidate(request: ModelCandidateRequest, http_request: Request):
    from nexus.dev import model_management
    from nexus.dev.service import DevServiceError

    try:
        model_management.require_local_operator(http_request, http_request.headers.get("authorization"))
        return model_management.set_candidate(request.model_id, request.candidate)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.post("/dev/model-management/qualifications")
def qualify_model(request: ModelQualificationRequest, http_request: Request):
    from nexus.dev import model_management
    from nexus.dev.service import DevServiceError
    try:
        model_management.require_local_operator(http_request, http_request.headers.get("authorization"))
        return model_management.qualify(
            request.model_id, provider=request.provider, capabilities=request.capabilities,
            efforts=request.efforts, cost_class=request.cost_class,
            max_risk_level=request.max_risk_level,
        )
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.post("/dev/model-management/qualifications/revoke")
def revoke_model_qualification(request: ModelQualificationRevokeRequest, http_request: Request):
    from nexus.dev import model_management
    from nexus.dev.service import DevServiceError
    try:
        model_management.require_local_operator(http_request, http_request.headers.get("authorization"))
        return model_management.revoke_qualification(request.model_id)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.get("/dev/test-profiles")
def list_dev_test_profiles(project_id: str = Query(min_length=1, max_length=128)):
    """Return operator-owned verification profiles bound to one authorized project."""
    from nexus.dev.service import DevServiceError, validate_dev_project
    from nexus.dev.test_profiles import (
        VerificationProfileConfigurationError,
        list_test_profiles,
    )

    try:
        validate_dev_project(project_id)
        return {"profiles": list_test_profiles(project_id)}
    except DevServiceError as error:
        raise _dev_error_response(error) from error
    except VerificationProfileConfigurationError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "TEST_PROFILE_CONFIG_INVALID", "message": str(error)},
        ) from error


@router.get("/dev/execution-status")
def dev_execution_status():
    """Whether the server-side write-execution feature gate is enabled.

    Read-only, unauthenticated-by-design (it reveals no project data) --
    the Cockpit UI polls this to decide whether to show a usable Run
    control at all, per F2.2 Phase 4's "never display a misleading
    enabled Run action" requirement."""
    from nexus.dev import feature_gate  # noqa: PLC0415

    return {"execution_enabled": feature_gate.execution_enabled()}


@router.get("/dev/integration-status")
def dev_integration_status():
    from nexus.dev import integration_gate  # noqa: PLC0415
    return {"integration_enabled": integration_gate.integration_enabled()}


@router.post("/dev/sessions/{session_id}/turns")
def create_dev_turn(session_id: str, request: DevTurnCreateRequest):
    """Start one development turn (subject to the execution feature gate
    and this session's own authorization/worktree checks). Returns
    immediately with the turn's PENDING record -- poll
    GET /dev/sessions/{id}/turns/{turn_id} or stream
    GET /dev/sessions/{id}/events?turn_id=... for the result."""
    from nexus.dev.service import DevServiceError  # noqa: PLC0415
    from nexus.dev.executor import run_dev_turn  # noqa: PLC0415

    if not request.prompt.strip():
        raise HTTPException(status_code=422, detail="prompt must not be empty")

    try:
        return run_dev_turn(session_id, request.prompt, model=request.model, effort=request.effort)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.post("/dev/sessions/{session_id}/interrupt")
def interrupt_dev_turn_route(session_id: str, turn_id: str | None = None):
    """Request cancellation of the session's active turn (or a specific
    ``turn_id``). A no-op (status=NOT_ACTIVE) if nothing is running."""
    from nexus.dev.service import DevServiceError  # noqa: PLC0415
    from nexus.dev.executor import interrupt_dev_turn  # noqa: PLC0415

    try:
        return interrupt_dev_turn(session_id, turn_id)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.post("/dev/sessions/{session_id}/missions")
def start_dev_mission(session_id: str, request: DevMissionCreateRequest):
    from nexus.dev.mission_runner import start_mission
    from nexus.dev.service import DevServiceError

    try:
        return start_mission(
            session_id,
            request.description,
            model=request.model,
            effort=request.effort,
            allowed_models=request.allowed_models,
            test_profile=request.test_profile,
            timeout_seconds=request.timeout_seconds,
        )
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.get("/dev/sessions/{session_id}/missions")
def list_dev_missions(session_id: str):
    from nexus.dev.mission_runner import list_missions
    from nexus.dev.service import DevServiceError

    try:
        return {"missions": list_missions(session_id)}
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.get("/dev/sessions/{session_id}/missions/{mission_id}")
def get_dev_mission(session_id: str, mission_id: str):
    from nexus.dev.mission_runner import get_mission
    from nexus.dev.service import DevServiceError

    try:
        return get_mission(session_id, mission_id)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.get("/dev/sessions/{session_id}/missions/{mission_id}/progress")
def get_dev_mission_progress(session_id: str, mission_id: str):
    from nexus.dev.mission_runner import get_mission
    from nexus.dev.service import DevServiceError

    try:
        mission = get_mission(session_id, mission_id)
        return {
            "mission_id": mission["mission_id"],
            "status": mission["status"],
            "attempt_count": mission["attempt_count"],
            "current_turn_id": mission["current_turn_id"],
            "updated_at": mission["updated_at"],
            "error_code": mission["error_code"],
            "error_message": mission["error_message"],
        }
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.get("/dev/sessions/{session_id}/missions/{mission_id}/tests")
def get_dev_mission_tests(session_id: str, mission_id: str):
    from nexus.dev.mission_runner import get_mission
    from nexus.dev.service import DevServiceError

    try:
        mission = get_mission(session_id, mission_id)
        return {"mission_id": mission_id, "tests": mission["tests"]}
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.get("/dev/sessions/{session_id}/missions/{mission_id}/result")
def get_dev_mission_result(session_id: str, mission_id: str):
    from nexus.dev.mission_runner import get_mission
    from nexus.dev.service import DevServiceError

    try:
        mission = get_mission(session_id, mission_id)
        return {
            "mission_id": mission_id,
            "status": mission["status"],
            "result": mission["result"],
            "error_code": mission["error_code"],
            "error_message": mission["error_message"],
        }
    except DevServiceError as error:
        raise _dev_error_response(error) from error


@router.post("/dev/sessions/{session_id}/missions/{mission_id}/interrupt")
def interrupt_dev_mission(session_id: str, mission_id: str):
    from nexus.dev.mission_runner import interrupt_mission
    from nexus.dev.service import DevServiceError

    try:
        return interrupt_mission(session_id, mission_id)
    except DevServiceError as error:
        raise _dev_error_response(error) from error


def _parse_dev_event_cursor(session_id: str, turn_id: str, cursor: str | None) -> int | None:
    if cursor is None:
        return None
    if len(cursor) > 512:
        raise HTTPException(status_code=400, detail={"code": "INVALID_EVENT_CURSOR", "message": "Event cursor is invalid."})
    prefix = f"{session_id}:{turn_id}:"
    if not cursor.startswith(prefix):
        raise HTTPException(status_code=400, detail={"code": "INVALID_EVENT_CURSOR", "message": "Event cursor does not belong to this session and turn."})
    value = cursor[len(prefix):]
    if not value.isascii() or not value.isdigit() or len(value) > 16:
        raise HTTPException(status_code=400, detail={"code": "INVALID_EVENT_CURSOR", "message": "Event cursor is invalid."})
    return int(value)


async def _dev_event_stream(session_id: str, turn_id: str | None, request: Request, after_seq: int | None = None, raw_cursor: str | None = None):
    """Bounded SSE generator for a development turn -- exact mirror of
    _chat_event_stream, applied to nexus.dev.executor's event fan-out.
    A browser disconnect here (detected via request.is_disconnected())
    only stops this generator; it never touches the background thread
    actually running the turn, so the turn itself keeps running to
    completion and a later reconnect can reconcile against the durable
    turn row regardless of whether this stream was open when it
    finished."""
    from nexus.dev import executor  # noqa: PLC0415

    max_stream_seconds = 600

    resolved_turn_id = turn_id
    if resolved_turn_id is None:
        yield _sse_line(None, {"type": "nexus/noTurn", "sessionId": session_id})
        return

    subscription = executor.subscribe_events(resolved_turn_id, after_seq=after_seq)
    if subscription is None:
        yield _sse_line(None, {
            "type": "nexus/replayGap", "sessionId": session_id,
            "turnId": resolved_turn_id, "requested_cursor": raw_cursor,
            "reason": "volatile_events_unavailable",
            "reconcile": f"GET /api/dev/sessions/{session_id}/turns/{resolved_turn_id}",
        })
        snapshot = executor.turn_snapshot(resolved_turn_id)
        yield _sse_line(None, {"type": "nexus/turnSnapshot", "turn": snapshot, "sessionId": session_id})
        yield _sse_line(None, {
            "type": "nexus/turnEnded", "sessionId": session_id,
            "turnId": resolved_turn_id,
            "status": snapshot.get("status") if isinstance(snapshot, dict) else None,
        })
        return

    subscriber_queue, catch_up, active_done, replay_gap, oldest, latest = subscription
    expected_seq = (after_seq + 1) if after_seq is not None else latest + 1
    try:
        if after_seq is not None and after_seq > latest:
            yield _sse_line(None, {
                "type": "nexus/replayGap", "sessionId": session_id,
                "turnId": resolved_turn_id, "requested_cursor": raw_cursor,
                "reason": "cursor_ahead_of_stream",
                "reconcile": f"GET /api/dev/sessions/{session_id}/turns/{resolved_turn_id}",
            })
            return
        if replay_gap:
            yield _sse_line(None, {
                "type": "nexus/replayGap", "sessionId": session_id,
                "turnId": resolved_turn_id, "requested_cursor": raw_cursor,
                "oldest_available_seq": oldest, "latest_available_seq": latest,
                "reconcile": f"GET /api/dev/sessions/{session_id}/turns/{resolved_turn_id}",
            })
        for seq, event in catch_up:
            if expected_seq is not None and seq > expected_seq and not replay_gap:
                yield _sse_line(None, {
                    "type": "nexus/replayGap", "sessionId": session_id,
                    "turnId": resolved_turn_id, "requested_cursor": raw_cursor,
                    "oldest_available_seq": seq, "latest_available_seq": latest,
                    "reconcile": f"GET /api/dev/sessions/{session_id}/turns/{resolved_turn_id}",
                })
            yield _sse_line(f"{session_id}:{resolved_turn_id}:{seq}", event)
            expected_seq = seq + 1
        if replay_gap and not catch_up:
            expected_seq = latest + 1

        deadline = asyncio.get_event_loop().time() + max_stream_seconds
        while asyncio.get_event_loop().time() < deadline:
            if await request.is_disconnected():
                break
            try:
                seq, event = await asyncio.to_thread(subscriber_queue.get, True, 0.5)
            except asyncio.TimeoutError:
                continue
            except Exception:
                if active_done.is_set():
                    break
                continue
            if expected_seq is not None and seq > expected_seq:
                dropped_through = getattr(subscriber_queue, "dropped_through", 0)
                yield _sse_line(None, {
                    "type": "nexus/replayGap", "sessionId": session_id,
                    "turnId": resolved_turn_id,
                    "requested_cursor": raw_cursor,
                    "dropped_through_seq": dropped_through,
                    "next_available_seq": seq,
                    "reconcile": f"GET /api/dev/sessions/{session_id}/turns/{resolved_turn_id}",
                })
            yield _sse_line(f"{session_id}:{resolved_turn_id}:{seq}", event)
            expected_seq = seq + 1
            if active_done.is_set() and subscriber_queue.empty():
                break
        if active_done.is_set():
            snapshot = executor.turn_snapshot(resolved_turn_id)
            yield _sse_line(None, {
                "type": "nexus/turnEnded", "sessionId": session_id,
                "turnId": resolved_turn_id,
                "status": snapshot.get("status") if isinstance(snapshot, dict) else None,
            })
    finally:
        executor.unsubscribe_events(resolved_turn_id, subscriber_queue)


@router.get("/dev/sessions/{session_id}/events")
async def stream_dev_events(
    session_id: str,
    request: Request,
    turn_id: str | None = Query(default=None, min_length=1, max_length=128),
    cursor: str | None = Query(default=None, max_length=512),
):
    """Server-Sent Events stream for one development turn (the session's
    latest turn if ``turn_id`` is omitted). Event payloads are raw
    App-Server notification envelopes -- renderable text fields inside
    them are untrusted plain text and must be escaped by the client,
    never rendered as HTML."""
    from nexus.dev import executor  # noqa: PLC0415
    from nexus.dev.service import DevServiceError, get_session_detail  # noqa: PLC0415

    try:
        get_session_detail(session_id)
    except DevServiceError as error:
        raise _dev_error_response(error) from error

    resolved_turn_id = turn_id if turn_id is not None else executor.latest_turn_id_for_session(session_id)
    header_cursor = request.headers.get("last-event-id") or None
    if cursor is not None and header_cursor is not None and cursor != header_cursor:
        raise HTTPException(status_code=400, detail={"code": "EVENT_CURSOR_CONFLICT", "message": "Query cursor conflicts with Last-Event-ID."})
    raw_cursor = cursor if cursor is not None else header_cursor
    after_seq = _parse_dev_event_cursor(session_id, resolved_turn_id, raw_cursor) if resolved_turn_id is not None else None
    if resolved_turn_id is not None:
        try:
            executor.require_dev_session_turn(session_id, resolved_turn_id)
        except DevServiceError as error:
            raise _dev_error_response(error) from error
        latest_seq = executor.active_event_latest_seq(resolved_turn_id)
        if after_seq is not None and latest_seq is not None and after_seq > latest_seq:
            raise HTTPException(status_code=400, detail={"code": "INVALID_EVENT_CURSOR", "message": "Event cursor is ahead of this turn."})

    return StreamingResponse(
        _dev_event_stream(session_id, resolved_turn_id, request, after_seq=after_seq, raw_cursor=raw_cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
