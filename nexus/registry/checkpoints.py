"""Durable execution checkpoint registry for Nexus v2.0-F.1.

This module persists immutable Manager-approved plans and append-only,
per-Run execution checkpoints.

It intentionally does not implement resume behavior. Future recovery
logic may read this history, but F.1 owns persistence only.

Writes are fail-closed:
- serialization errors raise CheckpointWriteError;
- SQLite persistence errors raise CheckpointWriteError;
- a different plan cannot replace an already-approved plan;
- checkpoint sequence allocation is protected by an immediate write
  transaction so concurrent writers cannot independently allocate the
  same per-Run sequence.
"""

import json
import sqlite3
from contextlib import closing

from nexus.registry.database import get_connection


class CheckpointWriteError(RuntimeError):
    """Raised when durable orchestration state cannot be persisted."""


def _serialize(payload: dict) -> str:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise CheckpointWriteError(
            f"Checkpoint payload is not JSON-serializable: {error}"
        ) from error


def record_approved_plan(run_id: str, plan: dict) -> None:
    """Persist the immutable Manager-approved plan for a Run.

    Recording the exact same canonical plan again is idempotent.
    Attempting to replace it with a different approved plan fails closed.
    """

    plan_json = _serialize(plan)

    try:
        with closing(get_connection()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")

                row = connection.execute(
                    """
                    SELECT plan_json
                    FROM approved_plans
                    WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()

                if row is None:
                    connection.execute(
                        """
                        INSERT INTO approved_plans (
                            run_id,
                            plan_json
                        )
                        VALUES (?, ?)
                        """,
                        (run_id, plan_json),
                    )
                    return

                if row["plan_json"] != plan_json:
                    raise CheckpointWriteError(
                        "Approved plan is immutable and cannot be replaced "
                        f"for run {run_id!r}."
                    )

    except CheckpointWriteError:
        raise

    except sqlite3.Error as error:
        raise CheckpointWriteError(
            f"Failed to persist approved plan for run {run_id!r}: {error}"
        ) from error


def get_approved_plan(run_id: str) -> dict | None:
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT plan_json
            FROM approved_plans
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

    if row is None:
        return None

    return json.loads(row["plan_json"])


def record_checkpoint(
    run_id: str,
    boundary: str,
    payload: dict,
    worker_ordinal: int | None = None,
) -> int:
    """Append one ordered checkpoint and return its per-Run sequence."""

    payload_json = _serialize(payload)

    try:
        with closing(get_connection()) as connection:
            with connection:
                # Serialize writers before reading MAX(sequence). Without an
                # immediate transaction, concurrent writers could both observe
                # the same previous maximum before either inserts.
                connection.execute("BEGIN IMMEDIATE")

                row = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) AS max_sequence
                FROM checkpoints
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()

                sequence = int(row["max_sequence"]) + 1

                connection.execute(
                    """
                    INSERT INTO checkpoints (
                        run_id,
                        sequence,
                        boundary,
                        worker_ordinal,
                        payload_json
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        sequence,
                        boundary,
                        worker_ordinal,
                        payload_json,
                    ),
                )

    except sqlite3.Error as error:
        raise CheckpointWriteError(
            f"Failed to persist checkpoint for run {run_id!r} "
            f"boundary={boundary!r}: {error}"
        ) from error

    return sequence


def list_checkpoints(run_id: str) -> list[dict]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT
                sequence,
                boundary,
                worker_ordinal,
                payload_json,
                created_at
            FROM checkpoints
            WHERE run_id = ?
            ORDER BY sequence ASC
            """,
            (run_id,),
        ).fetchall()

    return [
        {
            "sequence": row["sequence"],
            "boundary": row["boundary"],
            "worker_ordinal": row["worker_ordinal"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def get_latest_checkpoint(run_id: str) -> dict | None:
    """Return the latest durable checkpoint for a Run."""

    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT
                sequence,
                boundary,
                worker_ordinal,
                payload_json,
                created_at
            FROM checkpoints
            WHERE run_id = ?
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()

    if row is None:
        return None

    return {
        "sequence": row["sequence"],
        "boundary": row["boundary"],
        "worker_ordinal": row["worker_ordinal"],
        "payload": json.loads(row["payload_json"]),
        "created_at": row["created_at"],
    }
