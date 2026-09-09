from __future__ import annotations
import dataclasses
from nexus.registry.database import get_connection
from nexus.orchestration.ownership import classify_ownership
from nexus.board.models import BoardColumn

RECOVERABLE_RUN_STATUSES = frozenset(("CREATED","ROUTING","RUNNING","REVIEWING"))
ACTIVE_RUN_STATUSES = frozenset(("CREATED","ROUTING","RUNNING","REVIEWING","AWAITING_APPROVAL"))
TERMINAL_RUN_STATUSES = frozenset(("COMPLETED","BLOCKED","FAILED","CANCELLED"))

_STATUS_TO_COLUMN = {
    "CREATED": BoardColumn.TODO,
    "READY": BoardColumn.TODO,
    "CLAIMED": BoardColumn.TODO,
    "RUNNING": BoardColumn.IN_PROGRESS,
    "REVIEW": BoardColumn.REVIEW,
    "COMPLETED": BoardColumn.DONE,
    "FAILED": BoardColumn.TODO,
}

def _col(status: str):
    c=_STATUS_TO_COLUMN.get(status)
    return c.value if c else None

def _recovery(status, oh, op):
    if status in TERMINAL_RUN_STATUSES:
        return {"eligible": False, "classification": "terminal", "reason": f"terminal {status!r}"}
    if status == "AWAITING_APPROVAL":
        return {"eligible": False, "classification": "awaiting_approval", "reason": "AWAITING_APPROVAL not auto-recoverable"}
    if status not in RECOVERABLE_RUN_STATUSES:
        return {"eligible": False, "classification": "unknown_status", "reason": f"status {status!r} not recoverable"}
    cls=classify_ownership(oh, op)
    if cls=="dead_same_host":
        return {"eligible": True, "classification": "recoverable", "reason": "dead_same_host", "ownership_class": cls}
    if cls=="live_same_host":
        return {"eligible": False, "classification": "blocked_live_owner", "reason": "live_same_host", "ownership_class": cls}
    if cls=="foreign_host":
        return {"eligible": False, "classification": "blocked_foreign_owner", "reason": "foreign_host", "ownership_class": cls}
    if cls=="unowned":
        return {"eligible": False, "classification": "blocked_unowned", "reason": "unowned legacy", "ownership_class": cls}
    if cls=="current_process":
        return {"eligible": False, "classification": "blocked_current_process", "reason": "current_process", "ownership_class": cls}
    return {"eligible": False, "classification": "blocked_unknown", "reason": f"ownership {cls}", "ownership_class": cls}

@dataclasses.dataclass(frozen=True)
class SupervisorRunObservation:
    run_id: str
    status: str
    owner_host: str | None
    owner_pid: int | None
    owner_acquired_at: str | None
    is_active: bool
    is_terminal: bool
    recovery_eligible: bool
    recovery_classification: str
    recovery_reason: str
    ownership_class: str

@dataclasses.dataclass(frozen=True)
class SupervisorMissionObservation:
    mission_id: str
    run_id: str
    status: str

@dataclasses.dataclass(frozen=True)
class SupervisorTaskObservation:
    task_id: str
    mission_id: str
    status: str
    assigned_agent: str | None
    board_column: str | None
    board_column_error: str | None

@dataclasses.dataclass(frozen=True)
class SupervisorAttemptObservation:
    attempt_id: str
    task_id: str
    status: str
    agent_id: str | None
    model: str | None

@dataclasses.dataclass(frozen=True)
class SupervisorSnapshot:
    runs: tuple
    missions: tuple
    tasks: tuple
    attempts: tuple
    errors: tuple = dataclasses.field(default_factory=tuple)

def _fetch_runs():
    with get_connection() as conn:
        cols=[r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
        has="owner_host" in cols
        rows=conn.execute("SELECT id,status,owner_host,owner_pid,owner_acquired_at FROM runs" if has else "SELECT id,status FROM runs").fetchall()
        return [dict(r) for r in rows]

def _fetch_missions():
    with get_connection() as conn:
        try:
            return [dict(r) for r in conn.execute("SELECT mission_id,run_id,status FROM missions").fetchall()]
        except Exception:
            return []

def _fetch_tasks():
    with get_connection() as conn:
        try:
            return [dict(r) for r in conn.execute("SELECT task_id,mission_id,status,assigned_agent FROM tasks").fetchall()]
        except Exception:
            return []

def _fetch_attempts():
    with get_connection() as conn:
        try:
            return [dict(r) for r in conn.execute("SELECT attempt_id,task_id,status,agent_id,model FROM attempts").fetchall()]
        except Exception:
            return []

def build_supervisor_snapshot():
    run_rows=sorted(_fetch_runs(), key=lambda x: x["id"])
    miss_rows=sorted(_fetch_missions(), key=lambda x: x["mission_id"])
    task_rows=sorted(_fetch_tasks(), key=lambda x: x["task_id"])
    att_rows=sorted(_fetch_attempts(), key=lambda x: x["attempt_id"])
    runs=[]
    errors=[]
    known=RECOVERABLE_RUN_STATUSES|ACTIVE_RUN_STATUSES|TERMINAL_RUN_STATUSES
    for r in run_rows:
        rid=r["id"]; st=r.get("status"); oh=r.get("owner_host"); op=r.get("owner_pid"); oaa=r.get("owner_acquired_at")
        if st not in known:
            errors.append(f"unknown run status {st!r} for {rid!r}")
            runs.append(SupervisorRunObservation(run_id=rid,status=st,owner_host=oh,owner_pid=op,owner_acquired_at=oaa,is_active=False,is_terminal=False,recovery_eligible=False,recovery_classification="unknown_status",recovery_reason=f"unknown status {st!r}",ownership_class="unknown"))
            continue
        is_active=st in ACTIVE_RUN_STATUSES; is_term=st in TERMINAL_RUN_STATUSES
        if op is not None:
            try: int(op)
            except Exception:
                errors.append(f"corrupt owner_pid for {rid!r}")
                runs.append(SupervisorRunObservation(run_id=rid,status=st,owner_host=oh,owner_pid=op,owner_acquired_at=oaa,is_active=is_active,is_terminal=is_term,recovery_eligible=False,recovery_classification="blocked_unknown",recovery_reason="corrupt owner_pid",ownership_class="unknown"))
                continue
        rec=_recovery(st, oh, op)
        runs.append(SupervisorRunObservation(run_id=rid,status=st,owner_host=oh,owner_pid=op,owner_acquired_at=oaa,is_active=is_active,is_terminal=is_term,recovery_eligible=rec["eligible"],recovery_classification=rec["classification"],recovery_reason=rec["reason"],ownership_class=rec.get("ownership_class","unknown")))
    missions=[]
    for m in miss_rows:
        st=m["status"]
        if st not in {"CREATED","PLANNING","READY","RUNNING","COMPLETED","FAILED"}:
            errors.append(f"unknown mission status {st!r} for {m['mission_id']!r}"); continue
        missions.append(SupervisorMissionObservation(mission_id=m["mission_id"],run_id=m["run_id"],status=st))
    tasks=[]
    for t in task_rows:
        st=t["status"]; col=_col(st); err=None
        if col is None:
            err=f"unknown task status {st!r} for {t['task_id']!r}"; errors.append(err)
        tasks.append(SupervisorTaskObservation(task_id=t["task_id"],mission_id=t["mission_id"],status=st,assigned_agent=t["assigned_agent"],board_column=col,board_column_error=err))
    attempts=[]
    for a in att_rows:
        st=a["status"]
        if st not in {"PENDING","RUNNING","REVIEW","COMPLETED","FAILED"}:
            errors.append(f"unknown attempt status {st!r} for {a['attempt_id']!r}"); continue
        attempts.append(SupervisorAttemptObservation(attempt_id=a["attempt_id"],task_id=a["task_id"],status=st,agent_id=a["agent_id"],model=a["model"]))
    return SupervisorSnapshot(runs=tuple(runs),missions=tuple(missions),tasks=tuple(tasks),attempts=tuple(attempts),errors=tuple(errors))

def observe_supervisor_state():
    return build_supervisor_snapshot()
