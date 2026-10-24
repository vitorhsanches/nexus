"""Durable state for a Nexus Program (prompt -> finished project).

A Program is stored as one JSON document under nexus/storage/programs/,
outside the target repository, so writing it never dirties the repo the
integration pipeline merges into. Every transition is persisted with an
atomic replace, which is what makes ``nexus build --resume`` safe after a
crash.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from nexus.orchestration.ownership import (
    classify_ownership,
    current_host,
    current_pid,
)
from nexus.registry.database import STORAGE_DIR


PROGRAMS_DIR = STORAGE_DIR / "programs"

PROGRAM_STATUSES = {
    "BOOTSTRAPPED",
    "SPECIFIED",
    "RUNNING",
    "COMPLETED",
    "BLOCKED",
}
PHASE_STATUSES = {"PENDING", "RUNNING", "DONE", "BLOCKED"}


class ProgramNotFoundError(KeyError):
    pass


class ProgramOwnershipError(RuntimeError):
    """Raised when a Program cannot be safely (re)started.

    ``code`` is one of:
      PROGRAM_OWNER_ACTIVE  -- a live process on this host already owns it.
      PROGRAM_OWNER_FOREIGN -- owned by a different/unknown host; Nexus
                                never assumes a remote PID is dead just
                                because it is not visible locally.
    """

    def __init__(self, code: str, message: str, program_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.program_id = program_id


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_program_id() -> str:
    return f"PROGRAM-{uuid4().hex[:8].upper()}"


def program_path(program_id: str) -> Path:
    return PROGRAMS_DIR / f"{program_id}.json"


def save_program(program: dict) -> None:
    if program.get("status") not in PROGRAM_STATUSES:
        raise ValueError(f"Invalid program status: {program.get('status')!r}")

    PROGRAMS_DIR.mkdir(parents=True, exist_ok=True)
    program["updated_at"] = now_iso()
    target = program_path(program["program_id"])

    fd, tmp = tempfile.mkstemp(dir=PROGRAMS_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(program, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_program(program_id: str) -> dict:
    path = program_path(program_id)
    if not path.exists():
        raise ProgramNotFoundError(f"Program not found: {program_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_programs() -> list[dict]:
    if not PROGRAMS_DIR.exists():
        return []
    programs = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in PROGRAMS_DIR.glob("PROGRAM-*.json")
    ]
    return sorted(programs, key=lambda p: p.get("created_at", ""), reverse=True)


def acquire_ownership(program_id: str) -> dict:
    """Deterministically claim execution ownership of ``program_id``.

    Reuses nexus.orchestration.ownership's Run-level primitives directly
    (``classify_ownership``/``current_host``/``current_pid``) rather than
    building a second ownership scheme: Program JSON gets the same
    owner_host/owner_pid/owner_acquired_at shape the Runs table already
    uses. Always reloads from disk first, so ownership is decided from the
    freshest committed state, not a possibly-stale in-memory copy.

    - unowned / dead-same-host  -> claimed, persisted, returned.
    - current_process           -> already ours; returned unchanged (no
                                    redundant write).
    - live_same_host            -> ProgramOwnershipError(PROGRAM_OWNER_ACTIVE).
    - foreign/unknown host      -> ProgramOwnershipError(PROGRAM_OWNER_FOREIGN),
                                    fail closed -- a remote PID is never
                                    assumed dead just because it is not
                                    visible on this host.

    A hard process death leaves ownership stamped with no live process
    behind it; the next acquire_ownership() call reconciles it
    deterministically via classify_ownership's dead_same_host path. No
    lock server, socket, or daemon is introduced.
    """
    program = load_program(program_id)
    cls = classify_ownership(
        program.get("owner_host"),
        program.get("owner_pid"),
        program.get("owner_acquired_at"),
    )

    if cls == "live_same_host":
        raise ProgramOwnershipError(
            "PROGRAM_OWNER_ACTIVE",
            f"Program {program_id} is still owned by live process "
            f"{program.get('owner_pid')} on {program.get('owner_host')}.",
            program_id=program_id,
        )
    if cls in ("foreign_host", "unknown"):
        raise ProgramOwnershipError(
            "PROGRAM_OWNER_FOREIGN",
            f"Program {program_id} is owned by a foreign/unknown host "
            f"{program.get('owner_host')!r}; failing closed rather than "
            "assuming it is dead.",
            program_id=program_id,
        )
    if cls == "current_process":
        return program

    # unowned / dead_same_host: safe to claim.
    program["owner_host"] = current_host()
    program["owner_pid"] = current_pid()
    program["owner_acquired_at"] = now_iso()
    save_program(program)
    return program


def release_ownership(program: dict) -> None:
    """Clear ownership on a deterministic exit path (COMPLETED/BLOCKED).

    Best-effort by nature: a hard process death cannot call this, which is
    fine -- the next acquire_ownership() call reconciles the stale
    ownership it left behind instead.
    """
    program["owner_host"] = None
    program["owner_pid"] = None
    program["owner_acquired_at"] = None
    save_program(program)


def append_event(program: dict, kind: str, **fields) -> None:
    program.setdefault("events", []).append(
        {"at": now_iso(), "kind": kind, **fields}
    )
