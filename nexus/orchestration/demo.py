from nexus.registry.agents import create_agent, update_agent_status
from nexus.registry.runs import create_run, update_run_status


def create_demo_run() -> tuple[str, list[str]]:
    run_id = create_run(
        project_id="norte",
        input_text="Nexus demo: simulate Codex + big-pickle + Claude workers",
        intent="GO",
        risk="LOW",
    )

    update_run_status(run_id, "RUNNING")

    manager_id = create_agent(
        run_id=run_id,
        role="Manager",
        provider="codex",
        model="gpt-5.6-luna",
        effort="low",
        status="RUNNING",
    )

    big_pickle_id = create_agent(
        run_id=run_id,
        role="Worker",
        provider="omniroute",
        model="oc/big-pickle",
        effort="low",
        status="QUEUED",
        parent_agent_id=manager_id,
        branch="orchestrator/demo-big-pickle",
        worktree=r"C:\demo\big-pickle",
    )

    update_agent_status(big_pickle_id, "RUNNING")
    update_agent_status(big_pickle_id, "COMPLETED")

    claude_id = create_agent(
        run_id=run_id,
        role="Worker",
        provider="omniroute",
        model="cc/claude-sonnet-5-low",
        effort="low",
        status="RUNNING",
        parent_agent_id=manager_id,
        branch="orchestrator/demo-claude",
        worktree=r"C:\demo\claude-sonnet",
    )

    return run_id, [
        manager_id,
        big_pickle_id,
        claude_id,
    ]
