"""Data models for the Nexus Agent Execution Loop V1."""

from dataclasses import dataclass
from typing import Optional


AGENT_STATUSES = {
    "AVAILABLE",
    "BUSY",
    "FAILED",
    "OFFLINE",
}


@dataclass(slots=True)
class Agent:
    """An agent that can be assigned to execute a task."""

    agent_id: str
    name: str
    provider: str
    model: str
    capabilities: Optional[list[str]] = None
    status: str = "AVAILABLE"

    def __post_init__(self):
        if self.status not in AGENT_STATUSES:
            raise ValueError(f"Unknown agent status: {self.status!r}")
        if self.capabilities is not None:
            self.capabilities = list(self.capabilities)


@dataclass(slots=True)
class TaskExecutionResult:
    """Outcome of executing a single task by an agent."""

    task_id: str
    agent_id: str
    status: str
    output: Optional[str] = None
    error: Optional[str] = None
    routed_model: Optional[str] = None
