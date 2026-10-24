"""
Nexus run history cleanup.
Run once from Windows PowerShell: python cleanup_runs.py
Removes all BLOCKED and FAILED run records and marks stale RUNNING orphan
runs as FAILED so the web board starts clean.  COMPLETED runs are kept.
"""
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "nexus" / "storage" / "nexus.db"

if not DB_PATH.exists():
    sys.exit(f"DB not found at {DB_PATH}")

conn = sqlite3.connect(str(DB_PATH), timeout=10)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

cur = conn.cursor()

# 1. Mark orphan RUNNING runs (no owner) as FAILED
cur.execute("SELECT id FROM runs WHERE status='RUNNING' AND owner_pid IS NULL")
orphan_running = [r["id"] for r in cur.fetchall()]
for run_id in orphan_running:
    cur.execute(
        "UPDATE runs SET status='FAILED', finished_at=datetime('now') WHERE id=?",
        (run_id,),
    )
    cur.execute(
        """
        INSERT INTO checkpoints (run_id, sequence, boundary, worker_ordinal, payload_json, created_at)
        SELECT ?, COALESCE(MAX(sequence),0)+1, 'RUN_TERMINAL', NULL,
               '{"status":"FAILED","reason":"ORPHAN_NO_OWNER"}', datetime('now')
        FROM checkpoints WHERE run_id=?
        """,
        (run_id, run_id),
    )
    print(f"  Orphan -> FAILED: {run_id}")

conn.commit()

# 2. Delete all BLOCKED and FAILED runs with their dependent rows
cur.execute("SELECT id, status FROM runs WHERE status IN ('BLOCKED','FAILED') ORDER BY created_at")
to_delete = [(r["id"], r["status"]) for r in cur.fetchall()]
print(f"\nDeleting {len(to_delete)} BLOCKED/FAILED run records...")

for run_id, status in to_delete:
    cur.execute("DELETE FROM checkpoints WHERE run_id=?", (run_id,))
    cur.execute("DELETE FROM approved_plans WHERE run_id=?", (run_id,))
    cur.execute("DELETE FROM agents WHERE run_id=?", (run_id,))
    cur.execute("DELETE FROM runs WHERE id=?", (run_id,))
    print(f"  Deleted [{status}] {run_id}")

conn.commit()

# 3. Summary
cur.execute("SELECT id, status, created_at FROM runs ORDER BY created_at DESC")
remaining = cur.fetchall()
print(f"\nDone.  {len(remaining)} run(s) remaining:")
for r in remaining:
    print(f"  {r['id']} | {r['status']} | {r['created_at']}")

conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
conn.close()
