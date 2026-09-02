from nexus.dispatchers.omniroute import run_omniroute_worker


class PlanExecutionError(RuntimeError):
    pass


def build_worker_task(
    worker: dict,
    previous_failure: str | None = None,
) -> str:
    escalation_context = ""

    if previous_failure:
        escalation_context = f"""

ESCALATION CONTEXT
------------------
{previous_failure}
"""

    return f"""
Objective:
{worker["scope"]}

Routing:
- Route class: {worker["route_class"]}
- Execution path: {worker["execution_path"]}
- Provider: {worker["provider"]}
- Model: {worker["model"]}
- Effort: {worker["effort"]}

Requirements:
- Inspect the relevant files before changing anything.
- Stay strictly inside the assigned scope.
- Make the smallest correct change.
- Run the targeted validation required by the scope.
- Do not expand scope.
- Do not commit.
- Do not publish.
- Do not integrate into the primary branch.
{escalation_context}
Return:
- files changed;
- validation performed;
- result;
- blockers or unresolved assumptions.
""".strip()


def execute_worker(
    run_id: str,
    repo: str,
    manager_id: str,
    worker: dict,
    worker_index: int = 1,
    previous_failure: str | None = None,
) -> dict:
    execution_path = worker["execution_path"]

    print()
    print(f"NEXUS → WORKER {worker_index}")
    print("=" * 70)
    print(f"Route : {worker['route_class']}")
    print(f"Path  : {execution_path}")
    print(f"Model : {worker['model']}")
    print()

    if execution_path == "OMNIROUTE":
        result = run_omniroute_worker(
            run_id=run_id,
            repo=repo,
            task=build_worker_task(
                worker,
                previous_failure=previous_failure,
            ),
            model=worker["model"],
            effort=worker["effort"],
            parent_agent_id=manager_id,
        )

        result["worker_index"] = worker_index
        result["route_class"] = worker["route_class"]
        result["model"] = worker["model"]

        return result

    if execution_path == "NATIVE_CODEX":
        raise PlanExecutionError(
            "BLOCKED_EXECUTION_PATH_NOT_IMPLEMENTED: "
            "NATIVE_CODEX"
        )

    raise PlanExecutionError(
        f"Unsupported execution path: {execution_path}"
    )


def execute_plan(
    run_id: str,
    repo: str,
    manager_id: str,
    plan: dict,
) -> list[dict]:
    results = []

    for index, worker in enumerate(
        plan["workers"],
        start=1,
    ):
        result = execute_worker(
            run_id=run_id,
            repo=repo,
            manager_id=manager_id,
            worker=worker,
            worker_index=index,
        )

        results.append(result)

        if result["status"] != "COMPLETED":
            raise PlanExecutionError(
                f"Worker {index} failed with "
                f"status={result['status']} "
                f"exit_code={result['exit_code']}"
            )

    return results
