from dataclasses import dataclass
from typing import Optional


TASK_STATUSES = {
    "CREATED",
    "READY",
    "CLAIMED",
    "RUNNING",
    "REVIEW",
    "COMPLETED",
    "FAILED",
}

ATTEMPT_STATUSES = {
    "PENDING",
    "RUNNING",
    "REVIEW",
    "COMPLETED",
    "FAILED",
}


@dataclass(slots=True)
class Mission:
    mission_id: str
    run_id: str
    title: str
    description: Optional[str] = None


@dataclass(slots=True)
class Task:
    task_id: str
    mission_id: str
    title: str
    description: Optional[str] = None
    status: str = "CREATED"
    priority: str = "MEDIUM"
    dependencies: Optional[list[str]] = None
    assigned_agent: Optional[str] = None
    execution_policy: Optional[dict] = None
    acceptance_criteria: Optional[list[str]] = None


@dataclass(slots=True)
class Attempt:
    attempt_id: str
    task_id: str
    agent_id: str
    model: str
    status: str = "PENDING"
    result: Optional[str] = None
