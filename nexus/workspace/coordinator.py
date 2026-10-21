"""Durable, sequential coordination for approved Workspace plans (P2B.1).

This module coordinates existing Development missions. It never implements a
second executor and never transfers, applies, merges or pushes worktree data.
When a dependency has changes in its isolated worktree, the next step stops
with an explicit handoff block because P2B.1 has no safe transfer mechanism.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Iterable

from nexus.dev.service import DevServiceError
from nexus.registry import dev_missions
from nexus.registry import workspace as store
from nexus.registry import workspace_execution as execution_store

_workers: dict[str, threading.Thread] = {}
_workers_lock = threading.Lock()
ACTIVE_MISSION_STATUSES = frozenset({"PENDING", "RUNNING", "TESTING", "REPAIRING"})


def _fail(code: str, message: str, status: int = 409):
    raise DevServiceError(code, message, http_status=status)


def _approval(plan) -> dict:
    try:
        return json.loads(plan["approval_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _step_views(plan) -> list[dict]:
    panes = {pane["plan_agent_key"]: pane for pane in store.panes_for_plan(plan["id"])}
    proposal = json.loads(plan["proposal_json"])
    agents = proposal.get("agents") or []
    return [{"agent_key": agent["key"], "ordinal": index, "pane_id": panes[agent["key"]]["id"]}
            for index, agent in enumerate(agents) if agent["key"] in panes]


def authorize_execution(
    plan_id: str,
    *,
    agent_keys: Iterable[str],
    test_profiles: dict[str, str],
    max_turns_per_agent: int,
    mission_timeout_seconds: int,
    confirm_costs: bool = False,
) -> dict:
    """Authorize exactly the already approved plan for sequential execution."""
    plan = store.get_plan(plan_id)
    if plan is None:
        _fail("PLAN_NOT_FOUND", f"Unknown plan: {plan_id!r}", 404)
    if plan["status"] != "APPROVED":
        _fail("PLAN_NOT_APPROVED", "Only a fully approved plan can be executed.")
    approval = _approval(plan)
    planned = {agent["key"]: agent for agent in json.loads(plan["proposal_json"]).get("agents", [])}
    keys = list(agent_keys)
    expected_order = list(planned)
    if keys != expected_order:
        _fail("AGENTS_NOT_CONFIRMED", "Execution must use the exact approved agent order.", 400)
    if set(test_profiles) != set(planned) or any(not isinstance(value, str) or not value.strip() for value in test_profiles.values()):
        _fail("TEST_PROFILES_NOT_CONFIRMED", "Choose one trusted test profile for every agent.", 400)
    if max_turns_per_agent != approval.get("max_turns_per_agent") or mission_timeout_seconds != approval.get("mission_timeout_seconds"):
        _fail("LIMITS_NOT_CONFIRMED", "Execution limits must exactly match the plan approval.", 400)
    selected = approval.get("agent_models") or {}
    panes = {pane["plan_agent_key"]: pane for pane in store.panes_for_plan(plan_id)}
    if set(panes) != set(planned) or any(panes[key]["model"] != selected.get(key, {}).get("model") for key in planned):
        _fail("PANES_NOT_READY", "The approved plan does not have the exact authorized agents and models.")
    from nexus.dev.test_profiles import list_test_profiles
    available = {row["id"] for row in list_test_profiles(plan["project_id"])}
    missing = sorted(set(test_profiles.values()) - available)
    if missing:
        _fail("TEST_PROFILE_NOT_FOUND", "Trusted test profile unavailable: " + ", ".join(missing), 409)
    paid = [key for key, value in selected.items() if value.get("cost_status") not in {"free", "low_cost"}]
    if paid and not (confirm_costs or set(paid).issubset(set(approval.get("cost_confirmed_for") or []))):
        _fail("COST_CONFIRMATION_REQUIRED", "Execution needs explicit cost confirmation for the selected routes.", 400)
    steps = [
        {"agent_key": key, "ordinal": index, "pane_id": panes[key]["id"], "test_profile": test_profiles[key]}
        for index, key in enumerate(keys)
    ]
    try:
        execution_store.create_execution(
            plan_id, plan["project_id"], steps,
            max_turns_per_agent=max_turns_per_agent,
            mission_timeout_seconds=mission_timeout_seconds,
        )
    except ValueError as error:
        _fail("EXECUTION_ALREADY_AUTHORIZED", str(error), 409)
    return execution_store.view(plan_id)


def _mission_for(step: dict):
    mission_id = step["mission_id"] if step is not None else None
    if not mission_id:
        return None
    return dev_missions.get_mission(mission_id)


def _changed_files(mission) -> list:
    if mission is None:
        return []
    try:
        result = json.loads(mission["result_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        result = {}
    return result.get("changed_files") or []


def _is_interrupted(plan_id: str) -> bool:
    row = execution_store.get_execution(plan_id)
    return bool(row and row["interrupt_requested"])


def _step_result(mission) -> dict:
    try:
        return json.loads(mission["result_json"] or "{}") if mission else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _run(plan_id: str) -> None:
    from nexus.workspace import service

    try:
        execution = execution_store.view(plan_id)
        if execution is None:
            return
        for index, step in enumerate(execution["steps"]):
            if _is_interrupted(plan_id):
                execution_store.update_execution(plan_id, "INTERRUPTED", current_index=index)
                return
            persisted = execution_store.get_steps(plan_id)[index]
            if persisted["status"] in {"READY_FOR_REVIEW", "BLOCKED", "FAILED", "INTERRUPTED", "RESULT_UNCERTAIN"}:
                if persisted["status"] != "READY_FOR_REVIEW":
                    return
                continue
            if index:
                previous = execution_store.get_steps(plan_id)[index - 1]
                previous_mission = _mission_for(previous)
                if previous["status"] != "READY_FOR_REVIEW":
                    execution_store.update_execution(plan_id, "WAITING_DEPENDENCY", current_index=index)
                    return
                if _changed_files(previous_mission):
                    message = "Dependência concluída em outro worktree; transferência de alterações não é suportada nesta fase."
                    execution_store.update_step(plan_id, step["agent_key"], "BLOCKED", error_code="DEPENDENCY_WORKTREE_NOT_TRANSFERRED", error_message=message)
                    execution_store.update_execution(plan_id, "BLOCKED", current_index=index, error_code="DEPENDENCY_WORKTREE_NOT_TRANSFERRED", error_message=message)
                    return
            execution_store.update_execution(plan_id, "RUNNING", current_index=index)
            execution_store.update_step(plan_id, step["agent_key"], "STARTING")
            try:
                result = service.start_pane_mission(
                    step["pane_id"], None, confirm_model=None,
                    test_profile=step["test_profile"], timeout_seconds=None,
                )
            except DevServiceError as error:
                execution_store.update_step(plan_id, step["agent_key"], "BLOCKED" if error.code in {"DEPENDENCY_PENDING", "PANE_BUSY", "TEST_PROFILE_NOT_FOUND"} else "FAILED", error_code=error.code, error_message=str(error))
                execution_store.update_execution(plan_id, "BLOCKED" if error.code in {"DEPENDENCY_PENDING", "PANE_BUSY", "TEST_PROFILE_NOT_FOUND"} else "FAILED", current_index=index, error_code=error.code, error_message=str(error))
                return
            mission_id = result["mission"]["mission_id"]
            execution_store.update_step(plan_id, step["agent_key"], "RUNNING", mission_id=mission_id)
            while True:
                if _is_interrupted(plan_id):
                    try:
                        from nexus.dev import mission_runner
                        mission_runner.interrupt_mission(
                            result["pane"]["session"]["session_id"], mission_id
                        )
                    except DevServiceError:
                        pass
                mission = dev_missions.get_mission(mission_id)
                if mission is None or mission["status"] in ACTIVE_MISSION_STATUSES:
                    time.sleep(0.2)
                    continue
                status = mission["status"]
                if status == "READY_FOR_REVIEW":
                    execution_store.update_step(plan_id, step["agent_key"], status, result=_step_result(mission))
                    break
                if status in {"CANCELLED", "INTERRUPTED"}:
                    execution_store.update_step(plan_id, step["agent_key"], "INTERRUPTED", result=_step_result(mission), error_code=mission["error_code"], error_message=mission["error_message"])
                    execution_store.update_execution(plan_id, "INTERRUPTED", current_index=index, error_code="MISSION_INTERRUPTED", error_message=mission["error_message"])
                    return
                if status == "OUTCOME_UNKNOWN":
                    execution_store.update_step(plan_id, step["agent_key"], status, result=_step_result(mission), error_code=mission["error_code"], error_message=mission["error_message"])
                    execution_store.update_execution(plan_id, "RESULT_UNCERTAIN", current_index=index, error_code="MISSION_OUTCOME_UNKNOWN", error_message="Mission outcome is unknown; it was not replayed.")
                    return
                execution_store.update_step(plan_id, step["agent_key"], "FAILED", result=_step_result(mission), error_code=mission["error_code"], error_message=mission["error_message"])
                execution_store.update_execution(plan_id, "FAILED", current_index=index, error_code=mission["error_code"] or "MISSION_FAILED", error_message=mission["error_message"])
                return
        execution_store.update_execution(plan_id, "AWAITING_REVIEW", current_index=len(execution["steps"]), result={"human_approval_required": True, "integration_applied": False})
    except Exception as error:  # noqa: BLE001 - durable failure boundary
        execution_store.update_execution(plan_id, "RESULT_UNCERTAIN", error_code="COORDINATOR_INTERRUPTED", error_message=str(error))
    finally:
        with _workers_lock:
            _workers.pop(plan_id, None)


def start(plan_id: str) -> dict:
    execution = execution_store.view(plan_id)
    if execution is None:
        _fail("EXECUTION_NOT_AUTHORIZED", "Authorize this plan before starting it.")
    if execution["status"] == "AWAITING_EXECUTION":
        if not execution_store.claim_execution(plan_id):
            return execution_store.view(plan_id)
    elif execution["status"] != "RUNNING":
        return execution
    else:
        owner = execution.get("owner") or {}
        current_owner = {"host": socket.gethostname(), "pid": os.getpid()}
        if owner and owner != current_owner:
            # A different process may still own the durable worker. Fail closed
            # instead of creating a second monitor that could advance the plan.
            return execution
    with _workers_lock:
        worker = _workers.get(plan_id)
        if worker is None or not worker.is_alive():
            worker = threading.Thread(target=_run, args=(plan_id,), daemon=True, name=f"nexus-plan-{plan_id}")
            _workers[plan_id] = worker
            worker.start()
    return execution_store.view(plan_id)


def interrupt(plan_id: str) -> dict:
    execution = execution_store.view(plan_id)
    if execution is None:
        _fail("EXECUTION_NOT_AUTHORIZED", "This plan has no execution authorization.")
    if execution["status"] in {"AWAITING_REVIEW", "BLOCKED", "FAILED", "INTERRUPTED", "RESULT_UNCERTAIN"}:
        return execution
    execution_store.request_interrupt(plan_id)
    return execution_store.view(plan_id)


def recover(plan_id: str) -> dict:
    """Resume a stale durable worker without replaying an identified mission."""
    execution = state(plan_id)
    if execution["status"] != "RUNNING":
        return execution
    if not execution_store.claim_recovery(plan_id):
        return execution_store.view(plan_id)
    with _workers_lock:
        worker = _workers.get(plan_id)
        if worker is None or not worker.is_alive():
            worker = threading.Thread(target=_run, args=(plan_id,), daemon=True, name=f"nexus-plan-{plan_id}")
            _workers[plan_id] = worker
            worker.start()
    return execution_store.view(plan_id)


def state(plan_id: str) -> dict:
    execution = execution_store.view(plan_id)
    if execution is None:
        _fail("EXECUTION_NOT_AUTHORIZED", "This plan has no execution authorization.")
    # STARTING is a deliberate crash boundary: no mission id means the
    # process could have died before the Mission Runner accepted the launch.
    # Marking it uncertain prevents a restart from replaying the agent turn.
    for step in execution["steps"]:
        if step["status"] == "STARTING" and not step["mission_id"]:
            message = "Coordinator stopped before the mission was durably identified; it was not replayed."
            execution_store.update_step(plan_id, step["agent_key"], "RESULT_UNCERTAIN",
                                        error_code="COORDINATOR_START_INTERRUPTED", error_message=message)
            execution_store.update_execution(plan_id, "RESULT_UNCERTAIN",
                                             current_index=step["ordinal"],
                                             error_code="COORDINATOR_START_INTERRUPTED",
                                             error_message=message)
            break
    execution = execution_store.view(plan_id)
    return execution


def project_state(project_id: str) -> list[dict]:
    return [execution_store.view(row["plan_id"]) for row in execution_store.list_executions(project_id)]
