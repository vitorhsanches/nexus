"""Durable, sequential coordination for approved Workspace plans (P2B.1).

This module coordinates existing Development missions. It never implements a
second executor and never transfers, applies, merges or pushes worktree data.
When a dependency has changes in its isolated worktree, the next step stops
until an operator approves a verified handoff and explicitly resumes it.
"""

from __future__ import annotations

import json
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
    if not isinstance(proposal, dict):
        _fail("RESUME_AUTHORIZATION_INVALID", "Persisted plan proposal is invalid.")
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
    selected = approval.get("agent_models") or {}
    panes = {pane["plan_agent_key"]: pane for pane in store.panes_for_plan(plan_id)}
    if (not isinstance(selected, dict) or set(selected) != set(planned)
            or any(not isinstance(selected[key], dict) for key in planned)
            or set(panes) != set(planned)
            or any(
                panes[key]["model"] != selected[key].get("model")
                for key in planned
            )):
        _fail("PANES_NOT_READY", "The approved plan does not have the exact authorized agents and models.")
    from nexus.dev.executor import session_backend
    from nexus.registry import dev_sessions
    from nexus.workspace import service

    # P5.2B: a Codex Native agent has no trusted test profile to confirm --
    # never invent a fictitious one for it, and never silently accept one
    # a caller mistakenly supplies for it either.
    if not isinstance(test_profiles, dict) or any(
        not isinstance(key, str) for key in test_profiles
    ):
        _fail(
            "TEST_PROFILES_NOT_CONFIRMED",
            "Test profiles must be keyed by approved agent.", 400,
        )
    native_keys = set()
    for key, pane in panes.items():
        session = dev_sessions.get_session(pane["dev_session_id"])
        if session is None or session["project_id"] != plan["project_id"]:
            _fail("PANES_NOT_READY", "Approved pane has no Development session.")
        actual_backend = session_backend(session)
        expected_backend = selected[key].get(
            "backend", approval.get("backend", "omniroute")
        )
        if actual_backend != expected_backend:
            _fail(
                "SESSION_BACKEND_MISMATCH",
                "Session backend differs from the approved backend.",
            )
        if actual_backend == "codex_native":
            native_keys.add(key)
        elif actual_backend != "omniroute":
            _fail("UNSUPPORTED_BACKEND", "Persisted backend is not supported.")
    if set(test_profiles) & native_keys:
        _fail("TEST_PROFILE_NOT_APPLICABLE", "Codex Native agents have no test profile; omit them.", 400)
    required_profiles = set(planned) - native_keys
    if set(test_profiles) != required_profiles or any(not isinstance(value, str) or not value.strip() for value in test_profiles.values()):
        _fail("TEST_PROFILES_NOT_CONFIRMED", "Choose one trusted test profile for every OmniRoute agent.", 400)
    if max_turns_per_agent != approval.get("max_turns_per_agent") or mission_timeout_seconds != approval.get("mission_timeout_seconds"):
        _fail("LIMITS_NOT_CONFIRMED", "Execution limits must exactly match the plan approval.", 400)
    if required_profiles:
        from nexus.dev.test_profiles import list_test_profiles
        available = {row["id"] for row in list_test_profiles(plan["project_id"])}
        missing = sorted(set(test_profiles.values()) - available)
        if missing:
            _fail(
                "TEST_PROFILE_NOT_FOUND",
                "Trusted test profile unavailable: " + ", ".join(missing), 409,
            )
    if len(selected) != len(planned) or any(
        not isinstance(selected.get(key), dict) for key in planned
    ):
        _fail("MODEL_NOT_AVAILABLE", "Approved model is unavailable.", 409)
    charged = set()
    for key in keys:
        model = selected[key]["model"]
        backend = "codex_native" if key in native_keys else "omniroute"
        try:
            live = service._require_backend_model(model, backend)
        except DevServiceError:
            _fail("MODEL_NOT_AVAILABLE", "Approved model is unavailable.", 409)
        if live.get("cost_status") != "free":
            charged.add(model)
    confirmed = approval.get("cost_confirmed_for")
    confirmed_ids = {item for item in confirmed if isinstance(item, str)} if isinstance(confirmed, list) else set()
    missing_consent = charged - confirmed_ids
    if missing_consent and not confirm_costs:
        _fail("COST_CONFIRMATION_REQUIRED", "Execution needs explicit cost confirmation for the selected routes.", 400)
    steps = [
        {"agent_key": key, "ordinal": index, "pane_id": panes[key]["id"],
         "test_profile": test_profiles.get(key, "")}
        for index, key in enumerate(keys)
    ]
    try:
        execution_store.create_execution(
            plan_id, plan["project_id"], steps,
            max_turns_per_agent=max_turns_per_agent,
            mission_timeout_seconds=mission_timeout_seconds,
            confirmed_model_ids=sorted(missing_consent) if confirm_costs else [],
            expected_agent_models=selected,
        )
    except ValueError as error:
        code = ("EXECUTION_AUTHORIZATION_CHANGED" if "changed before" in str(error)
                else "EXECUTION_ALREADY_AUTHORIZED")
        _fail(code, str(error), 409)
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


def _native_agent_keys(steps) -> set:
    """Which plan steps' panes are, right now, Codex Native -- queried fresh
    (never inferred from a step's own evidence columns, which are empty for
    a step that has not launched yet)."""
    from nexus.chat.appserver_client import BACKEND_CODEX_NATIVE
    from nexus.dev.executor import session_backend
    from nexus.registry import dev_sessions

    keys = set()
    for step in steps:
        pane = store.get_pane(step["pane_id"])
        if pane is None:
            continue
        session = dev_sessions.get_session(pane["dev_session_id"])
        if session is not None and session_backend(session) == BACKEND_CODEX_NATIVE:
            keys.add(step["agent_key"])
    return keys


def _native_changed_files(step: dict, pane) -> list:
    """Generic native evidence reader (P5.3B), mirroring _changed_files's
    role for the legacy OmniRoute path: it tells the coordinator whether a
    verified handoff is required before the next agent can launch. Unlike
    _changed_files, a missing or invalid review is never read as "no
    changes" -- that silent-bypass shape is exactly what P5.2C's
    NATIVE_HANDOFF_NOT_SUPPORTED guard was added to prevent. It blocks
    instead, by raising. Staleness re-verification happens later, only if
    a handoff actually becomes required, inside handoff.verified_dependency.
    """
    from nexus.registry import dev_integrations, dev_sessions

    review_id = step["native_review_id"] if step is not None else None
    if not review_id:
        _fail("NATIVE_REVIEW_EVIDENCE_MISSING", "The completed native step has no bound review.")
    session = dev_sessions.get_session(pane["dev_session_id"]) if pane is not None else None
    if session is None:
        _fail("SESSION_NOT_FOUND", "The source agent's development session no longer exists.", 404)
    review = dev_integrations.get_review(review_id)
    if (review is None or review["dev_session_id"] != session["id"]
            or review["status"] != "REVIEWED" or review["error_code"]):
        _fail("NATIVE_REVIEW_EVIDENCE_INVALID", "The bound native review is not conclusively ready.")
    try:
        snapshot = review["snapshot"]
        if not snapshot.get("approval_ready"):
            _fail("NATIVE_REVIEW_EVIDENCE_INVALID", "The bound native review's snapshot is incomplete.")
        changed = (snapshot.get("diff") or {}).get("files")
    except (TypeError, AttributeError):
        _fail("NATIVE_REVIEW_EVIDENCE_INVALID", "The bound native review's snapshot is invalid.")
    if changed is None:
        _fail("NATIVE_REVIEW_EVIDENCE_INVALID", "The bound native review has no change evidence.")
    return changed


def _is_interrupted(plan_id: str) -> bool:
    row = execution_store.get_execution(plan_id)
    return bool(row and row["interrupt_requested"])


def _step_result(mission) -> dict:
    try:
        return json.loads(mission["result_json"] or "{}") if mission else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _resume_evidence(plan_id: str, execution: dict) -> dict:
    """Read-only eligibility for the one legacy handoff block we can resume."""
    from nexus.dev.models import list_dev_models
    from nexus.dev.test_profiles import list_test_profiles
    from nexus.registry import workspace_handoff
    from nexus.workspace import handoff, service

    plan = store.get_plan(plan_id)
    if plan is None:
        _fail("PLAN_NOT_FOUND", "The plan no longer exists.", 404)
    if plan["status"] != "APPROVED" or not plan["approval_json"]:
        _fail("RESUME_AUTHORIZATION_INVALID", "The plan approval is no longer valid.")
    steps = execution["steps"]
    index = execution["current_index"]
    if (execution["status"] != "BLOCKED"
            or execution["error_code"] != "DEPENDENCY_WORKTREE_NOT_TRANSFERRED"
            or execution["interrupt_requested"] or not isinstance(index, int)
            or index < 1 or index >= len(steps)):
        _fail("RESUME_NOT_ALLOWED", "Only an unresolved handoff block can be resumed.")
    pending = steps[index]
    previous = steps[index - 1]
    if (pending["status"] != "BLOCKED" or pending["error_code"] != execution["error_code"]
            or pending["mission_id"] or pending["native_review_id"] or pending["native_turn_id"]
            or pending["started_at"] or pending["result"] is not None
            or any(step["status"] != "READY_FOR_REVIEW" or not (step["mission_id"] or step["native_review_id"]) or step["error_code"]
                   for step in steps[:index])
            or any(step["status"] != "PENDING" or step["mission_id"] or step["native_review_id"] or step["native_turn_id"]
                   for step in steps[index + 1:])):
        _fail("RESUME_STEP_UNCERTAIN", "A step was started or its durable history is incomplete.")
    if execution["agent_keys"] != [step["agent_key"] for step in steps]:
        _fail("RESUME_AUTHORIZATION_INVALID", "Approved agent order changed.")
    approval = _approval(plan)
    try:
        proposal = json.loads(plan["proposal_json"])
    except (TypeError, ValueError):
        _fail("RESUME_AUTHORIZATION_INVALID", "Persisted plan proposal is invalid.")
    agents = proposal.get("agents") or []
    if (not isinstance(agents, list) or any(not isinstance(agent, dict) for agent in agents)
            or [agent.get("key") for agent in agents] != execution["agent_keys"]
            or approval.get("max_turns_per_agent") != execution["max_turns_per_agent"]
            or approval.get("mission_timeout_seconds") != execution["mission_timeout_seconds"]):
        _fail("RESUME_AUTHORIZATION_INVALID", "Persisted execution no longer matches the plan approval.")
    if (agents[index].get("depends_on") or []) != [previous["agent_key"]]:
        _fail("RESUME_TOPOLOGY_UNSUPPORTED", "Resume supports one declared dependency on the previous agent.")
    # P5.3B: a Codex Native agent has no trusted test profile (mirrors the
    # same split authorize_execution already enforces at authorization
    # time) and, like the model check below, its backend must not have
    # silently changed since then. test_profiles always has an entry for
    # every agent key (authorize_execution stores "" for a native one, never
    # omits the key) -- a completed step's own recorded evidence type is
    # the actual backend-consistency signal.
    native_keys = _native_agent_keys(steps)
    if any(step["mission_id"] and step["agent_key"] in native_keys
           or step["native_review_id"] and step["agent_key"] not in native_keys
           for step in steps):
        _fail("RESUME_AUTHORIZATION_INVALID", "An agent's backend changed since authorization.")
    available_profiles = {item["id"] for item in list_test_profiles(plan["project_id"])}
    if any(
        (step["agent_key"] in native_keys and step["test_profile"])
        or (step["agent_key"] not in native_keys and (
            step["test_profile"] != execution["test_profiles"].get(step["agent_key"])
            or step["test_profile"] not in available_profiles))
        for step in steps
    ):
        _fail("RESUME_AUTHORIZATION_INVALID", "A trusted test profile changed or disappeared.")
    catalog = {item["id"]: item for item in list_dev_models()["models"]}
    selected = approval.get("agent_models") or {}
    if not isinstance(selected, dict):
        _fail("RESUME_AUTHORIZATION_INVALID", "Approved model map is invalid.")
    confirmed = approval.get("cost_confirmed_for")
    confirmed_ids = set(confirmed) if isinstance(confirmed, list) else set()
    for step in steps:
        pane = store.get_pane(step["pane_id"])
        choice = selected.get(step["agent_key"])
        model = choice.get("model") if isinstance(choice, dict) else None
        if (pane is None or pane["plan_id"] != plan_id or pane["project_id"] != plan["project_id"]
                or pane["plan_agent_key"] != step["agent_key"] or pane["model"] != model
                or not isinstance(model, str)
                or model not in catalog
                or (catalog[model].get("cost_status") != "free" and model not in confirmed_ids)):
            _fail("RESUME_AUTHORIZATION_INVALID", "An exact model or its cost consent is invalid.")
    pending_pane = store.get_pane(pending["pane_id"])
    try:
        brief = service._brief_view(pending_pane)
    except (TypeError, ValueError, KeyError):
        _fail("RESUME_AUTHORIZATION_INVALID", "Pending agent authorization is invalid.")
    if not brief or not brief["authorization"]["applies"]:
        _fail("RESUME_AUTHORIZATION_INVALID", "The pending agent's plan authorization is exhausted.")
    if workspace_handoff.target_has_unresolved_transfer(pending_pane["dev_session_id"]):
        _fail("RESUME_TARGET_UNCERTAIN", "The destination has an unresolved transfer.")
    record = handoff.verified_dependency(plan_id, previous, pending)
    return {"eligible": True, "pending_agent": pending["agent_key"],
            "completed_agent": previous["agent_key"], "handoff_id": record["handoff_id"]}


def resume_eligibility(plan_id: str, execution: dict | None = None) -> dict:
    """Status endpoint helper: inspect without reserving or launching work."""
    execution = execution if execution is not None else execution_store.view(plan_id)
    if execution is None:
        return {"eligible": False, "code": "EXECUTION_NOT_AUTHORIZED"}
    try:
        return _resume_evidence(plan_id, execution)
    except DevServiceError as error:
        return {"eligible": False, "code": error.code, "message": str(error)}


def resume(plan_id: str) -> dict:
    execution = state(plan_id)
    evidence = _resume_evidence(plan_id, execution)
    plan = store.get_plan(plan_id)
    token = execution_store.claim_resume(
        plan_id, execution["current_index"], evidence["handoff_id"], plan["approval_json"]
    )
    if token is None:
        _fail("RESUME_CONCURRENT", "The plan, approval or handoff changed before the resume claim.")
    _spawn(plan_id, token)
    return state(plan_id)


# P5.2C: Codex Native has no mission to launch through mission_runner --
# it has one turn, gated by the P5.2A launch check, followed by a human-
# bound review instead of an auto-signalled READY_FOR_REVIEW.


def _is_native_pane(pane) -> bool:
    if pane is None:
        return False
    from nexus.chat.appserver_client import BACKEND_CODEX_NATIVE
    from nexus.dev.executor import session_backend
    from nexus.registry import dev_sessions

    session = dev_sessions.get_session(pane["dev_session_id"])
    return session is not None and session_backend(session) == BACKEND_CODEX_NATIVE


def _native_prompt(pane) -> str:
    from nexus.workspace import service

    return service._agent_context(pane) + "Execute the planned task above and satisfy every acceptance criterion."


def _start_native_turn(pane) -> dict:
    from nexus.dev.executor import run_dev_turn

    return run_dev_turn(pane["dev_session_id"], _native_prompt(pane), model=pane["model"])


def _heartbeat(plan_id: str, token: str, stop: threading.Event, lost: threading.Event) -> None:
    while not stop.wait(2):
        try:
            if not execution_store.renew_lease(plan_id, token):
                lost.set()
                return
        except Exception:  # noqa: BLE001 - fail closed on storage failure
            lost.set()
            return


def _run(plan_id: str, token: str) -> None:
    from nexus.registry import dev_sessions
    from nexus.workspace import service

    stop = threading.Event()
    lost = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(plan_id, token, stop, lost), daemon=True)
    heartbeat.start()

    def check_owner():
        if lost.is_set() or not execution_store.renew_lease(plan_id, token):
            raise execution_store.OwnershipLost(plan_id)

    def update(status, **kwargs):
        execution_store.update_execution(plan_id, status, owner_token=token, **kwargs)

    def step_update(key, status, **kwargs):
        execution_store.update_step(plan_id, key, status, owner_token=token, **kwargs)

    try:
        check_owner()
        execution = execution_store.view(plan_id)
        for index, step in enumerate(execution["steps"]):
            check_owner()
            persisted = execution_store.get_steps(plan_id)[index]
            if persisted["status"] == "READY_FOR_REVIEW":
                continue
            if persisted["status"] == "AWAITING_NATIVE_REVIEW":
                message = "Turn completed; bind a confirmed human review to this step to resume."
                update("BLOCKED", current_index=index, error_code="AWAITING_NATIVE_REVIEW", error_message=message)
                return
            if persisted["status"] in {"BLOCKED", "FAILED", "INTERRUPTED", "RESULT_UNCERTAIN"}:
                update(persisted["status"], current_index=index)
                return
            pane = store.get_pane(persisted["pane_id"])
            plan = store.get_plan(plan_id)
            approval = _approval(plan) if plan is not None else {}
            choice = (approval.get("agent_models") or {}).get(step["agent_key"]) or {}
            session = (
                dev_sessions.get_session(pane["dev_session_id"])
                if pane is not None else None
            )
            from nexus.dev.executor import session_backend
            if (plan is None or plan["status"] != "APPROVED" or session is None
                    or session["project_id"] != plan["project_id"]
                    or pane["model"] != choice.get("model")
                    or session_backend(session) != choice.get(
                        "backend", approval.get("backend", "omniroute")
                    )):
                message = "Approved model or backend no longer matches session."
                step_update(
                    step["agent_key"], "BLOCKED",
                    error_code="EXECUTION_AUTHORIZATION_CHANGED", error_message=message,
                )
                update(
                    "BLOCKED", current_index=index,
                    error_code="EXECUTION_AUTHORIZATION_CHANGED", error_message=message,
                )
                return
            is_native = _is_native_pane(pane)
            evidence_id = persisted["native_turn_id"] if is_native else persisted["mission_id"]
            # A durable evidence id is authoritative even if the step status
            # lags. Recovery only observes it and never calls the launcher.
            if not evidence_id:
                if persisted["status"] != "PENDING":
                    message = "Launch was interrupted before its evidence id was recorded; it was not replayed."
                    step_update(step["agent_key"], "RESULT_UNCERTAIN", error_code="COORDINATOR_START_INTERRUPTED", error_message=message)
                    update("RESULT_UNCERTAIN", current_index=index, error_code="COORDINATOR_START_INTERRUPTED", error_message=message)
                    return
                if _is_interrupted(plan_id):
                    step_update(step["agent_key"], "INTERRUPTED")
                    update("INTERRUPTED", current_index=index)
                    return
                if index:
                    previous = execution_store.get_steps(plan_id)[index - 1]
                    if previous["status"] != "READY_FOR_REVIEW":
                        update("BLOCKED", current_index=index, error_code="DEPENDENCY_PENDING")
                        return
                    previous_pane = store.get_pane(previous["pane_id"])
                    previous_is_native = _is_native_pane(previous_pane)
                    if is_native != previous_is_native:
                        # P5.3B: two Codex Native agents in sequence are the
                        # supported MVP topology (handoff.verified_dependency
                        # already proves this correct end to end -- P5.3A).
                        # A mixed native/OmniRoute pair is not comprobably
                        # safe yet and stays explicitly blocked.
                        message = ("Coordinated execution supports a Codex Native agent depending only on "
                                   "another Codex Native agent today; a mixed native/OmniRoute handoff is "
                                   "not implemented.")
                        step_update(step["agent_key"], "BLOCKED", error_code="MIXED_BACKEND_HANDOFF_NOT_SUPPORTED", error_message=message)
                        update("BLOCKED", current_index=index, error_code="MIXED_BACKEND_HANDOFF_NOT_SUPPORTED", error_message=message)
                        return
                    if previous_is_native:
                        try:
                            changed = _native_changed_files(previous, previous_pane)
                        except DevServiceError as error:
                            step_update(step["agent_key"], "BLOCKED", error_code=error.code, error_message=str(error))
                            update("BLOCKED", current_index=index, error_code=error.code, error_message=str(error))
                            return
                    else:
                        changed = _changed_files(_mission_for(previous))
                    if changed:
                        from nexus.workspace import handoff
                        try:
                            handoff.verified_dependency(plan_id, previous, persisted)
                        except DevServiceError as error:
                            code = ("DEPENDENCY_WORKTREE_NOT_TRANSFERRED"
                                    if error.code == "DEPENDENCY_WORKTREE_NOT_TRANSFERRED" else error.code)
                            step_update(step["agent_key"], "BLOCKED", error_code=code, error_message=str(error))
                            update("BLOCKED", current_index=index, error_code=code, error_message=str(error))
                            return
                try:
                    service._require_backend_model(
                        pane["model"], session_backend(session)
                    )
                except DevServiceError as error:
                    step_update(
                        step["agent_key"], "BLOCKED",
                        error_code=error.code, error_message=str(error),
                    )
                    update(
                        "BLOCKED", current_index=index,
                        error_code=error.code, error_message=str(error),
                    )
                    return
                update("RUNNING", current_index=index)
                # Persist the launch barrier before any external side effect.
                # If ownership changes during the call, recovery stops here as
                # uncertain and the old token cannot write or advance again.
                step_update(step["agent_key"], "STARTING")
                check_owner()
                if is_native:
                    try:
                        with execution_store.coordinated_launch(plan_id, step["agent_key"], token):
                            turn = _start_native_turn(pane)
                    except DevServiceError as error:
                        status = ("BLOCKED" if error.code in {
                            "DEPENDENCY_WORKTREE_NOT_TRANSFERRED", "SESSION_BUSY", "TOO_MANY_CONCURRENT_DEV_TURNS",
                        } else "FAILED")
                        step_update(step["agent_key"], status, error_code=error.code, error_message=str(error))
                        update(status, current_index=index, error_code=error.code, error_message=str(error))
                        return
                    evidence_id = turn["turn_id"]
                    step_update(step["agent_key"], "RUNNING", native_turn_id=evidence_id)
                else:
                    try:
                        with execution_store.coordinated_launch(plan_id, step["agent_key"], token):
                            result = service.start_pane_mission(
                                step["pane_id"], None, confirm_model=None,
                                test_profile=step["test_profile"], timeout_seconds=None,
                            )
                    except DevServiceError as error:
                        status = "BLOCKED" if error.code in {"DEPENDENCY_PENDING", "PANE_BUSY", "TEST_PROFILE_NOT_FOUND"} else "FAILED"
                        step_update(step["agent_key"], status, error_code=error.code, error_message=str(error))
                        update(status, current_index=index, error_code=error.code, error_message=str(error))
                        return
                    evidence_id = result["mission"]["mission_id"]
                    step_update(step["agent_key"], "RUNNING", mission_id=evidence_id)
            if is_native:
                while True:
                    check_owner()
                    turn = dev_sessions.get_turn(evidence_id)
                    if turn is None:
                        step_update(step["agent_key"], "RESULT_UNCERTAIN", error_code="TURN_RECORD_MISSING")
                        update("RESULT_UNCERTAIN", current_index=index, error_code="TURN_RECORD_MISSING")
                        return
                    if _is_interrupted(plan_id) and turn["status"] in {"PENDING", "RUNNING"}:
                        try:
                            from nexus.dev.executor import interrupt_dev_turn
                            check_owner()
                            interrupt_dev_turn(turn["dev_session_id"], evidence_id)
                        except DevServiceError:
                            pass
                    if turn["status"] in {"PENDING", "RUNNING"}:
                        time.sleep(0.2)
                        continue
                    status = turn["status"]
                    if status == "COMPLETED":
                        step_update(step["agent_key"], "AWAITING_NATIVE_REVIEW")
                        message = "Turn completed; create and confirm a human review for this session, then bind it to resume."
                        update("BLOCKED", current_index=index, error_code="AWAITING_NATIVE_REVIEW", error_message=message)
                        return
                    terminal = ("INTERRUPTED" if status == "CANCELLED"
                               else "RESULT_UNCERTAIN" if turn["error_code"] == "TURN_OUTCOME_UNKNOWN"
                               else "FAILED")
                    step_update(step["agent_key"], terminal, error_code=turn["error_code"], error_message=turn["error_message"])
                    update(terminal, current_index=index, error_code=turn["error_code"] or f"TURN_{terminal}", error_message=turn["error_message"])
                    return
            else:
                while True:
                    check_owner()
                    mission = dev_missions.get_mission(evidence_id)
                    if mission is None:
                        step_update(step["agent_key"], "RESULT_UNCERTAIN", error_code="MISSION_RECORD_MISSING")
                        update("RESULT_UNCERTAIN", current_index=index, error_code="MISSION_RECORD_MISSING")
                        return
                    if _is_interrupted(plan_id) and mission["status"] in ACTIVE_MISSION_STATUSES:
                        try:
                            from nexus.dev import mission_runner
                            check_owner()
                            mission_runner.interrupt_mission(mission["dev_session_id"], evidence_id)
                        except DevServiceError:
                            pass
                    if mission["status"] in ACTIVE_MISSION_STATUSES:
                        time.sleep(0.2)
                        continue
                    status = mission["status"]
                    if status == "READY_FOR_REVIEW":
                        step_update(step["agent_key"], status, result=_step_result(mission))
                        if _is_interrupted(plan_id):
                            update("INTERRUPTED", current_index=index + 1)
                            return
                        break
                    terminal = "INTERRUPTED" if status in {"CANCELLED", "INTERRUPTED"} else "RESULT_UNCERTAIN" if status == "OUTCOME_UNKNOWN" else "FAILED"
                    step_update(step["agent_key"], terminal, result=_step_result(mission), error_code=mission["error_code"], error_message=mission["error_message"])
                    update(terminal, current_index=index, error_code=mission["error_code"] or f"MISSION_{terminal}", error_message=mission["error_message"])
                    return
        update("AWAITING_REVIEW", current_index=len(execution["steps"]), result={"human_approval_required": True, "integration_applied": False})
    except execution_store.OwnershipLost:
        # No unfenced cleanup: the new owner is solely responsible for state.
        return
    except Exception as error:  # noqa: BLE001 - durable failure boundary
        try:
            update("RESULT_UNCERTAIN", error_code="COORDINATOR_INTERRUPTED", error_message=str(error))
        except execution_store.OwnershipLost:
            pass
    finally:
        stop.set()
        heartbeat.join(timeout=3)
        with _workers_lock:
            if _workers.get(plan_id) is threading.current_thread():
                _workers.pop(plan_id, None)


def _spawn(plan_id: str, token: str) -> None:
    with _workers_lock:
        worker = threading.Thread(target=_run, args=(plan_id, token), daemon=True, name=f"nexus-plan-{plan_id}")
        _workers[plan_id] = worker
        worker.start()


def start(plan_id: str) -> dict:
    execution = state(plan_id)
    if execution["status"] == "AWAITING_EXECUTION":
        token = execution_store.claim_execution(plan_id)
        if token:
            _spawn(plan_id, token)
    # Duplicate start requests do not attach or spawn workers. Recovery owns
    # the separate, stale-lease-only takeover path.
    return execution_store.view(plan_id)


def interrupt(plan_id: str) -> dict:
    execution = state(plan_id)
    if execution["status"] in {"AWAITING_REVIEW", "BLOCKED", "FAILED", "INTERRUPTED", "RESULT_UNCERTAIN"}:
        return execution
    execution_store.request_interrupt(plan_id)
    return execution_store.view(plan_id)


def recover(plan_id: str) -> dict:
    """Atomically take a stale lease and reconcile without replaying a launch."""
    execution = state(plan_id)
    if execution["status"] != "RUNNING":
        return execution
    token = execution_store.claim_recovery(plan_id)
    if token:
        _spawn(plan_id, token)
    return execution_store.view(plan_id)


def bind_native_review(plan_id: str, agent_key: str, review_id: str) -> dict:
    """Explicit operator action: attach a confirmed human review as the
    durable evidence that the current Codex Native step is done, then
    resume the sequence -- the native sibling of resume() for an approved
    handoff. Never creates, decides or re-validates the review itself
    (that stays entirely in nexus.dev.integration); only checks that the
    exact review named applies to the exact session, turn and step that
    is actually awaiting one, immediately before claiming the transition.
    """
    from nexus.dev import integration
    from nexus.registry import dev_integrations, dev_sessions

    execution = state(plan_id)
    if execution["status"] != "BLOCKED" or execution["error_code"] != "AWAITING_NATIVE_REVIEW":
        _fail("NATIVE_REVIEW_NOT_AWAITED", "This plan is not blocked awaiting a native review.")
    index = execution["current_index"]
    steps = execution["steps"]
    if not isinstance(index, int) or index < 0 or index >= len(steps) or steps[index]["agent_key"] != agent_key:
        _fail("NATIVE_REVIEW_NOT_AWAITED", "This agent is not the step currently awaiting a native review.")
    step = steps[index]
    if step["status"] != "AWAITING_NATIVE_REVIEW" or not step["native_turn_id"] or step["native_review_id"]:
        _fail("NATIVE_REVIEW_NOT_AWAITED", "This step is not awaiting a native review to be bound.")
    pane = store.get_pane(step["pane_id"])
    if pane is None:
        _fail("PANE_NOT_FOUND", "The agent's pane no longer exists.", 404)
    session = dev_sessions.get_session(pane["dev_session_id"])
    if session is None:
        _fail("SESSION_NOT_FOUND", "The agent's development session no longer exists.", 404)
    from nexus.dev.executor import session_backend
    plan = store.get_plan(plan_id)
    approval = _approval(plan) if plan is not None else {}
    choice = (approval.get("agent_models") or {}).get(agent_key) or {}
    if (plan is None or plan["status"] != "APPROVED"
            or session_backend(session) != "codex_native"
            or choice.get(
                "backend", approval.get("backend", "omniroute")
            ) != "codex_native"
            or pane["model"] != choice.get("model")):
        _fail(
            "EXECUTION_AUTHORIZATION_CHANGED",
            "Native review no longer matches approved pane and session.",
        )
    turn = dev_sessions.get_turn_for_session(session["id"], step["native_turn_id"])
    if turn is None or turn["status"] != "COMPLETED":
        _fail("NATIVE_REVIEW_TURN_NOT_READY", "The step's own turn is not durably COMPLETED.")
    # get_review() itself refuses a review that does not belong to this
    # exact session -- not re-checked here.
    review = integration.get_review(session["id"], review_id)
    if review["status"] != "REVIEWED":
        _fail("NATIVE_REVIEW_NOT_READY", f"Review is {review['status']}; it cannot be bound.")
    if not review["snapshot"].get("approval_ready"):
        _fail("NATIVE_REVIEW_INCOMPLETE", "This review snapshot cannot be completely reviewed.")
    latest = dev_integrations.list_reviews(session["id"])
    if not latest or latest[0]["review_id"] != review_id:
        _fail("NATIVE_REVIEW_STALE", "Only the most recent review for this session can be bound.")

    token = execution_store.claim_native_review(plan_id, agent_key, review_id)
    if token is None:
        _fail("NATIVE_REVIEW_CONCURRENT", "The plan, step or review changed before this claim.")
    _spawn(plan_id, token)
    return state(plan_id)


def state(plan_id: str) -> dict:
    """A state query is strictly read-only, including during STARTING."""
    execution = execution_store.view(plan_id)
    if execution is None:
        _fail("EXECUTION_NOT_AUTHORIZED", "This plan has no execution authorization.")
    execution["resume"] = resume_eligibility(plan_id, execution)
    return execution


def project_state(project_id: str) -> list[dict]:
    return [execution_store.view(row["plan_id"]) for row in execution_store.list_executions(project_id)]
