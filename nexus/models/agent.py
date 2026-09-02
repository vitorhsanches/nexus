from dataclasses import dataclass
from typing import Optional


AGENT_STATUSES = {
    "QUEUED",
    "RUNNING",
    "WAITING",
    "REVIEWING",
    "COMPLETED",
    "BLOCKED",
    "FAILED",
    "CANCELLED",
}


@dataclass(slots=True)
class Agent:
    id: str
    run_id: str
    role: str
    provider: str
    model: str
    effort: str
    status: str
    branch: Optional[str] = None
    worktree: Optional[str] = None
    parent_agent_id: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result: Optional[str] = None
