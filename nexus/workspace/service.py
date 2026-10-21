"""Multi-pane workspace service (P1).

A pane is a persisted agent identity that owns exactly one real
Development session. Everything that executes goes through the existing
engine -- nexus.dev.service.create_dev_session (authorization + isolated
worktree), nexus.dev.models.list_dev_models (approved live routes) and
nexus.dev.mission_runner.start_mission (Codex App-Server via OmniRoute,
trusted tests, single repair, READY_FOR_REVIEW). This module adds no
executor of its own and never applies, merges or pushes anything.

Invariants:

* One pane <-> one session <-> one worktree, so two writer panes never
  share a checkout. The UNIQUE constraint on workspace_panes.dev_session_id
  enforces it in the database as well.
* A pane's model only applies to missions started after the change; an
  active mission keeps the route it was started with.
* Every mission needs the caller to re-confirm the pane's exact route, so
  a cost-bearing route never runs implicitly.
* Hiding a pane is layout state; its session and history are kept.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from contextlib import contextmanager

from nexus.dev.service import DevServiceError
from nexus.registry import workspace as store
from nexus.workspace import policy

MAX_TITLE_LENGTH = 80
ACTIVE_MISSION_STATUSES = frozenset({"PENDING", "RUNNING", "TESTING", "REPAIRING"})


def _catalog() -> dict:
    from nexus.dev.models import list_dev_models

    return list_dev_models()


def _require_approved_model(model: str) -> dict:
    catalog = _catalog()
    for row in catalog["models"]:
        if row["id"] == model:
            return row
    message = catalog.get("catalog_error") or "The selected model is not an approved live Development route."
    raise DevServiceError("MODEL_NOT_ALLOWED", message, http_status=403)


def _require_pane(pane_id: str):
    row = store.get_pane(pane_id)
    if row is None:
        raise DevServiceError("PANE_NOT_FOUND", f"Unknown workspace pane: {pane_id!r}", http_status=404)
    return row


def _clean_title(title: str | None, role: str) -> str:
    cleaned = " ".join((title or "").split())
    if not cleaned:
        return policy.ROLES[role]
    if len(cleaned) > MAX_TITLE_LENGTH:
        raise DevServiceError("INVALID_TITLE", f"title exceeds {MAX_TITLE_LENGTH} characters.", http_status=400)
    return cleaned


def _missions(dev_session_id: str) -> list[dict]:
    from nexus.dev.mission_runner import list_missions

    try:
        return list_missions(dev_session_id)
    except DevServiceError:
        return []


def _latest_mission(missions: list[dict]) -> dict | None:
    if not missions:
        return None
    latest = max(missions, key=lambda item: (item["created_at"] or "", item["mission_id"]))
    result = latest.get("result") or {}
    return {
        "mission_id": latest["mission_id"],
        "status": latest["status"],
        "requested_model": latest["requested_model"],
        "attempt_count": latest["attempt_count"],
        "error_code": latest["error_code"],
        "error_message": latest["error_message"],
        "created_at": latest["created_at"],
        "finished_at": latest["finished_at"],
        "tests": [
            {"attempt_number": test["attempt_number"], "status": test["status"], "exit_code": test["exit_code"]}
            for test in latest["tests"]
        ],
        "changed_files": result.get("changed_files") or [],
        "human_approval_required": result.get("human_approval_required", latest["status"] == "READY_FOR_REVIEW"),
        "integration_applied": bool(result.get("integration_applied", False)),
    }


def pane_view(row) -> dict:
    from nexus.registry import dev_sessions

    session = dev_sessions.get_session(row["dev_session_id"])
    missions = _missions(row["dev_session_id"])
    latest = _latest_mission(missions)
    return {
        "pane_id": row["id"],
        "project_id": row["project_id"],
        "plan_id": row["plan_id"],
        "origin": row["origin"],
        "role": row["role"],
        "role_label": policy.ROLES.get(row["role"], row["role"]),
        "title": row["title"],
        "model": row["model"],
        "layout": {name: (row[name] if name == "position" else bool(row[name])) for name in store.LAYOUT_FIELDS},
        "session": None if session is None else {
            "session_id": session["id"],
            "status": session["status"],
            "branch": session["branch"],
            "worktree_path": session["worktree_path"],
            "base_sha": session["base_sha"],
        },
        "mission_count": len(missions),
        "latest_mission": latest,
        "busy": _pane_is_executing(row),
        "plan_agent_key": row["plan_agent_key"],
        "brief": _brief_view(row),
        "dependencies": _dependency_status(row),
        "messages": pane_messages(row),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _brief(row) -> dict | None:
    return json.loads(row["brief_json"]) if row["brief_json"] else None


def _plan_authorized_uses(pane_id: str) -> int:
    return sum(
        1
        for event in store.list_events(pane_id, limit=1000)
        if event["kind"] in {"MISSION_STARTED", "MESSAGE_SENT"} and json.loads(event["detail_json"]).get("authorized_by_plan")
    )


def _brief_view(row) -> dict | None:
    brief = _brief(row)
    if brief is None:
        return None
    authorization = brief.get("authorization") or {}
    used = _plan_authorized_uses(row["id"])
    return {
        "task": brief.get("task"),
        "acceptance_criteria": brief.get("acceptance_criteria") or [],
        "depends_on": brief.get("depends_on") or [],
        "summary": brief.get("summary"),
        "authorization": {
            "model": authorization.get("model"),
            "cost_status": authorization.get("cost_status"),
            "max_turns": authorization.get("max_turns"),
            "turns_used": used,
            "mission_timeout_seconds": authorization.get("mission_timeout_seconds"),
            "applies": bool(authorization) and authorization.get("model") == row["model"]
            and used < int(authorization.get("max_turns") or 0),
        },
    }


def _dependency_status(row) -> list[dict]:
    brief = _brief(row)
    if not brief or not brief.get("depends_on") or not row["plan_id"]:
        return []
    by_key = {pane["plan_agent_key"]: pane for pane in store.panes_for_plan(row["plan_id"])}
    status = []
    for key in brief["depends_on"]:
        pane = by_key.get(key)
        latest = _latest_mission(_missions(pane["dev_session_id"])) if pane is not None else None
        status.append({
            "key": key,
            "title": pane["title"] if pane is not None else key,
            "ready": bool(latest and latest["status"] == "READY_FOR_REVIEW"),
        })
    return status


def plan_task(project_id: str, task_text: str) -> dict:
    from nexus.dev.service import validate_dev_project

    if not isinstance(task_text, str) or not task_text.strip():
        raise DevServiceError("EMPTY_TASK", "task text must not be empty.", http_status=400)
    validate_dev_project(project_id)
    catalog = _catalog()
    proposal = policy.propose(task_text, catalog["models"])
    proposal["catalog_error"] = catalog.get("catalog_error")
    proposal["open_panes"] = store.count_open_panes(project_id)
    proposal["max_open_panes"] = policy.max_open_panes()
    plan_id = store.create_plan(
        project_id,
        task_text.strip(),
        policy_version=proposal["policy_version"],
        complexity=proposal["complexity"],
        proposal=proposal,
    )
    return {"plan_id": plan_id, "project_id": project_id, "task_text": task_text.strip(), **proposal}


def plan_view(row) -> dict:
    proposal = json.loads(row["proposal_json"])
    if "agents" not in proposal:
        # P1 keyword proposals stored before the Maestro plan schema existed.
        proposal["agents"] = [
            {"key": f"{pane['role']}{index + 1}", "role": pane["role"], "title": pane["title"], "model": pane.get("model"),
             "cost_status": pane.get("cost_status"), "task": row["task_text"], "acceptance_criteria": [],
             "depends_on": [], "touches": [], "independent": False, "conflicts_with": []}
            for index, pane in enumerate(proposal.get("panes", []))
        ]
        proposal.setdefault("source", "deterministic")
        proposal.setdefault("explanation", "Proposta determinística por palavras-chave (P1, sem IA).")
    keys = row.keys()
    from nexus.registry import workspace_execution

    return {
        "plan_id": row["id"],
        "project_id": row["project_id"],
        "task_text": row["task_text"],
        "created_at": row["created_at"],
        **proposal,
        "status": row["status"] if "status" in keys else "PROPOSED",
        "approval": json.loads(row["approval_json"]) if "approval_json" in keys and row["approval_json"] else None,
        "execution": workspace_execution.view(row["id"]),
    }


def list_plans(project_id: str) -> list[dict]:
    return [plan_view(row) for row in store.list_plans(project_id)]


def authorize_plan_execution(
    plan_id: str,
    *,
    agent_keys: list[str],
    test_profiles: dict[str, str],
    max_turns_per_agent: int,
    mission_timeout_seconds: int,
    confirm_costs: bool = False,
) -> dict:
    """Authorize and start one durable sequential plan execution."""
    from nexus.workspace import coordinator

    coordinator.authorize_execution(
        plan_id,
        agent_keys=agent_keys,
        test_profiles=test_profiles,
        max_turns_per_agent=max_turns_per_agent,
        mission_timeout_seconds=mission_timeout_seconds,
        confirm_costs=confirm_costs,
    )
    return coordinator.start(plan_id)


def plan_execution_state(plan_id: str) -> dict:
    from nexus.workspace import coordinator

    return coordinator.state(plan_id)


def interrupt_plan_execution(plan_id: str) -> dict:
    from nexus.workspace import coordinator

    return coordinator.interrupt(plan_id)


def create_pane(
    project_id: str,
    *,
    role: str,
    title: str | None = None,
    model: str | None = None,
    plan_id: str | None = None,
    _origin: str | None = None,
    _plan_agent_key: str | None = None,
    _brief: dict | None = None,
) -> dict:
    """Create one operational pane: a real Development session + identity."""
    from nexus.dev.service import create_dev_session

    if role not in policy.ROLES:
        raise DevServiceError("INVALID_ROLE", f"Unknown pane role: {role!r}", http_status=400)
    clean_title = _clean_title(title, role)
    if plan_id is not None:
        plan = store.get_plan(plan_id)
        if plan is None or plan["project_id"] != project_id:
            raise DevServiceError("PLAN_NOT_FOUND", f"Unknown plan for this project: {plan_id!r}", http_status=404)
    limit = policy.max_open_panes()
    if store.count_open_panes(project_id) >= limit:
        raise DevServiceError(
            "PANE_LIMIT_REACHED",
            f"At most {limit} open panes per project; hide one before adding another.",
            http_status=409,
        )
    if model is not None:
        _require_approved_model(model)
    # Authorization, worktree isolation and persistence of the session all
    # come from the existing Development service.
    session = create_dev_session(project_id, requested_model=model)
    pane_id = store.create_pane(
        project_id=project_id,
        dev_session_id=session["session_id"],
        origin=_origin or ("policy" if plan_id else "manual"),
        role=role,
        title=clean_title,
        model=model,
        plan_id=plan_id,
        plan_agent_key=_plan_agent_key,
        brief=_brief,
    )
    return pane_view(store.get_pane(pane_id))


MIN_PLAN_TURNS, MAX_PLAN_TURNS = 1, 6
MIN_MISSION_TIMEOUT, MAX_MISSION_TIMEOUT = 60, 3600


def approve_plan(
    plan_id: str,
    *,
    agent_models: dict[str, str],
    max_turns_per_agent: int,
    mission_timeout_seconds: int,
    confirm_costs: bool,
) -> dict:
    """Approve a Maestro plan: create every planned pane in one step.

    The request must name the exact model for every agent (an explicit
    change from the plan's recommendation is recorded, never silent). The
    approval also records an execution authorization per pane -- that exact
    model, at most ``max_turns_per_agent`` missions/messages and the mission
    timeout -- so later actions inside those limits need no extra
    confirmation. Nothing starts executing here.
    """
    plan_row = store.get_plan(plan_id)
    if plan_row is None:
        raise DevServiceError("PLAN_NOT_FOUND", f"Unknown plan: {plan_id!r}", http_status=404)
    plan = plan_view(plan_row)
    if plan["status"] != "PROPOSED":
        raise DevServiceError("PLAN_NOT_PROPOSED", f"Plan is {plan['status']}; only a proposed plan can be approved.", http_status=409)
    agents = plan["agents"]
    if not agents:
        raise DevServiceError("PLAN_HAS_NO_AGENTS", "This plan has no agents to create.", http_status=400)
    if not isinstance(agent_models, dict) or set(agent_models) != {agent["key"] for agent in agents}:
        raise DevServiceError("MODELS_NOT_CONFIRMED", "Confirm the exact model for every planned agent.", http_status=400)
    if not MIN_PLAN_TURNS <= max_turns_per_agent <= MAX_PLAN_TURNS:
        raise DevServiceError("INVALID_LIMITS", f"max_turns_per_agent must be {MIN_PLAN_TURNS}..{MAX_PLAN_TURNS}.", http_status=400)
    if not MIN_MISSION_TIMEOUT <= mission_timeout_seconds <= MAX_MISSION_TIMEOUT:
        raise DevServiceError("INVALID_LIMITS", f"mission_timeout_seconds must be {MIN_MISSION_TIMEOUT}..{MAX_MISSION_TIMEOUT}.", http_status=400)
    if len(agents) > policy.max_auto_panes():
        raise DevServiceError("PANE_LIMIT_REACHED", f"At most {policy.max_auto_panes()} agents per plan.", http_status=409)
    if store.count_open_panes(plan["project_id"]) + len(agents) > policy.max_open_panes():
        raise DevServiceError(
            "PANE_LIMIT_REACHED",
            f"Creating {len(agents)} agents would exceed {policy.max_open_panes()} open panes; hide some first.",
            http_status=409,
        )
    chosen = {}
    for agent in agents:
        model = agent_models[agent["key"]]
        row = _require_approved_model(model)
        chosen[agent["key"]] = {
            "model": model,
            "cost_status": row.get("cost_status") or "unknown",
            "recommended": agent.get("model"),
            "changed_by_user": model != agent.get("model"),
        }
    paid = sorted({item["model"] for item in chosen.values() if item["cost_status"] not in {"free", "low_cost"}})
    if paid and not confirm_costs:
        raise DevServiceError(
            "COST_CONFIRMATION_REQUIRED",
            "These routes are not free and need explicit confirmation: " + ", ".join(paid),
            http_status=400,
        )
    approval = {
        "agent_models": chosen,
        "max_turns_per_agent": max_turns_per_agent,
        "mission_timeout_seconds": mission_timeout_seconds,
        "cost_confirmed_for": paid,
        "financial_limit": None,  # not enforceable by this infrastructure; never claimed
        "panes": [],
        "errors": [],
    }
    if not store.claim_plan_for_approval(plan_id, approval):
        raise DevServiceError("PLAN_NOT_PROPOSED", "This plan is already being approved.", http_status=409)
    created = []
    for agent in agents:
        choice = chosen[agent["key"]]
        brief = {
            "task": agent["task"],
            "acceptance_criteria": agent.get("acceptance_criteria") or [],
            "depends_on": agent.get("depends_on") or [],
            "summary": plan.get("summary"),
            "authorization": {
                "plan_id": plan_id,
                "model": choice["model"],
                "cost_status": choice["cost_status"],
                "max_turns": max_turns_per_agent,
                "mission_timeout_seconds": mission_timeout_seconds,
            },
        }
        try:
            pane = create_pane(
                plan["project_id"],
                role=agent["role"],
                title=agent["title"],
                model=choice["model"],
                plan_id=plan_id,
                _origin="maestro",
                _plan_agent_key=agent["key"],
                _brief=brief,
            )
        except DevServiceError as error:
            # Panes already created keep their sessions; nothing is rolled back
            # or deleted. The partial result is recorded and reported.
            approval["errors"].append({"key": agent["key"], "code": error.code, "message": str(error)})
            break
        approval["panes"].append({"key": agent["key"], "pane_id": pane["pane_id"]})
        created.append(pane)
    status = "APPROVED" if not approval["errors"] else ("APPROVED_PARTIAL" if created else "APPROVAL_FAILED")
    store.finish_plan_approval(plan_id, status, approval)
    return {"plan": plan_view(store.get_plan(plan_id)), "panes": created}


def list_panes(project_id: str, *, include_hidden: bool = False) -> list[dict]:
    return [pane_view(row) for row in store.list_panes(project_id, include_hidden=include_hidden)]


def get_pane(pane_id: str) -> dict:
    return pane_view(_require_pane(pane_id))


def update_pane(pane_id: str, *, title: str | None = None, model: str | None = None, layout: dict | None = None) -> dict:
    row = _require_pane(pane_id)
    if title is not None:
        clean = _clean_title(title, row["role"])
        store.update_pane(pane_id, event_kind="RENAMED", event_detail={"from": row["title"], "to": clean}, title=clean)
    if model is not None and model != row["model"]:
        _require_approved_model(model)
        latest = _latest_mission(_missions(row["dev_session_id"]))
        active = latest["mission_id"] if latest and latest["status"] in ACTIVE_MISSION_STATUSES else None
        store.update_pane(
            pane_id,
            event_kind="MODEL_CHANGED",
            event_detail={
                "from": row["model"],
                "to": model,
                "applies_to": "future_turns_and_missions",
                "active_mission_unchanged": active,
            },
            model=model,
        )
    if layout:
        unknown = set(layout) - set(store.LAYOUT_FIELDS)
        if unknown:
            raise DevServiceError("INVALID_LAYOUT", f"Unknown layout fields: {sorted(unknown)}", http_status=400)
        if layout.get("hidden") is False and row["hidden"] and store.count_open_panes(row["project_id"]) >= policy.max_open_panes():
            raise DevServiceError("PANE_LIMIT_REACHED", "Open pane limit reached; hide another pane first.", http_status=409)
        kind = "LAYOUT_CHANGED"
        if "hidden" in layout and bool(layout["hidden"]) != bool(row["hidden"]):
            kind = "HIDDEN" if layout["hidden"] else "RESTORED"
        store.update_pane(pane_id, event_kind=kind, event_detail=dict(layout), **layout)
    return get_pane(pane_id)


def _authorize_use(row, confirm_model: str | None) -> bool:
    """Return True when the plan authorization covers this use.

    Otherwise the caller must re-confirm the pane's exact current model.
    """
    if not row["model"]:
        raise DevServiceError("MODEL_REQUIRED", "Choose a model for this pane first.", http_status=400)
    brief = _brief_view(row)
    if brief and brief["authorization"]["applies"] and confirm_model in (None, row["model"]):
        return True
    if confirm_model != row["model"]:
        raise DevServiceError(
            "MODEL_NOT_CONFIRMED",
            "Confirm the pane's exact current model (no plan authorization covers this use).",
            http_status=400,
        )
    return False


# Serializes "check gates, then start" for panes of the same plan (and for
# each unplanned pane on its own), so two requests cannot both pass the
# sequential check before either has registered its mission or turn. The
# Development executor already enforces one active turn per session and a
# global turn limit; this only adds the per-plan exclusion on top.
_gate_locks: dict[str, threading.Lock] = {}
_gate_locks_guard = threading.Lock()


@contextmanager
def _execution_lock(row):
    key = f"plan:{row['plan_id']}" if row["plan_id"] else f"pane:{row['id']}"
    with _gate_locks_guard:
        lock = _gate_locks.setdefault(key, threading.Lock())
    with lock:
        yield


def _pane_is_executing(pane_row) -> bool:
    """A pane executes while its latest mission is active (including the
    TESTING phase, which has no turn) or while it has an active turn."""
    from nexus.registry import dev_sessions

    latest = _latest_mission(_missions(pane_row["dev_session_id"]))
    if latest and latest["status"] in ACTIVE_MISSION_STATUSES:
        return True
    return dev_sessions.get_active_turn_for_session(pane_row["dev_session_id"]) is not None


def _check_execution_gates(row, confirm_model: str | None) -> bool:
    """Every write-capable action (mission or direct message) passes here.

    1. Dependencies of a planned agent must be READY_FOR_REVIEW.
    2. maestro-plan/1 declares sequential execution: no other agent of the
       same plan may be executing, with or without explicit depends_on.
    3. The plan authorization or an explicit model confirmation must cover
       the use (returns True when the plan authorization is used).
    """
    if _pane_is_executing(row):
        raise DevServiceError("PANE_BUSY", "This agent is already executing; wait for it to finish.", http_status=409)
    if any(not item["ready"] for item in _dependency_status(row)):
        raise DevServiceError(
            "DEPENDENCY_PENDING",
            "This agent depends on agents whose work is not ready for review yet.",
            http_status=409,
        )
    if row["plan_id"]:
        busy = [
            other["title"]
            for other in store.panes_for_plan(row["plan_id"])
            if other["id"] != row["id"] and _pane_is_executing(other)
        ]
        if busy:
            raise DevServiceError(
                "PLAN_AGENT_RUNNING",
                "Agents of this plan run one at a time; wait for: " + ", ".join(busy),
                http_status=409,
            )
    return _authorize_use(row, confirm_model)


def _run_gated(row, confirm_model: str | None, start: Callable[[bool], dict]) -> tuple[bool, dict]:
    with _execution_lock(row):
        by_plan = _check_execution_gates(row, confirm_model)
        return by_plan, start(by_plan)


def _agent_context(row) -> str:
    lines = [f"[Workspace agent: {row['title']} - role {policy.ROLES.get(row['role'], row['role'])}]"]
    brief = _brief(row)
    if brief:
        lines.append(f"Planned task: {brief.get('task')}")
        criteria = brief.get("acceptance_criteria") or []
        if criteria:
            lines.append("Acceptance criteria:\n" + "\n".join(f"- {item}" for item in criteria))
    return "\n".join(lines) + "\n"


def start_pane_mission(
    pane_id: str,
    description: str | None,
    *,
    confirm_model: str | None,
    test_profile: str,
    timeout_seconds: int | None = None,
) -> dict:
    """Start one mission in the pane's own session with the pane's model.

    Without an applicable plan authorization, ``confirm_model`` must equal
    the pane's current model. A planned pane whose dependencies are not
    READY_FOR_REVIEW yet is refused: P2A runs planned agents one after
    another and never lets two of them edit related code at the same time.
    """
    from nexus.dev.mission_runner import start_mission

    row = _require_pane(pane_id)
    brief = _brief(row)
    text = (description or "").strip()
    if not text and brief:
        text = "Execute the planned task above and satisfy every acceptance criterion."
    if not text:
        raise DevServiceError("EMPTY_MISSION", "description must not be empty.", http_status=400)

    def start(by_plan: bool) -> dict:
        approved = int(((brief or {}).get("authorization") or {}).get("mission_timeout_seconds") or 0)
        effective = timeout_seconds
        if by_plan:
            # The approved duration is a ceiling: a longer mission needs a new
            # explicit authorization (an explicit model confirmation once the
            # plan authorization no longer applies, or a new plan).
            if effective is None:
                effective = approved
            elif effective > approved:
                raise DevServiceError(
                    "TIMEOUT_EXCEEDS_AUTHORIZATION",
                    f"The plan authorized at most {approved} seconds per mission.",
                    http_status=403,
                )
        elif effective is None:
            effective = approved or 900
        return start_mission(
            row["dev_session_id"],
            _agent_context(row) + text,
            model=row["model"],
            allowed_models=[row["model"]],
            test_profile=test_profile,
            timeout_seconds=effective,
        )

    by_plan, mission = _run_gated(row, confirm_model, start)
    store.record_event(
        pane_id,
        "MISSION_STARTED",
        {"mission_id": mission["mission_id"], "model": row["model"], "status": mission["status"], "authorized_by_plan": by_plan},
    )
    return {"pane": get_pane(pane_id), "mission": mission}


def _snapshot(dev_session_id: str) -> list:
    """Content-addressed view of the worktree diff, to detect any change."""
    from nexus.dev import executor

    try:
        diff = executor.review_diff(dev_session_id)
    except DevServiceError:
        return [["?", "unavailable", None]]
    return sorted([file["path"], file["status"], file.get("content_sha256")] for file in diff.get("files", []))


def send_pane_message(pane_id: str, text: str, *, confirm_model: str | None = None) -> dict:
    """Send one direct message to an agent as a real Development turn.

    The turn runs in the pane's own worktree (Development Mode rules apply:
    execution gate, authorization, one active turn per session). Before and
    after snapshots of the diff classify the result as conversation-only or
    as code changes that still need verification.
    """
    from nexus.dev import executor, feature_gate

    row = _require_pane(pane_id)
    if not isinstance(text, str) or not text.strip():
        raise DevServiceError("EMPTY_MESSAGE", "message must not be empty.", http_status=400)
    if not feature_gate.execution_enabled():
        raise DevServiceError("DEV_EXECUTION_DISABLED", "Development Mode execution is disabled on this server.", http_status=403)
    holder: dict = {}

    def start(_by_plan: bool) -> dict:
        before = _snapshot(row["dev_session_id"])
        holder["message_id"] = store.add_pane_message(pane_id, text=text.strip(), model=row["model"], changes_before=before)
        try:
            return executor.run_dev_turn(row["dev_session_id"], _agent_context(row) + text.strip(), model=row["model"])
        except DevServiceError:
            store.finish_pane_message(holder["message_id"], outcome="failed", changes_after=None)
            raise

    by_plan, turn = _run_gated(row, confirm_model, start)
    message_id = holder["message_id"]
    store.set_pane_message_turn(message_id, turn["turn_id"])
    store.record_event(pane_id, "MESSAGE_SENT", {"message_id": message_id, "model": row["model"], "authorized_by_plan": by_plan})
    return get_pane(pane_id)


TERMINAL_TURN_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED", "OUTCOME_UNKNOWN"})


def pane_messages(row, limit: int = 20) -> list[dict]:
    from nexus.dev import executor

    out = []
    for message in store.list_pane_messages(row["id"], limit=limit):
        turn = None
        if message["dev_turn_id"]:
            try:
                turn = executor.get_turn_detail(row["dev_session_id"], message["dev_turn_id"])
            except DevServiceError:
                turn = None
        outcome = message["outcome"]
        if outcome is None and turn is not None and turn["status"] in TERMINAL_TURN_STATUSES:
            if turn["status"] == "COMPLETED":
                after = _snapshot(row["dev_session_id"])
                before = json.loads(message["changes_before_json"])
                outcome = "conversation" if after == before else "code_changed"
                store.finish_pane_message(message["id"], outcome=outcome, changes_after=after)
            else:
                outcome = "cancelled" if turn["status"] in {"CANCELLED", "INTERRUPTED"} else "failed"
                store.finish_pane_message(message["id"], outcome=outcome, changes_after=None)
        out.append({
            "message_id": message["id"],
            "text": message["text"],
            "model": message["model"],
            "status": turn["status"] if turn else ("FAILED" if outcome == "failed" else "PENDING"),
            "response_text": turn["response_text"] if turn else None,
            "error_code": turn["error_code"] if turn else None,
            "outcome": outcome,
            "created_at": message["created_at"],
        })
    return out


def pane_history(pane_id: str) -> dict:
    row = _require_pane(pane_id)
    events = [
        {"id": event["id"], "kind": event["kind"], "detail": json.loads(event["detail_json"]), "created_at": event["created_at"]}
        for event in store.list_events(pane_id)
    ]
    return {"pane_id": pane_id, "events": events, "missions": _missions(row["dev_session_id"])}


def workspace_state(project_id: str) -> dict:
    from nexus.dev import feature_gate, integration_gate
    from nexus.workspace import coordinator

    return {
        "project_id": project_id,
        "panes": list_panes(project_id),
        "hidden_panes": [pane for pane in list_panes(project_id, include_hidden=True) if pane["layout"]["hidden"]],
        "limits": {"max_open_panes": policy.max_open_panes(), "max_auto_panes": policy.max_auto_panes()},
        "roles": policy.ROLES,
        "execution_enabled": feature_gate.execution_enabled(),
        "integration_enabled": integration_gate.integration_enabled(),
        "coordination": coordinator.project_state(project_id),
        "policy_version": policy.POLICY_VERSION,
    }
