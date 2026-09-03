from datetime import datetime, timezone
from uuid import uuid4

from nexus.registry.database import get_connection


ACTIVE_RUN_STATUSES = (
    "CREATED",
    "ROUTING",
    "RUNNING",
    "REVIEWING",
    "AWAITING_APPROVAL",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_run(
    project_id: str,
    input_text: str,
    intent: str = "GO",
    status: str = "CREATED",
    risk: str | None = None,
) -> str:
    run_id = f"RUN-{uuid4().hex[:8].upper()}"

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO runs (
                id,
                project_id,
                input,
                intent,
                status,
                risk,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                project_id,
                input_text,
                intent,
                status,
                risk,
                _now(),
            ),
        )

    return run_id


def list_runs():
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT
                r.id,
                p.name AS project_name,
                r.intent,
                r.status,
                r.risk,
                r.created_at,
                COUNT(a.id) AS agent_count
            FROM runs r
            JOIN projects p ON p.id = r.project_id
            LEFT JOIN agents a ON a.run_id = r.id
            GROUP BY r.id
            ORDER BY r.created_at DESC
            """
        ).fetchall()


def count_active_runs() -> int:
    placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)

    with get_connection() as connection:
        row = connection.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM runs
            WHERE status IN ({placeholders})
            """,
            ACTIVE_RUN_STATUSES,
        ).fetchone()

    return int(row["total"])


def update_run_status(run_id: str, status: str) -> None:
    fields = ["status = ?"]
    params = [status]

    if status == "RUNNING":
        fields.append("started_at = COALESCE(started_at, ?)")
        params.append(_now())

    if status in {"COMPLETED", "BLOCKED", "FAILED", "CANCELLED"}:
        fields.append("finished_at = ?")
        params.append(_now())

    params.append(run_id)

    with get_connection() as connection:
        connection.execute(
            f"""
            UPDATE runs
            SET {", ".join(fields)}
            WHERE id = ?
            """,
            params,
        )


def get_run(run_id: str):
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT
                r.id,
                r.project_id,
                p.name AS project_name,
                r.input,
                r.intent,
                r.status,
                r.risk,
                r.created_at,
                r.started_at,
                r.finished_at,
                r.result,
                r.commit_sha
            FROM runs r
            JOIN projects p ON p.id = r.project_id
            WHERE r.id = ?
            """,
            (run_id,),
        ).fetchone()


def update_run_result(run_id: str, result: str | None = None, commit_sha: str | None = None) -> None:
    fields = []
    params = []

    if result is not None:
        fields.append("result = ?")
        params.append(result)

    if commit_sha is not None:
        fields.append("commit_sha = ?")
        params.append(commit_sha)

    if not fields:
        return

    params.append(run_id)

    with get_connection() as connection:
        connection.execute(
            f"""
            UPDATE runs
            SET {", ".join(fields)}
            WHERE id = ?
            """,
            params,
        )
