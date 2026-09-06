import json

from nexus.dispatchers.manager import run_manager
from nexus.orchestration.progressive import execute_progressively
from nexus.registry.runs import (
    create_run,
    update_run_result,
    update_run_risk,
    update_run_status,
)
from nexus.router import (
    ProjectAmbiguousError,
    ProjectNotFoundError,
    resolve_project,
    resolve_project_from_text,
)


class GoError(RuntimeError):
    """Raised for known, reportable Nexus go failures.

    The code attribute is one of the operational failure codes defined in
    the Nexus CLI milestone (for example PROJECT_NOT_FOUND, MANAGER_BLOCKED).
    """

    def __init__(self, code: str, message: str, run_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.run_id = run_id


def _resolve_project(project_query: str | None, request_text: str):
    try:
        if project_query:
            return resolve_project(project_query)

        return resolve_project_from_text(request_text)

    except ProjectNotFoundError as error:
        raise GoError("PROJECT_NOT_FOUND", str(error)) from error

    except ProjectAmbiguousError as error:
        raise GoError("PROJECT_AMBIGUOUS", str(error)) from error


def run_go(request_text: str, project_query: str | None = None) -> dict:
    """Execute the full Nexus go orchestration flow for a single request.

    1. resolve the project using the Project Router;
    2. create a Run;
    3. invoke the existing Luna Manager planning flow;
    4. validate the returned NEXUS_PLAN;
    5. execute each planned Worker through Progressive Capability Execution
       (Manager Review and retry/escalation are handled inside
       execute_progressively);
    6. mark the Run COMPLETED only once every Worker passes review.
    """
    project = _resolve_project(project_query, request_text)

    run_id = create_run(
        project_id=project.id,
        input_text=request_text,
        intent="GO",
        status="ROUTING",
    )

    print()
    print("NEXUS GO")
    print("=" * 70)
    print(f"Run     : {run_id}")
    print(f"Project : {project.name}")
    print(f"Repo    : {project.path}")
    print(f"Request : {request_text}")

    try:
        manager = run_manager(
            run_id=run_id,
            repo=project.path,
            task=request_text,
            model="gpt-5.6-luna",
            effort="low",
        )
    except OSError as error:
        update_run_status(run_id, "BLOCKED")
        raise GoError(
            "MANAGER_BLOCKED",
            f"Manager launch failed: {error}",
            run_id=run_id,
        ) from error

    if manager["status"] != "COMPLETED":
        update_run_status(run_id, "BLOCKED")

        if manager["status"] == "BLOCKED":
            raise GoError(
                "PLAN_INVALID",
                "Manager returned an invalid or unparseable plan: "
                f"{manager.get('error')}",
                run_id=run_id,
            )

        if manager["status"] == "LAUNCH_FAILED":
            raise GoError(
                "MANAGER_BLOCKED",
                "Manager could not be launched: "
                f"{manager.get('error')}",
                run_id=run_id,
            )

        raise GoError(
            "MANAGER_BLOCKED",
            "Manager planning failed with exit_code="
            f"{manager.get('exit_code')}.",
            run_id=run_id,
        )

    plan = manager["plan"]

    update_run_risk(run_id, plan.get("risk"))

    print()
    print("APPROVED PLAN")
    print("=" * 70)
    print(json.dumps(plan, indent=2, ensure_ascii=False))

    update_run_status(run_id, "RUNNING")

    worker_results = []

    for planned_worker in plan["workers"]:
        try:
            outcome = execute_progressively(
                run_id=run_id,
                repo=project.path,
                manager_id=manager["manager_id"],
                original_task=request_text,
                planned_worker=planned_worker,
                plan_risk=plan.get("risk"),
            )
        except OSError as error:
            update_run_status(run_id, "BLOCKED")
            raise GoError(
                "ESCALATION_UNAVAILABLE",
                f"Progressive execution failed unexpectedly: {error}",
                run_id=run_id,
            ) from error

        worker_results.append(outcome)

        if outcome["status"] != "COMPLETED":
            reason = outcome.get("reason")

            if reason == "WORKER_EXECUTION_FAILED":
                update_run_status(run_id, "FAILED")
                raise GoError(
                    "WORKER_EXECUTION_FAILED",
                    "A Worker failed to execute successfully.",
                    run_id=run_id,
                )

            if reason == "ESCALATION_UNAVAILABLE":
                update_run_status(run_id, "BLOCKED")
                raise GoError(
                    "ESCALATION_UNAVAILABLE",
                    outcome.get(
                        "error",
                        "No further escalation route available.",
                    ),
                    run_id=run_id,
                )

            update_run_status(run_id, "BLOCKED")
            raise GoError(
                "REVIEW_BLOCKED",
                "Manager Review blocked the run: "
                f"{outcome.get('reason', outcome.get('verdict'))}",
                run_id=run_id,
            )

    if worker_results:
        final_worker = worker_results[-1]["worker"]
    else:
        final_worker = None

    update_run_result(
        run_id,
        result=plan.get("summary"),
    )

    update_run_status(run_id, "COMPLETED")

    print()
    print("NEXUS GO RESULT")
    print("=" * 70)
    print(f"Run     : {run_id}")
    print("Status  : COMPLETED")
    print(f"Workers : {len(worker_results)}")
    print(f"Summary : {plan.get('summary')}")

    return {
        "run_id": run_id,
        "status": "COMPLETED",
        "project": project,
        "plan": plan,
        "workers": worker_results,
        "final_worker": final_worker,
    }
