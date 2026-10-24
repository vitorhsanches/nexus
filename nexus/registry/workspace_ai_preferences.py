"""Project-scoped, operator-owned AI defaults. No execution consent is stored here."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from nexus.registry.database import get_connection, initialize_database


def read(project_id: str) -> dict[str, dict]:
    try:
        with get_connection() as connection:
            rows = connection.execute(
                "SELECT role, executor, model_id, effort, updated_at "
                "FROM workspace_ai_preferences WHERE project_id = ?",
                (project_id,),
            ).fetchall()
    except sqlite3.OperationalError as error:
        if "no such table: workspace_ai_preferences" not in str(error).lower():
            raise
        initialize_database()
        return read(project_id)
    return {
        row["role"]: {
            "executor": row["executor"],
            "model_id": row["model_id"],
            "effort": row["effort"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    }


def save(project_id: str, role: str, executor: str, model_id: str, effort: str) -> dict:
    read(project_id)  # Initialize a legacy database before the write transaction.
    updated_at = datetime.now(UTC).isoformat()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO workspace_ai_preferences "
            "(project_id, role, executor, model_id, effort, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_id, role) DO UPDATE SET "
            "executor=excluded.executor, model_id=excluded.model_id, "
            "effort=excluded.effort, updated_at=excluded.updated_at",
            (project_id, role, executor, model_id, effort, updated_at),
        )
    return read(project_id)[role]


def clear(project_id: str, role: str) -> None:
    read(project_id)
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM workspace_ai_preferences WHERE project_id = ? AND role = ?",
            (project_id, role),
        )
