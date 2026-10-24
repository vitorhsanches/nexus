"""Durable operator selection for qualified OmniRoute models."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from nexus.registry.database import get_connection, initialize_database


def _now() -> str:
    return datetime.now(UTC).isoformat()


def read() -> dict:
    try:
        with get_connection() as connection:
            meta = connection.execute(
                "SELECT selection_configured FROM workspace_model_settings_meta WHERE id = 1"
            ).fetchone()
            rows = connection.execute(
                "SELECT model_id, active, candidate, updated_at FROM workspace_model_settings"
            ).fetchall()
    except sqlite3.OperationalError as error:
        # App routing may be used before the web lifespan initializes the
        # registry (for example, by a local CLI or a standalone router).
        # Initialize the actual schema and retry; do not turn a missing table
        # into an implicit policy bypass or swallow unrelated database errors.
        message = str(error).lower()
        if not any(
            f"no such table: {table}" in message
            for table in ("workspace_model_settings_meta", "workspace_model_settings")
        ):
            raise
        initialize_database()
        with get_connection() as connection:
            meta = connection.execute(
                "SELECT selection_configured FROM workspace_model_settings_meta WHERE id = 1"
            ).fetchone()
            rows = connection.execute(
                "SELECT model_id, active, candidate, updated_at FROM workspace_model_settings"
            ).fetchall()
    return {
        "selection_configured": bool(meta and meta["selection_configured"]),
        "models": {
            row["model_id"]: {
                "active": bool(row["active"]),
                "candidate": bool(row["candidate"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        },
    }


def effective_active_ids(qualified_ids: set[str]) -> set[str]:
    state = read()
    if not state["selection_configured"]:
        return set(qualified_ids)
    return {
        model_id for model_id in qualified_ids
        if state["models"].get(model_id, {}).get("active", False)
    }


def set_active(model_id: str, active: bool, qualified_ids: set[str]) -> dict:
    """Atomically configure the selection and change one qualified model.

    On the first operator change, seed today's qualified set so enabling or
    disabling one entry doesn't unexpectedly change any other current route.
    Models qualified later remain inactive until explicitly activated.
    """
    read()  # Ensure the persisted schema exists before the write transaction.
    now = _now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        meta = connection.execute(
            "SELECT selection_configured FROM workspace_model_settings_meta WHERE id = 1"
        ).fetchone()
        if not meta or not meta["selection_configured"]:
            connection.executemany(
                "INSERT OR IGNORE INTO workspace_model_settings(model_id, active, candidate, updated_at) VALUES (?, 1, 0, ?)",
                [(known_id, now) for known_id in sorted(qualified_ids)],
            )
        connection.execute(
            "INSERT INTO workspace_model_settings_meta(id, selection_configured, updated_at) VALUES (1, 1, ?) "
            "ON CONFLICT(id) DO UPDATE SET selection_configured = 1, updated_at = excluded.updated_at",
            (now,),
        )
        connection.execute(
            "INSERT INTO workspace_model_settings(model_id, active, candidate, updated_at) VALUES (?, ?, 0, ?) "
            "ON CONFLICT(model_id) DO UPDATE SET active = excluded.active, updated_at = excluded.updated_at",
            (model_id, int(active), now),
        )
        connection.commit()
    return read()


def set_candidate(model_id: str, candidate: bool) -> dict:
    read()  # Ensure the persisted schema exists before the write transaction.
    now = _now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO workspace_model_settings(model_id, active, candidate, updated_at) VALUES (?, 0, ?, ?) "
            "ON CONFLICT(model_id) DO UPDATE SET candidate = excluded.candidate, updated_at = excluded.updated_at",
            (model_id, int(candidate), now),
        )
        connection.commit()
    return read()
