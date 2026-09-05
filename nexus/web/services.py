"""Read-only aggregation layer for the Nexus Local Mission Board UI V1.

This module collects a unified view over the existing in-memory Nexus core
components - the Mission Engine, Task Registry, Agent Registry, and Agent
Workspace - and serializes them into plain dicts for the web layer. It is
strictly read-only: it never creates, mutates, or persists any data.

The live Agent Registry used by the Agent Execution Loop is shared through a
module-level singleton so the web views observe the same in-memory agents as
the rest of the process (it only reads through ``AgentRegistry.list_agents``
and never writes).
"""

from dataclasses import asdict, is_dataclass
from uuid import uuid4

from nexus.missions.service import (
    create_mission as create_engine_mission,
    create_mission_from_plan,
    list_missions,
)
from nexus.tasks import registry as task_registry
from nexus.web.agents import agent_registry
from nexus.workspaces import registry as session_registry


# Board columns rendered by the UI, mapped from the Task Registry statuses.
COLUMNS = (
    "BACKLOG",
    "READY",
    "RUNNING",
    "REVIEW",
    "DONE",
    "FAILED",
)

# Existing Task Registry statuses and their board column.
STATUS_TO_COLUMN = {
    "CREATED": "BACKLOG",
    "READY": "READY",
    "CLAIMED": "RUNNING",
    "RUNNING": "RUNNING",
    "REVIEW": "REVIEW",
    "COMPLETED": "DONE",
    "FAILED": "FAILED",
}


DEMO_TASK_TITLES = (
    "Analyze authentication requirements",
    "Design authentication architecture",
    "Implement authentication flow",
)


def _demo_plan():
    """Return the deterministic example plan used by the demo endpoint.

    Each worker carries the ``required_capabilities`` for its task so that the
    Capability Router can select the matching default agent at execution time.
    """
    capabilities_by_title = {
        DEMO_TASK_TITLES[0]: ["analysis"],
        DEMO_TASK_TITLES[1]: ["architecture"],
        DEMO_TASK_TITLES[2]: ["coding"],
    }
    return {
        "title": "Implement Authentication Module",
        "description": "A demo mission generated from the local Mission Board UI.",
        "workers": [
            {
                "scope": title,
                "reason": "Generated from the demo mission plan.",
                "required_capabilities": capabilities_by_title[title],
            }
            for title in DEMO_TASK_TITLES
        ],
    }


def create_demo_mission(run_id):
    """Create the deterministic demo mission through the Mission Engine.

    The mission and its tasks live only in the in-memory Mission Engine and
    Task Registry; nothing is persisted. Returns the created Mission.
    """
    return create_mission_from_plan(_demo_plan(), run_id=run_id)


def create_mission(title, description=None, project_id=None, execution_path=None):
    """Create an empty in-memory Mission through the Mission Engine.

    The mission lives only in the in-memory Mission Engine and is not
    persisted. Returns the created Mission.
    """
    return create_engine_mission(
        run_id="RUN-" + uuid4().hex[:4].upper(),
        title=title,
        description=description,
        project_id=project_id,
        execution_path=execution_path,
    )


def _to_dict(value):
    """Convert a dataclass (or SQLite Row) to a plain, JSON-safe dict."""
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "keys") and callable(getattr(value, "keys")):
        return dict(value)
    return value


def get_missions():
    """Return all missions plus a per-column task breakdown for the board."""
    missions = list_missions()
    payload = []

    for mission in missions:
        item = _to_dict(mission)
        item["board"] = _mission_board(mission.mission_id)
        payload.append(item)

    return payload


def get_tasks():
    """Return all tasks enriched with their board column and attempt data."""
    attempts_by_task = {}
    for attempt in task_registry.list_attempts():
        attempts_by_task.setdefault(attempt.task_id, []).append(_to_dict(attempt))

    tasks = []
    for task in task_registry.list_tasks():
        item = _to_dict(task)
        item["board_column"] = STATUS_TO_COLUMN.get(task.status, "BACKLOG")
        item["attempts"] = attempts_by_task.get(task.task_id, [])
        tasks.append(item)

    return tasks


def get_agents():
    """Return all registered agents with their active execution sessions."""
    agents = []

    for agent in agent_registry.list_agents():
        item = _to_dict(agent)
        sessions = session_registry.list_sessions(agent_id=agent.agent_id)
        item["sessions"] = [_to_dict(s) for s in sessions]
        agents.append(item)

    return agents


def get_sessions():
    """Return every execution session, enriched with agent/task labels."""
    agents_by_id = {
        agent.agent_id: agent for agent in agent_registry.list_agents()
    }
    tasks_by_id = {task.task_id: task for task in task_registry.list_tasks()}

    sessions = []
    for session in session_registry.list_sessions():
        item = _to_dict(session)
        agent = agents_by_id.get(session.agent_id)
        task = tasks_by_id.get(session.task_id)
        item["agent_name"] = agent.name if agent is not None else None
        item["agent_status"] = agent.status if agent is not None else None
        item["task_title"] = task.title if task is not None else None
        item["mission_id"] = task.mission_id if task is not None else None
        sessions.append(item)

    return sessions


def get_board():
    """Return the full board view (columns -> task cards) for the UI."""
    tasks = get_tasks()
    columns = {column: [] for column in COLUMNS}

    for task in tasks:
        columns.setdefault(task["board_column"], []).append(task)

    return {
        "columns": [
            {"name": name, "tasks": columns.get(name, [])} for name in COLUMNS
        ],
        "tasks": tasks,
    }


def get_summary():
    """Return aggregate counters for the dashboard header."""
    missions = list_missions()
    tasks = task_registry.list_tasks()
    agents = agent_registry.list_agents()
    sessions = session_registry.list_sessions()

    active_sessions = [s for s in sessions if s.status in ("CREATED", "RUNNING")]

    return {
        "missions": len(missions),
        "tasks": len(tasks),
        "agents": len(agents),
        "sessions": len(sessions),
        "active_sessions": len(active_sessions),
    }


def _mission_board(mission_id):
    """Group a mission's tasks into the six UI board columns."""
    columns = {column: [] for column in COLUMNS}
    task_ids = []

    for task in task_registry.list_tasks():
        if task.mission_id != mission_id:
            continue
        column = STATUS_TO_COLUMN.get(task.status, "BACKLOG")
        columns.setdefault(column, []).append(task.task_id)
        task_ids.append(task.task_id)

    return {
        "mission_id": mission_id,
        "columns": [
            {"name": name, "task_ids": columns.get(name, [])} for name in COLUMNS
        ],
        "task_ids": task_ids,
    }