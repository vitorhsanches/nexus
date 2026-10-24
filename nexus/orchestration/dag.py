"""GO plan Worker dependency graph -- Nexus Multi-Agent MVP Phase 1.

Phase 1 scope only: PLAN -> DAG -> ELIGIBILITY. This module adds a
deterministic dependency graph over ``plan["workers"]`` (integer
``depends_on`` indices) and a pure eligibility-batch function a later
phase will use to dispatch independent Workers concurrently.

Nothing here changes execution: ``orchestration/go.py`` still executes
``plan["workers"]`` sequentially, exactly as before this phase.
``execute_progressively()``, ``AdaptiveRoutingService``,
``select_best_route()``, hard gates, review, escalation, and recovery are
all untouched.

The cycle-detection algorithm is NOT duplicated here: it is imported from
``nexus.orchestration.graph``, the same pure primitive
``nexus.missions.scheduler`` (the Mission/Task engine's Task DAG) now also
delegates to, so the Task graph and this Worker graph share one tested
algorithm instead of drifting into two incompatible DAG implementations.
"""

from nexus.orchestration.graph import GraphCycleError, detect_cycle


class WorkerDependencyError(ValueError):
    """Raised when a GO plan's Worker dependency graph is invalid."""


def normalize_worker_dependencies(workers: list) -> list:
    """Return a new list of worker dicts with ``depends_on`` defaulted to [].

    Never mutates ``workers`` or any worker dict in place. A worker with
    no ``depends_on`` key, or an explicit ``None``, is treated as
    ``depends_on=[]`` -- fully independent -- which is exactly how a plan
    generated before this phase already behaved (Requirement 8: old plans
    keep working unchanged).
    """
    normalized = []

    for worker in workers:
        new_worker = dict(worker)
        if new_worker.get("depends_on") is None:
            new_worker["depends_on"] = []
        normalized.append(new_worker)

    return normalized


def validate_worker_dependencies(workers: list) -> None:
    """Validate the ``depends_on`` graph of ``plan["workers"]``.

    ``workers`` should already be normalized (see
    ``normalize_worker_dependencies``) so every worker dict has a
    ``depends_on`` key. Indices refer to positions in ``workers`` itself
    (Requirement 1: zero-based, referencing another worker in the same
    plan). This function never reorders ``workers`` -- Mission/plan order
    is preserved by construction (Requirement 4).

    Raises ``WorkerDependencyError`` for any of:
      - ``depends_on`` missing or not a list;
      - a non-integer entry (bool is explicitly rejected even though
        Python's ``bool`` is an ``int`` subclass -- ``True``/``False`` are
        never valid worker indices);
      - a negative index;
      - an index that does not exist in this plan (``>= len(workers)``);
      - a self-dependency;
      - a duplicate dependency id on the same worker;
      - a dependency cycle anywhere in the plan.
    """
    worker_count = len(workers)

    for index, worker in enumerate(workers):
        depends_on = worker.get("depends_on")

        if not isinstance(depends_on, list):
            raise WorkerDependencyError(
                f"Worker {index} has an invalid depends_on (must be a "
                f"list of integers): {depends_on!r}"
            )

        seen = set()

        for raw_dep in depends_on:
            # bool is a subclass of int in Python -- True/False must never
            # be accepted as a worker index, so this check comes first and
            # is explicit rather than relying on isinstance(raw_dep, int).
            if isinstance(raw_dep, bool) or not isinstance(raw_dep, int):
                raise WorkerDependencyError(
                    f"Worker {index} has a non-integer depends_on entry: "
                    f"{raw_dep!r}"
                )

            if raw_dep < 0:
                raise WorkerDependencyError(
                    f"Worker {index} has a negative depends_on index: "
                    f"{raw_dep!r}"
                )

            if raw_dep >= worker_count:
                raise WorkerDependencyError(
                    f"Worker {index} depends on index {raw_dep!r}, which "
                    f"does not exist in this plan "
                    f"({worker_count} worker(s) total)."
                )

            if raw_dep == index:
                raise WorkerDependencyError(
                    f"Worker {index} depends on itself."
                )

            if raw_dep in seen:
                raise WorkerDependencyError(
                    f"Worker {index} has a duplicate depends_on index: "
                    f"{raw_dep!r}"
                )

            seen.add(raw_dep)

    graph = {
        index: list(worker.get("depends_on") or [])
        for index, worker in enumerate(workers)
    }

    try:
        detect_cycle(graph)
    except GraphCycleError as error:
        raise WorkerDependencyError(
            f"Worker dependency cycle detected: {error}"
        ) from error


def eligible_worker_batch(workers: list, completed_indices) -> list:
    """Return the deterministic, ascending list of currently-eligible worker
    indices.

    A worker index is eligible when it is not already in
    ``completed_indices`` and every index in its ``depends_on`` is in
    ``completed_indices``. Assumes ``workers`` has already passed
    ``validate_worker_dependencies`` (no unresolved/negative/cyclic
    indices). This is a pure function with no side effects -- the future
    parallel executor (not built in this phase) will call it once per
    scheduling round to get the full set of Workers ready to dispatch
    concurrently, instead of the one-at-a-time
    ``nexus.missions.scheduler.next_eligible_task`` pattern.

    Example (matching the spec):
        workers = [{"depends_on": []}, {"depends_on": []}, {"depends_on": [0, 1]}]
        eligible_worker_batch(workers, set())      -> [0, 1]
        eligible_worker_batch(workers, {0})        -> [1]
        eligible_worker_batch(workers, {0, 1})     -> [2]
    """
    completed = set(completed_indices)
    eligible = []

    for index, worker in enumerate(workers):
        if index in completed:
            continue

        depends_on = worker.get("depends_on") or []

        if all(dep in completed for dep in depends_on):
            eligible.append(index)

    return eligible


def sink_nodes(workers: list) -> list:
    """Return the sorted list of sink-node indices in the Worker DAG.

    A sink node is a Worker that is NOT a dependency of any other Worker --
    i.e. no other Worker has this index in its depends_on. For a single-
    sink DAG (typical: one synthesis Worker at the end) this returns
    exactly that Worker; for a flat plan with no dependencies every Worker
    is a sink.

    Assumes workers have been normalized (depends_on defaults to []).

    Returns indices in ascending order (deterministic).
    """
    depended_on: set[int] = set()

    for worker in workers:
        for dep in worker.get("depends_on") or []:
            depended_on.add(dep)

    return sorted(
        index
        for index in range(len(workers))
        if index not in depended_on
    )
