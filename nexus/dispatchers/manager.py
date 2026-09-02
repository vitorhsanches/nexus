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


def _extract_plan(output: str) -> dict:
    match = PLAN_PATTERN.search(output)

    if not match:
        raise ValueError(
            "Manager did not return a valid NEXUS plan envelope."
        )

    return json.loads(match.group(1))


def run_manager(
    run_id: str,
    repo: str,
    task: str,
    model: str = "gpt-5.6-luna",
    effort: str = "low",
) -> dict:
    codex = _find_codex()

    manager_id = create_agent(
        run_id=run_id,
        role="Manager",
        provider="codex",
        model=model,
        effort=effort,
        status="RUNNING",
    )

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
    print("NEXUS → MANAGER")
    print("=" * 70)
    print(f"Agent : {manager_id}")
    print(f"Model : {model}")
    print(f"Effort: {effort}")
    print(f"Repo  : {repo}")
    print()

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

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
