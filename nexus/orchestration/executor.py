from nexus.dispatchers.omniroute import run_omniroute_worker


class PlanExecutionError(RuntimeError):
    pass


def _build_worker_task(worker: dict) -> str:
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

Return:
- files changed;
- validation performed;
- result;
- blockers or unresolved assumptions.
""".strip()


def execute_plan(
    run_id: str,
    repo: str,
    manager_id: str,
    plan: dict,
) -> list[dict]:
    results = []

    workers = plan["workers"]

    for index, worker in enumerate(workers, start=1):
        execution_path = worker["execution_path"]

        print()
        print(f"NEXUS → WORKER {index}")
        print("=" * 70)
        print(f"Route : {worker['route_class']}")
        print(f"Path  : {execution_path}")
        print(f"Model : {worker['model']}")
        print()

        if execution_path == "OMNIROUTE":
            result = run_omniroute_worker(
                run_id=run_id,
                repo=repo,
                task=_build_worker_task(worker),
                model=worker["model"],
                effort=worker["effort"],
                parent_agent_id=manager_id,
            )

            result["worker_index"] = index
            result["route_class"] = worker["route_class"]

            results.append(result)

            if result["status"] != "COMPLETED":
                raise PlanExecutionError(
                    f"Worker {index} failed with "
                    f"status={result['status']} "
                    f"exit_code={result['exit_code']}"
                )

            continue

        if execution_path == "NATIVE_CODEX":
            raise PlanExecutionError(
                "BLOCKED_EXECUTION_PATH_NOT_IMPLEMENTED: "
                "NATIVE_CODEX"
            )

        raise PlanExecutionError(
            f"Unsupported execution path: {execution_path}"
        )

    return results

