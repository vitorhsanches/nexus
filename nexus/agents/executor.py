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

    def execute_task(
        self,
        task_id,
        agent_id=None,
        adapter=None,
        workspace_path=None,
        mission_context=None,
        hold_for_review=False,
        route_override=None,
    ):
        """Execute a task with an explicit or automatically selected agent.

        When ``agent_id`` is omitted, the capability router selects the best
        available agent for the task's required capabilities. The execution is
        tracked in an AgentSession, which is marked COMPLETED on success and
        FAILED on exception. Returns a TaskExecutionResult on success and
        raises an exception on failure paths that prevent execution.

        When ``hold_for_review`` is True, a successful adapter run stops the
        Task at REVIEW instead of finalizing it to COMPLETED: the Worker
        Session still completes successfully, the Agent is still released to
        AVAILABLE, and the returned TaskExecutionResult reports status
        REVIEW. Legacy/default behavior (``hold_for_review=False``) is
        unchanged and finalizes REVIEW -> COMPLETED within this call.

        ``route_override``, when provided, is forwarded to the adapter via
        the ExecutionContext so a caller-selected route (already validated
        against the approved escalation policy) can be used for this single
        attempt without mutating the Task's own ``execution_policy``.
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
                task,
                agent,
                required,
                workspace_path=workspace_path,
                mission_context=mission_context,
                route_override=route_override,
            )
            adapter_result = active_adapter.run(context)

            if not adapter_result.success:
                raise RuntimeError(adapter_result.error or "Adapter execution failed")

            # Advance to REVIEW once the adapter reports success. Legacy
            # (non-reviewed) callers finalize immediately to COMPLETED;
            # reviewed callers stop at REVIEW until a verdict is applied.
            update_task_status(task_id, "REVIEW")
            if not hold_for_review:
                update_task_status(task_id, "COMPLETED")
            ok = True
            final_status = "REVIEW" if hold_for_review else "COMPLETED"
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
            status=final_status,
            output=adapter_result.output or "Task executed successfully by agent",
            error=None,
            routed_model=adapter_result.routed_model,
        )

    @staticmethod
    def _run_task_lifecycle(task_id):
        """Advance a task up to RUNNING from wherever it currently is.

        Runs before the adapter is invoked. Tests may monkeypatch this
        staticmethod to simulate a lifecycle failure before real work starts.
        Handles both a fresh Task (CREATED) and a Task returning for a
        review-driven retry (already READY from a prior REVIEW verdict) by
        only advancing through the chain steps still ahead of it.
        """
        chain = ("READY", "CLAIMED", "RUNNING")
        task = get_task(task_id)
        if task.status in chain:
            remaining = chain[chain.index(task.status) + 1 :]
        else:
            remaining = chain
        for target in remaining:
            update_task_status(task_id, target)

    @staticmethod
    def _build_context(
        task,
        agent,
        required_capabilities,
        workspace_path=None,
        mission_context=None,
        route_override=None,
    ):
        policy = task.execution_policy if isinstance(task.execution_policy, dict) else None
        resolved_mission_context = mission_context
        if resolved_mission_context is None and policy is not None:
            resolved_mission_context = policy.get("mission_context")

        resolved_workspace_path = workspace_path
        if resolved_workspace_path is None and policy is not None:
            resolved_workspace_path = policy.get("workspace_path")

        return ExecutionContext(
            task_id=task.task_id,
            task_title=task.title,
            required_capabilities=list(required_capabilities or []),
            execution_policy=policy,
            mission_context=resolved_mission_context,
            workspace_path=resolved_workspace_path,
            agent_id=agent.agent_id,
            agent_model=agent.model,
            task_description=task.description,
            acceptance_criteria=(
                list(task.acceptance_criteria)
                if task.acceptance_criteria is not None
                else None
            ),
            route_override=route_override,
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
