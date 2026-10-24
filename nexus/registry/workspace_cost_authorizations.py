"""Durable, atomic cost-consent records for Workspace executions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from nexus.registry.database import get_connection

LEASE_SECONDS = 2 * 60 * 60


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime) -> str:
    return value.isoformat()


def _view(row, active: int = 0) -> dict:
    return {
        "authorization_id": row["id"], "project_id": row["project_id"],
        "model": row["model_id"], "cost_status": row["cost_status"],
        "max_uses": row["max_uses"], "uses_reserved": row["uses_reserved"],
        "uses_remaining": max(0, row["max_uses"] - row["uses_reserved"]),
        "max_concurrent": row["max_concurrent"], "active_executions": active,
        "expires_at": row["expires_at"], "status": row["status"],
        "created_at": row["created_at"], "revoked_at": row["revoked_at"],
    }


def list_for_project(project_id: str) -> list[dict]:
    with get_connection() as connection:
        now = _stamp(_now())
        connection.execute(
            "UPDATE workspace_cost_authorizations SET status='EXPIRED' "
            "WHERE project_id=? AND status='ACTIVE' AND expires_at<=?", (project_id, now),
        )
        rows = connection.execute(
            "SELECT * FROM workspace_cost_authorizations WHERE project_id=? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
        return [
            _view(row, connection.execute(
                """SELECT COUNT(*) FROM workspace_cost_authorization_leases AS lease
                   JOIN workspace_cost_authorizations AS auth ON auth.id=lease.authorization_id
                   WHERE auth.project_id=? AND auth.model_id=?
                   AND lease.released_at IS NULL AND lease.expires_at>?""",
                (row["project_id"], row["model_id"], now),
            ).fetchone()[0]) for row in rows
        ]


def create(project_id: str, model_id: str, cost_status: str, *, max_uses: int,
           expires_at: str, max_concurrent: int) -> dict:
    auth_id, now = uuid4().hex, _stamp(_now())
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE workspace_cost_authorizations SET status='EXPIRED' "
            "WHERE project_id=? AND model_id=? AND status='ACTIVE' AND expires_at<=?",
            (project_id, model_id, now),
        )
        connection.execute(
            "UPDATE workspace_cost_authorizations SET status='REVOKED', revoked_at=? "
            "WHERE project_id=? AND model_id=? AND status='ACTIVE'", (now, project_id, model_id),
        )
        connection.execute(
            """INSERT INTO workspace_cost_authorizations
               (id, project_id, model_id, cost_status, max_uses, expires_at, max_concurrent, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (auth_id, project_id, model_id, cost_status, max_uses, expires_at, max_concurrent, now),
        )
        row = connection.execute("SELECT * FROM workspace_cost_authorizations WHERE id=?", (auth_id,)).fetchone()
        return _view(row)


def revoke(project_id: str, authorization_id: str) -> bool:
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE workspace_cost_authorizations SET status='REVOKED', revoked_at=? "
            "WHERE id=? AND project_id=? AND status='ACTIVE'",
            (_stamp(_now()), authorization_id, project_id),
        )
        return cursor.rowcount == 1


def reserve(project_id: str, model_id: str) -> tuple[str, dict] | None:
    """Consume one use and one concurrency slot atomically; fail closed."""
    now = _now()
    stamp = _stamp(now)
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE workspace_cost_authorization_leases SET released_at=? "
            "WHERE released_at IS NULL AND expires_at<=?", (stamp, stamp),
        )
        row = connection.execute(
            """SELECT * FROM workspace_cost_authorizations
               WHERE project_id=? AND model_id=? AND status='ACTIVE' ORDER BY created_at DESC LIMIT 1""",
            (project_id, model_id),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] <= stamp:
            connection.execute("UPDATE workspace_cost_authorizations SET status='EXPIRED' WHERE id=?", (row["id"],))
            return None
        active = connection.execute(
            """SELECT COUNT(*) FROM workspace_cost_authorization_leases AS lease
               JOIN workspace_cost_authorizations AS auth ON auth.id=lease.authorization_id
               WHERE auth.project_id=? AND auth.model_id=?
               AND lease.released_at IS NULL AND lease.expires_at>?""",
            (project_id, model_id, stamp),
        ).fetchone()[0]
        if row["uses_reserved"] >= row["max_uses"] or active >= row["max_concurrent"]:
            return None
        cursor = connection.execute(
            """UPDATE workspace_cost_authorizations SET uses_reserved=uses_reserved+1
               WHERE id=? AND status='ACTIVE' AND expires_at>? AND uses_reserved<max_uses""",
            (row["id"], stamp),
        )
        if cursor.rowcount != 1:
            return None
        lease_id = uuid4().hex
        connection.execute(
            """INSERT INTO workspace_cost_authorization_leases
               (id, authorization_id, created_at, expires_at) VALUES (?, ?, ?, ?)""",
            (lease_id, row["id"], stamp, _stamp(now + timedelta(seconds=LEASE_SECONDS))),
        )
        updated = connection.execute("SELECT * FROM workspace_cost_authorizations WHERE id=?", (row["id"],)).fetchone()
        return lease_id, _view(updated, active + 1)


def bind_lease(lease_id: str, execution_kind: str, execution_id: str) -> None:
    with get_connection() as connection:
        connection.execute(
            "UPDATE workspace_cost_authorization_leases SET execution_kind=?, execution_id=? WHERE id=?",
            (execution_kind, execution_id, lease_id),
        )


def release(lease_id: str) -> None:
    with get_connection() as connection:
        connection.execute(
            "UPDATE workspace_cost_authorization_leases SET released_at=? WHERE id=? AND released_at IS NULL",
            (_stamp(_now()), lease_id),
        )


def release_execution(execution_kind: str, execution_id: str) -> None:
    with get_connection() as connection:
        connection.execute(
            """UPDATE workspace_cost_authorization_leases SET released_at=?
               WHERE execution_kind=? AND execution_id=? AND released_at IS NULL""",
            (_stamp(_now()), execution_kind, execution_id),
        )
