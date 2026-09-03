"""AgentExecutor for the Nexus Agent Execution Loop V1.

Assigns an available agent to a task from the existing Task Registry,
drives both the task and agent lifecycles, and simulates the agent's work.
No external AI models are invoked.
"""

from nexus.agents.models import TaskExecutionResult
from nexus.agents.registry import NoAvailableAgentError
from nexus.capabilities.router import select_agent_for_capabilities
from nexus.tasks.registry import get_task, update_task_status


class AgentExecutor:
    """Coordinates task execution through the in-memory Agent Registry."""

    def __init__(self, agent_registry):
        self.agent_registry = agent_registry

    def execute_task(self, task_id, agent_id=None):
        """Execute a task with an explicit or automatically selected agent.

        When ``agent_id`` is omitted, the capability router selects the best
        available agent for the task's required capabilities. Returns a
        TaskExecutionResult on success and raises an exception on failure
        paths that prevent execution.
        """
        task = get_task(task_id)

        if agent_id is not None:
            agent = self.agent_registry.get_agent(agent_id)
        else:
            required = _required_capabilities(task)
            agent = select_agent_for_capabilities(
                required_capabilities=required,
                agent_registry=self.agent_registry,
            )

        # Assign agent and move both lifecycles through their valid chains.
        task.assigned_agent = agent.agent_id
        self.agent_registry.update_agent_status(agent.agent_id, "BUSY")

        ok = False
        try:
            self._run_task_lifecycle(task_id)
            ok = True
        finally:
            if ok:
                self.agent_registry.update_agent_status(agent.agent_id, "AVAILABLE")
            else:
                self.agent_registry.update_agent_status(agent.agent_id, "FAILED")

        return TaskExecutionResult(
            task_id=task_id,
            agent_id=agent.agent_id,
            status="COMPLETED",
            output="Task executed successfully by agent",
            error=None,
        )

    @staticmethod
    def _run_task_lifecycle(task_id):
        """Advance a task through the valid CREATED..COMPLETED chain."""
        chain = [
            "READY",
            "CLAIMED",
            "RUNNING",
            "REVIEW",
            "COMPLETED",
        ]
        for target in chain:
            update_task_status(task_id, target)


def _required_capabilities(task):
    """Read the task's required capabilities from its execution policy.

    The Task model carries no dedicated capabilities column, so required
    capabilities are read from the ``execution_policy`` dict when present.
    Defaults to an empty list when unset, which selects the tightest available
    agent.
    """
    policy = task.execution_policy
    if not isinstance(policy, dict):
        return []
    return list(policy.get("required_capabilities") or [])
