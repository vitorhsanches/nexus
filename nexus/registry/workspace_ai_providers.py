"""Operator switches for OmniRoute OAuth providers, independent of OAuth."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from nexus.registry.database import get_connection, initialize_database


def read() -> dict[str, bool]:
    try:
        with get_connection() as connection:
            rows = connection.execute("SELECT provider, enabled FROM workspace_ai_providers").fetchall()
    except sqlite3.OperationalError as error:
        if "no such table: workspace_ai_providers" not in str(error).lower():
            raise
        initialize_database()
        return read()
    return {row["provider"]: bool(row["enabled"]) for row in rows}


def set_enabled(provider: str, enabled: bool) -> None:
    read()  # Ensure an older database has the table before the write transaction.
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO workspace_ai_providers(provider, enabled, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
            (provider, int(enabled), datetime.now(UTC).isoformat()),
        )
