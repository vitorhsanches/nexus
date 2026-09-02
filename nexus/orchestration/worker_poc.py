from nexus.dispatchers.omniroute import run_omniroute_worker
from nexus.registry.agents import create_agent, update_agent_status
from nexus.registry.database import initialize_database
from nexus.registry.projects import sync_projects
from nexus.registry.runs import create_run, update_run_status


POC_REPO = (
    r"C:\Users\Vitor Sanches\Desktop\Devops"
    r"\codex-omniroute-poc"
)


TASK = """
Fix the bug in calculator.py so all existing tests pass.

Requirements:
- Inspect the relevant files first.
- Make the smallest possible correct change.
- Do not modify test_calculator.py.
- Run the relevant test after the change.
- Do not use apply_patch.
- Do not use MCP tools.
- Do not commit or publish anything.
""".strip()


def main() -> None:
    initialize_database()
    sync_projects()

    run_id = create_run(
        project_id="orchestrator-poc",
        input_text="Real Nexus OmniRoute Worker integration POC",
        intent="GO",
        risk="LOW",
    )

    update_run_status(run_id, "RUNNING")

    dispatcher_id = create_agent(
        run_id=run_id,
        role="Dispatcher",
        provider="nexus",
        model="internal",
        effort="n/a",
        status="RUNNING",
    )

    print()
    print("NEXUS REAL WORKER POC")
    print("=" * 70)
    print(f"Run        : {run_id}")
    print(f"Dispatcher : {dispatcher_id}")
    print()

    try:
        result = run_omniroute_worker(
            run_id=run_id,
            repo=POC_REPO,
            task=TASK,
            model="cc/claude-sonnet-5-low",
            effort="low",
            parent_agent_id=dispatcher_id,
        )

        if result["status"] == "COMPLETED":
            update_agent_status(
                dispatcher_id,
                "COMPLETED",
            )

            update_run_status(
                run_id,
                "COMPLETED",
            )
        else:
            update_agent_status(
                dispatcher_id,
                "FAILED",
            )

            update_run_status(
                run_id,
                "FAILED",
            )

        print()
        print("NEXUS RESULT")
        print("=" * 70)
        print(f"Run       : {run_id}")
        print(f"Worker    : {result['agent_id']}")
        print(f"Status    : {result['status']}")
        print(f"Exit code : {result['exit_code']}")
        print(f"Branch    : {result['branch']}")
        print(f"Worktree  : {result['worktree']}")

    except Exception:
        update_agent_status(
            dispatcher_id,
            "FAILED",
        )

        update_run_status(
            run_id,
            "FAILED",
        )

        raise


if __name__ == "__main__":
    main()
