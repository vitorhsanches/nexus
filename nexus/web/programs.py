"""Read-only operational projection over the Nexus Program layer.

Programs (nexus.program.state) are the durable prompt->finished-project
state that ``nexus build``/``nexus run`` writes as JSON files, independent
of which process launched them. Each phase references one or more real
Runs (nexus.registry.runs/agents/checkpoints), which this module joins in
to expose Manager/Worker/Reviewer lineage, routing decisions and file
activity for a Program without ever re-deriving Nexus behavior by
guessing: every field here is read from persisted state, or is explicitly
``None``/omitted when Nexus has not recorded it.

Never mutates Program state, a Run, or any git worktree/repository -- this
module is strictly read-only observability.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from nexus import failure_classification as fc
from nexus.program import acceptance as acceptance_module
from nexus.program import state as program_state
from nexus.program.runner import program_acceptance_summary
from nexus.registry.checkpoints import list_checkpoints
from nexus.web import operational

# Failure classes that mean a human, not a retry, must act next -- Section
# 12's "waiting for user" requirement. Anything else that ends up BLOCKED
# is an operational/technical block, not a request for approval.
_NEEDS_HUMAN = frozenset(
    {fc.AUTHORITY_REQUIRED, fc.POLICY_BLOCKED, fc.UNSUPPORTED_CAPABILITY}
)

_FILES_CHANGED_LIMIT = 30
_NOISE_DIR_PREFIXES = ("node_modules/", "dist/", "build/", "__pycache__/", ".git/")
# git's well-known empty-tree object -- always present in any repository,
# used as the "before" revision for the very first phase's diff so it is
# not silently marked unavailable just because there is no earlier stage.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _stage_run_ids(stage: dict | None) -> list[str]:
    return list((stage or {}).get("run_ids") or [])


def _all_run_ids(program: dict) -> list[str]:
    ids: list[str] = []
    for stage in [*(program.get("phases") or []), program.get("final")]:
        ids.extend(_stage_run_ids(stage))
    # De-duplicate, keep first-seen order.
    seen: set[str] = set()
    ordered = []
    for run_id in ids:
        if run_id not in seen:
            seen.add(run_id)
            ordered.append(run_id)
    return ordered


def _current_stage(program: dict) -> tuple[str, int | None, dict | None]:
    """Return (label, 1-based phase number or None, the stage dict).

    ``label`` is one of "phase", "final", or "" when nothing is active/next
    (e.g. the Program never got past bootstrap, or already completed).
    Never invents an in-progress phase: a phase only counts as current when
    its own recorded status says so, or -- once nothing is RUNNING -- the
    first PENDING/BLOCKED phase encountered in order.
    """
    phases = program.get("phases") or []
    for index, stage in enumerate(phases):
        if stage.get("status") == "RUNNING":
            return "phase", index + 1, stage
    final = program.get("final")
    if final and final.get("status") == "RUNNING":
        return "final", len(phases) + 1, final
    for index, stage in enumerate(phases):
        if stage.get("status") in ("PENDING", "BLOCKED"):
            return "phase", index + 1, stage
    if final and final.get("status") in ("PENDING", "BLOCKED"):
        return "final", len(phases) + 1, final
    return "", None, None


def _waiting_for_user(program: dict, current_stage: dict | None) -> tuple[bool, str | None]:
    """True + a short reason only when Nexus itself recorded a failure
    class that means a human decision is required -- never inferred from
    the Program simply being BLOCKED for a technical/retryable reason."""
    if program.get("status") != "BLOCKED":
        return False, None
    failure_class = (current_stage or {}).get("last_failure_class")
    if failure_class in _NEEDS_HUMAN:
        return True, failure_class
    return False, None


def _phase_summary(index: int, phase_spec: dict, stage: dict) -> dict:
    return {
        "index": index,
        "title": stage.get("title") or phase_spec.get("title"),
        "goal": phase_spec.get("goal"),
        "status": stage.get("status"),
        "attempts": stage.get("attempts", 0),
        "last_failure_class": stage.get("last_failure_class"),
        "last_error": stage.get("last_error"),
        "run_ids": _stage_run_ids(stage),
        "target_revision": stage.get("target_revision"),
        "acceptance_summary": stage.get("acceptance_summary") or acceptance_module.summarize([]),
    }


def _files_changed(repo: str | None, before_rev: str | None, after_rev: str | None) -> dict:
    """Bounded, read-only ``git diff --stat`` between two revisions of the
    Program's own primary repository -- never a directory scan, never a
    mutation, capped to ``_FILES_CHANGED_LIMIT`` files. Returns
    ``{"available": False}`` (never fabricated data) when the repo or
    either revision is unknown."""
    if not repo or not before_rev or not after_rev or before_rev == after_rev:
        return {"available": False}
    try:
        result = subprocess.run(
            ["git", "diff", "--stat", "--no-color", f"{before_rev}..{after_rev}"],
            cwd=repo, capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False}
    if result.returncode != 0:
        return {"available": False}

    files = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        name = line.split("|", 1)[0].strip()
        if any(name.startswith(prefix) for prefix in _NOISE_DIR_PREFIXES):
            continue
        files.append(name)
        if len(files) >= _FILES_CHANGED_LIMIT:
            break
    return {"available": True, "files": files, "truncated": len(files) >= _FILES_CHANGED_LIMIT}


# Checkpoint boundaries that carry a routing/recovery decision worth
# surfacing on their own, distinct from the generic operational timeline.
_ROUTING_DECISIONS = {"RETRY_SAME_TIER", "FALLBACK_ROUTE_ATTEMPT", "FALLBACK_EXHAUSTED"}
_ROUTING_VERDICTS = {"RETRY", "ESCALATE", "BLOCKED"}


def _routing_events(checkpoints: list[dict]) -> list[dict]:
    """Extract retry/fallback/escalation decisions Nexus already recorded.

    Only reads the explicit ``decision``/``verdict``/``reason`` fields
    progressive.py itself wrote to each checkpoint payload -- never
    inferred from a model name or free text.
    """
    events = []
    for cp in checkpoints:
        payload = cp.get("payload") or {}
        decision = payload.get("decision")
        verdict = payload.get("verdict")
        kind = None
        if cp.get("boundary") == "WORKER_ATTEMPT" and decision in _ROUTING_DECISIONS:
            kind = decision
        elif cp.get("boundary") == "LIFECYCLE" and verdict in _ROUTING_VERDICTS:
            kind = verdict
        elif cp.get("boundary") == "EXECUTION_START":
            kind = "INITIAL_ROUTE"
        if kind is None:
            continue
        events.append({
            "sequence": cp.get("sequence"),
            "created_at": cp.get("created_at"),
            "worker_ordinal": cp.get("worker_ordinal"),
            "kind": kind,
            "model": payload.get("model") or payload.get("next_model"),
            "provider": payload.get("provider") or payload.get("next_provider"),
            "route_class": payload.get("route_class"),
            "failure_class": payload.get("reason") if cp.get("boundary") == "WORKER_ATTEMPT" else None,
        })
    return events


def _run_bundle(run_id: str) -> dict | None:
    detail = operational.get_operational_run_detail(run_id)
    if detail is None:
        return None
    checkpoints = list_checkpoints(run_id)
    return {
        "run_id": run_id,
        "status": detail.get("display_status") or detail.get("status"),
        "agents": detail.get("agents") or [],
        "checkpoints": checkpoints,
        "routing_events": _routing_events(checkpoints),
    }


def _telemetry_for_runs(run_ids: list[str]) -> dict:
    """Best-effort aggregate telemetry across every Run this Program has
    launched. Telemetry is process-local in-memory state
    (nexus.telemetry.runs), so a Program driven from the CLI is invisible
    to a separately-running web server process -- an honest ``None``/empty
    result in that case, never a fabricated estimate."""
    from nexus.telemetry import get_run_summary  # noqa: PLC0415

    total_calls = 0
    total_tokens = 0
    have_tokens = False
    models: set[str] = set()
    providers: set[str] = set()
    retries = fallbacks = escalations = 0

    for run_id in run_ids:
        summary = get_run_summary(run_id)
        total_calls += summary.get("llm_calls") or 0
        if summary.get("total_tokens") is not None:
            have_tokens = True
            total_tokens += summary["total_tokens"]
        models.update(summary.get("models_used") or [])
        providers.update(summary.get("providers_used") or [])
        retries += summary.get("retry_count") or 0
        fallbacks += summary.get("fallback_count") or 0
        escalations += summary.get("escalation_count") or 0

    return {
        "llm_calls": total_calls,
        "total_tokens": total_tokens if have_tokens else None,
        "models_used": sorted(models),
        "providers_used": sorted(providers),
        "retry_count": retries,
        "fallback_count": fallbacks,
        "escalation_count": escalations,
    }


def summarize_program(program: dict) -> dict:
    """Compact projection for the Program list (sidebar)."""
    phases = program.get("phases") or []
    stage_label, phase_number, current_stage = _current_stage(program)
    waiting, waiting_reason = _waiting_for_user(program, current_stage)
    contract = program.get("task_contract") or {}
    authority = (contract.get("authority_resolution") or {})

    return {
        "program_id": program.get("program_id"),
        "name": program.get("name"),
        "project_id": program.get("project_id"),
        "path": program.get("path"),
        "status": program.get("status"),
        "created_at": program.get("created_at"),
        "updated_at": program.get("updated_at"),
        "blocked_reason": program.get("blocked_reason"),
        "phase_count": len(phases),
        "current_phase": phase_number,
        "current_phase_label": (current_stage or {}).get("title") if current_stage else None,
        "current_stage_kind": stage_label or None,
        "autonomy_profile": authority.get("autonomy_profile"),
        "waiting_for_user": waiting,
        "waiting_reason": waiting_reason,
        "acceptance_summary": program_acceptance_summary(program),
    }


def list_program_summaries() -> list[dict]:
    """Newest-first Program summaries (running, completed, blocked alike)."""
    return [summarize_program(p) for p in program_state.list_programs()]


def get_program_view(program_id: str) -> dict | None:
    """Full detail for one Program: phases, acceptance, active agents,
    routing/failure events, file activity, and best-effort telemetry."""
    try:
        program = program_state.load_program(program_id)
    except program_state.ProgramNotFoundError:
        return None

    spec = program.get("spec") or {}
    phase_specs = spec.get("phases") or []
    phases = [
        _phase_summary(index + 1, phase_specs[index] if index < len(phase_specs) else {}, stage)
        for index, stage in enumerate(program.get("phases") or [])
    ]

    final_stage = program.get("final") or {}
    final_summary = {
        "title": final_stage.get("title") or "Final verification",
        "status": final_stage.get("status"),
        "attempts": final_stage.get("attempts", 0),
        "last_failure_class": final_stage.get("last_failure_class"),
        "last_error": final_stage.get("last_error"),
        "run_ids": _stage_run_ids(final_stage),
        "acceptance": final_stage.get("acceptance") or [],
        "acceptance_summary": final_stage.get("acceptance_summary") or acceptance_module.summarize([]),
    }

    stage_label, phase_number, current_stage = _current_stage(program)
    waiting, waiting_reason = _waiting_for_user(program, current_stage)

    run_ids = _all_run_ids(program)
    runs = {rid: bundle for rid in run_ids if (bundle := _run_bundle(rid)) is not None}

    active_run_ids = _stage_run_ids(current_stage) if current_stage else []
    active_agents: list[dict] = []
    for rid in active_run_ids:
        bundle = runs.get(rid)
        if bundle:
            active_agents.extend(bundle["agents"])

    # Bounded file-activity: diff this phase's committed revision against
    # the previous stage's, in the Program's own primary repo.
    revisions = [_EMPTY_TREE] + [s.get("target_revision") for s in program.get("phases") or []]
    if final_stage.get("target_revision"):
        revisions.append(final_stage["target_revision"])
    files_changed_by_stage = []
    for i in range(1, len(revisions)):
        files_changed_by_stage.append(
            _files_changed(program.get("path"), revisions[i - 1], revisions[i])
        )

    routing_timeline: list[dict] = []
    for rid in run_ids:
        bundle = runs.get(rid)
        if bundle:
            for event in bundle["routing_events"]:
                routing_timeline.append({**event, "run_id": rid})
    routing_timeline.sort(key=lambda e: (e.get("created_at") or "", e.get("sequence") or 0))

    return {
        "program_id": program.get("program_id"),
        "name": program.get("name"),
        "project_id": program.get("project_id"),
        "path": program.get("path"),
        "status": program.get("status"),
        "created_at": program.get("created_at"),
        "updated_at": program.get("updated_at"),
        "blocked_reason": program.get("blocked_reason"),
        "stop_after_spec": program.get("stop_after_spec", False),
        "task_contract": program.get("task_contract"),
        "spec": {
            "summary": spec.get("summary"),
            "stack": spec.get("stack"),
            "setup_commands": spec.get("setup_commands"),
            "final_verify_commands": spec.get("final_verify_commands"),
            "acceptance_criteria": spec.get("acceptance_criteria"),
        } if spec else None,
        "phases": phases,
        "final": final_summary,
        "current_phase": phase_number,
        "current_stage_kind": stage_label or None,
        "waiting_for_user": waiting,
        "waiting_reason": waiting_reason,
        "acceptance_summary": program_acceptance_summary(program),
        "events": program.get("events") or [],
        "runs": runs,
        "active_agents": active_agents,
        "routing_timeline": routing_timeline,
        "files_changed_by_stage": files_changed_by_stage,
        "telemetry": _telemetry_for_runs(run_ids),
    }
