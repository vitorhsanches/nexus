"""Execution context and adapter contract for Real Agent Adapter Layer V1.

``AgentExecutor`` builds an :class:`ExecutionContext` from the task, mission,
and agent it already looked up and hands it to an :class:`ExecutionAdapter`.
The adapter performs the actual unit of work and returns an
:class:`AdapterResult`; the executor alone still owns session/task lifecycle
transitions based on that result.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class ExecutionContext:
    """Everything an adapter needs to execute a task's work."""

    task_id: str
    task_title: str
    required_capabilities: list = field(default_factory=list)
    execution_policy: Optional[dict] = None
    mission_context: Optional[dict] = None
    workspace_path: Optional[str] = None
    agent_id: Optional[str] = None
    agent_model: Optional[str] = None
    task_description: Optional[str] = None
    acceptance_criteria: Optional[list] = None


@dataclass(slots=True)
class AdapterResult:
    """Outcome of an adapter's execution attempt."""

    success: bool
    output: Optional[str] = None
    error: Optional[str] = None
    routed_model: Optional[str] = None


class ExecutionAdapter(ABC):
    """Pluggable execution backend invoked by ``AgentExecutor``."""

    @abstractmethod
    def run(self, context: ExecutionContext) -> AdapterResult:
        """Execute the task described by ``context``.

        Must never raise for expected failure modes; instead return an
        ``AdapterResult`` with ``success=False`` and an ``error`` message.
        Unexpected exceptions may propagate and are treated as failures by
        the executor.
        """
        raise NotImplementedError
