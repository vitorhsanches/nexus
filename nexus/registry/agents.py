from datetime import datetime, timezone
from uuid import uuid4

from nexus.registry.database import get_connection


ACTIVE_AGENT_STATUSES = (
    "QUEUED",
    "RUNNING",
    "WAITING",
    "REVIEWING",
)

# Last-resort safety bound on the durable `full_result` column -- guards
# SQLite/the API against a pathological dump (a runaway tool loop echoing
# megabytes of text), never a realistic report. Deliberately far larger
# than the legacy 8000/16000-char `result` preview bounds, which exist only
# to keep bulk agent listings small, not to cap a deliverable's real size.
FULL_RESULT_SAFETY_LIMIT = 2_000_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_agent(
    run_id: str,
    role: str,
    provider: str,
    model: str,
    effort: str = "low",
    status: str = "QUEUED",
    parent_agent_id: str | None = None,
    branch: str | None = None,
    worktree: str | None = None,
    route_class: str | None = None,
) -> str:
    """Create one agent row.

    ``provider``/``model``/``effort`` are the ACTUAL execution identity --
    for a Worker, the route already resolved by the Adaptive Router (see
    nexus.orchestration.progressive.resolve_initial_worker_route), never
    the Manager's plan-time guess. ``route_class`` is the one planned
    field that IS authoritative: the Manager's capability requirement,
    which the router maps to a capability class but never overwrites.
    """
    agent_id = f"AGENT-{uuid4().hex[:8].upper()}"

    started_at = _now() if status == "RUNNING" else None

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO agents (
                id,
                run_id,
                role,
                provider,
                model,
                effort,
                status,
                branch,
                worktree,
                parent_agent_id,
                started_at,
                route_class
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_id,
                run_id,
                role,
                provider,
                model,
                effort,
                status,
                branch,
                worktree,
                parent_agent_id,
                started_at,
                route_class,
            ),
        )

    return agent_id


def list_agents(active_only: bool = False):
    where_clause = ""

    if active_only:
        placeholders = ",".join("?" for _ in ACTIVE_AGENT_STATUSES)
        where_clause = f"WHERE a.status IN ({placeholders})"
        params = ACTIVE_AGENT_STATUSES
    else:
        params = ()

    with get_connection() as connection:
        return connection.execute(
            f"""
            SELECT
                a.id,
                a.run_id,
                p.name AS project_name,
                a.role,
                a.provider,
                a.model,
                a.effort,
                a.status,
                a.branch,
                a.worktree,
                a.route_class,
                (a.full_result IS NOT NULL) AS has_full_result
            FROM agents a
            JOIN runs r ON r.id = a.run_id
            JOIN projects p ON p.id = r.project_id
            {where_clause}
            ORDER BY
                CASE a.status
                    WHEN 'RUNNING' THEN 1
                    WHEN 'REVIEWING' THEN 2
                    WHEN 'WAITING' THEN 3
                    WHEN 'QUEUED' THEN 4
                    ELSE 5
                END,
                a.started_at DESC
            """,
            params,
        ).fetchall()


def count_active_agents() -> int:
    placeholders = ",".join("?" for _ in ACTIVE_AGENT_STATUSES)

    with get_connection() as connection:
        row = connection.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM agents
            WHERE status IN ({placeholders})
            """,
            ACTIVE_AGENT_STATUSES,
        ).fetchone()

    return int(row["total"])


def update_agent_status(agent_id: str, status: str) -> None:
    fields = ["status = ?"]
    params = [status]

    if status == "RUNNING":
        fields.append("started_at = COALESCE(started_at, ?)")
        params.append(_now())

    if status in {"COMPLETED", "BLOCKED", "FAILED", "CANCELLED"}:
        fields.append("finished_at = ?")
        params.append(_now())

    params.append(agent_id)

    with get_connection() as connection:
        connection.execute(
            f"""
            UPDATE agents
            SET {", ".join(fields)}
            WHERE id = ?
            """,
            params,
        )


def update_agent_execution(
    agent_id: str,
    branch: str | None = None,
    worktree: str | None = None,
    result: str | None = None,
    full_result: str | None = None,
) -> None:
    """Persist execution metadata for one agent.

    ``result`` is the existing bounded preview column: unchanged behaviour,
    unchanged callers. ``full_result`` is the complete, untruncated
    deliverable text (e.g. a Worker's semantic last-message output) --
    pass it only for the genuine user-facing result, never for a raw
    diagnostic transcript. Capped by FULL_RESULT_SAFETY_LIMIT as a last
    resort; `full_result_truncated` is set only if that huge bound was
    actually hit, so truncation is always explicit, never silent.
    """
    fields = []
    params = []

    if branch is not None:
        fields.append("branch = ?")
        params.append(branch)

    if worktree is not None:
        fields.append("worktree = ?")
        params.append(worktree)

    if result is not None:
        fields.append("result = ?")
        params.append(result)

    if full_result is not None:
        hit_safety_limit = len(full_result) > FULL_RESULT_SAFETY_LIMIT
        fields.append("full_result = ?")
        params.append(full_result[:FULL_RESULT_SAFETY_LIMIT] if hit_safety_limit else full_result)
        fields.append("full_result_truncated = ?")
        params.append(1 if hit_safety_limit else 0)

    if not fields:
        return

    params.append(agent_id)

    with get_connection() as connection:
        connection.execute(
            f"""
            UPDATE agents
            SET {", ".join(fields)}
            WHERE id = ?
            """,
            params,
        )


def list_agents_for_run(run_id: str):
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT
                a.id,
                a.run_id,
                a.role,
                a.provider,
                a.model,
                a.effort,
                a.status,
                a.branch,
                a.worktree,
                a.parent_agent_id,
                a.started_at,
                a.finished_at,
                a.result,
                a.route_class,
                (a.full_result IS NOT NULL) AS has_full_result
            FROM agents a
            WHERE a.run_id = ?
            ORDER BY
                COALESCE(a.started_at, "9999"),
                a.id
            """,
            (run_id,),
        ).fetchall()


def get_agent(agent_id: str):
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT
                id,
                run_id,
                role,
                provider,
                model,
                effort,
                status,
                branch,
                worktree,
                parent_agent_id,
                started_at,
                finished_at,
                result
            FROM agents
            WHERE id = ?
            """,
            (agent_id,),
        ).fetchone()


def get_agent_result(agent_id: str) -> dict | None:
    """Explicit, on-demand accessor for one agent's complete result.

    Deliberately separate from get_agent()/list_agents*() so that no bulk
    listing or routine status read ever pulls the (potentially large)
    full_result column -- only this function, backing the dedicated
    full-result API endpoint, does.
    """
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT id, result, full_result, full_result_truncated
            FROM agents
            WHERE id = ?
            """,
            (agent_id,),
        ).fetchone()

    if row is None:
        return None

    full_result = row["full_result"]
    return {
        "agent_id": row["id"],
        "preview": row["result"],
        "has_full_result": full_result is not None,
        "full_result": full_result,
        "full_result_truncated": bool(row["full_result_truncated"]),
    }
