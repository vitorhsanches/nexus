"""Execution orchestration for the Nexus Local Mission Board V1.

Thin orchestration over the existing Agent Execution Loop and Agent Workspace.
For a given task it drives the whole execution flow by reusing the current
abstractions: the Task Registry, the AgentExecutor (which selects an agent via
the capability router, opens a workspace session, and advances the task
lifecycle), and the Task Registry Attempt record. No external AI models are
invoked.
"""

from nexus.agents.executor import AgentExecutor
from nexus.tasks import registry as task_registry
from nexus.web.agents import agent_registry


def execute_task(task_id):
    """Execute a task through the existing pipeline and return a summary.

    Retrieves the task from the Task Registry, runs the existing
    ``AgentExecutor`` (which selects an agent automatically via the capability
    router, creates a workspace execution session, and advances the task status
    through CREATED -> RUNNING -> COMPLETED), records an execution attempt, and
    returns an execution summary dict.
    """
    task_registry.get_task(task_id)

    executor = AgentExecutor(agent_registry)
    result = executor.execute_task(task_id)

    agent = agent_registry.get_agent(result.agent_id)
    attempt = task_registry.create_attempt(
        task_id=task_id,
        agent_id=result.agent_id,
        model=agent.model,
        status=result.status,
        result=result.output,
    )

    task = task_registry.get_task(task_id)
    session = executor.last_session

    return {
        "task_id": task_id,
        "status": task.status,
        "assigned_agent": result.agent_id,
        "attempt_id": attempt.attempt_id,
        "session_id": session.session_id if session is not None else None,
        "output": result.output,
    }
