"""Review service for Nexus Real Review Gate V1.

Keeps two responsibilities cleanly separated:

- ``review_task_attempt``: gathers evidence and asks an injected Reviewer
  for a verdict, returning a validated ReviewDecision. Never mutates Task
  or Attempt state.
- ``apply_review_decision``: the single central place that applies a
  verdict to the Task/Attempt lifecycle. Never talks to a Reviewer."""

from dataclasses import dataclass
from typing import Optional

from nexus.reviews.reviewer import coerce_decision
from nexus.tasks import registry as task_registry


class ReviewApplicationError(RuntimeError):
    """Raised when a review decision cannot be safely applied."""


@dataclass(slots=True, frozen=True)
class AppliedReviewResult:
    """Structured outcome of applying a ReviewDecision."""

    verdict: str
    task_status: str
    attempt_status: str
    task_id: str
    attempt_id: str


def build_review_evidence(
    mission=None,
    task=None,
    attempt=None,
    worker_output=None,
    routed_model=None,
    runtime_context=None,
):
    """Assemble the evidence dict handed to a Reviewer.

    Intentionally a plain dict (not a strict schema): Reviewer
    implementations only need to read from it, while ReviewDecision -- the
    reviewer's *output* -- is what stays strictly validated.
    """
    return {
        "mission": mission,
        "task": task,
        "attempt": attempt,
        "worker_output": worker_output,
        "routed_model": routed_model,
        "runtime_context": runtime_context,
    }


def review_task_attempt(reviewer, evidence):
    """Ask ``reviewer`` for a verdict over ``evidence``.

    Returns a validated ReviewDecision. Raises
    ``nexus.reviews.models.InvalidReviewDecisionError`` if the reviewer's
    output is malformed -- malformed reviewer output always fails closed
    instead of being coerced into a default verdict.
    """
    raw = reviewer.review(evidence)
    return coerce_decision(raw)


def _require_review_pair(task_id, attempt_id):
    """Return a validated Task/Attempt pair currently awaiting review."""
    task = task_registry.get_task(task_id)

    if task.status != "REVIEW":
        raise ReviewApplicationError(
            f"Task {task_id!r} is not in REVIEW "
            f"(status={task.status!r})."
        )

    attempt = task_registry.get_attempt(attempt_id)

    if attempt.task_id != task_id:
        raise ReviewApplicationError(
            f"Attempt {attempt_id!r} does not belong "
            f"to task {task_id!r}."
        )

    if attempt.status != "REVIEW":
        raise ReviewApplicationError(
            f"Attempt {attempt_id!r} is not in REVIEW "
            f"(status={attempt.status!r})."
        )

    return task, attempt


def apply_review_decision(task_id, attempt_id, decision):
    """Apply a validated ReviewDecision to the Task/Attempt lifecycle.

    PASS:     Task REVIEW -> COMPLETED,  Attempt REVIEW -> COMPLETED
    RETRY:    Task REVIEW -> READY,      Attempt REVIEW -> FAILED (terminal
              rejected evidence; a new Attempt is created by the caller)
    ESCALATE: same as RETRY
    BLOCKED:  Task REVIEW -> FAILED,     Attempt REVIEW -> FAILED

    Requires the Task to currently be in REVIEW; raises
    ``ReviewApplicationError`` otherwise so a verdict can never be applied
    to a Task that is not actually awaiting review.
    """
    task, attempt = _require_review_pair(
        task_id,
        attempt_id,
    )

    verdict = decision.verdict
    evidence_note = f"[REVIEW:{verdict}] {decision.summary}"
    annotated_result = (
        f"{attempt.result}\n\n{evidence_note}" if attempt.result else evidence_note
    )

    if verdict == "PASS":
        task_registry.update_task_status(task_id, "COMPLETED")
        task_registry.update_attempt_status(
            attempt_id, "COMPLETED", result=annotated_result
        )
    elif verdict in ("RETRY", "ESCALATE"):
        task_registry.update_task_status(task_id, "READY")
        task_registry.update_attempt_status(
            attempt_id, "FAILED", result=annotated_result
        )
    elif verdict == "BLOCKED":
        task_registry.update_task_status(task_id, "FAILED")
        task_registry.update_attempt_status(
            attempt_id, "FAILED", result=annotated_result
        )
    else:
        raise ReviewApplicationError(f"Unknown review verdict: {verdict!r}")

    task = task_registry.get_task(task_id)
    attempt = task_registry.get_attempt(attempt_id)

    return AppliedReviewResult(
        verdict=verdict,
        task_status=task.status,
        attempt_status=attempt.status,
        task_id=task_id,
        attempt_id=attempt_id,
    )


def apply_review_failure(task_id, attempt_id, reason):
    """Fail closed when the reviewer itself cannot produce a valid verdict."""
    task, attempt = _require_review_pair(
        task_id,
        attempt_id,
    )

    evidence_note = (
        f"[REVIEW_ERROR] {reason}"
    )

    annotated_result = (
        f"{attempt.result}\n\n{evidence_note}"
        if attempt.result
        else evidence_note
    )

    task_registry.update_task_status(
        task_id,
        "FAILED",
    )

    task_registry.update_attempt_status(
        attempt_id,
        "FAILED",
        result=annotated_result,
    )

    task = task_registry.get_task(task_id)
    attempt = task_registry.get_attempt(attempt_id)

    return AppliedReviewResult(
        verdict="REVIEW_ERROR",
        task_status=task.status,
        attempt_status=attempt.status,
        task_id=task_id,
        attempt_id=attempt_id,
    )


def apply_escalation_unavailable(task_id, attempt_id, decision, reason):
    """Fail an Attempt/Task closed when escalation was requested but no
    stronger approved route exists.

    Distinct from ``apply_review_decision`` because a RETRY/ESCALATE verdict
    normally resolves to Task REVIEW -> READY; when the retry/escalation
    policy determines no further route is available, the Task must instead
    terminate REVIEW -> FAILED directly (never pass through READY, which
    would incorrectly make it look retryable/eligible again).
    """
    task, attempt = _require_review_pair(
        task_id,
        attempt_id,
    )

    evidence_note = (
        f"[REVIEW:{decision.verdict}] "
        f"{decision.summary} ({reason})"
    )
    annotated_result = (
        f"{attempt.result}\n\n{evidence_note}" if attempt.result else evidence_note
    )

    task_registry.update_task_status(task_id, "FAILED")
    task_registry.update_attempt_status(attempt_id, "FAILED", result=annotated_result)

    task = task_registry.get_task(task_id)
    attempt = task_registry.get_attempt(attempt_id)

    return AppliedReviewResult(
        verdict=decision.verdict,
        task_status=task.status,
        attempt_status=attempt.status,
        task_id=task_id,
        attempt_id=attempt_id,
    )
