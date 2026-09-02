import re
import subprocess
from pathlib import Path

from nexus.registry.agents import (
    create_agent,
    update_agent_execution,
    update_agent_status,
)


ADAPTER_PATH = (
    Path.home()
    / ".codex"
    / "skills"
    / "multi-agent-development-manager"
    / "scripts"
    / "omniroute-worker.ps1"
)


def _extract(pattern: str, output: str) -> str | None:
    match = re.search(pattern, output, re.MULTILINE)

    if not match:
        return None

    return match.group(1).strip()


def run_omniroute_worker(
    run_id: str,
    repo: str,
    task: str,
    model: str,
    effort: str = "low",
    parent_agent_id: str | None = None,
) -> dict:
    if not ADAPTER_PATH.exists():
        raise FileNotFoundError(
            f"OmniRoute Worker adapter not found: {ADAPTER_PATH}"
        )

    agent_id = create_agent(
        run_id=run_id,
        role="Worker",
        provider="omniroute",
        model=model,
        effort=effort,
        status="RUNNING",
        parent_agent_id=parent_agent_id,
    )

    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ADAPTER_PATH),
        "-Repo",
        repo,
        "-Model",
        model,
        "-Effort",
        effort,
        "-Task",
        task,
    ]

    print()
    print("NEXUS → OMNIROUTE WORKER")
    print("=" * 70)
    print(f"Agent : {agent_id}")
    print(f"Model : {model}")
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

    branch = _extract(r"^Branch\s*:\s*(.+)$", output)

    worktree = _extract(
        r"^WORKTREE_PRESERVED=(.+)$",
        output,
    )

    if worktree is None:
        worktree = _extract(
            r"^Worktree\s*:\s*(.+)$",
            output,
        )

    update_agent_execution(
        agent_id=agent_id,
        branch=branch,
        worktree=worktree,
        result=output[-8000:],
    )

    final_status = "COMPLETED" if exit_code == 0 else "FAILED"

    update_agent_status(
        agent_id,
        final_status,
    )

    return {
        "agent_id": agent_id,
        "exit_code": exit_code,
        "status": final_status,
        "branch": branch,
        "worktree": worktree,
    }
