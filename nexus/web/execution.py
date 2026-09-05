"""Execution orchestration for the Nexus Local Mission Board V1.

Thin orchestration over the existing Agent Execution Loop and Agent Workspace.
For a given task it drives the whole execution flow by reusing the current
abstractions: the Task Registry, the AgentExecutor (which selects an agent via
the capability router, opens a workspace session, and advances the task
lifecycle), and the Task Registry Attempt record. Execution policy resolution
(simulated vs. omniroute, and the real repository target for omniroute) is
delegated to ``nexus.agents.policy.resolve_execution_policy`` so the safe
default (SimulatedAdapter) is always used unless a task explicitly opts in.
"""

from nexus.agents.executor import AgentExecutor
from nexus.agents.policy import resolve_execution_policy
from nexus.policies.escalation import validate_route_override
from nexus.tasks import registry as task_registry
from nexus.web.agents import agent_registry


def execute_task(
    task_id,
    mission_context=None,
    hold_for_review=False,
    route_override=None,
    adapter=None,
):
    """Execute a task through the existing pipeline and return a summary.

    Retrieves the task from the Task Registry, resolves its execution policy
    (adapter + workspace target), runs the existing ``AgentExecutor`` (which
    selects an agent automatically via the capability router, creates a
    workspace execution session, and advances the task status through
    CREATED -> RUNNING -> COMPLETED, or RUNNING -> FAILED on adapter
    failure), records an execution attempt, and returns an execution summary
    dict. Raises ``ExecutionPolicyError`` before any lifecycle transition or
    subprocess invocation when the policy cannot be resolved safely.
    """
    task = task_registry.get_task(task_id)

    validated_route_override = validate_route_override(
        task.execution_policy,
        route_override,
    )

    resolved = resolve_execution_policy(task.execution_policy)

    executor = AgentExecutor(
        agent_registry, adapter=adapter if adapter is not None else resolved.adapter
    )

    try:
        result = executor.execute_task(
            task_id,
            workspace_path=resolved.workspace_path,
            mission_context=mission_context,
            hold_for_review=hold_for_review,
            route_override=validated_route_override,
        )
    except Exception:
        task = task_registry.get_task(task_id)
        agent_id = task.assigned_agent
        if agent_id is not None:
            agent = agent_registry.get_agent(agent_id)
            attempt = task_registry.create_attempt(
                task_id=task_id,
                agent_id=agent_id,
                model=agent.model,
                status=task.status,
                result=None,
            )
        raise

    agent = agent_registry.get_agent(result.agent_id)
    attempt = task_registry.create_attempt(
        task_id=task_id,
        agent_id=result.agent_id,
        model=result.routed_model or agent.model,
        status=result.status,
        result=result.output,
    )

    task = task_registry.get_task(task_id)
    session = executor.last_session

    return {
        "task_id": task_id,
        "status": task.status,
        "assigned_agent": result.agent_id,
        "execution_path": resolved.execution_path,
        "workspace_path": resolved.workspace_path,
        "project_id": resolved.project_id,
        "routed_model": result.routed_model,
        "attempt_id": attempt.attempt_id,
        "session_id": session.session_id if session is not None else None,
        "output": result.output,
    }
