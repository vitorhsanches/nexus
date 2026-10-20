"""Phase 4.4: Approved Change Integration — NEXUS Execution missions.

Integration pipeline:

  PRIMARY REPO (untouched during worker execution)
      │
      └── temporary integration branch + worktree (staging surface)
              │
              ├── DAG ROUND 1
              │     ├── concurrent workers  (repo = integration_wt)
              │     ├── reviews PASS
              │     └── approved changes cherry-picked in plan-index order
              │
              ├── DAG ROUND 2
              │     └── workers start from updated integration HEAD
              │         (parent code changes are visible)
              │
              ...
              │
              └── fully validated integration state
                      │
                      ├── dirty-overlap check
                      ├── git merge --ff-only → PRIMARY
                      └── cleanup integration worktree + branch

Raises IntegrationError for all failures (worker, review, git, conflict).
go.py catches IntegrationError and translates to GoError.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from uuid import uuid4

from nexus.orchestration.context import build_dependency_context
from nexus.program.bootstrap import _identity_args
from nexus.orchestration.dag import eligible_worker_batch
from nexus.orchestration.progressive import execute_progressively
from nexus.orchestration.js_dependency_prep import (
    JsDependencyPrepError,
    copy_node_modules,
    detect_js_project,
    install_dependencies,
    run_baseline_tests,
    verify_test_tooling,
)
from nexus.failure_contract import build_failure, review_failure
from nexus.registry.checkpoints import record_checkpoint
from nexus.worktree_safety import safe_rmtree, validate_destructive_target


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class IntegrationError(RuntimeError):
    """Raised for all integration-pipeline failures.

    code       – short machine-readable reason code
    run_status – "BLOCKED" (needs manual reconciliation) or
                 "FAILED" (internal/contract error)
    payload    – additional diagnostic fields (never secrets)
    """

    def __init__(self, code: str, *, run_status: str, **payload):
        if run_status not in ("BLOCKED", "FAILED"):
            raise ValueError(f"IntegrationError run_status must be BLOCKED or FAILED, got {run_status!r}")
        msg = code
        if payload:
            # Brief summary for the exception message; full detail in payload
            first = next(iter(payload.items()))
            msg = f"{code} ({first[0]}={first[1]!r})"
        super().__init__(msg)
        self.code = code
        self.run_status = run_status
        self.payload = payload


# Codes that originate in the integration layer (vs. worker/review failures).
# INTEGRATION_FAILED checkpoint is recorded only for these.
_INTEGRATION_LAYER_CODES = frozenset({
    "INTEGRATION_CONFLICT",
    "INTEGRATION_DIRTY_OVERLAP",
    "PRIMARY_REPO_CHANGED_UNEXPECTEDLY",
    "READ_ONLY_WORKER_MODIFIED_REPOSITORY",
    "GIT_TOOL_FAILURE",
    "INTEGRATION_INTERNAL_ERROR",
    "CHECKPOINT_PERSISTENCE_FAILED",
})


# ---------------------------------------------------------------------------
# Git helper
# ---------------------------------------------------------------------------


def _git(args: list[str], *, cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git sub-command.  Raises IntegrationError(GIT_TOOL_FAILURE) on failure.

    Uses subprocess.run; never shell=True.  Captures stdout/stderr for
    diagnostics.  Truncates to avoid giant checkpoint payloads.
    """
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        raise IntegrationError(
            "GIT_TOOL_FAILURE",
            run_status="FAILED",
            command=" ".join(["git"] + args[:5]),   # truncate long arg lists
            cwd=cwd,
            returncode=result.returncode,
            stderr=result.stderr.strip()[-800:],
            stdout=result.stdout.strip()[-200:],
        )
    return result


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _parse_porcelain(output: str) -> set[str]:
    """Parse ``git status --porcelain`` output into a set of relative paths."""
    paths: set[str] = set()
    for line in output.splitlines():
        line = line.rstrip()
        if not line:
            continue
        # XY FILENAME  or  XY ORIGINAL -> RENAMED
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.add(path.strip())
    return paths


def _create_integration_container() -> str:
    # tempfile.mkdtemp yields an owner-only directory that does not inherit the
    # %TEMP% ACL, so the sandboxed Worker/Review principal cannot reach a
    # worktree placed inside it. A plain directory inherits the grant.
    container = os.path.realpath(
        os.path.join(tempfile.gettempdir(), f"nexus_intg_root_{uuid4().hex}")
    )
    os.makedirs(container)
    return container


def _add_worker_worktree(
    *,
    integration_wt: str,
    cleanup_root: str,
    run_id: str,
    worker_index: int,
) -> str:
    sub_wt = os.path.join(cleanup_root, f"worker-{worker_index}")
    head = _git(["rev-parse", "HEAD"], cwd=integration_wt).stdout.strip()
    _git(
        [
            "worktree",
            "add",
            "-b",
            f"nexus/worker/{run_id}/{worker_index}",
            sub_wt,
            head,
        ],
        cwd=integration_wt,
    )
    return sub_wt


def _remove_worker_worktree(
    *,
    integration_wt: str,
    sub_wt: str,
    run_id: str,
    worker_index: int,
) -> None:
    _git(["worktree", "remove", "--force", sub_wt], cwd=integration_wt, check=False)
    _git(
        ["branch", "-D", f"nexus/worker/{run_id}/{worker_index}"],
        cwd=integration_wt,
        check=False,
    )


def _cleanup_integration_worktree(
    primary_repo: str,
    cleanup_root: str,
    integration_wt: str | None,
    integration_branch: str | None,
) -> None:
    """Best-effort cleanup of integration worktree and branch.

    Never raises; any error is suppressed so the original exception propagates.
    Idempotent — safe to call multiple times.
    """
    if integration_wt:
        try:
            if os.path.lexists(integration_wt):
                safe_target = validate_destructive_target(
                    cleanup_root,
                    integration_wt,
                    protected_repo=primary_repo,
                )
                _git(
                    ["worktree", "remove", "--force", str(safe_target)],
                    cwd=primary_repo,
                    check=False,
                )
                if os.path.lexists(safe_target):
                    safe_rmtree(
                        cleanup_root,
                        safe_target,
                        protected_repo=primary_repo,
                    )
        except Exception:  # noqa: BLE001
            pass

    try:
        os.rmdir(cleanup_root)
    except OSError:
        pass

    if integration_branch:
        try:
            _git(
                ["branch", "-D", integration_branch],
                cwd=primary_repo,
                check=False,
            )
        except Exception:  # noqa: BLE001
            pass


def _safe_record_checkpoint(run_id: str, boundary: str, payload: dict) -> None:
    """Record a checkpoint, suppressing write errors so they never mask real errors."""
    try:
        record_checkpoint(run_id=run_id, boundary=boundary, payload=payload)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Worker-change helpers
# ---------------------------------------------------------------------------


def _commit_worker_changes(
    *,
    worktree: str,
    run_id: str,
    worker_index: int,
    agent_id: str,
) -> str | None:
    """Stage any changes in the worker worktree and create a Nexus-owned commit.

    Returns the new commit SHA if changes exist, or None if the worktree is
    already clean (no diff to commit — worker legitimately made no changes).

    Nexus owns integration commits.  Worker-created commits are treated as
    already-committed tracked changes; ``git add -A`` stages any remaining
    unstaged modifications on top of them, and we create one clean commit
    attributed to Nexus regardless.
    """
    # Stage everything (tracked modifications + new files + deletions)
    _git(["add", "-A"], cwd=worktree)

    # Check if there is anything to commit
    status_result = _git(["status", "--porcelain"], cwd=worktree)
    if not status_result.stdout.strip():
        return None  # clean — worker made no repository changes

    msg = (
        f"nexus: worker {worker_index} approved changes\n\n"
        f"Run: {run_id}\n"
        f"Agent: {agent_id}\n"
        f"Worker-index: {worker_index}"
    )
    _git([*_identity_args(worktree), "commit", "--no-verify", "-m", msg], cwd=worktree)

    sha_result = _git(["rev-parse", "HEAD"], cwd=worktree)
    return sha_result.stdout.strip()


# ---------------------------------------------------------------------------
# Round application
# ---------------------------------------------------------------------------


def _apply_round(
    *,
    run_id: str,
    integration_wt: str,
    workers: list[dict],
    eligible: list[int],
    round_outcomes: dict[int, dict],
) -> None:
    """Apply approved worker changes to the integration worktree.

    Processes eligible workers in ascending plan-index order (deterministic).

    READ_ONLY workers: verified clean; error if they modified the repository.
    WORKSPACE_WRITE workers (and authority=None for backward-compat):
        - Nexus creates a commit in the worker worktree.
        - Cherry-picks that commit onto integration_wt.
        - On conflict: cherry-pick aborted; IntegrationError raised.
    Workers with no diff: skipped cleanly (no empty commit).
    """
    for wi in sorted(eligible):
        worker = workers[wi]
        outcome = round_outcomes[wi]
        authority = worker.get("authority")

        worker_info = outcome.get("worker") or {}
        worktree = worker_info.get("worktree")
        agent_id = worker_info.get("agent_id") or f"unknown-{wi}"
        reviewer_id = (outcome.get("review") or {}).get("reviewer_id")

        if authority == "READ_ONLY":
            if worktree:
                dirty = _git(["status", "--porcelain"], cwd=worktree, check=False)
                if dirty.returncode == 0 and dirty.stdout.strip():
                    raise IntegrationError(
                        "READ_ONLY_WORKER_MODIFIED_REPOSITORY",
                        run_status="FAILED",
                        worker_index=wi,
                        worker_id=agent_id,
                        worktree=worktree,
                        dirty_files=sorted(_parse_porcelain(dirty.stdout)),
                    )
            # No commit, no cherry-pick — READ_ONLY workers are analysis-only.
            continue

        # WORKSPACE_WRITE (or authority=None — backward-compat: pre-4.3B plans)
        if not worktree:
            # No worktree reported (test mocks that omit it, or launch failures).
            # Record a skipped marker and move on.
            _safe_record_checkpoint(
                run_id,
                "INTEGRATION_WORKER_APPLIED",
                {
                    "worker_index": wi,
                    "worker_id": agent_id,
                    "skipped": True,
                    "reason": "no_worktree_reported",
                },
            )
            continue

        sha = _commit_worker_changes(
            worktree=worktree,
            run_id=run_id,
            worker_index=wi,
            agent_id=agent_id,
        )

        if sha is None:
            # Worker made no repository changes — valid outcome for WORKSPACE_WRITE.
            # A worker may legitimately determine that no source change is required.
            _safe_record_checkpoint(
                run_id,
                "INTEGRATION_WORKER_APPLIED",
                {
                    "worker_index": wi,
                    "worker_id": agent_id,
                    "skipped": True,
                    "reason": "no_diff",
                },
            )
            continue

        record_checkpoint(
            run_id=run_id,
            boundary="INTEGRATION_WORKER_COMMITTED",
            payload={
                "worker_index": wi,
                "worker_id": agent_id,
                "commit_sha": sha,
                "reviewer_id": reviewer_id,
            },
        )

        # Cherry-pick onto integration worktree
        pick = _git(
            [*_identity_args(integration_wt), "cherry-pick", sha],
            cwd=integration_wt,
            check=False,
        )

        if pick.returncode != 0:
            _git(["cherry-pick", "--abort"], cwd=integration_wt, check=False)
            raise IntegrationError(
                "INTEGRATION_CONFLICT",
                run_status="BLOCKED",
                worker_index=wi,
                worker_id=agent_id,
                commit_sha=sha,
                worktree=worktree,
                stderr=pick.stderr.strip()[-500:],
            )

        record_checkpoint(
            run_id=run_id,
            boundary="INTEGRATION_WORKER_APPLIED",
            payload={
                "worker_index": wi,
                "worker_id": agent_id,
                "commit_sha": sha,
            },
        )


# ---------------------------------------------------------------------------
# Finalization
# ---------------------------------------------------------------------------


def _finalize(
    *,
    run_id: str,
    primary_repo: str,
    cleanup_root: str,
    integration_wt: str,
    integration_branch: str,
    original_sha: str,
) -> None:
    """Apply the integration branch to the primary repo via fast-forward merge.

    Steps:
    1. Verify primary HEAD still matches original_sha.
    2. Detect dirty-path overlap; block if any.
    3. Fast-forward primary to integration branch.
    4. Record INTEGRATION_COMPLETED.
    5. Remove integration worktree and branch.

    On any failure: raises IntegrationError; cleanup is done by the caller
    (run_integrated_plan's except block).  Primary repo is never mutated on
    failure.
    """
    record_checkpoint(
        run_id=run_id,
        boundary="INTEGRATION_FINALIZE_START",
        payload={
            "integration_branch": integration_branch,
            "integration_worktree": integration_wt,
            "original_sha": original_sha,
        },
    )

    # 1. Verify primary was not externally advanced during the run
    current_sha = _git(["rev-parse", "HEAD"], cwd=primary_repo).stdout.strip()
    if current_sha != original_sha:
        raise IntegrationError(
            "PRIMARY_REPO_CHANGED_UNEXPECTEDLY",
            run_status="BLOCKED",
            original_sha=original_sha,
            current_sha=current_sha,
        )

    # 2. Dirty-overlap check
    dirty_out = _git(["status", "--porcelain"], cwd=primary_repo)
    dirty_files = _parse_porcelain(dirty_out.stdout)

    intg_diff = _git(
        ["diff", original_sha, "HEAD", "--name-only"],
        cwd=integration_wt,
    )
    intg_files = {p for p in intg_diff.stdout.splitlines() if p.strip()}

    if dirty_files and intg_files:
        overlap = dirty_files & intg_files
        if overlap:
            raise IntegrationError(
                "INTEGRATION_DIRTY_OVERLAP",
                run_status="BLOCKED",
                overlap=sorted(overlap),
                dirty_files=sorted(dirty_files),
                integration_files=sorted(intg_files),
            )

    # 3. Fast-forward primary branch to integration branch
    merge = _git(
        ["merge", "--ff-only", integration_branch],
        cwd=primary_repo,
        check=False,
    )

    if merge.returncode != 0:
        raise IntegrationError(
            "GIT_TOOL_FAILURE",
            run_status="BLOCKED",
            command=f"git merge --ff-only {integration_branch}",
            cwd=primary_repo,
            returncode=merge.returncode,
            stderr=merge.stderr.strip()[-500:],
        )

    # 4. Record success
    final_sha = _git(["rev-parse", "HEAD"], cwd=primary_repo).stdout.strip()

    record_checkpoint(
        run_id=run_id,
        boundary="INTEGRATION_COMPLETED",
        payload={
            "integration_branch": integration_branch,
            "original_sha": original_sha,
            "final_sha": final_sha,
            "files_integrated": sorted(intg_files),
        },
    )

    # 5. Cleanup integration worktree and branch (success path)
    _cleanup_integration_worktree(
        primary_repo, cleanup_root, integration_wt, integration_branch
    )


# ---------------------------------------------------------------------------
# Worker-failure translation
# ---------------------------------------------------------------------------


def _worker_outcome_to_integration_error(
    outcome: dict,
    worker_ordinal: int,
) -> IntegrationError:
    """Translate a non-COMPLETED execute_progressively outcome to IntegrationError.

    Codes mirror what _raise_worker_failure() in go.py used, so go.py can
    raise the equivalent GoError without losing semantic meaning.
    """
    reason = outcome.get("reason")

    if reason == "WORKER_EXECUTION_FAILED":
        return IntegrationError(
            "WORKER_EXECUTION_FAILED",
            run_status="FAILED",
            worker_ordinal=worker_ordinal,
            # Phase 4.1: carry the worker sub-dict (its own `reason`, e.g.
            # WORKER_TIMEOUT/TRANSIENT_PROVIDER_ERROR) so Program failure
            # classification can read it instead of falling back to
            # UNKNOWN_FAILURE.
            worker=outcome.get("worker"),
        )
    if reason == "ESCALATION_UNAVAILABLE":
        return IntegrationError(
            "ESCALATION_UNAVAILABLE",
            run_status="BLOCKED",
            worker_ordinal=worker_ordinal,
            error=outcome.get("error"),
            review=outcome.get("review"),
        )
    if reason == "INITIAL_ROUTING_UNAVAILABLE":
        return IntegrationError(
            "INITIAL_ROUTING_UNAVAILABLE",
            run_status="BLOCKED",
            worker_ordinal=worker_ordinal,
            error=outcome.get("error"),
            error_type=outcome.get("error_type"),
            route_class=outcome.get("route_class"),
            capability=outcome.get("capability"),
            risk=outcome.get("risk"),
        )
    # Default: REVIEW_BLOCKED (covers REVIEW_FAILED, unknown verdicts, etc.)
    review = outcome.get("review") or {}
    history = outcome.get("history") or []
    last = history[-1] if history else {}
    if reason == "REVIEW_FAILED":
        # The Review dispatcher itself failed before producing a verdict, so
        # no history entry exists for this attempt; the dispatcher's own
        # result dict carries the reviewer identity instead.
        routing = review.get("routing") or {}
        failure = review_failure(
            "REVIEW_FAILED",
            review,
            reviewer_id=review.get("reviewer_id"),
            reviewer_model=routing.get("model"),
            reviewer_provider=routing.get("provider"),
            worker_ordinal=worker_ordinal,
            worktree=last.get("worktree"),
        )
    else:
        failure = review_failure(
            "REVIEW_BLOCKED",
            review,
            reviewer_id=last.get("reviewer_id"),
            reviewer_model=last.get("reviewer_model"),
            reviewer_provider=last.get("reviewer_provider"),
            worker_ordinal=worker_ordinal,
            worktree=last.get("worktree"),
        )
    return IntegrationError(
        "REVIEW_BLOCKED",
        run_status="BLOCKED",
        worker_ordinal=worker_ordinal,
        reason=reason or outcome.get("verdict"),
        error=outcome.get("error"),
        # Phase 4.1: carry the Reviewer's own verdict/failure_class/summary
        # so Program failure classification and corrective-mission context
        # can use the real evidence instead of the bare code "REVIEW_BLOCKED".
        review=review,
        # Phase 4.1 hardening: the exact route this Worker used, so a
        # CAPABILITY_FAILURE-classified retry can exclude it from the
        # SAME capability's next selection via the existing routing
        # blocked_models gate, not only natural-language prompt guidance.
        worker_route=outcome.get("worker_route"),
        # Failure-observability fix: the same structured failure
        # (failure_class/category/agent identity/recoverable hint) already
        # computed for the REVIEW checkpoint, so go.py's RUN_TERMINAL
        # checkpoint does not have to -- and previously could not --
        # reconstruct it from scratch.
        failure=failure,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_integrated_plan(
    run_id: str,
    primary_repo: str,
    manager_id: str,
    request_text: str,
    workers: list[dict],
    plan_risk,
    exclude_models: frozenset[str] = frozenset(),
    prepare_dependencies: bool = False,
) -> list[dict]:
    """Execute workers and integrate approved changes into the primary repo.

    Replaces _run_workers_by_round() in go.py, adding Phase 4.4 integration.

    Phase 1-3 scheduler semantics are fully preserved:
    - eligible_worker_batch() DAG scheduling (unchanged)
    - ThreadPoolExecutor concurrency within each round (unchanged)
    - as_completed() round draining before failure decisions (unchanged)
    - deterministic failure selection by ascending plan index (unchanged)
    - semantic dependency context via build_dependency_context() (unchanged)
    - stable, pre-sized worker_results list indexed by plan position (unchanged)

    Phase 4.4 additions:
    - Integration worktree created from primary HEAD (primary untouched)
    - Each round's workers execute with repo=integration_wt so their
      Codex sub-worktrees start from the current integration state
      (dependent workers see upstream code changes, not just text output)
    - Approved WORKSPACE_WRITE changes cherry-picked onto integration_wt
      between rounds in deterministic plan-index order
    - Final dirty-overlap check + ff-only merge to primary after all rounds

    Raises IntegrationError for all failures.  Codes are:
    - Integration layer: INTEGRATION_CONFLICT, INTEGRATION_DIRTY_OVERLAP,
      PRIMARY_REPO_CHANGED_UNEXPECTEDLY, READ_ONLY_WORKER_MODIFIED_REPOSITORY,
      GIT_TOOL_FAILURE, INTEGRATION_INTERNAL_ERROR
    - Worker/review layer (same codes as original GoError): WORKER_EXECUTION_FAILED,
      ESCALATION_UNAVAILABLE, INITIAL_ROUTING_UNAVAILABLE, REVIEW_BLOCKED

    go.py catches IntegrationError and translates to GoError.
    Primary repo is never mutated on any failure path.
    """
    worker_count = len(workers)

    if worker_count == 0:
        return []

    # ── 1. Capture primary repo state ─────────────────────────────────────
    original_sha = _git(["rev-parse", "HEAD"], cwd=primary_repo).stdout.strip()
    original_branch = _git(
        ["rev-parse", "--abbrev-ref", "HEAD"], cwd=primary_repo
    ).stdout.strip()
    initial_dirty = _parse_porcelain(
        _git(["status", "--porcelain"], cwd=primary_repo).stdout
    )

    # ── 2. Create integration branch + worktree ────────────────────────────
    integration_branch = f"nexus/integration/{run_id}"

    # A private container makes the worktree a provable strict descendant.
    cleanup_root = _create_integration_container()
    integration_wt = os.path.join(cleanup_root, "worktree")

    try:
        _git(
            ["worktree", "add", "-b", integration_branch, integration_wt],
            cwd=primary_repo,
        )
    except IntegrationError:
        _cleanup_integration_worktree(
            primary_repo, cleanup_root, integration_wt, integration_branch
        )
        raise

    record_checkpoint(
        run_id=run_id,
        boundary="INTEGRATION_STAGE_CREATED",
        payload={
            "integration_branch": integration_branch,
            "integration_worktree": integration_wt,
            "original_sha": original_sha,
            "original_branch": original_branch,
            "initial_dirty_files": sorted(initial_dirty),
        },
    )

    # ── 2b. Explicit, opt-in JavaScript dependency preparation ─────────────
    # Disabled unless the caller explicitly asked for it (default GO
    # behavior is completely unchanged). Runs on the host, before any
    # Worker/Reviewer Codex subprocess for this Run -- never inside their
    # sandbox, never against the primary repository. A failure here means
    # the expensive Worker never launches at all.
    js_project: dict | None = None
    # Whether each Worker's own sub-worktree should get a post-change test
    # run (nexus.orchestration.progressive.execute_progressively), to give
    # the Reviewer independent TEST_STATUS evidence instead of relying on
    # the Reviewer's own read-only sandbox to execute project tests. Only
    # meaningful when a declared test command actually resolved above.
    run_js_tests = False
    if prepare_dependencies:
        try:
            js_project = detect_js_project(integration_wt)
            if js_project is None:
                raise JsDependencyPrepError(
                    "JS_PROJECT_NOT_DETECTED",
                    "prepare_dependencies was requested but no npm-managed "
                    "project (package.json + package-lock.json) was found "
                    f"at {integration_wt}.",
                )
            install_result = install_dependencies(integration_wt)
            tool_check = verify_test_tooling(integration_wt, js_project["manifest"])
            run_js_tests = bool(tool_check["binary"])
            baseline = (
                run_baseline_tests(integration_wt) if tool_check["binary"] else None
            )
            record_checkpoint(
                run_id=run_id,
                boundary="JS_DEPENDENCY_READY",
                payload={
                    "install_output_tail": install_result["output_tail"][-500:],
                    "test_script": tool_check["test_script"],
                    "test_binary": tool_check["binary"],
                    "baseline_tests": baseline,
                },
            )
        except JsDependencyPrepError as error:
            record_checkpoint(
                run_id=run_id,
                boundary="JS_DEPENDENCY_PREP_FAILED",
                payload={
                    "code": error.code,
                    "message": str(error),
                    **{
                        k: v
                        for k, v in error.detail.items()
                        if isinstance(v, (str, int, float, bool, list, type(None)))
                    },
                },
            )
            _cleanup_integration_worktree(
                primary_repo, cleanup_root, integration_wt, integration_branch
            )
            raise IntegrationError(
                error.code,
                run_status="BLOCKED",
                error=str(error),
                failure=build_failure(
                    stage="DEPENDENCY_PREPARATION",
                    code=error.code,
                    failure_class="TOOL_FAILURE",
                    message=str(error),
                ),
                **{
                    k: v
                    for k, v in error.detail.items()
                    if isinstance(v, (str, int, float, bool, list, type(None)))
                },
            ) from error

    # ── 3. Execute DAG rounds ──────────────────────────────────────────────
    worker_results: list = [None] * worker_count
    completed_indices: set[int] = set()
    round_number = 0

    try:
        while len(completed_indices) < worker_count:
            eligible = eligible_worker_batch(workers, completed_indices)

            if not eligible:
                raise IntegrationError(
                    "INTEGRATION_INTERNAL_ERROR",
                    run_status="FAILED",
                    reason="SCHEDULER_DEADLOCK",
                    completed=len(completed_indices),
                    total=worker_count,
                )

            record_checkpoint(
                run_id=run_id,
                boundary="INTEGRATION_ROUND_START",
                payload={
                    "round": round_number,
                    "eligible_indices": eligible,
                    "completed_count": len(completed_indices),
                },
            )

            # Build dependency contexts for eligible workers from their
            # completed parents (Phase 3 semantic output propagation, unchanged)
            worker_contexts: dict[int, str | None] = {}
            for worker_index in eligible:
                dep_ctx, _entries = build_dependency_context(
                    depends_on=workers[worker_index].get("depends_on") or [],
                    worker_results=worker_results,
                )
                worker_contexts[worker_index] = dep_ctx or None

            sub_worktrees: dict[int, str] = {}
            try:
                for worker_index in eligible:
                    sub_worktrees[worker_index] = _add_worker_worktree(
                        integration_wt=integration_wt,
                        cleanup_root=cleanup_root,
                        run_id=run_id,
                        worker_index=worker_index,
                    )
                    if (
                        prepare_dependencies
                        and js_project is not None
                        and os.path.isdir(os.path.join(integration_wt, "node_modules"))
                    ):
                        # `git worktree add` never materializes node_modules
                        # (it is git-ignored); propagate the already-prepared
                        # copy so this Worker -- and the Reviewer inspecting
                        # the same sub-worktree -- can actually run the
                        # project's tests, not just its own self-report.
                        # (A genuinely zero-dependency project never gets a
                        # node_modules from `npm ci` at all -- nothing to
                        # propagate, which is not a failure.)
                        try:
                            copy_node_modules(integration_wt, sub_worktrees[worker_index])
                        except JsDependencyPrepError as error:
                            raise IntegrationError(
                                error.code,
                                run_status="BLOCKED",
                                error=str(error),
                                worker_ordinal=worker_index + 1,
                                failure=build_failure(
                                    stage="DEPENDENCY_PREPARATION",
                                    code=error.code,
                                    failure_class="TOOL_FAILURE",
                                    message=str(error),
                                    worker_ordinal=worker_index + 1,
                                ),
                            ) from error
                # Each worker runs in its own sub-worktree created from the
                # current integration HEAD; approved commits are cherry-picked
                # back onto the integration worktree in plan order.
                with ThreadPoolExecutor(max_workers=len(eligible)) as executor:
                    futures = {
                        executor.submit(
                            execute_progressively,
                            run_id=run_id,
                            repo=sub_worktrees[worker_index],
                            manager_id=manager_id,
                            original_task=request_text,
                            planned_worker=workers[worker_index],
                            plan_risk=plan_risk,
                            worker_ordinal=worker_index + 1,
                            dependency_context=worker_contexts.get(worker_index),
                            exclude_models=exclude_models,
                            run_js_tests=run_js_tests,
                        ): worker_index
                        for worker_index in eligible
                    }

                    round_outcomes: dict[int, dict] = {}
                    round_exceptions: dict[int, Exception] = {}

                    # Drain the WHOLE round before deciding — Phase 2 guarantee:
                    # no in-flight worker is cancelled mid-round.
                    for future in as_completed(futures):
                        wi = futures[future]
                        try:
                            round_outcomes[wi] = future.result()
                        except Exception as exc:  # noqa: BLE001
                            round_exceptions[wi] = exc

                # Store outcomes (including successful ones from a partially-failed
                # round) so dependency context is available regardless of failure.
                for wi in eligible:
                    if wi in round_outcomes:
                        outcome = round_outcomes[wi]
                        outcome["_planned_scope"] = workers[wi].get("scope") or ""
                        worker_results[wi] = outcome

                # Check for failures
                any_failure = bool(round_exceptions) or any(
                    round_outcomes[wi]["status"] != "COMPLETED"
                    for wi in eligible
                    if wi in round_outcomes
                )

                if any_failure:
                    # Deterministic: report first failure by ascending plan index
                    for wi in eligible:
                        if wi in round_exceptions:
                            exc = round_exceptions[wi]
                            raise IntegrationError(
                                "WORKER_EXECUTION_FAILED",
                                run_status="FAILED",
                                worker_ordinal=wi + 1,
                                error=str(exc),
                            ) from exc
                        outcome = round_outcomes.get(wi)
                        if outcome is not None and outcome["status"] != "COMPLETED":
                            raise _worker_outcome_to_integration_error(outcome, wi + 1)
                    # Unreachable — any_failure=True must have a failed worker
                    raise IntegrationError(
                        "INTEGRATION_INTERNAL_ERROR",
                        run_status="FAILED",
                        reason="any_failure_but_no_failed_worker",
                    )

                # All workers in this round passed — apply their approved changes
                _apply_round(
                    run_id=run_id,
                    integration_wt=integration_wt,
                    workers=workers,
                    eligible=eligible,
                    round_outcomes=round_outcomes,
                )
            finally:
                for worker_index, sub_wt in sub_worktrees.items():
                    _remove_worker_worktree(
                        integration_wt=integration_wt,
                        sub_wt=sub_wt,
                        run_id=run_id,
                        worker_index=worker_index,
                    )

            completed_indices.update(eligible)

            record_checkpoint(
                run_id=run_id,
                boundary="INTEGRATION_ROUND_COMPLETED",
                payload={
                    "round": round_number,
                    "completed_count": len(completed_indices),
                },
            )

            round_number += 1

        # ── 4. Finalize — ff-only merge to primary ─────────────────────────
        _finalize(
            run_id=run_id,
            primary_repo=primary_repo,
            cleanup_root=cleanup_root,
            integration_wt=integration_wt,
            integration_branch=integration_branch,
            original_sha=original_sha,
        )

    except IntegrationError as error:
        # Record INTEGRATION_FAILED for integration-layer errors only
        if error.code in _INTEGRATION_LAYER_CODES:
            _safe_record_checkpoint(
                run_id,
                "INTEGRATION_FAILED",
                {
                    "error_code": error.code,
                    "run_status": error.run_status,
                    **{
                        k: v
                        for k, v in error.payload.items()
                        if isinstance(v, (str, int, float, bool, list, type(None)))
                    },
                },
            )

        # Best-effort cleanup; _finalize already cleaned up on its success path;
        # for all other failure paths, cleanup hasn't happened yet.
        _cleanup_integration_worktree(
            primary_repo, cleanup_root, integration_wt, integration_branch
        )
        raise

    return worker_results
