import os
import socket
import ctypes
from datetime import datetime, timezone
from nexus.registry.database import get_connection
def _now():
    return datetime.now(timezone.utc).isoformat()
def current_host():
    return socket.gethostname()
def current_pid():
    return os.getpid()
def is_pid_alive(pid):
    if pid is None:
        return False
    try:
        pid=int(pid)
    except Exception:
        return False
    if pid<=0:
        return False
    try:
        if os.name=="nt":
            k32=ctypes.windll.kernel32
            handle=k32.OpenProcess(0x1000, False, pid)
            if handle:
                k32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid,0)
            return True
    except OSError as e:
        import errno
        if getattr(e,'errno',None)==errno.ESRCH:
            return False
        if getattr(e,'errno',None)==errno.EPERM:
            return True
        return False
    except Exception:
        return False
def ensure_ownership_columns():
    with get_connection() as conn:
        cols=[r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
        if "owner_host" not in cols:
            conn.execute("ALTER TABLE runs ADD COLUMN owner_host TEXT")
        if "owner_pid" not in cols:
            conn.execute("ALTER TABLE runs ADD COLUMN owner_pid INTEGER")
        if "owner_acquired_at" not in cols:
            conn.execute("ALTER TABLE runs ADD COLUMN owner_acquired_at TEXT")
def get_ownership(run_id):
    with get_connection() as conn:
        row=conn.execute("SELECT owner_host, owner_pid, owner_acquired_at FROM runs WHERE id=?",(run_id,)).fetchone()
        if row is None:
            return None
        return dict(row)
def establish_ownership(run_id):
    ensure_ownership_columns()
    host=current_host()
    pid=current_pid()
    now=_now()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("SELECT owner_host, owner_pid FROM runs WHERE id=?",(run_id,)).fetchone()
        if row is None:
            conn.rollback()
            return
        if row["owner_host"] is not None or row["owner_pid"] is not None:
            conn.rollback()
            return
        conn.execute("UPDATE runs SET owner_host=?, owner_pid=?, owner_acquired_at=? WHERE id=?",(host,pid,now,run_id))
        conn.commit()
def classify_ownership(owner_host, owner_pid):
    if owner_host is None or owner_pid is None:
        return "unowned"
    if owner_host != current_host():
        if not owner_host:
            return "unknown"
        return "foreign_host"
    if int(owner_pid) == current_pid():
        return "current_process"
    if is_pid_alive(int(owner_pid)):
        return "live_same_host"
    return "dead_same_host"
def try_acquire_for_recovery(run_id):
    ensure_ownership_columns()
    host=current_host()
    pid=current_pid()
    now=_now()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("SELECT owner_host, owner_pid, status FROM runs WHERE id=?",(run_id,)).fetchone()
        if row is None:
            conn.rollback()
            return False
        status=row["status"]
        if status not in ("CREATED","ROUTING","RUNNING","REVIEWING"):
            conn.rollback()
            return False
        owner_host=row["owner_host"]
        owner_pid=row["owner_pid"]
        cls=classify_ownership(owner_host, owner_pid)
        if cls!="dead_same_host":
            conn.rollback()
            return False
        conn.execute("UPDATE runs SET owner_host=?, owner_pid=?, owner_acquired_at=? WHERE id=?",(host,pid,now,run_id))
        conn.commit()
        return True
def try_acquire_for_resume(run_id):
    from nexus.orchestration.resume import ResumeError
    ensure_ownership_columns()
    host=current_host()
    pid=current_pid()
    now=_now()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("SELECT owner_host, owner_pid, status FROM runs WHERE id=?",(run_id,)).fetchone()
        if row is None:
            conn.rollback()
            raise ResumeError("RUN_NOT_FOUND", "No Run exists with id "+repr(run_id)+".", run_id=run_id)
        status=row["status"]
        if status=="AWAITING_APPROVAL":
            conn.rollback()
            raise ResumeError("RESUME_NOT_AVAILABLE", "Run "+repr(run_id)+" awaiting approval.", run_id=run_id)
        if status not in ("CREATED","ROUTING","RUNNING","REVIEWING"):
            conn.rollback()
            raise ResumeError("RUN_TERMINAL", "Run "+repr(run_id)+" is not resumable from status "+repr(status)+".", run_id=run_id)
        owner_host=row["owner_host"]
        owner_pid=row["owner_pid"]
        cls=classify_ownership(owner_host, owner_pid)
        if cls=="unowned":
            conn.execute("UPDATE runs SET owner_host=?, owner_pid=?, owner_acquired_at=? WHERE id=?",(host,pid,now,run_id))
            conn.commit()
            return
        if cls=="dead_same_host":
            conn.execute("UPDATE runs SET owner_host=?, owner_pid=?, owner_acquired_at=? WHERE id=?",(host,pid,now,run_id))
            conn.commit()
            return
        if cls=="current_process":
            conn.rollback()
            return
        if cls=="live_same_host":
            conn.rollback()
            raise ResumeError("RESUME_OWNER_ACTIVE", "Run "+repr(run_id)+" is still owned by live process "+str(owner_pid)+" on "+str(owner_host)+".", run_id=run_id)
        conn.rollback()
        raise ResumeError("RESUME_OWNER_FOREIGN", "Run "+repr(run_id)+" owned by foreign/unknown host "+repr(owner_host)+"; fail closed.", run_id=run_id)
