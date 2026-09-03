"""Execution session service for Nexus Agent Workspace V1.

Coordinates the lifecycle of AgentSession instances on top of the in-memory
session registry, bridging tasks to agents without touching the router,
missions, or board.
"""

from datetime import datetime, timezone

from nexus.workspaces.models import AgentSession
from nexus.workspaces.registry import create_session, update_session


def _now():
    return datetime.now(timezone.utc).isoformat()


def start_session(task_id, agent_id):
    """Create a session and immediately mark it RUNNING.

    Returns the created AgentSession with a starting timestamp.
    """
    session = create_session(
        task_id=task_id,
        agent_id=agent_id,
        status="RUNNING",
        started_at=_now(),
    )
    return session


def complete_session(session_id, result):
    """Mark a session COMPLETED with the given result.

    Returns the updated AgentSession.
    """
    return update_session(
        session_id,
        status="COMPLETED",
        finished_at=_now(),
        result=result,
        error=None,
    )


def fail_session(session_id, error):
    """Mark a session FAILED with the given error message.

    Returns the updated AgentSession.
    """
    return update_session(
        session_id,
        status="FAILED",
        finished_at=_now(),
        error=str(error),
    )


def cancel_session(session_id):
    """Mark a session CANCELLED (terminal, no result). Returns the session."""
    return update_session(
        session_id,
        status="CANCELLED",
        finished_at=_now(),
    )
