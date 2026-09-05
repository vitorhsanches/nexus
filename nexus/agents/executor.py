"""AgentExecutor for the Nexus Agent Execution Loop V1.

Assigns an available agent to a task from the existing Task Registry, drives
both the task and agent lifecycles, and delegates the actual unit of work to
a pluggable ``ExecutionAdapter`` (see ``nexus.agents.adapters``). Each
execution is wrapped in an AgentSession from the Nexus Agent Workspace V1,
which tracks the task-to-agent link across the run. The default adapter is
the local ``SimulatedAdapter``, so no external AI models are invoked unless a
different adapter is explicitly passed in.
"""

from nexus.agents.adapters.base import ExecutionContext
from nexus.agents.adapters.simulated import SimulatedAdapter
from nexus.agents.models import TaskExecutionResult
from nexus.agents.registry import NoAvailableAgentError
from nexus.capabilities.router import select_agent_for_capabilities
from nexus.tasks.registry import get_task, update_task_status
from nexus.workspaces.service import complete_session, fail_session, start_session


class AgentExecutor:
    """Coordinates task execution through the in-memory Agent Registry."""

    def __init__(self, agent_registry, adapter=None):
        self.agent_registry = agent_registry
        self.last_session = None
        # Defaults to the simulated adapter so existing/default behavior
        # never invokes external models unless a real adapter is supplied.
        self.adapter = adapter if adapter is not None else SimulatedAdapter()

    def execute_task(self, task_id, agent_id=None, adapter=None, workspace_path=None):
        """Execute a task with an explicit or automatically selected agent.

        When ``agent_id`` is omitted, the capability router selects the best
        available agent for the task's required capabilities. The execution is
        tracked in an AgentSession, which is marked COMPLETED on success and
        FAILED on exception. Returns a TaskExecutionResult on success and
        raises an exception on failure paths that prevent execution.
        """
        task = get_task(task_id)
        required = _required_capabilities(task)

        if agent_id is not None:
            agent = self.agent_registry.get_agent(agent_id)
        else:
            agent = select_agent_for_capabilities(
                required_capabilities=required,
                agent_registry=self.agent_registry,
            )

        # Assign agent and move the agent lifecycle into BUSY.
        task.assigned_agent = agent.agent_id
        self.agent_registry.update_agent_status(agent.agent_id, "BUSY")

        # 1-2. Create the AgentSession and mark it RUNNING.
        session = start_session(task_id=task_id, agent_id=agent.agent_id)
        self.last_session = session

        active_adapter = adapter if adapter is not None else self.adapter

        ok = False
        reached_running = False
        adapter_result = None
        try:
            # 3. Advance the task lifecycle up to RUNNING, then delegate the
            # actual work to the configured adapter.
            self._run_task_lifecycle(task_id)
            reached_running = True

            context = self._build_context(
                task, agent, required, workspace_path=workspace_path
            )
            adapter_result = active_adapter.run(context)

            if not adapter_result.success:
                raise RuntimeError(adapter_result.error or "Adapter execution failed")

            # Advance the remaining chain once the adapter reports success.
            for target in ("REVIEW", "COMPLETED"):
                update_task_status(task_id, target)
            ok = True
        except Exception as exc:  # noqa: BLE001 - session must reflect failure
            # 5. Mark the session FAILED on exception.
            fail_session(session.session_id, error=str(exc))
            # Only move the task to FAILED once it has actually reached
            # RUNNING; a lifecycle failure earlier in the chain must not
            # attempt an invalid transition.
            if reached_running:
                update_task_status(task_id, "FAILED")
            raise
        finally:
            if ok:
                self.agent_registry.update_agent_status(agent.agent_id, "AVAILABLE")
            else:
                self.agent_registry.update_agent_status(agent.agent_id, "FAILED")

        # 4. Mark the session COMPLETED on success.
        complete_session(session.session_id, result="Task executed successfully")

        return TaskExecutionResult(
            task_id=task_id,
            agent_id=agent.agent_id,
            status="COMPLETED",
            output=adapter_result.output or "Task executed successfully by agent",
            error=None,
            routed_model=adapter_result.routed_model,
        )

    @staticmethod
    def _run_task_lifecycle(task_id):
        """Advance a task through the valid CREATED..RUNNING chain.

        Runs before the adapter is invoked. Tests may monkeypatch this
        staticmethod to simulate a lifecycle failure before real work starts.
        """
        for target in ("READY", "CLAIMED", "RUNNING"):
            update_task_status(task_id, target)

    @staticmethod
    def _build_context(task, agent, required_capabilities, workspace_path=None):
        policy = task.execution_policy if isinstance(task.execution_policy, dict) else None
        mission_context = None
        if policy is not None:
            mission_context = policy.get("mission_context")

        resolved_workspace_path = workspace_path
        if resolved_workspace_path is None and policy is not None:
            resolved_workspace_path = policy.get("workspace_path")

        return ExecutionContext(
            task_id=task.task_id,
            task_title=task.title,
            required_capabilities=list(required_capabilities or []),
            execution_policy=policy,
            mission_context=mission_context,
            workspace_path=resolved_workspace_path,
            agent_id=agent.agent_id,
            agent_model=agent.model,
        )


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
