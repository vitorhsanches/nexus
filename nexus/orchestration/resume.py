"""Safe Run resume for Nexus v2.0-F.2.

This module resumes an existing, previously interrupted Run from its
durable checkpoint history WITHOUT ever re-invoking the Manager. The
original run_id and its immutable Manager-approved plan remain
authoritative for the entire resume.

Resume never re-executes a Worker whose WORKER_ATTEMPT checkpoint already
recorded COMPLETED for the current attempt -- that Worker's existing
Agent/worktree is reused for review-only continuation instead. A Worker is
only ever re-executed (a NEW Agent/worktree) when progressing through the
normal RETRY/ESCALATE ladder, exactly as an uninterrupted Run would.

Resume is fail-closed: any inconsistency between the approved plan and the
durable checkpoint history blocks the resume with an explicit, reportable
error rather than guessing or silently re-running work. The Manager is
never re-invoked to recreate missing or contradictory state.
"""

import os
from pathlib import Path

from nexus.orchestration.progressive import execute_progressively
from nexus.registry.agents import get_agent
from nexus.registry.checkpoints import (
    CheckpointWriteError,
    get_approved_plan,
    list_checkpoints,
    record_checkpoint,
)
from nexus.registry.projects import get_project
from nexus.orchestration.ownership import try_acquire_for_resume
from nexus.registry.runs import (
    get_run,
    update_run_result,
    update_run_status,
)


TERMINAL_RUN_STATUSES = {"COMPLETED", "BLOCKED", "FAILED", "CANCELLED"}

# Worker Agent statuses that are compatible with a completed execution
# attempt being reused for review-only resume continuation.
COMPLETED_AGENT_STATUSES = {"COMPLETED"}


class ResumeError(RuntimeError):
    """Raised for known, reportable Nexus resume failures."""

    def __init__(self, code: str, message: str, run_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.run_id = run_id


def _same_path(a: str, b: str) -> bool:
    """Compare two filesystem paths safely and case-insensitively."""

    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(
        os.path.normpath(str(b))
    )


# Boundaries that describe a Worker's own progressive-execution
# lifecycle. RUN_TERMINAL and other Run-level boundaries may also carry
# a worker_ordinal (for traceability) but must never be treated as
# durable per-Worker execution state during resume.
WORKER_LIFECYCLE_BOUNDARIES = frozenset(
    {"EXECUTION_START", "WORKER_ATTEMPT", "REVIEW", "LIFECYCLE"}
)


def _worker_ordinal_checkpoints(checkpoints: list[dict]) -> dict[int, list[dict]]:
    by_ordinal: dict[int, list[dict]] = {}

    for checkpoint in checkpoints:
        if checkpoint["boundary"] not in WORKER_LIFECYCLE_BOUNDARIES:
            continue

        ordinal = checkpoint.get("worker_ordinal")

        if ordinal is None:
            continue

        by_ordinal.setdefault(ordinal, []).append(checkpoint)

    return by_ordinal


def _checkpoint_route_from_payload(payload: dict) -> dict:
    return {
        "route_class": payload.get("route_class"),
        "model": payload.get("model"),
        "provider": payload.get("provider"),
        "effort": payload.get("effort"),
        "execution_path": payload.get("execution_path"),
    }


def _reconstruct_review_result(review_payload: dict) -> dict:
    """Reconstruct a review_worker()-shaped result from a durable checkpoint."""

    return {
        "status": "COMPLETED",
        "reviewer_id": review_payload.get("reviewer_id"),
        "review": {
            "verdict": review_payload.get("verdict"),
            "failure_class": review_payload.get("failure_class"),
            "summary": review_payload.get("summary"),
        },
        "routing": {
            "model": review_payload.get("reviewer_model"),
            "provider": review_payload.get("reviewer_provider"),
            "effort": review_payload.get("reviewer_effort"),
            "execution_path": review_payload.get("reviewer_execution_path"),
            "reason": review_payload.get("reviewer_routing_reason"),
            "degraded": review_payload.get("reviewer_degraded"),
        },
    }


def _failure_context_from_review_payload(review_payload: dict) -> str:
    return (
        "Previous Worker review:\n\n"
        f"Verdict:\n{review_payload.get('verdict')}\n\n"
        f"Failure class:\n{review_payload.get('failure_class')}\n\n"
        f"Review summary:\n{review_payload.get('summary')}\n\n"
        "Use this diagnostic context to avoid repeating the previous "
        "failure."
    )


def _verify_worker_agent_for_resume(
    run_id: str,
    worker_id: str,
    worktree: str,
    attempt_payload: dict,
) -> None:
    """Prove the existing Worker Agent is safe to reuse for review-only resume.

    Never fabricates the Agent. Any missing or contradictory Agent state
    fails closed with `RESUME_STATE_INCONSISTENT`. A missing physical
    worktree directory fails closed with `RESUME_WORKTREE_MISSING` and
    review_worker() is never called afterward.
    """

    agent = get_agent(worker_id)

    if agent is None:
        raise ResumeError(
            "RESUME_STATE_INCONSISTENT",
            f"No registered Agent exists for worker_id {worker_id!r}; "
            "the existing Worker Agent cannot be fabricated for "
            "review-only resume.",
            run_id=run_id,
        )

    if agent["run_id"] != run_id:
        raise ResumeError(
            "RESUME_STATE_INCONSISTENT",
            f"Agent {worker_id!r} belongs to run {agent['run_id']!r}, "
            f"not the resuming run {run_id!r}.",
            run_id=run_id,
        )

    if agent["role"] != "Worker":
        raise ResumeError(
            "RESUME_STATE_INCONSISTENT",
            f"Agent {worker_id!r} has role {agent['role']!r}; the Nexus "
            "convention requires role='Worker' for review-only reuse.",
            run_id=run_id,
        )

    if agent["status"] not in COMPLETED_AGENT_STATUSES:
        raise ResumeError(
            "RESUME_STATE_INCONSISTENT",
            f"Agent {worker_id!r} has status {agent['status']!r}, which "
            "is not compatible with a completed execution attempt.",
            run_id=run_id,
        )

    agent_worktree = agent["worktree"]

    if not agent_worktree or not _same_path(agent_worktree, worktree):
        raise ResumeError(
            "RESUME_STATE_INCONSISTENT",
            f"Agent {worker_id!r} registered worktree {agent_worktree!r} "
            f"does not match the checkpointed worktree {worktree!r}.",
            run_id=run_id,
        )

    checkpoint_model = attempt_payload.get("model")
    checkpoint_provider = attempt_payload.get("provider")
    checkpoint_effort = attempt_payload.get("effort")

    if (
        checkpoint_model
        and agent["model"]
        and agent["model"] != checkpoint_model
    ):
        raise ResumeError(
            "RESUME_STATE_INCONSISTENT",
            f"Agent {worker_id!r} model {agent['model']!r} contradicts "
            f"checkpointed route model {checkpoint_model!r}.",
            run_id=run_id,
        )

    if (
        checkpoint_provider
        and agent["provider"]
        and agent["provider"] != checkpoint_provider
    ):
        raise ResumeError(
            "RESUME_STATE_INCONSISTENT",
            f"Agent {worker_id!r} provider {agent['provider']!r} "
            f"contradicts checkpointed route provider "
            f"{checkpoint_provider!r}.",
            run_id=run_id,
        )

    if (
        checkpoint_effort
        and agent["effort"]
        and agent["effort"] != checkpoint_effort
    ):
        raise ResumeError(
            "RESUME_STATE_INCONSISTENT",
            f"Agent {worker_id!r} effort {agent['effort']!r} contradicts "
            f"checkpointed route effort {checkpoint_effort!r}.",
            run_id=run_id,
        )

    if not Path(worktree).is_dir():
        raise ResumeError(
            "RESUME_WORKTREE_MISSING",
            f"Worker worktree {worktree!r} no longer exists on disk; "
            "resume cannot safely reuse it for review-only continuation.",
            run_id=run_id,
        )


def _resume_state_for_ordinal(
    run_id: str,
    ordinal_checkpoints: list[dict],
) -> dict:
    """Reconstruct in-memory progressive-execution state from checkpoints.

    Fails closed with `ResumeError` (never a bare exception, and never
    by re-invoking the Manager) when the checkpoint history for this
    ordinal is missing required data or is internally inconsistent.
    """

    execution_start = None
    worker_attempts_by_attempt: dict[int, list[dict]] = {}
    reviews_by_attempt: dict[int, list[dict]] = {}
    lifecycle_by_attempt: dict[int, list[dict]] = {}

    for checkpoint in ordinal_checkpoints:
        boundary = checkpoint["boundary"]
        payload = checkpoint["payload"]

        if boundary == "EXECUTION_START":
            if execution_start is not None:
                raise ResumeError(
                    "RESUME_STATE_INCONSISTENT",
                    "Multiple EXECUTION_START checkpoints exist for the "
                    "same Worker ordinal.",
                    run_id=run_id,
                )
            execution_start = checkpoint
            continue

        if boundary not in ("WORKER_ATTEMPT", "REVIEW", "LIFECYCLE"):
            continue

        attempt = payload.get("attempt")

        if not isinstance(attempt, int) or attempt <= 0:
            raise ResumeError(
                "RESUME_STATE_INCONSISTENT",
                f"{boundary} checkpoint has an invalid attempt number "
                f"{attempt!r}.",
                run_id=run_id,
            )

        if boundary == "WORKER_ATTEMPT":
            worker_attempts_by_attempt.setdefault(attempt, []).append(checkpoint)
        elif boundary == "REVIEW":
            reviews_by_attempt.setdefault(attempt, []).append(checkpoint)
        else:
            lifecycle_by_attempt.setdefault(attempt, []).append(checkpoint)

    if execution_start is None:
        raise ResumeError(
            "RESUME_CHECKPOINT_MISSING",
            "No EXECUTION_START checkpoint exists for this Worker ordinal; "
            "resume cannot safely reconstruct the execution route.",
            run_id=run_id,
        )

    if not worker_attempts_by_attempt:
        raise ResumeError(
            "RESUME_STATE_INCONSISTENT",
            "EXECUTION_START exists but no WORKER_ATTEMPT checkpoint was "
            "recorded for this Worker ordinal.",
            run_id=run_id,
        )

    for attempt, entries in worker_attempts_by_attempt.items():
        if len(entries) > 1:
            raise ResumeError(
                "RESUME_STATE_INCONSISTENT",
                f"Multiple conflicting WORKER_ATTEMPT checkpoints exist "
                f"for attempt {attempt}.",
                run_id=run_id,
            )

    for attempt, entries in reviews_by_attempt.items():
        if len(entries) > 1:
            raise ResumeError(
                "RESUME_STATE_INCONSISTENT",
                f"Multiple conflicting REVIEW checkpoints exist for "
                f"attempt {attempt}.",
                run_id=run_id,
            )

    for attempt, entries in lifecycle_by_attempt.items():
        if len(entries) > 1:
            raise ResumeError(
                "RESUME_STATE_INCONSISTENT",
                f"Multiple conflicting LIFECYCLE checkpoints exist for "
                f"attempt {attempt}.",
                run_id=run_id,
            )

    max_attempt = max(worker_attempts_by_attempt)

    for candidate in range(1, max_attempt + 1):
        if candidate not in worker_attempts_by_attempt:
            raise ResumeError(
                "RESUME_STATE_INCONSISTENT",
                f"WORKER_ATTEMPT checkpoint for attempt {candidate} is "
                f"missing even though attempt {max_attempt} exists; "
                "attempt ordering is impossible.",
                run_id=run_id,
            )

    latest_attempt_checkpoint = worker_attempts_by_attempt[max_attempt][0]
    attempt_payload = latest_attempt_checkpoint["payload"]

    current_worker_route = _checkpoint_route_from_payload(attempt_payload)

    if any(
        current_worker_route[key] is None for key in ("route_class", "model")
    ):
        raise ResumeError(
            "RESUME_STATE_INCONSISTENT",
            "WORKER_ATTEMPT checkpoint is missing required route fields; "
            "resume cannot safely reconstruct the current route.",
            run_id=run_id,
        )

    if attempt_payload.get("status") != "COMPLETED":
        raise ResumeError(
            "RESUME_WORKTREE_MISSING",
            "The latest WORKER_ATTEMPT for this Worker ordinal did not "
            "complete successfully; resume cannot safely continue without "
            "re-running the Manager plan from GO.",
            run_id=run_id,
        )

    worktree = attempt_payload.get("worktree")
    worker_id = attempt_payload.get("worker_id")

    if not worktree or not worker_id:
        raise ResumeError(
            "RESUME_WORKTREE_MISSING",
            "The completed Worker attempt checkpoint is missing its "
            "worktree or worker_id; the existing Worker Agent cannot be "
            "safely reused for review-only resume.",
            run_id=run_id,
        )

    # Reconstruct same_tier_retries deterministically from the full
    # durable LIFECYCLE history across every attempt, exactly mirroring
    # the in-memory bookkeeping an uninterrupted Run would have.
    same_tier_retries = 0

    for attempt in sorted(lifecycle_by_attempt):
        lifecycle_payload = lifecycle_by_attempt[attempt][0]["payload"]
        verdict = lifecycle_payload.get("verdict")

        if verdict == "RETRY":
            same_tier_retries += 1
        elif verdict == "ESCALATE":
            same_tier_retries = 0

    # previous_failure prior to the latest attempt (used only when the
    # latest attempt still needs to execute or be reviewed fresh).
    prior_failure = None

    for attempt in sorted(reviews_by_attempt):
        if attempt >= max_attempt:
            continue

        review_payload = reviews_by_attempt[attempt][0]["payload"]

        if review_payload.get("verdict") in {"RETRY", "ESCALATE"}:
            prior_failure = _failure_context_from_review_payload(review_payload)

    review_entries = reviews_by_attempt.get(max_attempt, [])
    lifecycle_entries = lifecycle_by_attempt.get(max_attempt, [])

    if not review_entries:
        if lifecycle_entries:
            raise ResumeError(
                "RESUME_STATE_INCONSISTENT",
                f"A LIFECYCLE checkpoint exists for attempt {max_attempt} "
                "without a corresponding REVIEW checkpoint.",
                run_id=run_id,
            )

        # Branch C: WORKER_ATTEMPT COMPLETED but review never ran --
        # review-only resume. Prove the existing Worker Agent before
        # ever reusing it; never call review_worker if that proof fails.
        _verify_worker_agent_for_resume(
            run_id=run_id,
            worker_id=worker_id,
            worktree=worktree,
            attempt_payload=attempt_payload,
        )

        return {
            "current_worker": current_worker_route,
            "history": [],
            "attempt": max_attempt,
            "same_tier_retries": same_tier_retries,
            "previous_failure": prior_failure,
            "pending_worker_result": {
                "agent_id": worker_id,
                "worktree": worktree,
                "status": "COMPLETED",
            },
        }

    review_checkpoint = review_entries[0]
    review_payload = review_checkpoint["payload"]

    review_worker_id = review_payload.get("worker_id")

    if review_worker_id is not None and review_worker_id != worker_id:
        raise ResumeError(
            "RESUME_STATE_INCONSISTENT",
            f"REVIEW checkpoint worker_id {review_worker_id!r} does not "
            f"match WORKER_ATTEMPT worker_id {worker_id!r} for attempt "
            f"{max_attempt}.",
            run_id=run_id,
        )

    if review_payload.get("status") != "COMPLETED":
        raise ResumeError(
            "RESUME_STATE_INCONSISTENT",
            f"REVIEW checkpoint for attempt {max_attempt} did not "
            "complete successfully; resume cannot safely continue.",
            run_id=run_id,
        )

    verdict = review_payload.get("verdict")

    if not lifecycle_entries:
        # Branch D: crash after REVIEW COMPLETED but before LIFECYCLE.
        # Never call review_worker again -- reconstruct the persisted
        # review result and append only the missing LIFECYCLE
        # transition.
        return {
            "current_worker": current_worker_route,
            "history": [],
            "attempt": max_attempt,
            "same_tier_retries": same_tier_retries,
            "previous_failure": prior_failure,
            "pending_review_result": {
                "worker_result": {
                    "agent_id": worker_id,
                    "worktree": worktree,
                    "status": "COMPLETED",
                },
                "review_result": _reconstruct_review_result(review_payload),
            },
        }

    lifecycle_checkpoint = lifecycle_entries[0]
    lifecycle_payload = lifecycle_checkpoint["payload"]
    lifecycle_verdict = lifecycle_payload.get("verdict")
    lifecycle_worker_id = lifecycle_payload.get("worker_id")

    if lifecycle_worker_id is not None and lifecycle_worker_id != worker_id:
        raise ResumeError(
            "RESUME_STATE_INCONSISTENT",
            f"LIFECYCLE checkpoint worker_id {lifecycle_worker_id!r} does "
            f"not match WORKER_ATTEMPT worker_id {worker_id!r} for "
            f"attempt {max_attempt}.",
            run_id=run_id,
        )

    if lifecycle_verdict == "PASS":
        # The caller is expected to have already skipped this ordinal via
        # _completed_ordinals(); reaching here means the durable history
        # is contradictory.
        raise ResumeError(
            "RESUME_STATE_INCONSISTENT",
            f"A PASS LIFECYCLE checkpoint exists for attempt "
            f"{max_attempt} but this Worker ordinal was not recognized "
            "as completed.",
            run_id=run_id,
        )

    if lifecycle_verdict == "BLOCKED":
        raise ResumeError(
            "RESUME_STATE_INCONSISTENT",
            f"A BLOCKED LIFECYCLE checkpoint exists for attempt "
            f"{max_attempt}; the Run should already be terminal and "
            "cannot be resumed.",
            run_id=run_id,
        )

    if lifecycle_verdict == "RETRY":
        if verdict != "RETRY":
            raise ResumeError(
                "RESUME_STATE_INCONSISTENT",
                f"RETRY LIFECYCLE checkpoint exists for attempt "
                f"{max_attempt} but the durable REVIEW verdict was "
                f"{verdict!r}, not 'RETRY'.",
                run_id=run_id,
            )

        next_attempt = lifecycle_payload.get("next_attempt")

        if next_attempt != max_attempt + 1:
            raise ResumeError(
                "RESUME_STATE_INCONSISTENT",
                f"RETRY LIFECYCLE checkpoint for attempt {max_attempt} "
                f"has an incompatible next_attempt {next_attempt!r}.",
                run_id=run_id,
            )

        # Branch E: crash after a durable RETRY lifecycle transition,
        # before the next attempt's WORKER_ATTEMPT was recorded. Never
        # re-execute or re-review attempt max_attempt -- continue at
        # next_attempt on the same route (same capability tier).
        return {
            "current_worker": current_worker_route,
            "history": [],
            "attempt": next_attempt,
            "same_tier_retries": same_tier_retries,
            "previous_failure": _failure_context_from_review_payload(
                review_payload
            ),
            "pending_worker_result": None,
        }

    if lifecycle_verdict == "ESCALATE":
        next_attempt = lifecycle_payload.get("next_attempt")
        next_model = lifecycle_payload.get("next_model")
        next_provider = lifecycle_payload.get("next_provider")
        next_effort = lifecycle_payload.get("next_effort")
        next_execution_path = lifecycle_payload.get("next_execution_path")

        if (
            next_attempt != max_attempt + 1
            or not next_model
            or not next_execution_path
        ):
            raise ResumeError(
                "RESUME_UNSAFE_BOUNDARY",
                f"ESCALATE LIFECYCLE checkpoint for attempt {max_attempt} "
                "does not deterministically identify the escalated "
                "route; resume cannot safely perform fresh adaptive "
                "routing for an already-decided escalation.",
                run_id=run_id,
            )

        # Branch F: crash after a durable ESCALATE lifecycle transition.
        # Never re-execute or re-review attempt max_attempt -- continue
        # at next_attempt on the deterministic escalated route recorded
        # on the checkpoint itself. No fresh adaptive routing decision
        # is made.
        return {
            "current_worker": {
                "route_class": current_worker_route["route_class"],
                "model": next_model,
                "provider": next_provider,
                "effort": next_effort,
                "execution_path": next_execution_path,
            },
            "history": [],
            "attempt": next_attempt,
            "same_tier_retries": same_tier_retries,
            "previous_failure": _failure_context_from_review_payload(
                review_payload
            ),
            "pending_worker_result": None,
        }

    raise ResumeError(
        "RESUME_STATE_INCONSISTENT",
        f"LIFECYCLE checkpoint for attempt {max_attempt} has an unknown "
        f"verdict {lifecycle_verdict!r}.",
        run_id=run_id,
    )


def _completed_ordinals(checkpoints: list[dict]) -> set[int]:
    completed = set()

    for checkpoint in checkpoints:
        if checkpoint["boundary"] != "LIFECYCLE":
            continue

        if checkpoint["payload"].get("verdict") != "PASS":
            continue

        ordinal = checkpoint.get("worker_ordinal")

        if ordinal is not None:
            completed.add(ordinal)

    return completed


def _validate_plan_worker_ordinals(
    run_id: str,
    checkpoints: list[dict],
    worker_count: int,
) -> None:
    """Reject any checkpoint ordinal that falls outside the approved plan."""

    for checkpoint in checkpoints:
        ordinal = checkpoint.get("worker_ordinal")

        if ordinal is None:
            continue

        if ordinal <= 0 or ordinal > worker_count:
            raise ResumeError(
                "RESUME_STATE_INCONSISTENT",
                f"Checkpoint ordinal {ordinal} for boundary "
                f"{checkpoint['boundary']!r} is outside the approved "
                f"plan's {worker_count} Worker(s).",
                run_id=run_id,
            )


def _validate_completed_ordinals_are_contiguous(
    run_id: str,
    completed_ordinals: set[int],
    worker_count: int,
) -> None:
    """Enforce that PASSed Workers form a contiguous prefix of the plan."""

    for ordinal in completed_ordinals:
        if ordinal <= 0 or ordinal > worker_count:
            raise ResumeError(
                "RESUME_STATE_INCONSISTENT",
                f"PASSed Worker ordinal {ordinal} is outside the "
                f"approved plan's {worker_count} Worker(s).",
                run_id=run_id,
            )

    expected = set(range(1, len(completed_ordinals) + 1))

    if completed_ordinals != expected:
        raise ResumeError(
            "RESUME_STATE_INCONSISTENT",
            "PASSed Worker ordinals "
            f"{sorted(completed_ordinals)} do not form a contiguous "
            "prefix of the approved plan starting at 1.",
            run_id=run_id,
        )


def _resolve_manager_id(run_id: str, checkpoints: list[dict]) -> str:
    """Resolve the single, unambiguous manager_id from PLAN_APPROVED.

    The durable `approved_plans` row alone is not a sufficient
    checkpoint: an explicit `PLAN_APPROVED` checkpoint with an
    unambiguous `manager_id` must also exist. The Manager is never
    re-invoked to recreate this state.
    """

    plan_approved = [c for c in checkpoints if c["boundary"] == "PLAN_APPROVED"]

    if not plan_approved:
        raise ResumeError(
            "RESUME_CHECKPOINT_MISSING",
            f"Run {run_id!r} has an approved plan but no durable "
            "PLAN_APPROVED checkpoint; resume cannot safely proceed "
            "without re-invoking the Manager, which resume must never do.",
            run_id=run_id,
        )

    manager_ids = {
        checkpoint["payload"].get("manager_id") for checkpoint in plan_approved
    }
    manager_ids.discard(None)

    if not manager_ids:
        raise ResumeError(
            "RESUME_CHECKPOINT_MISSING",
            f"Run {run_id!r} has a PLAN_APPROVED checkpoint with no "
            "manager_id; resume cannot safely proceed without "
            "re-invoking the Manager, which resume must never do.",
            run_id=run_id,
        )

    if len(manager_ids) > 1:
        raise ResumeError(
            "RESUME_STATE_INCONSISTENT",
            f"Run {run_id!r} has contradictory PLAN_APPROVED checkpoints "
            f"naming different manager_ids: {sorted(manager_ids)!r}.",
            run_id=run_id,
        )

    return next(iter(manager_ids))


def resume_run(run_id: str) -> dict:
    """Resume an existing Run from its durable checkpoint history.

    The Manager is NEVER invoked during resume. The original run_id and its
    immutable approved plan remain authoritative. Repeated resumes of the
    same Run are deterministic: once a Worker ordinal has PASSed, it is
    never re-executed or re-reviewed.
    """

    run = get_run(run_id)

    if run is None:
        raise ResumeError(
            "RUN_NOT_FOUND",
            f"No Run exists with id {run_id!r}.",
            run_id=run_id,
        )

    if run["status"] in TERMINAL_RUN_STATUSES:
        raise ResumeError(
            "RUN_TERMINAL",
            f"Run {run_id!r} is already terminal with status "
            f"{run['status']!r} and cannot be resumed.",
            run_id=run_id,
        )

    if run["status"] == "AWAITING_APPROVAL":
        raise ResumeError(
            "RESUME_NOT_AVAILABLE",
            f"Run {run_id!r} is awaiting human approval and cannot be "
            "resumed automatically.",
            run_id=run_id,
        )

    plan = get_approved_plan(run_id)

    if plan is None:
        raise ResumeError(
            "RESUME_PLAN_MISSING",
            f"Run {run_id!r} has no durable approved plan; resume cannot "
            "safely proceed without re-invoking the Manager, which resume "
            "must never do.",
            run_id=run_id,
        )

    try_acquire_for_resume(run_id)

    project = get_project(run["project_id"])

    if project is None:
        raise ResumeError(
            "PROJECT_NOT_FOUND",
            f"Run {run_id!r} references project {run['project_id']!r} "
            "which no longer exists.",
            run_id=run_id,
        )

    checkpoints = list_checkpoints(run_id)

    # -- 1. Durable PLAN_APPROVED checkpoint is mandatory. -------------
    manager_id = _resolve_manager_id(run_id, checkpoints)

    workers = plan.get("workers", [])

    # -- 8. Reject any checkpoint ordinal outside the approved plan. ---
    _validate_plan_worker_ordinals(run_id, checkpoints, len(workers))

    completed_ordinals = _completed_ordinals(checkpoints)

    # -- 7. PASSed Workers must form a contiguous prefix of the plan. --
    _validate_completed_ordinals_are_contiguous(
        run_id, completed_ordinals, len(workers)
    )

    ordinal_checkpoints = _worker_ordinal_checkpoints(checkpoints)

    try:
        record_checkpoint(
            run_id=run_id,
            boundary="RESUME_START",
            payload={
                "previous_status": run["status"],
            },
        )
    except CheckpointWriteError as error:
        raise ResumeError(
            "CHECKPOINT_PERSISTENCE_FAILED",
            f"Could not persist resume checkpoint: {error}",
            run_id=run_id,
        ) from error

    update_run_status(run_id, "RUNNING")

    worker_results = []

    for worker_ordinal, planned_worker in enumerate(workers, start=1):
        if worker_ordinal in completed_ordinals:
            continue

        this_ordinal_checkpoints = ordinal_checkpoints.get(worker_ordinal)

        resume_state = None

        if this_ordinal_checkpoints:
            resume_state = _resume_state_for_ordinal(
                run_id, this_ordinal_checkpoints
            )
            resume_state["current_worker"]["scope"] = planned_worker.get("scope")

            checkpoint_route_class = resume_state["current_worker"].get(
                "route_class"
            )
            plan_route_class = planned_worker.get("route_class")

            if (
                checkpoint_route_class is not None
                and plan_route_class is not None
                and checkpoint_route_class != plan_route_class
            ):
                update_run_status(run_id, "BLOCKED")
                raise ResumeError(
                    "RESUME_UNSAFE_BOUNDARY",
                    "Durable checkpoint route_class "
                    f"{checkpoint_route_class!r} for Worker ordinal "
                    f"{worker_ordinal} does not match the approved plan's "
                    f"route_class {plan_route_class!r}; resuming would "
                    "cross an unsafe execution boundary.",
                    run_id=run_id,
                )

        try:
            outcome = execute_progressively(
                run_id=run_id,
                repo=project["path"],
                manager_id=manager_id,
                original_task=run["input"],
                planned_worker=planned_worker,
                plan_risk=plan.get("risk"),
                worker_ordinal=worker_ordinal,
                resume_state=resume_state,
            )
        except CheckpointWriteError as error:
            update_run_status(run_id, "BLOCKED")
            raise ResumeError(
                "CHECKPOINT_PERSISTENCE_FAILED",
                f"Could not persist execution checkpoint: {error}",
                run_id=run_id,
            ) from error

        except OSError as error:
            update_run_status(run_id, "BLOCKED")
            raise ResumeError(
                "ESCALATION_UNAVAILABLE",
                f"Progressive execution failed unexpectedly: {error}",
                run_id=run_id,
            ) from error

        worker_results.append(outcome)

        if outcome["status"] != "COMPLETED":
            reason = outcome.get("reason")

            if reason == "WORKER_EXECUTION_FAILED":
                update_run_status(run_id, "FAILED")
                record_checkpoint(
                    run_id=run_id,
                    boundary="RUN_TERMINAL",
                    payload={
                        "status": "FAILED",
                        "reason": "WORKER_EXECUTION_FAILED",
                    },
                    worker_ordinal=worker_ordinal,
                )
                raise ResumeError(
                    "WORKER_EXECUTION_FAILED",
                    "A Worker failed to execute successfully.",
                    run_id=run_id,
                )

            if reason == "ESCALATION_UNAVAILABLE":
                update_run_status(run_id, "BLOCKED")
                record_checkpoint(
                    run_id=run_id,
                    boundary="RUN_TERMINAL",
                    payload={
                        "status": "BLOCKED",
                        "reason": "ESCALATION_UNAVAILABLE",
                    },
                    worker_ordinal=worker_ordinal,
                )
                raise ResumeError(
                    "ESCALATION_UNAVAILABLE",
                    outcome.get(
                        "error",
                        "No further escalation route available.",
                    ),
                    run_id=run_id,
                )

            update_run_status(run_id, "BLOCKED")
            record_checkpoint(
                run_id=run_id,
                boundary="RUN_TERMINAL",
                payload={
                    "status": "BLOCKED",
                    "reason": outcome.get("reason", outcome.get("verdict")),
                },
                worker_ordinal=worker_ordinal,
            )
            raise ResumeError(
                "REVIEW_BLOCKED",
                "Manager Review blocked the run: "
                f"{outcome.get('reason', outcome.get('verdict'))}",
                run_id=run_id,
            )

    update_run_result(
        run_id,
        result=plan.get("summary"),
    )

    update_run_status(run_id, "COMPLETED")

    record_checkpoint(
        run_id=run_id,
        boundary="RUN_TERMINAL",
        payload={
            "status": "COMPLETED",
            "worker_count": len(workers),
        },
        worker_ordinal=len(workers) if workers else None,
    )

    return {
        "run_id": run_id,
        "status": "COMPLETED",
        "workers": worker_results,
    }
