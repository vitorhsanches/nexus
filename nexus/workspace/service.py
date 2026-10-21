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
        "busy": bool(latest and latest["status"] in ACTIVE_MISSION_STATUSES),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


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
    return {
        "plan_id": row["id"],
        "project_id": row["project_id"],
        "task_text": row["task_text"],
        "created_at": row["created_at"],
        **json.loads(row["proposal_json"]),
    }


def list_plans(project_id: str) -> list[dict]:
    return [plan_view(row) for row in store.list_plans(project_id)]


def create_pane(
    project_id: str,
    *,
    role: str,
    title: str | None = None,
    model: str | None = None,
    plan_id: str | None = None,
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
        origin="policy" if plan_id else "manual",
        role=role,
        title=clean_title,
        model=model,
        plan_id=plan_id,
    )
    return pane_view(store.get_pane(pane_id))


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
                "applies_to": "future_missions",
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


def start_pane_mission(
    pane_id: str,
    description: str,
    *,
    confirm_model: str,
    test_profile: str,
    timeout_seconds: int = 900,
) -> dict:
    """Start one mission in the pane's own session with the pane's model.

    ``confirm_model`` must equal the pane's current model: the caller
    re-confirms the exact route (and therefore its cost) for every mission.
    """
    from nexus.dev.mission_runner import start_mission

    row = _require_pane(pane_id)
    if not row["model"]:
        raise DevServiceError("MODEL_REQUIRED", "Choose a model for this pane before starting a mission.", http_status=400)
    if confirm_model != row["model"]:
        raise DevServiceError(
            "MODEL_NOT_CONFIRMED",
            "The confirmed model does not match the pane's current model.",
            http_status=400,
        )
    if not isinstance(description, str) or not description.strip():
        raise DevServiceError("EMPTY_MISSION", "description must not be empty.", http_status=400)
    role_context = (
        f"[Workspace agent: {row['title']} - role {policy.ROLES.get(row['role'], row['role'])}]\n"
    )
    mission = start_mission(
        row["dev_session_id"],
        role_context + description.strip(),
        model=row["model"],
        allowed_models=[confirm_model],
        test_profile=test_profile,
        timeout_seconds=timeout_seconds,
    )
    store.record_event(
        pane_id,
        "MISSION_STARTED",
        {"mission_id": mission["mission_id"], "model": row["model"], "status": mission["status"]},
    )
    return {"pane": get_pane(pane_id), "mission": mission}


def pane_history(pane_id: str) -> dict:
    row = _require_pane(pane_id)
    events = [
        {"id": event["id"], "kind": event["kind"], "detail": json.loads(event["detail_json"]), "created_at": event["created_at"]}
        for event in store.list_events(pane_id)
    ]
    return {"pane_id": pane_id, "events": events, "missions": _missions(row["dev_session_id"])}


def workspace_state(project_id: str) -> dict:
    from nexus.dev import feature_gate, integration_gate

    return {
        "project_id": project_id,
        "panes": list_panes(project_id),
        "hidden_panes": [pane for pane in list_panes(project_id, include_hidden=True) if pane["layout"]["hidden"]],
        "limits": {"max_open_panes": policy.max_open_panes(), "max_auto_panes": policy.max_auto_panes()},
        "roles": policy.ROLES,
        "execution_enabled": feature_gate.execution_enabled(),
        "integration_enabled": integration_gate.integration_enabled(),
        "policy_version": policy.POLICY_VERSION,
    }
