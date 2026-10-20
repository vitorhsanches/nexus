"""Background execution manager for the Nexus GO web interface.

Launches run_go() in a daemon thread and exposes polling state so the
web layer can stream progress without WebSockets.
"""

from __future__ import annotations

import sys
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional


_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _configure_console_encoding() -> None:
    """Keep mission logs from crashing on Windows consoles with legacy codepages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            # Captured/test streams may not support changing their encoding.
            pass


def launch_go(
    request_text: str,
    project_id: Optional[str],
    manager_model: Optional[str] = None,
    prepare_dependencies: bool = False,
) -> str:
    """Launch run_go() in a background thread. Returns a job_id immediately."""
    _configure_console_encoding()
    job_id = f"JOB-{uuid.uuid4().hex[:8].upper()}"

    with _lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "running",
            "run_id": None,
            "error": None,
            "error_code": None,
            "started_at": _now_iso(),
            "finished_at": None,
            "project_id": project_id,
            "mission_text": request_text[:200],
            "manager_model": manager_model,
            "prepare_dependencies": prepare_dependencies,
        }

    thread = threading.Thread(
        target=_worker,
        args=(job_id, request_text, project_id, manager_model, prepare_dependencies),
        daemon=True,
        name=f"nexus-go-{job_id}",
    )
    thread.start()
    return job_id


def _worker(
    job_id: str,
    request_text: str,
    project_id: Optional[str],
    manager_model: Optional[str] = None,
    prepare_dependencies: bool = False,
) -> None:
    """Thread target: runs run_go() and updates job state."""
    # Late import to avoid circular imports; go.py imports many orchestration
    # modules that are not needed until a run is actually launched.
    from nexus.orchestration.go import GoError, run_go  # noqa: PLC0415

    try:
        result = run_go(
            request_text,
            project_query=project_id,
            manager_model=manager_model,
            prepare_dependencies=prepare_dependencies,
        )
        with _lock:
            _jobs[job_id]["run_id"] = result.get("run_id")
            _jobs[job_id]["status"] = "completed"
            _jobs[job_id]["finished_at"] = _now_iso()
    except GoError as exc:
        with _lock:
            _jobs[job_id]["run_id"] = exc.run_id
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)
            _jobs[job_id]["error_code"] = exc.code
            _jobs[job_id]["finished_at"] = _now_iso()
    except Exception as exc:  # pragma: no cover – unexpected errors
        with _lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = f"{type(exc).__name__}: {exc}"
            _jobs[job_id]["finished_at"] = _now_iso()


def get_job(job_id: str) -> Optional[dict]:
    """Return current job state, enriched with a DB probe if run_id is missing."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        state = dict(job)

    # If run_id is still None but the job is running, probe the DB.
    # run_go() calls create_run() near the very start, so within a couple of
    # seconds the run will appear in the registry.
    if state["run_id"] is None and state["status"] == "running":
        probed = _probe_for_run_id(state)
        if probed:
            state["run_id"] = probed

    return state


def _probe_for_run_id(state: dict) -> Optional[str]:
    """Scan list_runs() for a run created after this job started."""
    try:
        from nexus.registry import runs as runs_registry  # noqa: PLC0415

        rows = runs_registry.list_runs()
        started_at = state.get("started_at", "")
        project_id = state.get("project_id")

        for row in rows:
            run = dict(row)
            if run.get("created_at", "") >= started_at:
                if project_id is None:
                    return run["id"]
                # Match by project name or id (case-insensitive substring)
                project_name = str(run.get("project_name", "")).lower()
                if project_id.lower() in project_name or project_id.lower() == project_name:
                    return run["id"]
    except Exception:
        pass
    return None


def list_jobs(limit: int = 20) -> list[dict]:
    """Return recently-launched jobs, newest first."""
    with _lock:
        jobs = list(_jobs.values())
    return sorted(jobs, key=lambda j: j.get("started_at", ""), reverse=True)[:limit]
