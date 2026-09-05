"""Dependency graph validation and scheduling for Nexus Mission Execution V1.

Pure, testable functions over Task-like objects (anything with task_id,
mission_id, status, dependencies). Kept independent of the Task Registry so
graph logic can be unit tested without any global state.
"""

from dataclasses import dataclass


class MissionDependencyError(ValueError):
    """Raised when a Mission's Task dependency graph is invalid."""


def _dependency_ids(task):
    return list(task.dependencies or [])


def validate_dependencies(mission_id, mission_tasks, known_tasks):
    """Validate the dependency graph of a Mission's Tasks.

    ``mission_tasks`` are the Tasks belonging to ``mission_id``.
    ``known_tasks`` is a mapping of task_id -> Task for every Task known
    globally (across all Missions), used to distinguish an unknown
    dependency id from a cross-Mission one.

    Raises MissionDependencyError on any of: unknown dependency id,
    cross-Mission dependency, self dependency, duplicate dependency id,
    cycle, or a graph with no progress possible.
    """
    mission_task_ids = {task.task_id for task in mission_tasks}

    for task in mission_tasks:
        if task.mission_id != mission_id:
            raise MissionDependencyError(
                f"Task {task.task_id!r} belongs to Mission "
                f"{task.mission_id!r}, not {mission_id!r}."
            )

        deps = _dependency_ids(task)

        if len(deps) != len(set(deps)):
            raise MissionDependencyError(
                f"Task {task.task_id!r} has duplicate dependency ids."
            )

        for dep_id in deps:
            if dep_id == task.task_id:
                raise MissionDependencyError(
                    f"Task {task.task_id!r} depends on itself."
                )

            if dep_id in mission_task_ids:
                continue

            dep_task = known_tasks.get(dep_id)
            if dep_task is None:
                raise MissionDependencyError(
                    f"Task {task.task_id!r} depends on unknown task {dep_id!r}."
                )

            raise MissionDependencyError(
                f"Task {task.task_id!r} depends on task {dep_id!r} which "
                f"belongs to a different Mission {dep_task.mission_id!r} "
                f"(cross-Mission dependency)."
            )

    _detect_cycle(mission_tasks)


def _detect_cycle(mission_tasks):
    """Raise MissionDependencyError if the Mission's Tasks form a cycle."""
    graph = {task.task_id: _dependency_ids(task) for task in mission_tasks}

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {task_id: WHITE for task_id in graph}

    def visit(task_id, stack):
        color[task_id] = GRAY
        for dep_id in graph.get(task_id, []):
            if dep_id not in graph:
                # Dependency outside this Mission's task set (already
                # validated as belonging to this Mission or rejected above).
                continue
            if color[dep_id] == GRAY:
                raise MissionDependencyError(
                    "Dependency cycle detected involving task "
                    f"{dep_id!r} (via {' -> '.join(stack + [dep_id])})."
                )
            if color[dep_id] == WHITE:
                visit(dep_id, stack + [dep_id])
        color[task_id] = BLACK

    for task_id in graph:
        if color[task_id] == WHITE:
            visit(task_id, [task_id])


def is_eligible(task, tasks_by_id):
    """Return True if a Task is eligible to execute right now.

    Eligible means CREATED status and every dependency task is COMPLETED.
    """
    if task.status != "CREATED":
        return False
    for dep_id in _dependency_ids(task):
        dep_task = tasks_by_id.get(dep_id)
        if dep_task is None or dep_task.status != "COMPLETED":
            return False
    return True


def next_eligible_task(mission_tasks, tasks_by_id):
    """Return the next Task to execute in deterministic Mission order.

    ``mission_tasks`` must already be in deterministic Mission order
    (Mission.tasks order, falling back to Task Registry insertion order).
    Returns None when no Task is currently eligible.
    """
    for task in mission_tasks:
        if is_eligible(task, tasks_by_id):
            return task
    return None


def has_incomplete_work(mission_tasks):
    """Return True if any Mission Task has not reached a terminal status."""
    return any(task.status not in ("COMPLETED", "FAILED") for task in mission_tasks)


def dependency_ancestors(task, tasks_by_id):
    """Return the transitive set of dependency ancestor task ids for a Task.

    Traversal is a plain DFS; assumes the graph has already been validated
    (no cycles, all dependency ids resolvable within ``tasks_by_id``).
    """
    seen = set()
    stack = list(_dependency_ids(task))
    while stack:
        dep_id = stack.pop()
        if dep_id in seen:
            continue
        seen.add(dep_id)
        dep_task = tasks_by_id.get(dep_id)
        if dep_task is not None:
            stack.extend(_dependency_ids(dep_task))
    return seen
