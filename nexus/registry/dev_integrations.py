"""Durable review and integration history for Development Mode (F3.2)."""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone

from nexus.registry.database import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_review(*, review_id: str, dev_session_id: str, project_id: str,
                base_sha: str, target_head: str, target_branch: str,
                snapshot: dict) -> dict:
    now = _now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT OR IGNORE INTO dev_integration_reviews
               (review_id, dev_session_id, project_id, base_sha, target_head,
                target_branch, snapshot_json, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'REVIEWED', ?, ?)""",
            (review_id, dev_session_id, project_id, base_sha, target_head,
             target_branch, json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), now, now),
        )
        row = connection.execute(
            "SELECT * FROM dev_integration_reviews WHERE review_id = ?", (review_id,)
        ).fetchone()
        return _dict(row)


def get_review(review_id: str):
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM dev_integration_reviews WHERE review_id = ?", (review_id,)
        ).fetchone()
    return _dict(row) if row else None


def list_reviews(dev_session_id: str) -> list[dict]:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM dev_integration_reviews WHERE dev_session_id = ? ORDER BY created_at DESC",
            (dev_session_id,),
        ).fetchall()
    return [_dict(row) for row in rows]


def update_review(review_id: str, *, status: str, candidate_sha: str | None = None,
                  result_sha: str | None = None, error_code: str | None = None,
                  error_message: str | None = None) -> bool:
    with get_connection() as connection:
        cursor = connection.execute(
            """UPDATE dev_integration_reviews SET status = ?,
               candidate_sha = COALESCE(?, candidate_sha),
               result_sha = COALESCE(?, result_sha), error_code = ?,
               error_message = ?, updated_at = ? WHERE review_id = ?""",
            (status, candidate_sha, result_sha, error_code, error_message, _now(), review_id),
        )
        return cursor.rowcount == 1


def acquire_project_lock(project_id: str, review_id: str) -> bool:
    """Atomically serialize apply attempts across processes."""
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT review_id FROM dev_integration_locks WHERE project_id = ?", (project_id,)
        ).fetchone()
        if existing:
            return False
        connection.execute(
            """INSERT INTO dev_integration_locks
               (project_id, review_id, owner_host, owner_pid, acquired_at)
               VALUES (?, ?, ?, ?, ?)""",
            (project_id, review_id, socket.gethostname(), os.getpid(), _now()),
        )
        return True


def release_project_lock(project_id: str, review_id: str) -> None:
    with get_connection() as connection:
        connection.execute(
            "DELETE FROM dev_integration_locks WHERE project_id = ? AND review_id = ?",
            (project_id, review_id),
        )


def get_project_lock(project_id: str):
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM dev_integration_locks WHERE project_id = ?", (project_id,)
        ).fetchone()
    return dict(row) if row else None


def _dict(row) -> dict:
    result = dict(row)
    result["snapshot"] = json.loads(result.pop("snapshot_json"))
    return result
