"""In-memory task registry for Nexus Task Registry V1."""

from uuid import uuid4

from nexus.tasks.lifecycle import transition
from nexus.tasks.models import ATTEMPT_STATUSES, Attempt, Task

# Internal in-memory stores.
_tasks = {}
_attempts = {}
_next_attempt = {}


class TaskNotFoundError(KeyError):
    pass


def _new_id(prefix):
    return f"{prefix}-{uuid4().hex[:8].upper()}"


def create_task(
    mission_id,
    title,
    description=None,
    status="CREATED",
    priority="MEDIUM",
    dependencies=None,
    assigned_agent=None,
    execution_policy=None,
    acceptance_criteria=None,
):
    task_id = _new_id("TASK")
    task = Task(
        task_id=task_id,
        mission_id=mission_id,
        title=title,
        description=description,
        status=status,
        priority=priority,
        dependencies=list(dependencies) if dependencies is not None else None,
        assigned_agent=assigned_agent,
        execution_policy=execution_policy,
        acceptance_criteria=(
            list(acceptance_criteria) if acceptance_criteria is not None else None
        ),
    )
    _tasks[task_id] = task
    return task


def get_task(task_id):
    try:
        return _tasks[task_id]
    except KeyError:
        raise TaskNotFoundError(task_id)


def list_tasks():
    return list(_tasks.values())


def update_task_status(task_id, status):
    task = get_task(task_id)
    task.status = transition(task.status, status)
    return task


def create_attempt(task_id, agent_id, model, status="PENDING", result=None):
    task = get_task(task_id)
    sequence = _next_attempt.get(task_id, 0) + 1
    _next_attempt[task_id] = sequence
    attempt_id = f"ATT-{task_id}-{sequence}"
    attempt = Attempt(
        attempt_id=attempt_id,
        task_id=task_id,
        agent_id=agent_id,
        model=model,
        status=status,
        result=result,
    )
    _attempts.setdefault(task_id, []).append(attempt)
    return attempt


def get_attempt(attempt_id):
    for attempts in _attempts.values():
        for attempt in attempts:
            if attempt.attempt_id == attempt_id:
                return attempt
    raise TaskNotFoundError(attempt_id)


def list_attempts(task_id=None):
    if task_id is not None:
        return list(_attempts.get(task_id, []))
    return [attempt for attempts in _attempts.values() for attempt in attempts]


ATTEMPT_TRANSITIONS = {
    "PENDING": {
        "RUNNING",
        "REVIEW",
        "COMPLETED",
        "FAILED",
    },
    "RUNNING": {
        "REVIEW",
        "COMPLETED",
        "FAILED",
    },
    "REVIEW": {
        "COMPLETED",
        "FAILED",
    },
    "COMPLETED": set(),
    "FAILED": set(),
}


class InvalidAttemptTransitionError(ValueError):
    """Raised when an Attempt lifecycle transition is not allowed."""


def update_attempt_status(attempt_id, status, result=None):
    """Transition an existing Attempt without reopening terminal evidence."""
    if status not in ATTEMPT_STATUSES:
        raise ValueError(
            f"Unknown attempt status: {status!r}"
        )

    attempt = get_attempt(attempt_id)
    current = attempt.status

    allowed = ATTEMPT_TRANSITIONS.get(current)
    if allowed is None:
        raise InvalidAttemptTransitionError(
            f"Unknown current Attempt status: {current!r}."
        )

    if status not in allowed:
        raise InvalidAttemptTransitionError(
            f"Invalid Attempt transition: "
            f"{current!r} -> {status!r}."
        )

    attempt.status = status

    if result is not None:
        attempt.result = result

    return attempt
