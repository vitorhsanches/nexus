"""Bounded autonomous mission execution inside a Development worktree.

One initial Codex turn, one trusted verification profile, and at most one
repair turn are allowed. Integration remains exclusively human initiated.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from contextvars import copy_context

from nexus.dev import executor, feature_gate
from nexus.dev.service import DevServiceError, validate_dev_project
from nexus.dev.test_profiles import resolve_test_profile
from nexus.dev.worktree import DevWorktreeError, verify_dev_worktree
from nexus.failure_contract import sanitize_error_text
from nexus.registry import dev_missions, dev_sessions, workspace_handoff

MAX_LOG_CHARS = 20_000
DEFAULT_MISSION_TIMEOUT_SECONDS = 900
MIN_MISSION_TIMEOUT_SECONDS = 5
MAX_MISSION_TIMEOUT_SECONDS = 3600
POLL_INTERVAL_SECONDS = 0.1

_active_lock = threading.Lock()
_active_missions: dict[str, threading.Event] = {}


def _clean_log(value: str | None) -> str:
    text = sanitize_error_text(value or "") or ""
    if len(text) <= MAX_LOG_CHARS:
        return text
    return text[:MAX_LOG_CHARS] + "\n[output truncated]"


def _mission_dict(row) -> dict:
    result = json.loads(row["result_json"]) if row["result_json"] else None
    tests = []
    for test in dev_missions.list_test_runs(row["id"]):
        tests.append({
            "test_run_id": test["id"],
            "attempt_number": test["attempt_number"],
            "profile": test["profile"],
            "argv": json.loads(test["argv_json"]),
            "status": test["status"],
            "exit_code": test["exit_code"],
            "stdout": test["stdout_text"],
            "stderr": test["stderr_text"],
            "error_code": test["error_code"],
            "started_at": test["started_at"],
            "finished_at": test["finished_at"],
        })
    return {
        "mission_id": row["id"],
        "dev_session_id": row["dev_session_id"],
        "description": row["description"],
        "requested_model": row["requested_model"],
        "requested_effort": row["requested_effort"] if "requested_effort" in set(row.keys()) else None,
        "allowed_models": json.loads(row["allowed_models_json"]),
        "test_profile": row["test_profile"],
        "status": row["status"],
        "attempt_count": row["attempt_count"],
        "current_turn_id": row["current_turn_id"],
        "interrupt_requested": bool(row["interrupt_requested"]),
        "result": result,
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "updated_at": row["updated_at"],
        "tests": tests,
    }


def _require_session_worktree(dev_session_id: str) -> tuple[object, str]:
    session = dev_sessions.get_session(dev_session_id)
    if session is None:
        raise DevServiceError("SESSION_NOT_FOUND", f"Unknown development session: {dev_session_id!r}", http_status=404)
    workspace = validate_dev_project(session["project_id"])
    if session["status"] != dev_sessions.STATUS_ACTIVE:
        raise DevServiceError("SESSION_NOT_ACTIVE", "Autonomous missions require an ACTIVE development session.", http_status=409)
    if not session["worktree_path"]:
        raise DevServiceError("WORKTREE_MISSING", "The development session has no worktree.", http_status=409)
    try:
        worktree = verify_dev_worktree(workspace, session["worktree_path"], dev_session_id)
    except DevWorktreeError as error:
        raise DevServiceError("WORKTREE_INVALID", str(error), http_status=409) from error
    return session, str(worktree)


def start_mission(
    dev_session_id: str,
    description: str,
    *,
    model: str | None,
    effort: str | None = None,
    allowed_models: list[str],
    test_profile: str,
    timeout_seconds: int = DEFAULT_MISSION_TIMEOUT_SECONDS,
) -> dict:
    if not isinstance(description, str) or not description.strip():
        raise DevServiceError("EMPTY_MISSION", "description must not be empty.", http_status=400)
    if len(description) > 100_000:
        raise DevServiceError("MISSION_TOO_LARGE", "description exceeds the allowed size.", http_status=400)
    if not feature_gate.execution_enabled():
        raise DevServiceError(
            "DEV_EXECUTION_DISABLED",
            "Development Mode execution is disabled on this server.",
            http_status=403,
        )
    session, _worktree = _require_session_worktree(dev_session_id)
    selected_model = model or session["requested_model"]
    if not isinstance(allowed_models, list) or len(allowed_models) > 50 or any(
        not isinstance(item, str) or not item.strip() or len(item) > 128 for item in allowed_models
    ):
        raise DevServiceError("INVALID_ALLOWED_MODELS", "allowed_models contains an invalid route identifier.", http_status=400)
    if not selected_model:
        raise DevServiceError("MODEL_REQUIRED", "An explicit selected model is required.", http_status=400)
    backend = executor.session_backend(session)
    if backend in {executor.BACKEND_CODEX_NATIVE, executor.BACKEND_CLAUDE_NATIVE}:
        # Native backends: the local account's own catalog, never OmniRoute's.
        if backend == executor.BACKEND_CODEX_NATIVE:
            executor.validate_native_choice(selected_model, effort)
        else:
            executor.validate_claude_choice(selected_model, effort)
        if selected_model not in allowed_models:
            raise DevServiceError("MODEL_NOT_CONFIRMED", "The selected model was not explicitly confirmed by the caller.", http_status=400)
        normalized_allowed = [selected_model]
    else:
        normalized_allowed = _validate_omniroute_mission_choice(selected_model, effort, allowed_models)
    if not MIN_MISSION_TIMEOUT_SECONDS <= timeout_seconds <= MAX_MISSION_TIMEOUT_SECONDS:
        raise DevServiceError("INVALID_TIMEOUT", "timeout_seconds is outside the allowed range.", http_status=400)
    return _create_and_launch(dev_session_id, session, description, selected_model, effort, normalized_allowed,
                              test_profile, timeout_seconds)


def _validate_omniroute_mission_choice(selected_model: str, effort: str | None, allowed_models: list[str]) -> list[str]:
    from nexus.dev.models import list_dev_models

    model_catalog = list_dev_models()
    normalized_allowed = [item["id"] for item in model_catalog["models"]]
    if selected_model not in normalized_allowed:
        message = model_catalog.get("catalog_error") or "The selected model is not an approved live Development route."
        raise DevServiceError("MODEL_NOT_ALLOWED", message, http_status=403)
    if selected_model not in allowed_models:
        raise DevServiceError("MODEL_NOT_CONFIRMED", "The selected model was not explicitly confirmed by the caller.", http_status=400)
    from nexus.workspace.omniroute_oauth import require_connected_route
    require_connected_route(selected_model)
    selected_row = next(item for item in model_catalog["models"] if item["id"] == selected_model)
    if selected_model.startswith(("cc/", "cx/")) and not effort:
        raise DevServiceError("AI_EFFORT_REQUIRED", "Escolha o esforço da missão antes de executar.", http_status=422)
    if effort is not None and (effort not in selected_row.get("supported_efforts", []) or (
        selected_model.startswith(("cc/", "cx/")) and effort not in selected_row.get("confirmed_efforts", []))):
        raise DevServiceError("EFFORT_NOT_SUPPORTED", "Esforço não confirmado para a rota exata da missão.", http_status=422)
    return normalized_allowed


def _create_and_launch(dev_session_id, session, description, selected_model, effort, normalized_allowed,
                       test_profile, timeout_seconds) -> dict:
    try:
        mission_id = dev_missions.create_mission(
            dev_session_id,
            description.strip(),
            requested_model=selected_model,
            requested_effort=effort,
            allowed_models=normalized_allowed,
            test_profile=test_profile,
        )
    except workspace_handoff.HandoffBlocked as error:
        raise DevServiceError("DEPENDENCY_WORKTREE_NOT_TRANSFERRED", str(error), http_status=409) from error
    except dev_missions.MissionBusyError as error:
        raise DevServiceError("MISSION_BUSY", str(error), http_status=409) from error

    if resolve_test_profile(test_profile, session["project_id"]) is None:
        dev_missions.update_status(
            mission_id,
            "BLOCKED",
            error_code="TEST_PROFILE_NOT_FOUND",
            error_message="The requested trusted test profile is not configured on this server.",
        )
        return get_mission(dev_session_id, mission_id)

    stop_event = threading.Event()
    with _active_lock:
        _active_missions[mission_id] = stop_event
    # Preserve the coordinator's private launch credential for this mission's
    # own turns. SQLite still verifies the durable owner token at each insert.
    launch_context = copy_context()
    thread = threading.Thread(
        target=launch_context.run,
        args=(_run_mission_body, mission_id, timeout_seconds, stop_event),
        daemon=True,
        name=f"nexus-mission-{mission_id}",
    )
    try:
        thread.start()
    except RuntimeError as error:
        with _active_lock:
            _active_missions.pop(mission_id, None)
        dev_missions.update_status(
            mission_id, "FAILED", error_code="MISSION_START_FAILED",
            error_message=_clean_log(str(error)) or "Mission thread could not be started.",
        )
        raise DevServiceError("MISSION_START_FAILED", "Mission could not be started.", http_status=503) from error
    return get_mission(dev_session_id, mission_id)


def _stop_requested(mission_id: str, stop_event: threading.Event) -> bool:
    if stop_event.is_set():
        return True
    row = dev_missions.get_mission(mission_id)
    return bool(row and row["interrupt_requested"])


def _wait_for_turn(
    mission_id: str,
    dev_session_id: str,
    turn_id: str,
    deadline: float,
    stop_event: threading.Event,
) -> dict:
    while True:
        turn = executor.get_turn_detail(dev_session_id, turn_id)
        if turn["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            return turn
        if _stop_requested(mission_id, stop_event):
            executor.interrupt_dev_turn(dev_session_id, turn_id)
            raise InterruptedError("Mission interrupted by the user.")
        if time.monotonic() >= deadline:
            executor.interrupt_dev_turn(dev_session_id, turn_id)
            raise TimeoutError("Mission execution exceeded its time limit.")
        time.sleep(POLL_INTERVAL_SECONDS)


def _execute_test_profile(
    mission_id: str,
    attempt_number: int,
    profile_name: str,
    project_id: str,
    worktree: str,
    stop_event: threading.Event,
    mission_deadline: float,
) -> dict:
    resolved = resolve_test_profile(profile_name, project_id)
    if resolved is None:
        raise DevServiceError("TEST_PROFILE_NOT_FOUND", "Trusted test profile is unavailable.", http_status=409)
    argv, profile_timeout = resolved
    test_run_id = dev_missions.create_test_run(mission_id, attempt_number, profile_name, argv)
    process = None
    try:
        process = subprocess.Popen(
            argv,
            cwd=worktree,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            env={**os.environ, "CI": "true"},
        )
        deadline = min(mission_deadline, time.monotonic() + profile_timeout)
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if _stop_requested(mission_id, stop_event):
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=5)
                    dev_missions.finish_test_run(test_run_id, "CANCELLED", exit_code=process.returncode, stdout_text=_clean_log(stdout), stderr_text=_clean_log(stderr), error_code="MISSION_INTERRUPTED")
                    raise InterruptedError("Mission interrupted during verification.")
                if time.monotonic() >= deadline:
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=5)
                    dev_missions.finish_test_run(test_run_id, "TIMEOUT", exit_code=process.returncode, stdout_text=_clean_log(stdout), stderr_text=_clean_log(stderr), error_code="TEST_TIMEOUT")
                    raise TimeoutError("Trusted verification profile exceeded its time limit.")
        status = "PASSED" if process.returncode == 0 else "FAILED"
        result = {"status": status, "exit_code": process.returncode, "stdout": _clean_log(stdout), "stderr": _clean_log(stderr)}
        dev_missions.finish_test_run(test_run_id, status, exit_code=process.returncode, stdout_text=result["stdout"], stderr_text=result["stderr"])
        return result
    except (InterruptedError, TimeoutError):
        raise
    except Exception as error:
        if process is not None and process.poll() is None:
            process.terminate()
        message = _clean_log(str(error)) or "Verification subprocess failed."
        dev_missions.finish_test_run(test_run_id, "FAILED", stderr_text=message, error_code="TEST_PROCESS_FAILED")
        raise DevServiceError("TEST_PROCESS_FAILED", message, http_status=500) from error


def _prompt(description: str) -> str:
    return (
        "Execute this bounded autonomous development mission inside the current isolated worktree. "
        "Do not push, merge, publish, apply integration, change credentials, or work outside this worktree. "
        "Implement only what is needed for the mission; Nexus will run the trusted test profile separately.\n\n"
        f"Mission:\n{description}"
    )


def _repair_prompt(description: str, test_result: dict) -> str:
    evidence = _clean_log((test_result.get("stdout") or "") + "\n" + (test_result.get("stderr") or ""))
    return (
        "The trusted verification for the mission failed. This is the only automatic repair attempt. "
        "Diagnose the evidence and repair the implementation inside the same worktree. "
        "Do not push, merge, publish, apply integration, or change the test command.\n\n"
        f"Mission:\n{description}\n\nVerification evidence:\n{evidence}"
    )


def _finish_ready(mission_id: str, dev_session_id: str, attempt_count: int) -> None:
    result = {
        "summary": "Implementation and trusted verification completed; human review is required.",
        "changed_files": executor.changed_files(dev_session_id),
        "review": executor.review_diff(dev_session_id),
        "attempts": attempt_count,
        "human_approval_required": True,
        "integration_applied": False,
    }
    dev_missions.update_status(mission_id, "READY_FOR_REVIEW", attempt_count=attempt_count, result=result)


def _run_mission_body(mission_id: str, timeout_seconds: int, stop_event: threading.Event) -> None:
    row = dev_missions.get_mission(mission_id)
    if row is None:
        return
    session_id = row["dev_session_id"]
    deadline = time.monotonic() + timeout_seconds
    try:
        session, worktree = _require_session_worktree(session_id)
        dev_missions.update_status(mission_id, "RUNNING", attempt_count=1)
        turn = executor.run_dev_turn(session_id, _prompt(row["description"]), model=row["requested_model"],
                                     **({"effort": row["requested_effort"]} if row["requested_effort"] else {}))
        dev_missions.update_status(mission_id, "RUNNING", current_turn_id=turn["turn_id"], attempt_count=1)
        turn = _wait_for_turn(mission_id, session_id, turn["turn_id"], deadline, stop_event)
        if turn["status"] != "COMPLETED":
            raise DevServiceError("EXECUTION_FAILED", turn.get("error_message") or "Initial development turn failed.", http_status=500)

        dev_missions.update_status(mission_id, "TESTING", attempt_count=1)
        tested = _execute_test_profile(mission_id, 1, row["test_profile"], session["project_id"], worktree, stop_event, deadline)
        if _stop_requested(mission_id, stop_event):
            raise InterruptedError("Mission interrupted after verification.")
        if tested["status"] == "PASSED":
            _finish_ready(mission_id, session_id, 1)
            return

        dev_missions.update_status(mission_id, "REPAIRING", attempt_count=2)
        repair = executor.run_dev_turn(session_id, _repair_prompt(row["description"], tested), model=row["requested_model"],
                                       **({"effort": row["requested_effort"]} if row["requested_effort"] else {}))
        dev_missions.update_status(mission_id, "REPAIRING", current_turn_id=repair["turn_id"], attempt_count=2)
        repair = _wait_for_turn(mission_id, session_id, repair["turn_id"], deadline, stop_event)
        if repair["status"] != "COMPLETED":
            raise DevServiceError("REPAIR_FAILED", repair.get("error_message") or "Automatic repair turn failed.", http_status=500)

        dev_missions.update_status(mission_id, "TESTING", attempt_count=2)
        retested = _execute_test_profile(mission_id, 2, row["test_profile"], session["project_id"], worktree, stop_event, deadline)
        if retested["status"] == "PASSED":
            _finish_ready(mission_id, session_id, 2)
        else:
            dev_missions.update_status(
                mission_id, "FAILED", attempt_count=2, error_code="TESTS_FAILED_AFTER_REPAIR",
                error_message="Trusted verification still failed after the single allowed repair attempt.",
                result={"attempts": 2, "human_approval_required": True, "integration_applied": False},
            )
    except InterruptedError as error:
        dev_missions.update_status(mission_id, "CANCELLED", error_code="MISSION_INTERRUPTED", error_message=_clean_log(str(error)))
    except TimeoutError as error:
        dev_missions.update_status(mission_id, "FAILED", error_code="MISSION_TIMEOUT", error_message=_clean_log(str(error)))
    except DevServiceError as error:
        dev_missions.update_status(mission_id, "FAILED", error_code=error.code, error_message=_clean_log(str(error)))
    except Exception as error:  # noqa: BLE001 -- terminal persistence is the last-resort safety boundary
        dev_missions.update_status(mission_id, "FAILED", error_code="UNEXPECTED_ERROR", error_message=_clean_log(str(error)) or "Unexpected mission error.")
    finally:
        with _active_lock:
            _active_missions.pop(mission_id, None)


def get_mission(dev_session_id: str, mission_id: str) -> dict:
    row = dev_missions.get_mission_for_session(dev_session_id, mission_id)
    if row is None:
        raise DevServiceError("MISSION_NOT_FOUND", "Autonomous mission not found in this development session.", http_status=404)
    return _mission_dict(row)


def list_missions(dev_session_id: str) -> list[dict]:
    if dev_sessions.get_session(dev_session_id) is None:
        raise DevServiceError("SESSION_NOT_FOUND", f"Unknown development session: {dev_session_id!r}", http_status=404)
    return [_mission_dict(row) for row in dev_missions.list_missions(dev_session_id)]


def interrupt_mission(dev_session_id: str, mission_id: str) -> dict:
    mission = get_mission(dev_session_id, mission_id)
    if mission["status"] not in dev_missions.ACTIVE_STATUSES:
        return {"status": "NOT_ACTIVE", "mission": mission}
    dev_missions.request_interrupt(mission_id)
    with _active_lock:
        event = _active_missions.get(mission_id)
    if event is not None:
        event.set()
    return {"status": "INTERRUPT_REQUESTED", "mission_id": mission_id}


def recover_orphaned_missions() -> list[str]:
    from nexus.orchestration.ownership import classify_ownership

    return dev_missions.recover_orphaned_missions(
        lambda host, pid, acquired_at: classify_ownership(host, pid, acquired_at) == "dead_same_host"
    )
