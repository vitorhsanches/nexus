"""Run a phase's setup/verify commands against the integrated repository.

Verification never runs in the primary repository: installs and builds
rewrite lockfiles and emit artifacts, and any dirty path that a later
integration also touches blocks that Run (INTEGRATION_DIRTY_OVERLAP).
Instead a persistent detached worktree is reset to the primary HEAD before
every verification. ``git clean -fd`` keeps ignored paths such as
node_modules, so dependency installs stay incremental between phases.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from nexus.process_timeout import kill_process_tree, timeout_from_env
from nexus.program.bootstrap import git


DEFAULT_VERIFY_TIMEOUT_SECONDS = 20 * 60
OUTPUT_TAIL_CHARS = 4000


def verify_worktree_path(repo: str) -> Path:
    repo_path = Path(repo)
    return repo_path.parent / ".nexus-verify" / repo_path.name


def prepare_verify_worktree(
    repo: str, revision: str | None = None
) -> tuple[Path, str]:
    repo_path = Path(repo)
    head = git(["rev-parse", revision or "HEAD"], repo_path).stdout.strip()
    worktree = verify_worktree_path(repo)

    if not (worktree / ".git").exists():
        worktree.parent.mkdir(parents=True, exist_ok=True)
        git(["worktree", "prune"], repo_path)
        git(["worktree", "add", "--detach", str(worktree), head], repo_path)
    else:
        git(["checkout", "--detach", "--force", head], worktree)
        git(["clean", "-fd"], worktree)

    return worktree, head


def verification_env() -> dict[str, str]:
    env = dict(os.environ)
    # Test runners such as vitest/jest enter watch mode without CI set.
    env["CI"] = "true"
    path_key = next((key for key in env if key.upper() == "PATH"), "PATH")
    interpreter_dir = os.path.dirname(sys.executable)
    inherited = env.get(path_key, "")
    env[path_key] = (
        interpreter_dir + os.pathsep + inherited if inherited else interpreter_dir
    )
    return env


def run_command(command: str, cwd: Path, timeout: int | None) -> dict:
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        shell=True,
        env=verification_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    timed_out = False
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_tree(process)
        output, _ = process.communicate()

    exit_code = process.returncode
    return {
        "command": command,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "passed": not timed_out and exit_code == 0,
        "output_tail": (output or "")[-OUTPUT_TAIL_CHARS:],
    }


def run_verification(
    repo: str,
    setup_commands: list[str],
    verify_commands: list[str],
    revision: str | None = None,
) -> dict:
    """Run commands against one explicitly selected integrated revision."""
    worktree, head = prepare_verify_worktree(repo, revision)
    timeout = timeout_from_env(
        "NEXUS_VERIFY_TIMEOUT_SECONDS", DEFAULT_VERIFY_TIMEOUT_SECONDS
    )

    results = []
    for command in [*setup_commands, *verify_commands]:
        print(f"[verify] {command}")
        result = run_command(command, worktree, timeout)
        results.append(result)
        print(f"[verify]   -> {'PASS' if result['passed'] else 'FAIL'}")
        if not result["passed"]:
            break

    return {
        "passed": all(r["passed"] for r in results),
        "head": head,
        "results": results,
    }


def failure_excerpt(verification: dict) -> str:
    failed = next((r for r in verification["results"] if not r["passed"]), None)
    if failed is None:
        return ""
    status = "timed out" if failed["timed_out"] else f"exit code {failed['exit_code']}"
    return (
        f"Command `{failed['command']}` failed ({status}). Output tail:\n"
        f"{failed['output_tail'][-2500:]}"
    )
