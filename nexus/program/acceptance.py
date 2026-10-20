"""Structured acceptance criteria: requirements -> evidence -> decision.

A Program phase (or the whole Program) finishing its Manager/Worker/Review
cycle and passing build/test commands proves the code *compiles and its
existing tests pass* -- it does not prove the requested feature was
actually built. This module represents each acceptance criterion the
Architect declares, and evaluates it against real, deterministic evidence
gathered from the exact verified git revision:

  - "command": a specific command's exit code (run in the same detached
    verify worktree as the phase's own verify commands, at the same
    revision).
  - "file_exists": a repository-relative path exists at that revision.

A criterion with no deterministic verifier is never marked satisfied by
assertion -- the status vocabulary has UNVERIFIED for exactly that case.
"""

from __future__ import annotations

from pathlib import Path

from nexus.program.verify import prepare_verify_worktree, run_command


PENDING = "PENDING"
SATISFIED = "SATISFIED"
UNSATISFIED = "UNSATISFIED"
UNVERIFIED = "UNVERIFIED"

STATUSES = frozenset({PENDING, SATISFIED, UNSATISFIED, UNVERIFIED})

VERIFICATION_TYPES = frozenset({"command", "file_exists"})

MAX_CRITERIA = 10
MAX_DESCRIPTION_CHARS = 500


class AcceptanceError(ValueError):
    pass


def validate_criteria(value, *, field: str, seen_ids: set[str], allow_empty: bool) -> list[dict]:
    """Validate one criteria list (top-level or one phase's), fail closed.

    ``seen_ids`` accumulates ids across the whole spec so ids are unique
    everywhere, not just within one list -- this is what makes a criterion
    traceable end to end instead of colliding with another phase's.
    """
    if not isinstance(value, list):
        raise AcceptanceError(f"{field} must be a list.")
    if not allow_empty and not value:
        raise AcceptanceError(f"{field} must not be empty.")
    if len(value) > MAX_CRITERIA:
        raise AcceptanceError(f"{field} has more than {MAX_CRITERIA} criteria.")

    normalized: list[dict] = []
    for index, raw in enumerate(value, start=1):
        where = f"{field}[{index}]"
        if not isinstance(raw, dict):
            raise AcceptanceError(f"{where} is not an object.")

        criterion_id = raw.get("id")
        if not isinstance(criterion_id, str) or not criterion_id.strip():
            raise AcceptanceError(f"{where} id is missing.")
        criterion_id = criterion_id.strip()
        if criterion_id in seen_ids:
            raise AcceptanceError(f"{where} id {criterion_id!r} is not unique.")
        seen_ids.add(criterion_id)

        description = raw.get("description")
        if not isinstance(description, str) or not description.strip():
            raise AcceptanceError(f"{where} description is missing.")
        description = description.strip()[:MAX_DESCRIPTION_CHARS]

        verification_type = raw.get("verification_type")
        if verification_type not in VERIFICATION_TYPES:
            raise AcceptanceError(
                f"{where} verification_type must be one of {sorted(VERIFICATION_TYPES)}, "
                f"got {verification_type!r}."
            )

        required = raw.get("required", True)
        if not isinstance(required, bool):
            raise AcceptanceError(f"{where} required must be a boolean.")

        criterion = {
            "id": criterion_id,
            "description": description,
            "verification_type": verification_type,
            "required": required,
        }

        if verification_type == "command":
            command = raw.get("command")
            if not isinstance(command, str) or not command.strip():
                raise AcceptanceError(f"{where} command is required for verification_type=command.")
            criterion["command"] = command.strip()
        else:
            path = raw.get("path")
            if not isinstance(path, str) or not path.strip():
                raise AcceptanceError(f"{where} path is required for verification_type=file_exists.")
            path = path.strip().replace("\\", "/")
            if path.startswith("/") or path.startswith("..") or "/../" in f"/{path}/":
                raise AcceptanceError(f"{where} path must be repository-relative and not escape it.")
            criterion["path"] = path

        normalized.append(criterion)

    return normalized


def _evaluate_one(criterion: dict, worktree: Path, timeout: int | None) -> dict:
    if criterion["verification_type"] == "file_exists":
        exists = (worktree / criterion["path"]).exists()
        return {
            **criterion,
            "status": SATISFIED if exists else UNSATISFIED,
            "evidence": f"path {criterion['path']!r} "
            + ("exists" if exists else "does not exist")
            + f" at the verified revision.",
        }

    result = run_command(criterion["command"], worktree, timeout)
    passed = result["passed"]
    status_note = "timed out" if result["timed_out"] else f"exit_code={result['exit_code']}"
    return {
        **criterion,
        "status": SATISFIED if passed else UNSATISFIED,
        "evidence": f"`{criterion['command']}` {status_note}."
        + (f" Output tail:\n{result['output_tail'][-1000:]}" if not passed else ""),
    }


def evaluate_criteria(repo: str, revision: str, criteria: list[dict], timeout: int | None) -> list[dict]:
    """Evaluate every criterion against ``revision``, real evidence only.

    Returns one result dict per criterion (never drops one), each carrying
    its own ``status``/``evidence`` alongside the original criterion
    fields, so nothing in the input list can silently disappear.
    """
    if not criteria:
        return []
    worktree, _head = prepare_verify_worktree(repo, revision)
    return [_evaluate_one(criterion, worktree, timeout) for criterion in criteria]


def unverified(criteria: list[dict]) -> list[dict]:
    """Evaluation results for criteria that were never run (e.g. because a
    prerequisite build/test step failed first) -- honest UNVERIFIED, never
    a fabricated pass."""
    return [
        {**criterion, "status": UNVERIFIED, "evidence": "Not evaluated: an earlier verification step failed."}
        for criterion in criteria
    ]


def failed_required(results: list[dict]) -> list[dict]:
    return [r for r in results if r["required"] and r["status"] != SATISFIED]


def gate_passed(results: list[dict]) -> bool:
    return not failed_required(results)


def summarize(results: list[dict]) -> dict:
    return {
        "acceptance_total": len(results),
        "acceptance_satisfied": sum(1 for r in results if r["status"] == SATISFIED),
        "acceptance_unsatisfied": sum(1 for r in results if r["status"] == UNSATISFIED),
        "acceptance_unverified": sum(1 for r in results if r["status"] == UNVERIFIED),
    }


def render_failure_excerpt(results: list[dict], limit: int = 2600) -> str:
    failed = failed_required(results)
    if not failed:
        return ""
    lines = ["Unsatisfied required acceptance criteria:"]
    for criterion in failed:
        lines.append(f"- [{criterion['id']}] {criterion['description']} -> {criterion['status']}")
        lines.append(f"  Evidence: {criterion['evidence']}")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 3] + "..."
