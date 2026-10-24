"""Shared Run/Program ownership with conservative native process checks."""

import ctypes
import errno
import os
import socket
from ctypes import wintypes
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from nexus.registry.database import get_connection

# Acquisition timestamps in old Python/Windows environments can have lower
# resolution than process FILETIME. A small positive delta is not PID reuse.
PID_REUSE_CLOCK_MARGIN = timedelta(seconds=1)


def _now():
    return datetime.now(UTC).isoformat()


def current_host():
    return socket.gethostname()


def current_pid():
    return os.getpid()


def _valid_pid(pid):
    if isinstance(pid, bool) or not isinstance(pid, (int, str)):
        return None
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return None
    return value if 0 < value <= 0xFFFFFFFF else None


@lru_cache(maxsize=1)
def _windows_api():
    # HANDLE is pointer-sized on Windows x64. Do not use ctypes' default int
    # return type or share mutable function prototypes with unrelated callers.
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.GetExitCodeProcess.restype = wintypes.BOOL
    filetime_pointer = ctypes.POINTER(wintypes.FILETIME)
    api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [filetime_pointer] * 4
    api.GetProcessTimes.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


def _windows_process_info(pid):
    """Return (alive/dead/unknown, creation time); only evidence allows dead."""
    handle = None
    try:
        api = _windows_api()
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            # A valid, positive DWORD PID absent from the process table gives
            # ERROR_INVALID_PARAMETER. Access denied and all other errors are
            # unavailable evidence, not proof that the owner has exited.
            return ("dead" if ctypes.get_last_error() == 87 else "unknown"), None
        exit_code = wintypes.DWORD()
        if not api.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return "unknown", None
        if exit_code.value != 259:  # STILL_ACTIVE
            return "dead", None
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not api.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            return "alive", None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        if ticks < 116444736000000000:
            return "alive", None
        # Avoid floating point rounding of 64-bit FILETIME timestamps.
        timestamp = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=(ticks - 116444736000000000) // 10)
        return "alive", timestamp
    except (OSError, AttributeError, OverflowError, ValueError):
        return "unknown", None
    finally:
        if handle:
            api.CloseHandle(handle)


def is_pid_alive(pid):
    """True=active, False=proven exited, None=invalid or unavailable evidence."""
    value = _valid_pid(pid)
    if value is None:
        return None
    if os.name == "nt":
        status, _created = _windows_process_info(value)
        return {"alive": True, "dead": False, "unknown": None}[status]
    try:
        os.kill(value, 0)
        return True
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        return None

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

def get_process_created_at(pid):
    """Return a native UTC creation time, or None when identity is unavailable."""
    value = _valid_pid(pid)
    if value is None or os.name != "nt":
        return None
    status, created = _windows_process_info(value)
    return created if status == "alive" else None


def classify_ownership(owner_host, owner_pid, owner_acquired_at=None):
    if owner_host is None and owner_pid is None:
        return "unowned"
    if not owner_host:
        return "unknown"
    if owner_host != current_host():
        return "foreign_host"
    pid = _valid_pid(owner_pid)
    if pid is None:
        return "unknown"
    if pid == current_pid():
        return "current_process"
    alive = is_pid_alive(pid)
    if alive is None:
        return "unknown"
    if alive is False:
        return "dead_same_host"
    if owner_acquired_at is not None:
        created = get_process_created_at(pid)
        if created is not None:
            try:
                acquired = datetime.fromisoformat(owner_acquired_at)
                if acquired.tzinfo is None:
                    return "unknown"
                if created > acquired + PID_REUSE_CLOCK_MARGIN:
                    return "dead_same_host"
                if created > acquired:
                    # Within clock precision, identity is ambiguous. Block
                    # recovery rather than calling a known live PID dead.
                    return "unknown"
            except (AttributeError, TypeError, ValueError, OverflowError):
                return "unknown"
    return "live_same_host"

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
        if cls not in ("dead_same_host", "unowned"):
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
