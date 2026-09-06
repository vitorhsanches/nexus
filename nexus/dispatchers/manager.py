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


PLAN_PATTERN = re.compile(
    r"NEXUS_PLAN_BEGIN\s*(\{.*?\})\s*NEXUS_PLAN_END",
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


def _validate_plan(plan: dict) -> dict:
    allowed_complexity = {"LOW", "MEDIUM", "HIGH"}
    allowed_risk = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}

    allowed_routes = {
        "mechanical",
        "standard-coding",
        "complex-coding",
        "review-critical",
        "security-critical",
    }

    allowed_paths = {
        "OMNIROUTE",
        "NATIVE_CODEX",
    }

    allowed_efforts = {
        "low",
        "medium",
        "high",
    }

    if plan.get("complexity") not in allowed_complexity:
        raise ValueError(
            f"Invalid complexity: {plan.get('complexity')!r}"
        )

    if plan.get("risk") not in allowed_risk:
        raise ValueError(
            f"Invalid risk: {plan.get('risk')!r}"
        )

    parallelism = plan.get("parallelism")

    if (
        not isinstance(parallelism, int)
        or isinstance(parallelism, bool)
        or not 1 <= parallelism <= 3
    ):
        raise ValueError(
            f"Invalid parallelism: {parallelism!r}"
        )

    summary = plan.get("summary")

    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Plan summary is missing.")

    allowed_intents = {"EXECUTION", "ANALYSIS", "QUESTION", "PLANNING"}

    intent = plan.get("intent")

    if intent not in allowed_intents:
        raise ValueError(
            f"Invalid intent: {intent!r}"
        )

    workers = plan.get("workers")

    if not isinstance(workers, list):
        raise ValueError("Plan workers must be a list.")

    if intent == "EXECUTION" and not workers:
        raise ValueError(
            "EXECUTION plan must contain at least one Worker."
        )

    if intent == "QUESTION" and workers:
        raise ValueError(
            "QUESTION plan must not contain workers."
        )

    for index, worker in enumerate(workers, start=1):
        if not isinstance(worker, dict):
            raise ValueError(
                f"Worker {index} is not an object."
            )

        route_class = worker.get("route_class")

        if route_class not in allowed_routes:
            raise ValueError(
                f"Worker {index} has invalid route_class: "
                f"{route_class!r}"
            )

        execution_path = worker.get("execution_path")

        if execution_path not in allowed_paths:
            raise ValueError(
                f"Worker {index} has invalid execution_path: "
                f"{execution_path!r}"
            )

        effort = worker.get("effort")

        if effort not in allowed_efforts:
            raise ValueError(
                f"Worker {index} has invalid effort: "
                f"{effort!r}"
            )

        provider = worker.get("provider")
        model = worker.get("model")
        scope = worker.get("scope")
        reason = worker.get("reason")

        for field_name, value in (
            ("provider", provider),
            ("model", model),
            ("scope", scope),
            ("reason", reason),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"Worker {index} has invalid {field_name}."
                )

        # Prevent prompt-template placeholders from being accepted.
        if "|" in model:
            raise ValueError(
                f"Worker {index} model looks like a template: "
                f"{model!r}"
            )

    return plan


def _extract_plan(output: str) -> dict:
    matches = list(PLAN_PATTERN.finditer(output))

    if not matches:
        raise ValueError(
            "Manager did not return a NEXUS plan envelope."
        )

    errors = []

    # Codex may echo the prompt before printing the Manager response.
    # Always inspect envelopes from newest to oldest.
    for match in reversed(matches):
        try:
            plan = json.loads(match.group(1))
            return _validate_plan(plan)
        except (json.JSONDecodeError, ValueError) as error:
            errors.append(str(error))

    details = "; ".join(errors[:3])

    raise ValueError(
        "Manager returned plan envelopes, but none passed validation. "
        f"Errors: {details}"
    )


def run_manager(
    run_id: str,
    repo: str,
    task: str,
    model: str = "gpt-5.6-luna",
    effort: str = "low",
) -> dict:
    manager_id = create_agent(
        run_id=run_id,
        role="Manager",
        provider="codex",
        model=model,
        effort=effort,
        status="RUNNING",
    )

    try:
        codex = _find_codex()
    except (RuntimeError, FileNotFoundError) as error:
        update_agent_status(manager_id, "FAILED")

        return {
            "manager_id": manager_id,
            "status": "LAUNCH_FAILED",
            "exit_code": None,
            "plan": None,
            "error": str(error),
        }

    prompt = f"""
Use $multi-agent-development-manager.

You are the planning Manager for a Nexus orchestration run.

This invocation is PLANNING ONLY.

Do NOT:
- modify repository files;
- create Workers;
- create threads;
- commit;
- publish;
- apply migrations.

Audit only the repository context necessary to classify and decompose the
request.

Use the CURRENT multi-agent-development-manager Skill as the routing source of
truth.

The Nexus execution layer will create Workers after receiving your plan.

User request:

{task}

Return exactly one machine-readable planning envelope at the end of your
response.

Required format:

NEXUS_PLAN_BEGIN
{{
  "complexity": "LOW|MEDIUM|HIGH",
  "risk": "LOW|MEDIUM|HIGH|CRITICAL",
  "parallelism": 1,
  "summary": "short summary",
  "intent": "EXECUTION|ANALYSIS|QUESTION|PLANNING",
  "workers": [
    {{
      "route_class": "mechanical|standard-coding|complex-coding|review-critical|security-critical",
      "execution_path": "OMNIROUTE|NATIVE_CODEX",
      "provider": "provider name",
      "model": "exact model",
      "effort": "low|medium|high",
      "scope": "bounded worker scope",
      "reason": "routing justification"
    }}
  ]
}}
NEXUS_PLAN_END

Do not place markdown fences around the JSON envelope.
""".strip()

    command = [
        str(codex),
        "exec",
        "--ephemeral",
        "-C",
        repo,
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
    print("NEXUS ? MANAGER")
    print("=" * 70)
    print(f"Agent : {manager_id}")
    print(f"Model : {model}")
    print(f"Effort: {effort}")
    print(f"Repo  : {repo}")
    print()

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        update_agent_status(manager_id, "FAILED")

        return {
            "manager_id": manager_id,
            "status": "LAUNCH_FAILED",
            "exit_code": None,
            "plan": None,
            "error": str(error),
        }

    output_lines: list[str] = []

    assert process.stdout is not None

    for line in process.stdout:
        print(line, end="")
        output_lines.append(line)

    exit_code = process.wait()
    output = "".join(output_lines)

    update_agent_execution(
        agent_id=manager_id,
        result=output[-16000:],
    )

    if exit_code != 0:
        update_agent_status(manager_id, "FAILED")

        return {
            "manager_id": manager_id,
            "status": "FAILED",
            "exit_code": exit_code,
            "plan": None,
        }

    try:
        plan = _extract_plan(output)
    except Exception as error:
        update_agent_status(manager_id, "BLOCKED")

        return {
            "manager_id": manager_id,
            "status": "BLOCKED",
            "exit_code": exit_code,
            "plan": None,
            "error": str(error),
        }

    update_agent_status(manager_id, "COMPLETED")

    return {
        "manager_id": manager_id,
        "status": "COMPLETED",
        "exit_code": exit_code,
        "plan": plan,
    }

