from nexus.registry.database import get_connection, initialize_database
from nexus.orchestration.ownership import try_acquire_for_recovery, classify_ownership
from nexus.orchestration.resume import resume_run, ResumeError

RECOVERABLE_RUN_STATUSES = ("CREATED", "ROUTING", "RUNNING", "REVIEWING")

def recover_all(dry_run=False):
    initialize_database()
    with get_connection() as conn:
        rows = conn.execute("SELECT id, status, owner_host, owner_pid FROM runs").fetchall()
    recovered = []
    skipped = []
    failed = []
    for r in rows:
        run_id = r["id"]
        status = r["status"]
        if status not in RECOVERABLE_RUN_STATUSES:
            skipped.append((run_id, status))
            continue
        owner_host = r["owner_host"] if "owner_host" in r.keys() else None
        owner_pid = r["owner_pid"] if "owner_pid" in r.keys() else None
        # need to ensure columns exist; if missing treat as unowned legacy => skip
        cls = classify_ownership(owner_host, owner_pid)
        if cls not in ("dead_same_host", "unowned"):
            skipped.append((run_id, cls))
            continue
        if dry_run:
            recovered.append(run_id)
            continue
        acquired = try_acquire_for_recovery(run_id)
        if not acquired:
            skipped.append((run_id, "race_lost"))
            continue
        try:
            resume_run(run_id)
            recovered.append(run_id)
        except ResumeError as e:
            failed.append((run_id, e.code, str(e)))
        except Exception as e:
            failed.append((run_id, "UNKNOWN", str(e)))
    return {"recovered": recovered, "skipped": skipped, "failed": failed}

def recover_one(run_id, dry_run=False):
    from nexus.registry.runs import get_run
    initialize_database()
    run = get_run(run_id)
    if run is None:
        raise ResumeError("RUN_NOT_FOUND", f"No Run {run_id!r}", run_id=run_id)
    if run["status"] not in RECOVERABLE_RUN_STATUSES:
        code = "RESUME_NOT_AVAILABLE" if run["status"] == "AWAITING_APPROVAL" else "RUN_TERMINAL"
        raise ResumeError(code, f"Not recoverable {run_id} from status {run['status']!r}", run_id=run_id)
    from nexus.registry.database import get_connection
    from nexus.orchestration.ownership import classify_ownership
    with get_connection() as conn:
        row = conn.execute("SELECT owner_host, owner_pid FROM runs WHERE id=?", (run_id,)).fetchone()
        cls = classify_ownership(row["owner_host"] if row else None, row["owner_pid"] if row else None)
    if cls not in ("dead_same_host", "unowned"):
        raise ResumeError("RECOVERY_NOT_ELIGIBLE", f"Run {run_id!r} not eligible: {cls}", run_id=run_id)
    if dry_run:
        return {"run_id": run_id, "dry_run": True}
    acquired = try_acquire_for_recovery(run_id)
    if not acquired:
        raise ResumeError("RECOVERY_RACE_LOST", f"Run {run_id!r} race lost", run_id=run_id)
    return resume_run(run_id)


ACTIVE_AGENT_STATUSES = ("QUEUED", "RUNNING", "WAITING", "REVIEWING")



def _has_plan_approved(run_id: str) -> bool:
    """Return True if an approved plan exists for *run_id*."""
    with get_connection() as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type=\'table\' AND name=\'approved_plans\'"
        ).fetchall()]
        if not tables:
            return False
        row = conn.execute(
            "SELECT COUNT(*) FROM approved_plans WHERE run_id=?", (run_id,)
        ).fetchone()
        return row is not None and row[0] > 0


def _release_ownership(run_id: str) -> None:
    """Release ownership of *run_id* by setting owner columns to NULL."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE runs SET owner_host=NULL, owner_pid=NULL, owner_acquired_at=NULL WHERE id=?",
            (run_id,)
        )
        conn.commit()


def reconcile_orphan_run(run_id: str, owner_pid, owner_host: str) -> None:
    """Mark all non-terminal agents for *run_id* as FAILED, mark the run as FAILED,
    and write a LIFECYCLE_ORPHAN_DETECTED checkpoint.

    Does NOT call resume_run() — detection only, not replay.
    """
    from nexus.registry.agents import list_agents_for_run, update_agent_status
    from nexus.registry.runs import update_run_status
    from nexus.registry.checkpoints import record_checkpoint
    from nexus.registry.database import get_connection

    agents = list_agents_for_run(run_id)
    stale_agent_ids = []
    for agent in agents:
        if agent["status"] in ACTIVE_AGENT_STATUSES:
            update_agent_status(agent["id"], "FAILED")
            stale_agent_ids.append(agent["id"])

    # Capture current run status before any update
    with get_connection() as conn:
        row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        previous_run_status = row["status"] if row else None

    # CASE A: no approved plan → run has no durable execution state → terminal FAILED
    # CASE B: approved plan exists → run has recoverable checkpoint state → release ownership only
    if _has_plan_approved(run_id):
        # CASE B: release ownership; run stays non-terminal so `nexus recover` can reacquire
        _release_ownership(run_id)
        case = "B_release_ownership"
    else:
        # CASE A: mark run terminal (no recoverable state)
        update_run_status(run_id, "FAILED")
        case = "A_terminal_failed"

    record_checkpoint(
        run_id=run_id,
        boundary="LIFECYCLE_ORPHAN_DETECTED",
        payload={
            "previous_run_status": previous_run_status,
            "stale_agent_ids": stale_agent_ids,
            "owner_pid": owner_pid,
            "owner_host": owner_host,
            "reason": "owner_process_dead",
            "case": case,
        },
    )


def reconcile_stale_ownership() -> list:
    """Scan all same-host non-terminal runs owned by dead PIDs.

    For each run whose owning PID is dead (or PID-reused):
      1. Mark all non-terminal agents as FAILED (with finished_at).
      2. Mark the run as FAILED (terminal).
      3. Write LIFECYCLE_ORPHAN_DETECTED checkpoint.

    Does NOT call resume_run() — detection only, not replay.
    Returns list of reconciled run_ids.

    Called automatically at GO startup to prevent permanently-orphaned state.
    """
    initialize_database()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, status, owner_host, owner_pid, owner_acquired_at FROM runs"
        ).fetchall()

    reconciled = []
    for r in rows:
        run_id = r["id"]
        status = r["status"]
        if status not in RECOVERABLE_RUN_STATUSES:
            continue
        owner_host = r["owner_host"] if "owner_host" in r.keys() else None
        owner_pid = r["owner_pid"] if "owner_pid" in r.keys() else None
        owner_acquired_at = (
            r["owner_acquired_at"] if "owner_acquired_at" in r.keys() else None
        )

        cls = classify_ownership(owner_host, owner_pid, owner_acquired_at)
        if cls != "dead_same_host":
            continue

        reconcile_orphan_run(run_id, owner_pid, owner_host)
        reconciled.append(run_id)

    return reconciled
