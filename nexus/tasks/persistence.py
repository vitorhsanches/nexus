"""Additive persistence for Task/Attempt."""
import json
from nexus.registry.database import get_connection

def _ensure():
    with get_connection() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                mission_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL,
                priority TEXT NOT NULL,
                dependencies TEXT,
                assigned_agent TEXT,
                execution_policy TEXT,
                acceptance_criteria TEXT
            );
            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                result TEXT
            );
        """)

def _enc(v):
    if v is None:
        return None
    return json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",",":"))

def _dec(s, field_name):
    if s is None:
        return None
    try:
        return json.loads(s)
    except Exception as e:
        raise ValueError(f"Corrupt persisted JSON for {field_name}: {e}") from e

def persist_task(t):
    _ensure()
    with get_connection() as c:
        c.execute("INSERT INTO tasks(task_id,mission_id,title,description,status,priority,dependencies,assigned_agent,execution_policy,acceptance_criteria) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET mission_id=excluded.mission_id,title=excluded.title,description=excluded.description,status=excluded.status,priority=excluded.priority,dependencies=excluded.dependencies,assigned_agent=excluded.assigned_agent,execution_policy=excluded.execution_policy,acceptance_criteria=excluded.acceptance_criteria", (t.task_id,t.mission_id,t.title,t.description,t.status,t.priority,_enc(t.dependencies),t.assigned_agent,_enc(t.execution_policy),_enc(t.acceptance_criteria)))
        c.commit()

def persist_attempt(a):
    _ensure()
    with get_connection() as c:
        c.execute("INSERT INTO attempts(attempt_id,task_id,agent_id,model,status,result) VALUES (?,?,?,?,?,?) ON CONFLICT(attempt_id) DO UPDATE SET task_id=excluded.task_id,agent_id=excluded.agent_id,model=excluded.model,status=excluded.status,result=excluded.result", (a.attempt_id,a.task_id,a.agent_id,a.model,a.status,a.result))
        c.commit()

def load_tasks():
    _ensure()
    from nexus.tasks.models import TASK_STATUSES, Task
    with get_connection() as c:
        rows=c.execute("SELECT task_id,mission_id,title,description,status,priority,dependencies,assigned_agent,execution_policy,acceptance_criteria FROM tasks").fetchall()
        out=[]
        for r in rows:
            status=r["status"]
            if status not in TASK_STATUSES:
                raise ValueError(f"Corrupt task status: {status!r} for {r['task_id']!r}")
            out.append(Task(task_id=r["task_id"],mission_id=r["mission_id"],title=r["title"],description=r["description"],status=status,priority=r["priority"],dependencies=_dec(r["dependencies"],"dependencies"),assigned_agent=r["assigned_agent"],execution_policy=_dec(r["execution_policy"],"execution_policy"),acceptance_criteria=_dec(r["acceptance_criteria"],"acceptance_criteria")))
        return out

def load_attempts():
    _ensure()
    from nexus.tasks.models import ATTEMPT_STATUSES, Attempt
    with get_connection() as c:
        rows=c.execute("SELECT attempt_id,task_id,agent_id,model,status,result FROM attempts").fetchall()
        out=[]
        for r in rows:
            status=r["status"]
            if status not in ATTEMPT_STATUSES:
                raise ValueError(f"Corrupt attempt status: {status!r} for {r['attempt_id']!r}")
            out.append(Attempt(attempt_id=r["attempt_id"],task_id=r["task_id"],agent_id=r["agent_id"],model=r["model"],status=status,result=r["result"]))
        return out
