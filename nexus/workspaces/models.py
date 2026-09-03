"""Data models for the Nexus Agent Workspace V1 execution sessions."""

from dataclasses import dataclass, field
from typing import Any, Optional


AGENT_SESSION_STATUSES = {
    "CREATED",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
}


@dataclass(slots=True)
class AgentSession:
    """A single execution session linking a task to an agent."""

    session_id: str
    task_id: str
    agent_id: str
    status: str = "CREATED"
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    current_action: Optional[str] = None
    context: Optional[dict] = None
    result: Optional[str] = None
    error: Optional[str] = None

    def __post_init__(self):
        if self.status not in AGENT_SESSION_STATUSES:
            raise ValueError(f"Unknown session status: {self.status!r}")
        if self.context is not None and not isinstance(self.context, dict):
            raise TypeError("context must be a dict or None")
