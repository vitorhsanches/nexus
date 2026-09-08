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
        if cls != "dead_same_host":
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
    if cls != "dead_same_host":
        raise ResumeError("RECOVERY_NOT_ELIGIBLE", f"Run {run_id!r} not eligible: {cls}", run_id=run_id)
    if dry_run:
        return {"run_id": run_id, "dry_run": True}
    acquired = try_acquire_for_recovery(run_id)
    if not acquired:
        raise ResumeError("RECOVERY_RACE_LOST", f"Run {run_id!r} race lost", run_id=run_id)
    return resume_run(run_id)
