import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from nexus.dispatchers.manager import run_manager
from nexus.dispatchers.triage import MissionScope, classify_mission_scope
from nexus.policies.reasoning_effort import select_reasoning_effort
from nexus.policies.manager_model import (
    ManagerModelUnavailableError,
    select_manager_model,
    validate_manual_manager_model,
)
from nexus.orchestration.scout import ScoutStageFailedError, run_scout_stage
from nexus.orchestration.context import build_dependency_context
from nexus.orchestration.dag import (
    WorkerDependencyError,
    eligible_worker_batch,
    normalize_worker_dependencies,
    sink_nodes,
    validate_worker_dependencies,
)
from nexus.orchestration.progressive import execute_progressively
from nexus.orchestration.integration import IntegrationError, run_integrated_plan
from nexus.failure_contract import build_failure, review_failure, worker_failure
from nexus.registry.checkpoints import (
    CheckpointWriteError,
    record_approved_plan,
    record_checkpoint,
)
from nexus.orchestration.ownership import establish_ownership
from nexus.orchestration.recovery import reconcile_stale_ownership
from nexus.orchestration.strategy import (
    ExecutionStrategy,
    choose_execution_strategy,
    direct_worker_for,
    infer_pre_execution_risk,
)
from nexus.telemetry import get_run_summary, record_call, start_run
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

    def __init__(
        self,
        code: str,
        message: str,
        run_id: str | None = None,
        *,
        detail: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.run_id = run_id
        # Phase 4: the raw outcome dict (worker/review) at the raise site,
        # when one exists -- lets nexus.failure_classification recover the
        # Reviewer/Worker's own finer-grained classification instead of
        # re-parsing the message text.
        self.detail = detail


def _resolve_project(project_query: str | None, request_text: str):
    try:
        if project_query:
            return resolve_project(project_query)

        return resolve_project_from_text(request_text)

    except ProjectNotFoundError as error:
        raise GoError("PROJECT_NOT_FOUND", str(error)) from error

    except ProjectAmbiguousError as error:
        raise GoError("PROJECT_AMBIGUOUS", str(error)) from error


def _review_outcome_failure(outcome: dict, worker_ordinal):
    """Map a non-accepting Review outcome onto the shared review_failure()."""
    reason = outcome.get("reason")
    code = reason if reason in ("REVIEW_FAILED", "UNKNOWN_REVIEW_VERDICT") else "REVIEW_BLOCKED"
    review = outcome.get("review") or {}
    history = outcome.get("history") or []
    last = history[-1] if history else {}
    if code == "REVIEW_FAILED":
        # The Review dispatcher failed; its own result carries the reviewer identity.
        routing = review.get("routing") or {}
        return review_failure(
            code,
            review,
            reviewer_id=review.get("reviewer_id"),
            reviewer_model=routing.get("model"),
            reviewer_provider=routing.get("provider"),
            worker_ordinal=worker_ordinal,
            worktree=last.get("worktree"),
        )
    return review_failure(
        code,
        review,
        reviewer_id=last.get("reviewer_id"),
        reviewer_model=last.get("reviewer_model"),
        reviewer_provider=last.get("reviewer_provider"),
        worker_ordinal=worker_ordinal,
        worktree=last.get("worktree"),
    )


def _record_terminal_failure(run_id: str, *, status: str, reason: str, failure: dict):
    """Persist a RUN_TERMINAL checkpoint that carries the structured failure.

    ``reason`` keeps its legacy meaning; ``failure`` carries the specific cause.
    """
    record_checkpoint(
        run_id=run_id,
        boundary="RUN_TERMINAL",
        payload={"status": status, "reason": reason, "failure": failure},
    )


def _raise_worker_failure(run_id: str, worker_ordinal: int, outcome: dict):
    """Translate one Worker's terminal non-COMPLETED outcome into the
    matching GoError, exactly as the pre-Phase-2 sequential loop did.

    Always raises; never returns. Kept as its own function so the
    round-based scheduler (Requirement 1) can call the exact same
    reason-based branching for a Worker chosen anywhere in the plan,
    instead of only ever the "current" Worker of a single for-loop.
    """
    reason = outcome.get("reason")

    if reason == "WORKER_EXECUTION_FAILED":
        update_run_status(run_id, "FAILED")
        record_checkpoint(
            run_id=run_id,
            boundary="RUN_TERMINAL",
            payload={
                "status": "FAILED",
                "reason": "WORKER_EXECUTION_FAILED",
                "failure": worker_failure(outcome.get("worker") or {}, worker_ordinal),
            },
            worker_ordinal=worker_ordinal,
        )
        raise GoError(
            "WORKER_EXECUTION_FAILED",
            "A Worker failed to execute successfully.",
            run_id=run_id,
            detail=outcome,
        )

    if reason == "ESCALATION_UNAVAILABLE":
        update_run_status(run_id, "BLOCKED")
        record_checkpoint(
            run_id=run_id,
            boundary="RUN_TERMINAL",
            payload={
                "status": "BLOCKED",
                "reason": "ESCALATION_UNAVAILABLE",
                "failure": build_failure(
                    stage="ROUTING",
                    code="ESCALATION_UNAVAILABLE",
                    message=outcome.get("error"),
                    worker_ordinal=worker_ordinal,
                ),
            },
            worker_ordinal=worker_ordinal,
        )
        raise GoError(
            "ESCALATION_UNAVAILABLE",
            outcome.get(
                "error",
                "No further escalation route available.",
            ),
            run_id=run_id,
        )

    if reason == "INITIAL_ROUTING_UNAVAILABLE":
        # This failure happens inside resolve_initial_worker_route(),
        # strictly before any Worker executes and before any real
        # Manager Review step runs. It must never be reported as
        # "Manager Review blocked the run" -- that label is reserved
        # for an actual review outcome (REVIEW_FAILED, a BLOCKED
        # verdict, or an unrecognized verdict below). The original
        # router cause is preserved verbatim (never secrets --
        # routing exceptions never carry credentials).
        update_run_status(run_id, "BLOCKED")
        record_checkpoint(
            run_id=run_id,
            boundary="RUN_TERMINAL",
            payload={
                "status": "BLOCKED",
                "reason": "INITIAL_ROUTING_UNAVAILABLE",
                "error": outcome.get("error"),
                "error_type": outcome.get("error_type"),
                "route_class": outcome.get("route_class"),
                "capability": outcome.get("capability"),
                "risk": outcome.get("risk"),
                "route_rejections": outcome.get("route_rejections", ()),
                "failure": build_failure(
                    stage="ROUTING",
                    code="INITIAL_ROUTING_UNAVAILABLE",
                    message=outcome.get("error"),
                    worker_ordinal=worker_ordinal,
                ),
            },
            worker_ordinal=worker_ordinal,
        )
        raise GoError(
            "INITIAL_ROUTING_UNAVAILABLE",
            "Initial adaptive routing could not resolve a Worker "
            "route for this plan; no Worker executed and no "
            "review step ran: "
            f"{outcome.get('error', reason)}",
            run_id=run_id,
            detail=outcome,
        )

    update_run_status(run_id, "BLOCKED")
    record_checkpoint(
        run_id=run_id,
        boundary="RUN_TERMINAL",
        payload={
            "status": "BLOCKED",
            "reason": outcome.get("reason", outcome.get("verdict")),
            # Preserve the original router/escalation cause (never
            # secrets -- routing exceptions never carry
            # credentials) instead of collapsing it to a bare
            # reason code, so Recovery/Web Board can show why.
            "error": outcome.get("error"),
            "error_type": outcome.get("error_type"),
            "route_class": outcome.get("route_class"),
            "capability": outcome.get("capability"),
            "failure": _review_outcome_failure(outcome, worker_ordinal),
        },
        worker_ordinal=worker_ordinal,
    )
    raise GoError(
        "REVIEW_BLOCKED",
        "Manager Review blocked the run: "
        f"{outcome.get('reason', outcome.get('verdict'))}",
        run_id=run_id,
        detail=outcome,
    )


def _run_workers_by_round(
    run_id: str,
    repo: str,
    manager_id: str,
    request_text: str,
    workers: list,
    plan_risk,
) -> list:
    """Execute plan["workers"] concurrently, one DAG round at a time.

    Requirement 1/2: replaces the old sequential ``for`` loop with a
    scheduler driven by ``eligible_worker_batch()``
    (nexus.orchestration.dag, Phase 1). Every Worker eligible in the same
    round (its depends_on are all already COMPLETED) is submitted to a
    ThreadPoolExecutor and runs concurrently; the scheduler then waits for
    the WHOLE round to finish before deciding whether to start the next
    round. concurrent.futures only -- no multiprocessing, no asyncio.

    Requirement 3/4: execute_progressively() itself is called completely
    unchanged, once per Worker, and is still the sole, complete unit of
    execution for a single Worker (Adaptive Routing -> Worker -> Review ->
    Retry/Escalation). Nothing here shares mutable retry/routing state
    between Workers -- each thread calls execute_progressively() with its
    own planned_worker dict (deep-copied internally by
    resolve_initial_worker_route()) and its own local attempt/retry
    counters, exactly as a sequential call would.

    Requirement 5/6: worker_ordinal is always ``worker_index + 1``,
    fixed by the Worker's position in plan["workers"] -- never by thread
    start/finish order. Results are written into a pre-sized list by
    index (``worker_results[worker_index] = outcome``), never appended in
    completion order, so the returned list's order always matches
    plan["workers"]'s order regardless of which thread finished first.

    Requirement 8: a Worker that ends terminally non-COMPLETED aborts the
    Run, but only after every Worker already started in that SAME round
    has been allowed to finish naturally (as_completed() is drained fully
    before any GoError is raised) -- no in-flight Worker of the same
    round is ever cancelled. Dependents of a failed Worker are never
    scheduled, because a failed Worker's index is never added to
    ``completed_indices`` and eligible_worker_batch() requires every
    depends_on index to be completed.

    When more than one Worker in the same round ends non-COMPLETED (or
    raises), the one reported is chosen deterministically by ascending
    plan index (the same order eligible_worker_batch() already returns),
    never by which thread happened to finish/raise first.
    """
    worker_count = len(workers)
    worker_results: list = [None] * worker_count
    completed_indices: set[int] = set()

    if worker_count == 0:
        return worker_results

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        while len(completed_indices) < worker_count:
            eligible = eligible_worker_batch(workers, completed_indices)

            if not eligible:
                # The DAG was already validated acyclic with only
                # resolvable indices before this scheduler ever started,
                # so this should be unreachable. Fail closed rather than
                # spin or hang if it is ever reached regardless.
                update_run_status(run_id, "BLOCKED")
                raise GoError(
                    "PLAN_INVALID",
                    "No Worker is eligible to run but the plan has "
                    "incomplete work remaining; this indicates a "
                    "scheduler/dependency-graph inconsistency.",
                    run_id=run_id,
                )

            # Phase 3 Multi-Agent MVP: build dependency context for each
            # eligible Worker from its COMPLETED direct parents' outputs.
            # Context is built BEFORE submitting to the executor so it
            # is frozen (the same logical context survives retries inside
            # execute_progressively -- Requirement 10).
            worker_contexts: dict[int, str] = {}
            for worker_index in eligible:
                dep_ctx_text, _entries = build_dependency_context(
                    depends_on=workers[worker_index].get("depends_on") or [],
                    worker_results=worker_results,
                )
                worker_contexts[worker_index] = dep_ctx_text or None

            futures = {
                executor.submit(
                    execute_progressively,
                    run_id=run_id,
                    repo=repo,
                    manager_id=manager_id,
                    original_task=request_text,
                    planned_worker=workers[worker_index],
                    plan_risk=plan_risk,
                    worker_ordinal=worker_index + 1,
                    dependency_context=worker_contexts.get(worker_index),
                ): worker_index
                for worker_index in eligible
            }

            round_outcomes: dict = {}
            round_exceptions: dict = {}

            # Drain the WHOLE round before deciding anything -- every
            # Worker already started here is allowed to finish naturally
            # (Requirement 8), regardless of any earlier failure in this
            # same batch.
            for future in as_completed(futures):
                worker_index = futures[future]
                try:
                    round_outcomes[worker_index] = future.result()
                except Exception as error:  # noqa: BLE001 - re-examined below
                    round_exceptions[worker_index] = error

            any_failure = bool(round_exceptions) or any(
                round_outcomes[worker_index]["status"] != "COMPLETED"
                for worker_index in eligible
                if worker_index in round_outcomes
            )

            if not any_failure:
                for worker_index in eligible:
                    outcome = round_outcomes[worker_index]
                    # Phase 3: stash the planned scope so the context
                    # builder can include provenance without reaching
                    # back into the plan list.
                    outcome["_planned_scope"] = (
                        workers[worker_index].get("scope") or ""
                    )
                    worker_results[worker_index] = outcome
                    completed_indices.add(worker_index)
                continue

            # Store every outcome we do have (successful Workers of a
            # partially-failed round keep their real, valid result) before
            # raising -- never overwritten, never treated as a Run success.
            for worker_index in eligible:
                if worker_index in round_outcomes:
                    outcome = round_outcomes[worker_index]
                    outcome["_planned_scope"] = (
                        workers[worker_index].get("scope") or ""
                    )
                    worker_results[worker_index] = outcome

            # Deterministic: report the first failure by ascending plan
            # index, never by race/completion order.
            for worker_index in eligible:
                worker_ordinal = worker_index + 1

                if worker_index in round_exceptions:
                    error = round_exceptions[worker_index]

                    if isinstance(error, CheckpointWriteError):
                        update_run_status(run_id, "BLOCKED")
                        raise GoError(
                            "CHECKPOINT_PERSISTENCE_FAILED",
                            "Could not persist execution checkpoint: "
                            f"{error}",
                            run_id=run_id,
                        ) from error

                    if isinstance(error, OSError):
                        update_run_status(run_id, "BLOCKED")
                        raise GoError(
                            "ESCALATION_UNAVAILABLE",
                            "Progressive execution failed unexpectedly: "
                            f"{error}",
                            run_id=run_id,
                        ) from error

                    # Unknown/unexpected error: preserve and propagate it
                    # exactly as-is, same as the pre-Phase-2 sequential
                    # loop did (it never wrapped an unrecognized
                    # exception type into a GoError).
                    raise error

                outcome = round_outcomes[worker_index]

                if outcome["status"] != "COMPLETED":
                    _raise_worker_failure(run_id, worker_ordinal, outcome)

    return worker_results


def run_go(
    request_text: str,
    project_query: str | None = None,
    *,
    planning_required: bool = False,
    task_type: str | None = None,
    execution_mode: str | None = None,
    required_capabilities: tuple[str, ...] = (),
    unsupported_capabilities: tuple[str, ...] = (),
    authority_result: str | None = None,
    autonomy_profile: str | None = None,
    exclude_models: frozenset[str] = frozenset(),
    manager_model: str | None = None,
    prepare_dependencies: bool = False,
) -> dict:
    """Execute the full Nexus go orchestration flow for a single request.

    1. resolve the project using the Project Router;
    2. create a Run;
    3. choose DIRECT or DELEGATED execution deterministically;
    4. invoke Manager planning only for DELEGATED execution;
    5. execute each planned Worker through Progressive Capability Execution
       (Manager Review and retry/escalation are handled inside
       execute_progressively);
    6. mark the Run COMPLETED only once every Worker passes review.

    ``planning_required`` forces DELEGATED execution (Manager planning) even
    when the request text looks bounded; Program phases use it.

    ``exclude_models`` is forwarded to every Worker's initial adaptive route
    resolution (``AdaptiveRoutingService.select_route_for_capability``'s
    existing ``blocked_models`` hard-eligibility gate). Program's bounded
    phase retry passes the model(s) a previous Manager Review already
    judged CAPABILITY_INSUFFICIENT here, so a repeated route_class request
    for the same capability deterministically lands on a different,
    cost-aware-ranked eligible route instead of blindly repeating one
    already proven insufficient.

    ``manager_model``, when given, overrides the scope-based Manager model
    policy (``select_manager_model``) with the caller's explicit choice.
    It affects only the Manager -- Worker adaptive routing and Reviewer
    policy are untouched. It is validated against
    ``MANAGER_MODEL_CHOICES`` *before* any Run is created, so an invalid
    choice fails fast with no Run row and no Manager dispatch attempt.
    ``None`` (the default) preserves the existing automatic behavior.

    ``prepare_dependencies``, when True, opts this Run into explicit
    JavaScript dependency readiness (nexus.orchestration.js_dependency_prep):
    before any Worker/Reviewer Codex subprocess runs, the integration
    worktree gets `npm ci --ignore-scripts` (host-run, never inside the
    agent sandbox, never against the primary repository) and a baseline
    `npm test`, then node_modules is propagated into each Worker's
    sub-worktree. Default is False -- completely unchanged GO behavior.
    An explicit request that cannot be honored (no package.json +
    package-lock.json, install failure, missing declared test binary)
    raises before any Worker launches, with the exact reason -- an
    explicit request is never silently ignored or bypassed.
    """
    if manager_model is not None:
        # Fail before touching the registry at all: an invalid manual
        # selection must never create a Run.
        try:
            validate_manual_manager_model(manager_model)
        except ManagerModelUnavailableError as error:
            raise GoError(
                "MANAGER_MODEL_INVALID",
                str(error),
                run_id=None,
            ) from error

    # Detect and clean up runs owned by processes that are no longer alive.
    # This runs before creating a new run so the DB is always in a consistent
    # state when this instance starts doing real work.
    _reconciled = reconcile_stale_ownership()
    for _r_id in _reconciled:
        print(f"[nexus] reconciled orphan run {_r_id} -> FAILED")

    project = _resolve_project(project_query, request_text)

    run_id = create_run(
        project_id=project.id,
        input_text=request_text,
        intent="GO",
        status="ROUTING",
        manager_mode="manual" if manager_model is not None else "auto",
        manager_requested_model=manager_model,
    )
    establish_ownership(run_id)

    # Phase 4.1: deterministic, side-effect-free mission-scope triage.
    # This classifies the raw request as BOUNDED or BROAD *before* the
    # Manager plans anything. The strategy gate then sends eligible bounded
    # work directly to one Worker; delegated work retains Manager planning.
    triage_result = classify_mission_scope(request_text)
    strategy_risk = infer_pre_execution_risk(request_text)
    strategy, strategy_reason = choose_execution_strategy(
        request_text,
        complexity="LOW" if triage_result.scope == MissionScope.BOUNDED else "HIGH",
        risk=strategy_risk,
        planning_required=planning_required,
    )
    start_run(
        run_id,
        strategy.value,
        task_type=task_type,
        execution_mode=execution_mode,
        required_capabilities=required_capabilities,
        unsupported_capabilities=unsupported_capabilities,
        authority_result=authority_result,
        autonomy_profile=autonomy_profile,
    )

    try:
        record_checkpoint(
            run_id=run_id,
            boundary="MISSION_TRIAGE",
            payload={
                "classification": triage_result.scope.value,
                "reason_codes": list(triage_result.reason_codes),
                "execution_strategy": strategy.value,
                "strategy_reason": strategy_reason,
                "task_type": task_type,
                "execution_mode": execution_mode,
                "required_capabilities": list(required_capabilities),
                "unsupported_capabilities": list(unsupported_capabilities),
                "authority_result": authority_result,
                "autonomy_profile": autonomy_profile,
            },
        )
    except CheckpointWriteError as error:
        update_run_status(run_id, "BLOCKED")
        raise GoError(
            "CHECKPOINT_PERSISTENCE_FAILED",
            f"Could not persist mission triage state: {error}",
            run_id=run_id,
        ) from error

    print()
    print("NEXUS GO")
    print("=" * 70)
    print(f"Run     : {run_id}")
    print(f"Project : {project.name}")
    print(f"Repo    : {project.path}")
    print(f"Request : {request_text}")
    print(f"Triage  : {triage_result.scope.value}")

    # Phase 4.2: branch on triage classification.
    # BOUNDED: no scouts; existing run_manager path unchanged.
    # BROAD: run the two-scout pre-planning stage first, build an
    #        evidence bundle, and pass it to run_manager.
    repository_evidence: str | None = None

    if triage_result.scope == MissionScope.BROAD:
        try:
            repository_evidence = run_scout_stage(
                run_id=run_id,
                repo=project.path,
                request_text=request_text,
            )
        except ScoutStageFailedError as error:
            update_run_status(run_id, "BLOCKED")
            raise GoError(
                "SCOUT_STAGE_FAILED",
                f"Repository scout stage failed; no evidence available: {error}",
                run_id=run_id,
            ) from error

    if strategy == ExecutionStrategy.DIRECT:
        manager = {
            "status": "COMPLETED",
            "manager_id": "direct-strategy",
            "plan": {
                "complexity": "LOW",
                "risk": strategy_risk,
                "parallelism": 1,
                "intent": "EXECUTION",
                "summary": request_text,
                "workers": [direct_worker_for(request_text)],
            },
        }
    else:
        # Select Manager effort and model only when delegation will invoke it.
        _manager_complexity = (
            "MEDIUM"
            if triage_result.scope == MissionScope.BROAD
            else "LOW"
        )
        _manager_effort = select_reasoning_effort(
            role="MANAGER",
            complexity=_manager_complexity,
            risk=None,
        ).effort

        if manager_model is not None:
            # Manual override, already validated above. Never substituted:
            # the exact requested identifier is what gets dispatched.
            _manager_model = manager_model
        else:
            # Manager model policy: BOUNDED → Luna (cost-efficient),
            # BROAD → Sol (stronger multi-worker planning).
            # OmniRoute owns provider availability; Nexus expresses the
            # preference.
            _manager_model = select_manager_model(triage_result.scope)

        print(f"Mgr Mdl : {_manager_model}")
        print(f"Mgr Eff : {_manager_effort}")

        try:
            manager = run_manager(
                run_id=run_id,
                repo=project.path,
                task=request_text,
                model=_manager_model,
                effort=_manager_effort,
                repository_evidence=repository_evidence,
            )
        except OSError as error:
            update_run_status(run_id, "BLOCKED")
            _record_terminal_failure(
                run_id,
                status="BLOCKED",
                reason="MANAGER_BLOCKED",
                failure=build_failure(
                    stage="PLANNING",
                    code="MANAGER_BLOCKED",
                    failure_class="TOOL_FAILURE",
                    message=f"{type(error).__name__}: {error}",
                    agent_role="Manager",
                    model=_manager_model,
                    # Codex CLI is the execution engine, not the model
                    # provider -- the Manager routes through OmniRoute,
                    # same as Worker/Reviewer.
                    provider="omniroute",
                ),
            )
            raise GoError(
                "MANAGER_BLOCKED",
                f"Manager launch failed: {error}",
                run_id=run_id,
            ) from error
        record_call(
            run_id, role="MANAGER", model=_manager_model,
            execution_strategy=strategy.value, result=manager.get("status"),
        )

    if manager["status"] != "COMPLETED":
        update_run_status(run_id, "BLOCKED")

        manager_identity = {
            "agent_id": manager.get("manager_id"),
            "agent_role": "Manager",
            "model": _manager_model if manager.get("manager_id") else None,
            "provider": "omniroute",
        }

        if manager["status"] == "BLOCKED":
            _record_terminal_failure(
                run_id,
                status="BLOCKED",
                reason="PLAN_INVALID",
                failure=build_failure(
                    stage="PLANNING",
                    code="PLAN_INVALID",
                    message=manager.get("error"),
                    **manager_identity,
                ),
            )
            raise GoError(
                "PLAN_INVALID",
                "Manager returned an invalid or unparseable plan: "
                f"{manager.get('error')}",
                run_id=run_id,
            )

        if manager["status"] == "LAUNCH_FAILED":
            _record_terminal_failure(
                run_id,
                status="BLOCKED",
                reason="MANAGER_BLOCKED",
                failure=build_failure(
                    stage="PLANNING",
                    code="MANAGER_BLOCKED",
                    failure_class="TOOL_FAILURE",
                    message=manager.get("error"),
                    **manager_identity,
                ),
            )
            raise GoError(
                "MANAGER_BLOCKED",
                "Manager could not be launched: "
                f"{manager.get('error')}",
                run_id=run_id,
            )

        # The Manager subprocess ran and exited unsuccessfully (or was
        # killed by the watchdog) without ever producing a plan.
        # manager.get("error") -- when present -- is the real, bounded,
        # already-redacted diagnostic nexus.dispatchers.manager.run_manager
        # extracted from the actual Codex transcript (e.g. an auth/token
        # failure, a transport error, "MANAGER_TIMEOUT"). Older/alternate
        # callers that never set it still get exit_code alone, never a
        # KeyError. Either way this is a process/tool-level failure, not a
        # model-capability judgment: the Manager never got far enough to
        # produce content to judge.
        manager_error = manager.get("error")
        detail_message = (
            f"exit_code={manager.get('exit_code')}: {manager_error}"
            if manager_error
            else f"exit_code={manager.get('exit_code')}"
        )
        _record_terminal_failure(
            run_id,
            status="BLOCKED",
            reason="MANAGER_BLOCKED",
            failure=build_failure(
                stage="PLANNING",
                code="MANAGER_BLOCKED",
                failure_class="TOOL_FAILURE",
                message=detail_message,
                **manager_identity,
            ),
        )
        raise GoError(
            "MANAGER_BLOCKED",
            f"Manager planning failed with {detail_message}.",
            run_id=run_id,
        )

    plan = manager["plan"]

    update_run_risk(run_id, plan.get("risk"))

    # Phase 2 Multi-Agent MVP: normalize/validate the Worker dependency
    # graph defensively here too (Phase 1's Manager-side validation in
    # dispatchers/manager.py already does this for freshly generated
    # plans, but a resumed/replayed or hand-edited APPROVED plan reaches
    # this point without going through _validate_plan() again). A bad
    # graph must fail closed as PLAN_INVALID, never execute.
    # validate_worker_dependencies() validates only (returns None); the
    # normalized list itself comes from normalize_worker_dependencies().
    normalized_workers = normalize_worker_dependencies(plan.get("workers", []))

    try:
        validate_worker_dependencies(normalized_workers)
    except WorkerDependencyError as error:
        update_run_status(run_id, "BLOCKED")
        raise GoError(
            "PLAN_INVALID",
            f"Worker dependency graph is invalid: {error}",
            run_id=run_id,
        ) from error

    plan["workers"] = normalized_workers

    # Requirement 11: a plan with more than one Worker is now executed by
    # the round-based concurrent scheduler (_run_workers_by_round), so it
    # is marked "round_based" here for orchestration/resume.py to detect
    # and fail closed on (Requirement 13) rather than silently attempting
    # an unsafe sequential-checkpoint-based resume. A single-Worker plan
    # is unaffected by this phase and stays "sequential".
    execution_model = (
        "round_based" if len(plan["workers"]) > 1 else "sequential"
    )

    try:
        record_approved_plan(run_id, plan)
        record_checkpoint(
            run_id=run_id,
            boundary="PLAN_APPROVED",
            payload={
                "manager_id": manager["manager_id"],
                "risk": plan.get("risk"),
                "complexity": plan.get("complexity"),
                "parallelism": plan.get("parallelism"),
                "worker_count": len(plan.get("workers", [])),
                "execution_model": execution_model,
                "execution_strategy": strategy.value,
                "strategy_reason": strategy_reason,
                # Advisory only -- never changes risk/complexity/routing.
                # Surfaces a possible risk/intent mismatch (e.g. CRITICAL
                # claimed for a read-only plan) so Recovery/Web Board can
                # show it; None when no such pattern is detected.
                "risk_consistency_notice": plan.get(
                    "risk_consistency_notice"
                ),
            },
        )
    except CheckpointWriteError as error:
        update_run_status(run_id, "BLOCKED")
        raise GoError(
            "CHECKPOINT_PERSISTENCE_FAILED",
            f"Could not persist approved orchestration state: {error}",
            run_id=run_id,
        ) from error

    print()
    print("APPROVED PLAN")
    print("=" * 70)
    print(json.dumps(plan, indent=2, ensure_ascii=False))

    update_run_status(run_id, "RUNNING")

    try:
        worker_results = run_integrated_plan(
            run_id=run_id,
            primary_repo=project.path,
            manager_id=manager["manager_id"],
            request_text=request_text,
            workers=plan["workers"],
            plan_risk=plan.get("risk"),
            exclude_models=exclude_models,
            prepare_dependencies=prepare_dependencies,
        )
    except IntegrationError as error:
        update_run_status(run_id, error.run_status)
        record_checkpoint(
            run_id=run_id,
            boundary="RUN_TERMINAL",
            payload={
                "status": error.run_status,
                "reason": error.code,
                # A REVIEW_BLOCKED-origin error already carries the full
                # structured failure (failure_class/category/agent
                # identity/recoverable hint) computed at Review time; reuse
                # it instead of rebuilding a degraded generic one that would
                # null those fields out and mislabel the stage.
                "failure": error.payload.get("failure") or build_failure(
                    stage="EXECUTION" if error.code == "WORKER_EXECUTION_FAILED" else "INTEGRATION",
                    code=error.code,
                    failure_class="TOOL_FAILURE" if error.code == "GIT_TOOL_FAILURE" else None,
                    message=str(error),
                ),
                **{
                    k: v
                    for k, v in error.payload.items()
                    if isinstance(v, (str, int, float, bool, list, type(None)))
                },
            },
        )
        raise GoError(
            error.code, str(error), run_id=run_id, detail=error.payload
        ) from error

    # Phase 3 Multi-Agent MVP: deterministic DAG-aware aggregation.
    # Sink nodes = Workers that no other Worker depends on.
    sinks = sink_nodes(plan["workers"])

    sink_outputs = []
    for si in sinks:
        outcome = worker_results[si] if si < len(worker_results) else None
        worker_info = (outcome or {}).get("worker") or {}
        sink_outputs.append({
            "worker_index": si,
            "worker_ordinal": si + 1,
            "output": worker_info.get("output") or "",
        })

    if len(sinks) == 1:
        aggregation_type = "single_sink"
    elif len(sinks) > 1:
        aggregation_type = "multi_sink"
    else:
        aggregation_type = "empty"

    final_result = {
        "type": aggregation_type,
        "sink_indices": sinks,
        "outputs": sink_outputs,
    }

    # Backward-compatibility shim: pre-Phase-3 consumers expect a
    # "final_worker" key containing the worker sub-dict of the single
    # terminal result, or None when the result is ambiguous.
    #
    # Semantics:
    #   zero workers  -> None  (no work was done)
    #   one sink       -> the "worker" sub-dict of that sink's outcome
    #   multiple sinks -> None (ambiguous; callers must use final_result)
    #
    # Source is always sink_nodes(), never worker_results[-1].
    if len(sinks) == 1:
        _sink_outcome = worker_results[sinks[0]] if sinks[0] < len(worker_results) else None
        final_worker = (_sink_outcome or {}).get("worker") or None
    else:
        final_worker = None  # zero workers or multiple sinks -> ambiguous

    try:
        update_run_result(
            run_id,
            result=plan.get("summary"),
        )

        update_run_status(run_id, "COMPLETED")
    except Exception as error:
        # Record the cause, then re-raise the original exception unchanged, so
        # the run fails exactly as it did before this record existed.
        try:
            _record_terminal_failure(
                run_id,
                status="FAILED",
                reason="FINALIZATION_FAILED",
                failure=build_failure(
                    stage="FINALIZATION",
                    code="FINALIZATION_FAILED",
                    message=f"{type(error).__name__}: {error}",
                ),
            )
        except Exception:  # noqa: BLE001 - the original exception must propagate
            pass
        raise

    record_checkpoint(
        run_id=run_id,
        boundary="RUN_TERMINAL",
        payload={
            "status": "COMPLETED",
            "worker_count": len(worker_results),
            "sink_indices": sinks,
            "aggregation_type": aggregation_type,
        },
        worker_ordinal=len(plan["workers"]) if plan["workers"] else None,
    )
    record_checkpoint(
        run_id=run_id,
        boundary="RUN_TELEMETRY_SUMMARY",
        payload=get_run_summary(run_id),
        worker_ordinal=len(plan["workers"]) if plan["workers"] else None,
    )

    print()
    print("NEXUS GO RESULT")
    print("=" * 70)
    print(f"Run     : {run_id}")
    print("Status  : COMPLETED")
    print(f"Workers : {len(worker_results)}")
    print(f"Sinks   : {sinks}")
    print(f"Summary : {plan.get('summary')}")

    return {
        "run_id": run_id,
        "status": "COMPLETED",
        "project": project,
        "plan": plan,
        "execution_strategy": strategy.value,
        "strategy_reason": strategy_reason,
        "workers": worker_results,
        "final_result": final_result,
        # Backward-compatibility shim (Phase 3 compat fix).
        # None for zero-worker plans and multi-sink DAGs.
        # Single-sink DAGs: the worker sub-dict of that sink.
        "final_worker": final_worker,
    }
