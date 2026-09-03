"""In-memory execution session registry for Nexus Agent Workspace V1."""

from uuid import uuid4

from nexus.workspaces.models import AGENT_SESSION_STATUSES, AgentSession


class SessionNotFoundError(KeyError):
    pass


# Internal in-memory store.
_sessions = {}


def _new_id(prefix):
    return f"{prefix}-{uuid4().hex[:8].upper()}"


def _validate_status(status):
    if status not in AGENT_SESSION_STATUSES:
        raise ValueError(f"Unknown session status: {status!r}")


def create_session(
    task_id,
    agent_id,
    status="CREATED",
    started_at=None,
    finished_at=None,
    current_action=None,
    context=None,
    result=None,
    error=None,
):
    """Create and store a new AgentSession, returning the stored session."""
    _validate_status(status)
    session = AgentSession(
        session_id=_new_id("SESS"),
        task_id=task_id,
        agent_id=agent_id,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        current_action=current_action,
        context=context,
        result=result,
        error=error,
    )
    _sessions[session.session_id] = session
    return session


def get_session(session_id):
    try:
        return _sessions[session_id]
    except KeyError:
        raise SessionNotFoundError(session_id)


def list_sessions(task_id=None, agent_id=None):
    """List sessions, optionally filtered by task_id and/or agent_id."""
    sessions = list(_sessions.values())
    if task_id is not None:
        sessions = [s for s in sessions if s.task_id == task_id]
    if agent_id is not None:
        sessions = [s for s in sessions if s.agent_id == agent_id]
    return sessions


def update_session(session_id, **changes):
    """Update a stored session with the given attribute changes.

    Returns the updated session. Rejects unknown fields and invalid statuses.
    """
    session = get_session(session_id)
    for key, value in changes.items():
        if not hasattr(session, key):
            raise AttributeError(f"Unknown session field: {key!r}")
        if key == "status":
            _validate_status(value)
        setattr(session, key, value)
    return session
