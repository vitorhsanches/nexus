"""Intelligent Maestro (P2A): persistent planning conversation per project.

The Maestro is a regular Nexus Chat session (nexus.chat.service): Codex
App-Server routed through OmniRoute, sandbox "read-only", running in the
project's registered directory so it can read -- never write -- the code.
No new executor exists here.

Flow of one user message:

1. The user's own words are stored (workspace_maestro_messages).
2. If planning is authorized for this conversation, a Chat turn is sent
   with a planning envelope (nexus.workspace.maestro_plan.build_prompt).
   Otherwise the labelled deterministic policy answers immediately.
3. reconcile() (called on every read, so it survives restarts) turns a
   finished Chat turn into a validated plan. Anything invalid -- bad JSON,
   unknown fields, unapproved model, wrong agent count -- becomes a clearly
   labelled deterministic fallback; failed/cancelled turns too. A plan is
   only ever a proposal: nothing is created or executed here.

Planning authorization is separate from execution authorization: a model
confirmed for planning never authorizes any pane or mission.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from nexus.dev.service import DevServiceError
from nexus.registry import workspace as store
from nexus.workspace import maestro_plan, policy

FREE_COSTS = frozenset({"free", "low_cost"})
PENDING_TURN_STATUSES = frozenset({"PENDING", "RUNNING"})


def _catalog() -> dict:
    from nexus.dev.models import list_dev_models

    return list_dev_models()


def _project(project_id: str) -> dict:
    from nexus.dev.service import validate_dev_project
    from nexus.registry.projects import get_project

    validate_dev_project(project_id)
    row = get_project(project_id)
    return {"id": row["id"], "name": row["name"]}


def _conversation(project_id: str):
    row = store.get_conversation_for_project(project_id)
    if row is None:
        store.create_conversation(project_id)
        row = store.get_conversation_for_project(project_id)
    return row


def _chat_error(error) -> DevServiceError:
    return DevServiceError(getattr(error, "code", "CHAT_ERROR"), str(error), http_status=getattr(error, "http_status", 500))


def authorize_planning(project_id: str, model: str, *, confirm_cost: bool) -> dict:
    """Confirm the exact route the Maestro may use for planning only."""
    from nexus.chat import service as chat_service

    _project(project_id)
    catalog = _catalog()
    row = next((item for item in catalog["models"] if item["id"] == model), None)
    if row is None:
        raise DevServiceError(
            "MODEL_NOT_ALLOWED",
            catalog.get("catalog_error") or "The selected model is not an approved live route.",
            http_status=403,
        )
    if row.get("cost_status") not in FREE_COSTS and not confirm_cost:
        raise DevServiceError(
            "COST_CONFIRMATION_REQUIRED",
            f"{model} is classified as {row.get('cost_status') or 'unknown'} cost; confirm it explicitly to plan with it.",
            http_status=400,
        )
    conversation = _conversation(project_id)
    try:
        # Chat's own catalog never knows prices; the confirmation above is
        # what authorizes an unknown/standard cost route for planning.
        session = chat_service.create_session(project_id, model, allow_unknown_cost=True)
    except chat_service.ChatError as error:
        raise _chat_error(error) from error
    store.authorize_conversation(conversation["id"], chat_session_id=session["session_id"], model=model)
    return conversation_state(project_id)


def send_message(project_id: str, text: str) -> dict:
    from nexus.chat import service as chat_service
    from nexus.workspace import service as workspace_service

    if not isinstance(text, str) or not text.strip():
        raise DevServiceError("EMPTY_MESSAGE", "message must not be empty.", http_status=400)
    text = text.strip()
    project = _project(project_id)
    conversation = _conversation(project_id)
    reconcile(conversation["id"])
    if any(row["author"] == "user" and row["status"] in {"PENDING", "PROCESSING"} for row in store.list_messages(conversation["id"])):
        raise DevServiceError("MAESTRO_BUSY", "The Maestro is still answering the previous message.", http_status=409)

    catalog = _catalog()
    limit = policy.max_auto_panes()
    # The user message is stored first (PENDING); every path below finishes
    # it through one atomic _answer(). An interruption anywhere in between
    # is recovered by reconcile() on the next read.
    message_id = store.add_message(conversation["id"], author="user", text=text, status="PENDING")
    if not conversation["chat_session_id"]:
        plan = maestro_plan.deterministic(text, models=catalog["models"], limit=limit, reason=None)
        _answer(project_id, message_id, text, plan, reply=NOT_AUTHORIZED_REPLY, user_status="COMPLETED")
        return conversation_state(project_id)

    previous = _latest_plan_summary(conversation["id"])
    prompt = maestro_plan.build_prompt(
        user_text=text,
        project=project,
        models=catalog["models"],
        panes=workspace_service.list_panes(project_id),
        limit=limit,
        previous_summary=previous,
    )
    try:
        turn = chat_service.send_turn(conversation["chat_session_id"], prompt, idempotency_key=message_id)
    except chat_service.ChatError as error:
        plan = maestro_plan.deterministic(text, models=catalog["models"], limit=limit, reason=f"Maestro indisponível ({error.code})")
        _answer(project_id, message_id, text, plan, reply=UNREACHABLE_REPLY, user_status="FAILED",
                error_code=error.code, error_message=str(error)[:1000])
        return conversation_state(project_id)
    store.set_message_turn(message_id, turn["turn_id"])
    return conversation_state(project_id)


def _latest_plan_summary(conversation_id: str) -> str | None:
    for row in reversed(store.list_messages(conversation_id)):
        if row["plan_id"]:
            plan = store.get_plan(row["plan_id"])
            if plan is not None:
                return json.loads(plan["proposal_json"]).get("summary")
    return None


NOT_AUTHORIZED_REPLY = "O planejamento por IA ainda não foi autorizado. Esta é a proposta determinística (palavras-chave, sem IA)."
UNREACHABLE_REPLY = "Não consegui falar com o modelo do Maestro. Mostro a proposta determinística como alternativa."
INVALID_REPLY = "A resposta do Maestro não passou na validação e não será usada. Mostro a proposta determinística como alternativa."
FAILED_REPLY = "O modelo do Maestro falhou nesta mensagem. Mostro a proposta determinística como alternativa."
INTERRUPTED_REPLY = "O envio desta mensagem ao Maestro foi interrompido. Mostro a proposta determinística como alternativa."
# A PENDING message without a Chat turn after this long was never sent
# (the process stopped between storing it and sending it).
SEND_GRACE_SECONDS = 60


def _answer(project_id: str, message_id: str, task_text: str, plan: dict, *, reply: str, user_status: str,
            error_code: str | None = None, error_message: str | None = None) -> str | None:
    return store.record_maestro_answer(
        message_id,
        project_id=project_id,
        task_text=task_text,
        plan=plan,
        policy_version=plan.get("policy_version") or plan["schema_version"],
        reply=reply,
        user_status=user_status,
        error_code=error_code,
        error_message=error_message,
    )


def _age_seconds(created_at: str) -> float:
    try:
        created = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return float("inf")
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (datetime.now(UTC) - created).total_seconds()


def reconcile(conversation_id: str) -> None:
    """Finish every unanswered user message exactly once.

    Handles a finished Chat turn (validated plan or labelled fallback), a
    failed/cancelled turn, and interruptions: a message whose turn id was
    never recorded is re-linked through its idempotency key; one that was
    never sent at all gets a labelled fallback after SEND_GRACE_SECONDS;
    one stuck in the legacy PROCESSING state is simply finished again. The
    final write is a single transaction, so nothing is duplicated.
    """
    from nexus.registry import chat_sessions

    conversation = store.get_conversation(conversation_id)
    catalog = None
    limit = policy.max_auto_panes()
    for row in store.list_messages(conversation_id):
        if row["author"] != "user" or row["status"] not in {"PENDING", "PROCESSING"}:
            continue
        project_id = conversation["project_id"]
        turn_id = row["chat_turn_id"]
        if not turn_id and conversation["chat_session_id"]:
            found = chat_sessions.get_turn_by_idempotency_key(conversation["chat_session_id"], row["id"])
            if found is not None:
                turn_id = found["id"]
                store.set_message_turn(row["id"], turn_id)
        if catalog is None:
            catalog = _catalog()
        if not turn_id:
            if not conversation["chat_session_id"]:
                plan = maestro_plan.deterministic(row["text"], models=catalog["models"], limit=limit, reason=None)
                _answer(project_id, row["id"], row["text"], plan, reply=NOT_AUTHORIZED_REPLY, user_status="COMPLETED")
            elif _age_seconds(row["created_at"]) >= SEND_GRACE_SECONDS:
                plan = maestro_plan.deterministic(row["text"], models=catalog["models"], limit=limit,
                                                  reason="Envio ao Maestro interrompido")
                _answer(project_id, row["id"], row["text"], plan, reply=INTERRUPTED_REPLY, user_status="FAILED",
                        error_code="MAESTRO_SEND_INTERRUPTED")
            continue
        turn = chat_sessions.get_turn(turn_id)
        if turn is None or turn["status"] in PENDING_TURN_STATUSES:
            continue
        if turn["status"] == "COMPLETED" and not catalog["models"] and catalog.get("catalog_error"):
            # Transient discovery failure: retry on a later read instead of
            # rejecting a possibly valid plan.
            continue
        if turn["status"] == "COMPLETED":
            try:
                plan = maestro_plan.parse(turn["response_text"] or "", models=catalog["models"], limit=limit)
                reply = plan["explanation"]
                if plan["needs_clarification"]:
                    reply = f"{reply}\n\n{plan['needs_clarification']}".strip()
                _answer(project_id, row["id"], row["text"], plan, reply=reply, user_status="COMPLETED")
            except maestro_plan.PlanValidationError as error:
                plan = maestro_plan.deterministic(row["text"], models=catalog["models"], limit=limit,
                                                  reason=f"Resposta do Maestro inválida: {error}")
                _answer(project_id, row["id"], row["text"], plan, reply=INVALID_REPLY, user_status="COMPLETED",
                        error_code="INVALID_MAESTRO_PLAN", error_message=str(error)[:1000])
        else:
            code = turn["error_code"] or turn["status"]
            plan = maestro_plan.deterministic(row["text"], models=catalog["models"], limit=limit,
                                              reason=f"Maestro indisponível ({code})")
            _answer(project_id, row["id"], row["text"], plan, reply=FAILED_REPLY, user_status="FAILED",
                    error_code=code, error_message=(turn["error_message"] or "")[:1000])


def conversation_state(project_id: str) -> dict:
    from nexus.workspace import service as workspace_service

    conversation = _conversation(project_id)
    reconcile(conversation["id"])
    conversation = store.get_conversation(conversation["id"])
    messages = []
    for row in store.list_messages(conversation["id"]):
        plan = None
        if row["author"] == "maestro" and row["plan_id"]:
            plan_row = store.get_plan(row["plan_id"])
            plan = workspace_service.plan_view(plan_row) if plan_row is not None else None
        messages.append({
            "message_id": row["id"],
            "author": row["author"],
            "text": row["text"],
            "status": row["status"],
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "created_at": row["created_at"],
            "plan": plan,
        })
    model_row = None
    if conversation["model"]:
        model_row = next((item for item in _catalog()["models"] if item["id"] == conversation["model"]), None)
    return {
        "conversation_id": conversation["id"],
        "project_id": project_id,
        "planning": {
            "authorized": bool(conversation["chat_session_id"]),
            "model": conversation["model"],
            "cost_status": (model_row or {}).get("cost_status") if conversation["model"] else None,
            "model_available": model_row is not None if conversation["model"] else None,
            "authorized_at": conversation["planning_authorized_at"],
            "mode": "read-only",
        },
        "busy": any(item["author"] == "user" and item["status"] in {"PENDING", "PROCESSING"} for item in messages),
        "messages": messages,
    }
