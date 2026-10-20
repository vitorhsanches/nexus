from nexus.dispatchers.omniroute import run_omniroute_worker


# Single source of truth for execution paths the runtime executor can
# actually run. The Manager must not approve any execution_path that is
# not present here.
SUPPORTED_EXECUTION_PATHS = frozenset({"OMNIROUTE"})

# Execution paths that are recognized by the schema/prompt but are not
# yet implemented by the executor. Plan validation rejects these with an
# explicit reason instead of an "unknown execution path" error.
KNOWN_UNIMPLEMENTED_EXECUTION_PATHS = frozenset({"NATIVE_CODEX"})


# ---------------------------------------------------------------------------
# Phase 4.3C: Authority → Sandbox Mode mapping
# ---------------------------------------------------------------------------

# Single source of truth for worker authority → codex sandbox mode translation.
# All execution attempts for a worker (initial, retry, escalation) reach
# run_omniroute_worker via execute_worker, so this is the only place the
# mapping needs to live.
_AUTHORITY_TO_SANDBOX: dict[str, str] = {
    "READ_ONLY":       "read-only",
    "WORKSPACE_WRITE": "workspace-write",
}


def sandbox_mode_for_authority(authority: str | None) -> str:
    """Deterministically map a worker authority value to a sandbox mode string.

    READ_ONLY       → "read-only"
    WORKSPACE_WRITE → "workspace-write"
    None (absent)   → "workspace-write"   # backward-compat: pre-4.3B plans
                                           # omit authority; preserve the
                                           # historical default until Phase
                                           # 4.3E promotes strict mode.

    An explicit but unrecognized authority value fails closed: it is NOT
    silently mapped to workspace-write. Authority escalation must never be
    implicit.

    This function is deterministic, stateless, and has no side effects.
    It intentionally does not infer authority from task/scope keywords.
    """
    if authority is None:
        # Explicit compatibility fallback for legacy pre-4.3B plans.
        return "workspace-write"

    result = _AUTHORITY_TO_SANDBOX.get(authority)
    if result is None:
        raise ValueError(
            f"WORKER_AUTHORITY_INVALID: unrecognized authority {authority!r} "
            "reached the execution boundary. Expected 'READ_ONLY' or "
            "'WORKSPACE_WRITE'. This value should have been rejected by "
            "_validate_plan(); reaching here indicates a validation bypass."
        )
    return result



class PlanExecutionError(RuntimeError):
    pass


def build_worker_task(
    worker: dict,
    previous_failure: str | None = None,
    dependency_context: str | None = None,
) -> str:
    escalation_context = ""

    if previous_failure:
        escalation_context = f"""

ESCALATION CONTEXT
------------------
{previous_failure}
"""

    # Phase 3 Multi-Agent MVP: inject dependency context (parent Worker
    # outputs) as a clearly delimited untrusted-data block AFTER the
    # assigned task and BEFORE the return requirements. The context string
    # is pre-formatted by nexus.orchestration.context with provenance,
    # truncation markers, and a security boundary.
    dep_block = ""
    if dependency_context:
        dep_block = f"""

{dependency_context}
"""

    # Phase 4.3D: inject worker contract fields when present.
    # Only fields that are explicitly set are included -- no fake values
    # are injected for absent fields. Backward compatibility is preserved:
    # a worker dict that predates 4.3B will simply omit the contract block.
    deliverable = worker.get("deliverable")
    acceptance_criteria = worker.get("acceptance_criteria")
    if deliverable or acceptance_criteria:
        contract_parts = ["\nWORKER CONTRACT\n---------------"]
        if deliverable:
            contract_parts.append(f"Expected Deliverable:\n{deliverable}")
        if acceptance_criteria:
            contract_parts.append(f"Acceptance Criteria:\n{acceptance_criteria}")
        contract_block = "\n\n".join(contract_parts) + "\n"
    else:
        contract_block = ""

    return f"""
ASSIGNED TASK
=============
Objective:
{worker["scope"]}
{contract_block}
Routing:
- Route class: {worker["route_class"]}
- Execution path: {worker["execution_path"]}
- Provider: {worker["provider"]}
- Model: {worker["model"]}
- Effort: {worker["effort"]}
{dep_block}
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
    dependency_context: str | None = None,
) -> dict:
    execution_path = worker["execution_path"]

    print()
    print(f"NEXUS -> WORKER {worker_index}")
    print("=" * 70)
    print(f"Route : {worker['route_class']}")
    print(f"Path  : {execution_path}")
    print(f"Model : {worker['model']}")
    print()

    if execution_path == "OMNIROUTE":
        # Phase 4.3C: map worker authority to sandbox mode.
        # sandbox_mode_for_authority is deterministic and stateless;
        # authority is preserved by deepcopy through retries/escalations.
        result = run_omniroute_worker(
            run_id=run_id,
            repo=repo,
            task=build_worker_task(
                worker,
                previous_failure=previous_failure,
                dependency_context=dependency_context,
            ),
            model=worker["model"],
            effort=worker["effort"],
            parent_agent_id=manager_id,
            sandbox_mode=sandbox_mode_for_authority(worker.get("authority")),
            # The Manager's planned capability requirement -- never
            # overwritten by adaptive routing (unlike model/provider/
            # effort above, which are already the router's actual
            # decision at this point; see resolve_initial_worker_route).
            route_class=worker.get("route_class"),
        )

        result["worker_index"] = worker_index
        result["route_class"] = worker["route_class"]
        result["model"] = worker["model"]

        return result

    if execution_path in KNOWN_UNIMPLEMENTED_EXECUTION_PATHS:
        raise PlanExecutionError(
            f"BLOCKED_EXECUTION_PATH_NOT_IMPLEMENTED: {execution_path}"
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
