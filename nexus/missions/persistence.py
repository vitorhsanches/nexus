"""Additive persistence for Mission."""
import json
from nexus.registry.database import get_connection

def _ensure():
    with get_connection() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS missions (
                mission_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL,
                created_at TEXT,
                project_id TEXT,
                execution_path TEXT
            );
        """)

def persist_mission(m):
    _ensure()
    with get_connection() as c:
        c.execute("INSERT INTO missions(mission_id,run_id,title,description,status,created_at,project_id,execution_path) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(mission_id) DO UPDATE SET run_id=excluded.run_id,title=excluded.title,description=excluded.description,status=excluded.status,created_at=excluded.created_at,project_id=excluded.project_id,execution_path=excluded.execution_path", (m.mission_id,m.run_id,m.title,m.description,m.status,m.created_at,m.project_id,m.execution_path))
        c.commit()

def _enc(v):
    if v is None:
        return None
    return json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",",":"))

def persist_mission_bundle(mission):
    """Atomically persist Mission + all its Tasks in ONE SQLite transaction."""
    _ensure()
    from nexus.tasks.persistence import _ensure as _ensure_tasks
    _ensure_tasks()
    with get_connection() as c:
        try:
            c.execute("BEGIN")
            c.execute("INSERT INTO missions(mission_id,run_id,title,description,status,created_at,project_id,execution_path) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(mission_id) DO UPDATE SET run_id=excluded.run_id,title=excluded.title,description=excluded.description,status=excluded.status,created_at=excluded.created_at,project_id=excluded.project_id,execution_path=excluded.execution_path", (mission.mission_id,mission.run_id,mission.title,mission.description,mission.status,mission.created_at,mission.project_id,mission.execution_path))
            for t in (mission.tasks or []):
                c.execute("INSERT INTO tasks(task_id,mission_id,title,description,status,priority,dependencies,assigned_agent,execution_policy,acceptance_criteria) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET mission_id=excluded.mission_id,title=excluded.title,description=excluded.description,status=excluded.status,priority=excluded.priority,dependencies=excluded.dependencies,assigned_agent=excluded.assigned_agent,execution_policy=excluded.execution_policy,acceptance_criteria=excluded.acceptance_criteria", (t.task_id,t.mission_id,t.title,t.description,t.status,t.priority,_enc(t.dependencies),t.assigned_agent,_enc(t.execution_policy),_enc(t.acceptance_criteria)))
            c.commit()
        except Exception:
            c.rollback()
            raise

def load_missions():
    _ensure()
    from nexus.missions.models import MISSION_STATUSES, Mission
    with get_connection() as c:
        rows=c.execute("SELECT mission_id,run_id,title,description,status,created_at,project_id,execution_path FROM missions").fetchall()
        out=[]
        for r in rows:
            status=r["status"]
            if status not in MISSION_STATUSES:
                raise ValueError(f"Corrupt mission status: {status!r} for {r['mission_id']!r}")
            out.append(Mission(mission_id=r["mission_id"],run_id=r["run_id"],title=r["title"],description=r["description"],status=status,tasks=[],created_at=r["created_at"],project_id=r["project_id"],execution_path=r["execution_path"]))
        return out
