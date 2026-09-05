"""Mission execution orchestrator for Nexus Mission Execution Orchestrator V1.

Owns the Mission-level lifecycle, dependency validation, and sequential Task
scheduling, while reusing the existing Task execution pipeline
(``nexus.web.execution.execute_task``) for every actual unit of work. This
module never selects agents, adapters, or providers, and never creates
Attempts/Sessions itself -- that remains the Task Registry/AgentExecutor's
job.
"""

from dataclasses import dataclass
from typing import Optional

from nexus.agents.policy import ExecutionPolicyError
from nexus.missions import scheduler
from nexus.reviews.execution import ReviewedTaskFailedError, execute_reviewed_task
from nexus.missions.scheduler import MissionDependencyError
from nexus.missions.service import (
    get_mission as get_engine_mission,
    update_mission_status,
)
from nexus.tasks import registry as task_registry
from nexus.web import execution as execution_service
from nexus.web import services as web_services

# Deterministic bound on how many characters of predecessor evidence a Task
# may receive, regardless of how many ancestors it has.
MAX_MISSION_CONTEXT_CHARS = 16000

# Reserve a small deterministic slice for older ancestors while allocating
# most context budget to the closest/recent dependency evidence first.
MIN_EVIDENCE_SLICE_CHARS = 256

# Statuses that are safe to see on a pre-existing Task before Mission
# RUNNING begins; anything else fails closed.
_SAFE_PRE_EXISTING_TASK_STATUSES = {"CREATED", "COMPLETED"}


class MissionConflictError(RuntimeError):
    """Raised when a Mission cannot be executed due to a conflicting state."""


class MissionExecutionError(RuntimeError):
    """Raised when a Task fails during Mission execution.

    Carries the owning mission_id and the failed task_id so callers can
    report a precise, actionable error while the original exception is kept
    as ``__cause__``.
    """

    def __init__(self, mission_id, task_id, original_error):
        self.mission_id = mission_id
        self.task_id = task_id
        self.original_error = original_error
        super().__init__(
            f"Mission {mission_id!r} failed executing task {task_id!r}: "
            f"{original_error}"
        )


def execute_mission(mission_id, review=False, reviewer=None):
    """Execute a Mission end to end, reusing the existing Task pipeline.

    Plans the Mission if it has no Tasks yet, validates its dependency
    graph, then executes eligible Tasks one at a time in deterministic
    Mission order, passing each Task its relevant predecessor evidence.
    Returns a Mission execution summary dict. Idempotent for COMPLETED
    Missions (no new work is performed).

    When ``review=True``, every eligible Task is executed through the
    reviewed Task execution flow (``nexus.reviews.execution``): a Task only
    counts as dependency-COMPLETED once its reviewer returns PASS. RETRY and
    ESCALATE keep the same Task looping through new Attempts (bounded by the
    existing same-tier retry + escalation-ladder policy). BLOCKED or
    escalation-unavailable moves the Task to FAILED, which -- exactly like
    any other Task failure -- fails the whole Mission closed, so dependent
    Tasks never start. ``reviewer`` is required when ``review=True``.
    """
    mission = get_engine_mission(mission_id)

    if mission.status == "COMPLETED":
        return _summary_from_state(mission_id)

    if mission.status == "FAILED":
        raise MissionConflictError(f"Mission {mission_id!r} already FAILED.")

    try:
        mission_tasks = _mission_tasks(mission_id)
    except MissionDependencyError:
        _fail_mission(mission_id)
        raise

    if not mission_tasks:
        web_services.plan_mission(mission_id)
        mission = get_engine_mission(mission_id)
        try:
            mission_tasks = _mission_tasks(mission_id)
        except MissionDependencyError:
            _fail_mission(mission_id)
            raise

    if mission.status in ("CREATED", "PLANNING"):
        mission = _normalize_planned_mission_to_ready(mission_id)

    if mission.status != "READY":
        raise MissionConflictError(
            f"Mission {mission_id!r} is not READY for execution "
            f"(status={mission.status!r})."
        )

    try:
        _reject_unsafe_pre_existing_task_states(mission_id, mission_tasks)
    except MissionConflictError:
        _fail_mission(mission_id)
        raise

    known_tasks = {task.task_id: task for task in task_registry.list_tasks()}
    try:
        scheduler.validate_dependencies(mission_id, mission_tasks, known_tasks)
    except MissionDependencyError:
        _fail_mission(mission_id)
        raise

    mission = update_mission_status(mission_id, "RUNNING")

    try:
        _run_eligible_tasks(mission_id, mission_tasks, review=review, reviewer=reviewer)
    except MissionDependencyError:
        _fail_mission(mission_id)
        raise
    except (MissionExecutionError, ExecutionPolicyError):
        _fail_mission(mission_id)
        raise
    except Exception:
        _fail_mission(mission_id)
        raise

    update_mission_status(mission_id, "COMPLETED")
    return _summary_from_state(mission_id)


def _normalize_planned_mission_to_ready(mission_id):
    """Advance a legacy CREATED/PLANNING Mission that already has Tasks."""
    mission = get_engine_mission(mission_id)
    if mission.status == "CREATED":
        mission = update_mission_status(mission_id, "PLANNING")
    if mission.status == "PLANNING":
        mission = update_mission_status(mission_id, "READY")
    return mission


def _fail_mission(mission_id):
    """Move a non-terminal Mission to FAILED.

    Lifecycle failures are intentionally not swallowed: if Nexus cannot
    record the terminal failure state, callers must see that inconsistency
    instead of continuing with a potentially stale RUNNING Mission.
    """
    mission = get_engine_mission(mission_id)
    if mission.status in ("COMPLETED", "FAILED"):
        return mission
    return update_mission_status(mission_id, "FAILED")


def _mission_tasks(mission_id):
    """Return a Mission's Tasks in deterministic order.

    Prefers Mission.tasks order; falls back to Task Registry insertion
    order for any Task not present there (defensive, keeps ordering
    deterministic even for manually-created Tasks).
    """
    mission = get_engine_mission(mission_id)
    ordered = []
    seen = set()

    for task in mission.tasks or []:
        current = task_registry.get_task(task.task_id)
        if current.mission_id != mission_id:
            raise MissionDependencyError(
                f"Mission {mission_id!r} references task {current.task_id!r} "
                f"owned by different Mission {current.mission_id!r}."
            )
        ordered.append(current)
        seen.add(current.task_id)

    for task in task_registry.list_tasks():
        if task.mission_id == mission_id and task.task_id not in seen:
            ordered.append(task)
            seen.add(task.task_id)

    return ordered


def _reject_unsafe_pre_existing_task_states(mission_id, mission_tasks):
    """Fail closed if any pre-existing Task is not CREATED or COMPLETED."""
    for task in mission_tasks:
        if task.status not in _SAFE_PRE_EXISTING_TASK_STATUSES:
            raise MissionConflictError(
                f"Mission {mission_id!r} has task {task.task_id!r} in "
                f"unsafe pre-existing status {task.status!r}; refusing to "
                "start execution."
            )


def _run_eligible_tasks(mission_id, mission_tasks, review=False, reviewer=None):
    """Sequentially execute every eligible Task until the Mission finishes."""
    if review and reviewer is None:
        raise ValueError("reviewer is required when review=True.")

    while True:
        tasks_by_id = {task.task_id: task_registry.get_task(task.task_id) for task in mission_tasks}

        if not scheduler.has_incomplete_work(list(tasks_by_id.values())):
            return

        next_task = scheduler.next_eligible_task(mission_tasks, tasks_by_id)
        if next_task is None:
            raise MissionDependencyError(
                f"Mission {mission_id!r} has no eligible task but incomplete "
                "work remains (deadlock/no-progress)."
            )

        mission_context = _build_mission_context(mission_id, next_task, tasks_by_id)

        try:
            if review:
                execute_reviewed_task(
                    next_task.task_id,
                    reviewer=reviewer,
                    mission_context=mission_context,
                    mission=get_engine_mission(mission_id),
                )
            else:
                execution_service.execute_task(
                    next_task.task_id, mission_context=mission_context
                )
        except ExecutionPolicyError:
            raise
        except ReviewedTaskFailedError as exc:
            raise MissionExecutionError(mission_id, next_task.task_id, exc) from exc
        except Exception as exc:  # noqa: BLE001 - wrapped below.
            raise MissionExecutionError(mission_id, next_task.task_id, exc) from exc


def _build_mission_context(mission_id, task, tasks_by_id):
    """Build the runtime mission_context dict for a Task about to execute.

    Pulls predecessor evidence only from the Task's dependency ancestry
    (never from unrelated independent Tasks), and only from ancestors that
    are already COMPLETED, reading their latest Attempt's stored output.
    """
    mission = get_engine_mission(mission_id)

    ancestor_ids = scheduler.dependency_ancestors(task, tasks_by_id)
    ordered_ancestors = [
        candidate
        for candidate in tasks_by_id.values()
        if candidate.task_id in ancestor_ids and candidate.status == "COMPLETED"
    ]
    # Preserve deterministic Mission order among ancestors.
    order_index = {t.task_id: i for i, t in enumerate(tasks_by_id.values())}
    ordered_ancestors.sort(key=lambda t: order_index[t.task_id])

    completed_tasks = _bounded_predecessor_evidence(ordered_ancestors)

    return {
        "mission": {
            "id": mission.mission_id,
            "title": mission.title,
            "description": mission.description,
            "project_id": mission.project_id,
        },
        "current_task": {
            "task_id": task.task_id,
            "title": task.title,
            "description": task.description,
        },
        "completed_tasks": completed_tasks,
    }


def _bounded_predecessor_evidence(ancestor_tasks):
    """Build bounded predecessor evidence, prioritizing closest ancestors.

    Budget allocation runs from the nearest/recent ancestor backwards so the
    direct predecessor cannot be starved by a very large early analysis
    output. A small deterministic reserve is kept for older non-empty
    ancestors. The returned list is restored to logical Mission order.
    """
    evidence = []
    for task in ancestor_tasks:
        evidence.append(
            {
                "task_id": task.task_id,
                "title": task.title,
                "output": _latest_attempt_output(task.task_id),
            }
        )

    remaining = MAX_MISSION_CONTEXT_CHARS
    allocated = {}

    for reverse_index in range(len(evidence) - 1, -1, -1):
        entry = evidence[reverse_index]
        output = entry.get("output")

        if not output:
            allocated[reverse_index] = dict(entry)
            continue

        older_nonempty = sum(
            1
            for older in evidence[:reverse_index]
            if older.get("output")
        )
        reserve_for_older = min(
            remaining,
            older_nonempty * MIN_EVIDENCE_SLICE_CHARS,
        )
        available = max(0, remaining - reserve_for_older)

        if available <= 0 and remaining > 0:
            available = min(remaining, 1)

        kept = output[:available]
        result_entry = dict(entry)
        result_entry["output"] = kept

        if len(kept) < len(output):
            result_entry["output_truncated"] = True

        remaining -= len(kept)
        allocated[reverse_index] = result_entry

    return [allocated[index] for index in range(len(evidence))]


def _latest_attempt_output(task_id):
    """Return the latest Attempt's stored result/output for a Task, if any."""
    attempts = task_registry.list_attempts(task_id=task_id)
    if not attempts:
        return None
    return attempts[-1].result


def _summary_from_state(mission_id):
    """Build a Mission execution summary purely from existing evidence.

    Never creates Tasks, Attempts, or Sessions. Used both for the normal
    post-run summary and for the idempotent COMPLETED-Mission response.
    """
    mission = get_engine_mission(mission_id)
    mission_tasks = _mission_tasks(mission_id)

    task_results = [_task_result_from_state(task) for task in mission_tasks]

    completed = sum(1 for t in mission_tasks if t.status == "COMPLETED")
    failed = sum(1 for t in mission_tasks if t.status == "FAILED")
    skipped = sum(1 for t in mission_tasks if t.status == "CREATED")

    return {
        "mission_id": mission.mission_id,
        "status": mission.status,
        "project_id": mission.project_id,
        "execution_path": mission.execution_path,
        "total_tasks": len(mission_tasks),
        "completed_tasks": completed,
        "failed_tasks": failed,
        "skipped_tasks": skipped,
        "task_results": task_results,
    }


def _task_result_from_state(task):
    """Reconstruct a single Task's result dict purely from existing evidence.

    Does not create a new Session or Attempt. ``routed_model`` is only
    reconstructed for omniroute execution paths (SIMULATED Tasks always
    report None, matching pre-existing v1.5 semantics).
    """
    attempts = task_registry.list_attempts(task_id=task.task_id)
    last_attempt = attempts[-1] if attempts else None

    policy = task.execution_policy if isinstance(task.execution_policy, dict) else {}
    execution_path = policy.get("execution_path") or "simulated"
    project_id = policy.get("project_id")

    routed_model = None
    if execution_path == "omniroute" and last_attempt is not None:
        routed_model = last_attempt.model

    session_id = _reconstruct_session_id(task.task_id)

    return {
        "task_id": task.task_id,
        "status": task.status,
        "assigned_agent": task.assigned_agent,
        "execution_path": execution_path,
        "project_id": project_id,
        "routed_model": routed_model,
        "attempt_id": last_attempt.attempt_id if last_attempt else None,
        "session_id": session_id,
        "output": last_attempt.result if last_attempt else None,
    }


def _reconstruct_session_id(task_id):
    """Reconstruct the last known Session id for a Task from the registry."""
    from nexus.workspaces import registry as session_registry

    sessions = session_registry.list_sessions(task_id=task_id)
    if not sessions:
        return None
    return sessions[-1].session_id
