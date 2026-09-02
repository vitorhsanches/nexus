from dataclasses import dataclass
from typing import Optional


RUN_STATUSES = {
    "CREATED",
    "ROUTING",
    "RUNNING",
    "REVIEWING",
    "AWAITING_APPROVAL",
    "COMPLETED",
    "BLOCKED",
    "FAILED",
    "CANCELLED",
}


@dataclass(slots=True)
class Run:
    id: str
    project_id: str
    input: str
    intent: str
    status: str
    risk: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result: Optional[str] = None
    commit_sha: Optional[str] = None
