import json
import os
import re
import subprocess
from pathlib import Path

from nexus.registry.agents import (
    create_agent,
    update_agent_execution,
    update_agent_status,
)


REVIEW_PATTERN = re.compile(
    r"NEXUS_REVIEW_BEGIN\s*(\{.*?\})\s*NEXUS_REVIEW_END",
    re.DOTALL,
)


def _find_codex() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")

    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA is not available.")

    base = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
    candidates = list(base.glob("*/codex.exe"))

    if not candidates:
        raise FileNotFoundError(
            f"Codex executable not found under {base}"
        )

    return max(
        candidates,
        key=lambda path: path.stat().st_mtime,
    )


def _validate_review(review: dict) -> dict:
    allowed_verdicts = {
        "PASS",
        "RETRY",
        "ESCALATE",
        "BLOCKED",
    }

    allowed_failure_classes = {
        None,
        "TRANSIENT",
        "TOOL_FAILURE",
        "PROVIDER_FAILURE",
        "VALIDATION_FAILURE",
        "SCOPE_VIOLATION",
        "CAPABILITY_FAILURE",
        "REQUIREMENT_FAILURE",
        "UNKNOWN",
    }

    verdict = review.get("verdict")

    if verdict not in allowed_verdicts:
        raise ValueError(
            f"Invalid review verdict: {verdict!r}"
        )

    failure_class = review.get("failure_class")

    if failure_class not in allowed_failure_classes:
        raise ValueError(
            f"Invalid failure_class: {failure_class!r}"
        )

    summary = review.get("summary")

    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Review summary is missing.")

    evidence = review.get("evidence")

    if not isinstance(evidence, list) or not evidence:
        raise ValueError(
            "Review must contain evidence."
        )

    for item in evidence:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                "Review evidence contains an invalid item."
            )

    if verdict == "PASS" and failure_class is not None:
        raise ValueError(
            "PASS review cannot contain a failure_class."
        )

    return review


def _extract_review(output: str) -> dict:
    matches = list(REVIEW_PATTERN.finditer(output))

    if not matches:
        raise ValueError(
            "Manager did not return a review envelope."
        )

    errors = []

    for match in reversed(matches):
        try:
            review = json.loads(match.group(1))
            return _validate_review(review)
        except (json.JSONDecodeError, ValueError) as error:
            errors.append(str(error))

    raise ValueError(
        "No valid review envelope found. "
        + "; ".join(errors[:3])
    )


def review_worker(
    run_id: str,
    worker_id: str,
    worktree: str,
    original_task: str,
    worker_scope: str,
    model: str = "gpt-5.6-luna",
    effort: str = "low",
) -> dict:
    codex = _find_codex()

    reviewer_id = create_agent(
        run_id=run_id,
        role="ManagerReview",
        provider="codex",
        model=model,
        effort=effort,
        status="RUNNING",
        parent_agent_id=worker_id,
    )

    prompt = f"""
You are performing a narrow Manager review for a Nexus Worker.

REVIEW ONLY.

Do not:
- modify files;
- commit;
- publish;
- integrate;
- create Workers.

Original request:

{original_task}

Assigned Worker scope:

{worker_scope}

Review the actual Worker worktree.

You MUST inspect:
- git status;
- git diff;
- changed files;
- scope compliance;
- relevant validation evidence.

Run safe read-only validation when useful.

Determine whether the Worker result should be accepted.

Verdicts:

PASS
The implementation is correct, within scope, and sufficiently validated.

RETRY
The failure appears transient or fixable using the same capability tier.

ESCALATE
The implementation failed because the current capability tier appears
insufficient.

BLOCKED
The result cannot safely proceed without external action, missing
requirements, authorization, or a broader replan.

Return exactly:

NEXUS_REVIEW_BEGIN
{{
  "verdict": "PASS|RETRY|ESCALATE|BLOCKED",
  "failure_class": null,
  "summary": "short review conclusion",
  "evidence": [
    "concrete evidence"
  ]
}}
NEXUS_REVIEW_END

For PASS, failure_class must be null.

For non-PASS use one of:
TRANSIENT
TOOL_FAILURE
PROVIDER_FAILURE
VALIDATION_FAILURE
SCOPE_VIOLATION
CAPABILITY_FAILURE
REQUIREMENT_FAILURE
UNKNOWN

Do not place markdown fences around the envelope.
""".strip()

    command = [
        str(codex),
        "exec",
        "--ephemeral",
        "-C",
        worktree,
        "--sandbox",
        "read-only",
        "-c",
        f'model="{model}"',
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-c",
        'windows.sandbox="elevated"',
        prompt,
    ]

    print()
    print("NEXUS → MANAGER REVIEW")
    print("=" * 70)
    print(f"Reviewer : {reviewer_id}")
    print(f"Worker   : {worker_id}")
    print(f"Model    : {model}")
    print(f"Worktree : {worktree}")
    print()

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    output_lines = []

    assert process.stdout is not None

    for line in process.stdout:
        print(line, end="")
        output_lines.append(line)

    exit_code = process.wait()
    output = "".join(output_lines)

    update_agent_execution(
        reviewer_id,
        result=output[-16000:],
    )

    if exit_code != 0:
        update_agent_status(
            reviewer_id,
            "FAILED",
        )

        return {
            "reviewer_id": reviewer_id,
            "status": "FAILED",
            "review": None,
            "exit_code": exit_code,
        }

    try:
        review = _extract_review(output)

    except Exception as error:
        update_agent_status(
            reviewer_id,
            "BLOCKED",
        )

        return {
            "reviewer_id": reviewer_id,
            "status": "BLOCKED",
            "review": None,
            "exit_code": exit_code,
            "error": str(error),
        }

    update_agent_status(
        reviewer_id,
        "COMPLETED",
    )

    return {
        "reviewer_id": reviewer_id,
        "status": "COMPLETED",
        "review": review,
        "exit_code": exit_code,
    }
