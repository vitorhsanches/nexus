from datetime import datetime, timezone
from uuid import uuid4

from nexus.registry.database import get_connection


ACTIVE_AGENT_STATUSES = (
    "QUEUED",
    "RUNNING",
    "WAITING",
    "REVIEWING",
)


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
) -> str:
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
                started_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                a.worktree
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
) -> None:
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
                a.result
            FROM agents a
            WHERE a.run_id = ?
            ORDER BY
                COALESCE(a.started_at, "9999"),
                a.id
            """,
            (run_id,),
        ).fetchall()
