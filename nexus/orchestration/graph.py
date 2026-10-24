"""Generic, pure dependency-graph primitives shared by every Nexus DAG
consumer.

These functions know nothing about Task or Worker objects -- they operate
on a plain adjacency mapping ``{node_id: [dependency_id, ...]}``. This is
the single cycle-detection algorithm reused by both
``nexus.missions.scheduler`` (the Mission/Task engine's Task DAG, string
ids) and ``nexus.orchestration.dag`` (a GO plan's Worker DAG, integer
indices), so the algorithm only needs to be correct and tested once, and
both consumers stay behaviorally identical instead of drifting apart as
two incompatible DAG implementations.

Nothing here touches execution, routing, review, escalation, or recovery
-- it is pure graph math over plain dicts/lists.
"""


class GraphCycleError(ValueError):
    """Raised when a dependency graph contains a cycle."""


def detect_cycle(graph: dict) -> None:
    """Raise GraphCycleError if ``graph`` contains a dependency cycle.

    ``graph`` maps each node id to a list of the node ids it depends on.
    A dependency id that is not itself a key in ``graph`` is ignored here
    (callers are expected to validate id existence separately, before
    calling this) -- this function only detects cycles among nodes that
    are present as keys.

    Plain 3-color DFS (WHITE = unvisited, GRAY = on the current recursion
    stack, BLACK = fully processed). Deterministic given a deterministically
    ordered ``graph`` (Python dicts preserve insertion order).
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node_id: WHITE for node_id in graph}

    def visit(node_id, stack):
        color[node_id] = GRAY
        for dep_id in graph.get(node_id, []):
            if dep_id not in graph:
                continue
            if color[dep_id] == GRAY:
                raise GraphCycleError(
                    "Dependency cycle detected involving node "
                    f"{dep_id!r} (via "
                    f"{' -> '.join(str(item) for item in stack + [dep_id])})."
                )
            if color[dep_id] == WHITE:
                visit(dep_id, stack + [dep_id])
        color[node_id] = BLACK

    for node_id in graph:
        if color[node_id] == WHITE:
            visit(node_id, [node_id])
