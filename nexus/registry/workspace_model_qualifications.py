"""Durable, explicit qualification policy for discovered OmniRoute models."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from nexus.registry.database import get_connection


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _decode(row) -> dict:
    return {
        "model_id": row["model_id"],
        "provider": row["provider"],
        "capabilities": json.loads(row["capabilities_json"]),
        "efforts": json.loads(row["efforts_json"]),
        "cost_class": row["cost_class"],
        "max_risk_level": row["max_risk_level"],
        "compatibility": json.loads(row["compatibility_json"]),
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "revoked_at": row["revoked_at"],
    }


def list_all() -> dict[str, dict]:
    try:
        with get_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM workspace_model_qualifications"
            ).fetchall()
    except (PermissionError, sqlite3.OperationalError) as error:
        # Some read-only tools/tests invoke the static router before the
        # registry schema is initialized. Dynamic policy then fails closed.
        if isinstance(error, sqlite3.OperationalError) and "no such table" not in str(error).lower():
            raise
        return {}
    return {row["model_id"]: _decode(row) for row in rows}


def qualified() -> dict[str, dict]:
    return {
        model_id: record
        for model_id, record in list_all().items()
        if record["status"] == "QUALIFIED"
    }


def save(model_id: str, *, provider: str, capabilities: list[str], efforts: list[str],
         cost_class: str, max_risk_level: str, compatibility: dict) -> dict:
    now = _now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO workspace_model_qualifications
               (model_id, provider, capabilities_json, efforts_json, cost_class,
                max_risk_level, compatibility_json, status, created_at, updated_at, revoked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'QUALIFIED', ?, ?, NULL)
               ON CONFLICT(model_id) DO UPDATE SET provider=excluded.provider,
                capabilities_json=excluded.capabilities_json, efforts_json=excluded.efforts_json,
                cost_class=excluded.cost_class, max_risk_level=excluded.max_risk_level,
                compatibility_json=excluded.compatibility_json, status='QUALIFIED',
                updated_at=excluded.updated_at, revoked_at=NULL""",
            (model_id, provider, json.dumps(sorted(set(capabilities))),
             json.dumps(sorted(set(efforts))), cost_class, max_risk_level,
             json.dumps(compatibility, sort_keys=True), now, now),
        )
        connection.execute(
            "INSERT INTO workspace_model_settings(model_id, active, candidate, updated_at) VALUES (?, 0, 0, ?) "
            "ON CONFLICT(model_id) DO UPDATE SET active=0, candidate=0, updated_at=excluded.updated_at",
            (model_id, now),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM workspace_model_qualifications WHERE model_id = ?", (model_id,)
        ).fetchone()
    return _decode(row)


def revoke(model_id: str) -> dict | None:
    now = _now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE workspace_model_qualifications SET status='REVOKED', updated_at=?, revoked_at=? WHERE model_id=?",
            (now, now, model_id),
        )
        # Revocation also clears activation. Requalification must be followed
        # by a fresh, explicit P3.3D activation.
        connection.execute(
            "UPDATE workspace_model_settings SET active=0, updated_at=? WHERE model_id=?",
            (now, model_id),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM workspace_model_qualifications WHERE model_id = ?", (model_id,)
        ).fetchone()
    return _decode(row) if row else None
