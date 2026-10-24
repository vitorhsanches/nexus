"""Structured, persisted failure evidence for Runs.

A failure is recorded once, at the boundary where its evidence exists, and is
read back as the same object by the API and the Control Center. The contract is
one flat dict, with every field nullable. A field is ``None`` when the evidence
did not exist at the time; it is never filled in afterwards.

    stage            where it failed: PLANNING, ROUTING, EXECUTION, REVIEW,
                     INTEGRATION, FINALIZATION
    code             the specific reason code, e.g. WORKER_LAUNCH_FAILED
    failure_class    the existing failure class (TOOL_FAILURE, ...) or None
    category         environmental | capability | policy | provider | task,
                     derived from failure_class and code; None if unknown
    message          one bounded, redacted, single-line root error
    agent_id, agent_role, model, provider, worker_ordinal, worktree
                     provider is the route/resource provider (e.g. claude).
                     The execution transport (omniroute) is never reported
                     as the provider.
    recoverable      True / False when the category says so, else None
    recovery_hint    a fixed sentence per category; never generated text

The API adds ``source``: ``recorded`` when the failure was persisted with the
Run, or ``derived`` when a Run written before this contract was reconstructed
from its checkpoints.

Redaction has one implementation, here. Every message that is persisted or
exposed goes through ``sanitize_error_text``.
"""

from __future__ import annotations

import re

MESSAGE_LIMIT = 300

# Narrow last line of defense for text that reaches the database or an API.
# Patterns run in order. Bearer tokens come first, so an
# "Authorization: Bearer <token>" value is not left behind by the key=value
# pattern. The JSON pattern covers credential fields written as "key": "value".
_SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+\S+"),
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(
        r"(?i)[\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
        r"token|secret|password|authorization|credential)s?[\"']?"
        r"\s*[=:]\s*[\"']?[^\s\"',}]+"
    ),
)

_CATEGORY_BY_CLASS = {
    "TOOL_FAILURE": "environmental",
    "ENVIRONMENT_FAILURE": "environmental",
    "CAPABILITY_FAILURE": "capability",
    "SCOPE_VIOLATION": "policy",
    "PROVIDER_FAILURE": "provider",
    "TRANSIENT": "provider",
    "VALIDATION_FAILURE": "task",
    "REQUIREMENT_FAILURE": "task",
}

_CATEGORY_BY_CODE = {
    "ESCALATION_UNAVAILABLE": "policy",
    "INITIAL_ROUTING_UNAVAILABLE": "policy",
    "WORKER_LAUNCH_FAILED": "environmental",
    "GIT_TOOL_FAILURE": "environmental",
}

RECOVERY_HINTS = {
    "environmental": "Resolve the local tool or environment failure before retrying.",
    "capability": "Escalation to another capable route may be required.",
    "policy": "A routing or scope policy blocked this step. Review the policy decision before retrying.",
    "provider": "The model provider failed or was unavailable. Retry later or use another available route.",
    "task": "The produced change did not satisfy the task. Revise the task or the plan before retrying.",
}

_RECOVERABLE = {
    "environmental": True,
    "provider": True,
    "capability": True,
    "policy": False,
    "task": False,
}


def sanitize_error_text(value, limit: int = MESSAGE_LIMIT) -> str | None:
    """Single-line, bounded, credential-redacted text, or None if empty."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("<redacted>", text)
    if not text:
        return None
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


def category_for(failure_class: str | None, code: str | None) -> str | None:
    """Environmental, capability, policy, provider or task; None if unknown."""
    if failure_class and failure_class in _CATEGORY_BY_CLASS:
        return _CATEGORY_BY_CLASS[failure_class]
    if code and code in _CATEGORY_BY_CODE:
        return _CATEGORY_BY_CODE[code]
    return None


def build_failure(
    *,
    stage: str | None,
    code: str | None,
    failure_class: str | None = None,
    message=None,
    agent_id: str | None = None,
    agent_role: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    worker_ordinal: int | None = None,
    worktree: str | None = None,
) -> dict:
    """The structured failure payload. Unknown values stay None."""
    category = category_for(failure_class, code)
    return {
        "stage": stage,
        "code": code,
        "failure_class": failure_class,
        "category": category,
        "message": sanitize_error_text(message),
        "agent_id": agent_id,
        "agent_role": agent_role,
        "model": model,
        "provider": provider,
        "worker_ordinal": worker_ordinal,
        "worktree": worktree,
        "recoverable": _RECOVERABLE.get(category),
        "recovery_hint": RECOVERY_HINTS.get(category),
    }


def agent_identity(agent_id: str | None) -> dict:
    """Role, model, provider and worktree from the persisted agent row.

    Returns an empty dict when the agent is unknown, so nothing is invented.
    The registry import is local so this module stays importable on its own.
    """
    if not agent_id:
        return {}
    from nexus.registry.agents import get_agent

    # A diagnostic lookup must never fail the run it describes.
    try:
        row = get_agent(agent_id)
    except Exception:  # noqa: BLE001 - diagnostics are best-effort
        return {}
    if row is None:
        return {}
    row = dict(row)
    return {
        "agent_role": row.get("role"),
        "model": row.get("model"),
        "provider": row.get("provider"),
        "worktree": row.get("worktree"),
    }


def _tail(clean: str | None, limit: int) -> str | None:
    """The last ``limit`` characters, marked as cut when anything was dropped."""
    if not clean:
        return None
    if len(clean) <= limit:
        return clean
    return "..." + clean[-limit:]


def agent_output_tail(agent_id: str | None, limit: int = 300) -> str | None:
    """The tail of the output Nexus recorded for an agent, redacted and bounded.

    Used only to derive a legacy Run's root error from evidence Nexus already
    stored. The text is shown as the agent's own output, never interpreted.
    """
    if not agent_id:
        return None
    from nexus.registry.agents import get_agent

    try:
        row = get_agent(agent_id)
    except Exception:  # noqa: BLE001 - diagnostics are best-effort
        return None
    if row is None:
        return None
    return _tail(sanitize_error_text(dict(row).get("result"), limit=2000), limit)


def worker_failure(worker: dict, worker_ordinal: int | None) -> dict:
    """Structured failure for a Worker attempt that did not complete.

    ``worker`` is the Worker result dict from run_omniroute_worker(). A launch
    failure is already redacted and bounded at its source. An execution failure
    keeps the transcript tail as the root error: it is redacted as a whole first,
    and only then cut to its last 300 characters, so a secret straddling the cut
    cannot survive.
    """
    agent_id = worker.get("agent_id")
    identity = agent_identity(agent_id)
    worktree = worker.get("worktree") or identity.get("worktree")
    common = {
        "agent_id": agent_id,
        "agent_role": identity.get("agent_role") or "Worker",
        # The route the Worker was given; supplied by the orchestrator.
        "model": worker.get("model") or identity.get("model"),
        "provider": worker.get("provider"),
        "worker_ordinal": worker_ordinal,
        "worktree": worktree,
    }
    if worker.get("reason") == "WORKER_LAUNCH_FAILED":
        message = worker.get("error")
        if worker.get("exception_type") and message:
            message = f"{worker['exception_type']}: {message}"
        return build_failure(
            stage="EXECUTION",
            code="WORKER_LAUNCH_FAILED",
            failure_class="TOOL_FAILURE",
            message=message,
            **common,
        )
    return build_failure(
        stage="EXECUTION",
        code="WORKER_EXECUTION_FAILED",
        message=_tail(sanitize_error_text(worker.get("error"), limit=2000), 300),
        **common,
    )


def review_failure(
    code: str,
    review: dict,
    *,
    reviewer_id: str | None,
    reviewer_model: str | None,
    reviewer_provider: str | None,
    worker_ordinal: int | None,
    worktree: str | None = None,
) -> dict:
    """Structured failure for a Review that did not accept the Worker.

    ``review`` is the Reviewer's verdict dict, or the Review dispatcher's result
    when the Reviewer itself failed (``code`` REVIEW_FAILED). ``worktree`` is
    the reviewed worktree, which the Reviewer inspected.
    """
    if code == "REVIEW_FAILED":
        return build_failure(
            stage="REVIEW",
            code=code,
            message=review.get("error"),
            agent_id=reviewer_id,
            agent_role="ManagerReview" if reviewer_id else None,
            model=reviewer_model,
            provider=reviewer_provider,
            worker_ordinal=worker_ordinal,
            worktree=worktree,
        )
    evidence = review.get("evidence") or []
    message = review.get("summary")
    if evidence:
        message = f"{message} | {evidence[0]}" if message else evidence[0]
    return build_failure(
        stage="REVIEW",
        code=code,
        failure_class=review.get("failure_class"),
        message=message,
        agent_id=reviewer_id,
        agent_role="ManagerReview" if reviewer_id else None,
        model=reviewer_model,
        provider=reviewer_provider,
        worker_ordinal=worker_ordinal,
        worktree=worktree,
    )


_FAILED_TERMINAL_STATUSES = {"FAILED", "BLOCKED"}


def resolve_run_failure(checkpoints: list[dict]) -> dict | None:
    """The structured failure of a FAILED or BLOCKED Run, or None.

    A failure recorded with the run is returned as recorded. A Run written
    before recording existed is derived from its persisted checkpoints, and is
    labelled ``source: "derived"``. A field the checkpoints do not hold stays
    None. Nothing is inferred from transcript text.
    """
    terminal = None
    for cp in checkpoints or []:
        if cp.get("boundary") == "RUN_TERMINAL":
            terminal = cp
    if terminal is None:
        return None
    payload = terminal.get("payload") or {}
    if str(payload.get("status", "")).upper() not in _FAILED_TERMINAL_STATUSES:
        return None

    recorded = payload.get("failure")
    if recorded:
        return {**recorded, "source": "recorded"}

    # A legacy Run is labelled as derived. Nothing here is inferred from text.

    worker_cp = None
    review_cp = None
    for cp in checkpoints:
        p = cp.get("payload") or {}
        if cp.get("boundary") == "WORKER_ATTEMPT" and p.get("status") not in (None, "COMPLETED"):
            worker_cp = cp
        if cp.get("boundary") == "REVIEW" and (p.get("failure_class") or p.get("status") == "COMPLETED" and p.get("verdict") not in (None, "PASS")):
            review_cp = cp

    message = payload.get("error")
    if review_cp is not None and (worker_cp is None or review_cp.get("sequence", 0) > worker_cp.get("sequence", 0)):
        rp = review_cp.get("payload") or {}
        failure = build_failure(
            stage="REVIEW",
            code="REVIEW_" + str(rp.get("verdict") or "BLOCKED"),
            failure_class=rp.get("failure_class"),
            message=message or rp.get("summary"),
            agent_id=rp.get("reviewer_id"),
            agent_role="ManagerReview" if rp.get("reviewer_id") else None,
            model=rp.get("reviewer_model"),
            provider=rp.get("reviewer_provider"),
            worker_ordinal=review_cp.get("worker_ordinal"),
        )
    elif worker_cp is not None:
        wp = worker_cp.get("payload") or {}
        # A Worker that never started is recorded as LAUNCH_FAILED (legacy) or
        # with WORKER_LAUNCH_FAILED (current). Both are the same launch failure.
        launch = wp.get("reason") == "WORKER_LAUNCH_FAILED" or wp.get("status") == "LAUNCH_FAILED"
        identity = agent_identity(wp.get("worker_id"))
        if launch and wp.get("exception_type") and wp.get("error"):
            detail = f"{wp['exception_type']}: {wp.get('error')}"
        else:
            detail = wp.get("error") or agent_output_tail(wp.get("worker_id")) or message
        failure = build_failure(
            stage="EXECUTION",
            code="WORKER_LAUNCH_FAILED" if launch else (payload.get("reason") or "WORKER_EXECUTION_FAILED"),
            failure_class="TOOL_FAILURE" if launch else None,
            message=detail,
            agent_id=wp.get("worker_id"),
            agent_role="Worker" if wp.get("worker_id") else None,
            model=wp.get("model") or identity.get("model"),
            provider=wp.get("provider"),
            worker_ordinal=worker_cp.get("worker_ordinal"),
            worktree=wp.get("worktree") or identity.get("worktree"),
        )
    else:
        failure = build_failure(
            stage=None,
            code=payload.get("reason"),
            message=message,
            worker_ordinal=terminal.get("worker_ordinal"),
        )
    return {**failure, "source": "derived"}
