"""Opt-in JavaScript dependency readiness for GO missions.

RUN-E6B89A65 blocked because the Worker/Reviewer worktree had no
node_modules: `git worktree add` (nexus/orchestration/integration.py) only
ever materializes git-tracked files, and node_modules is -- correctly --
gitignored. Plain GO missions have never had a dependency-bootstrap step
(unlike the separate Program feature's opt-in `setup_commands`), so a
fresh worktree for an npm-managed project starts with no installed
dependencies and no way to prove its declared test command even runs.

This module provides that bootstrap step, explicitly opt-in and narrowly
scoped:

  detect_js_project()   -- is this an npm-managed project Nexus can safely
                            prepare? (package.json + package-lock.json only;
                            yarn/pnpm projects are not detected, not errored)
  install_dependencies() -- `npm ci --ignore-scripts`, run entirely by the
                            HOST Nexus process (full network access), never
                            inside the Worker/Reviewer Codex sandbox, and
                            only ever in a disposable integration worktree,
                            never the primary repository.
  verify_test_tooling()  -- confirms the project's declared `npm test`
                            binary actually exists in node_modules/.bin
                            afterward -- the exact fact RUN-E6B89A65's
                            Reviewer had to rediscover live, expensively.
  run_baseline_tests()   -- runs `npm test` once against the clean,
                            unmodified worktree to prove the environment
                            itself is healthy before the Worker starts.
  copy_node_modules()    -- propagates the now-installed node_modules into
                            each per-worker sub-worktree (same host,
                            same filesystem, same Node install -- never
                            copied across machines or Node versions).

`--ignore-scripts` is not a tunable: arbitrary repository lifecycle
scripts (preinstall/postinstall/prepare) must never run with host
privileges merely because a GO mission asked for its dependencies.
Verified compatible with FinaSync's own `npm test` (vitest) specifically;
a project whose test command genuinely requires a lifecycle script (e.g.
compiling a native addon) will fail at verify_test_tooling() with a clear
diagnostic rather than silently running untrusted scripts to work around it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from nexus.process_timeout import kill_process_tree, timeout_from_env

DEFAULT_PREP_TIMEOUT_SECONDS = 10 * 60
OUTPUT_TAIL_CHARS = 4000


class JsDependencyPrepError(RuntimeError):
    """Explicit dependency preparation cannot complete safely.

    Always means: do not launch the Worker. The caller
    (nexus.orchestration.integration.run_integrated_plan) wraps this into an
    IntegrationError carrying a build_failure()-shaped structured failure
    before any Codex subprocess is launched for this Run.
    """

    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail


def _subprocess_env() -> dict[str, str]:
    """Host environment for npm: full inherited PATH/network (this runs on
    the host, outside any agent sandbox) plus CI=true so a test runner such
    as vitest never enters interactive watch mode. Mirrors
    nexus.program.verify.verification_env(), the one other place Nexus
    already runs project tooling commands directly on the host.
    """
    env = dict(os.environ)
    env["CI"] = "true"
    path_key = next((key for key in env if key.upper() == "PATH"), "PATH")
    interpreter_dir = os.path.dirname(sys.executable)
    inherited = env.get(path_key, "")
    env[path_key] = (
        interpreter_dir + os.pathsep + inherited if inherited else interpreter_dir
    )
    return env


def detect_js_project(worktree: str) -> dict | None:
    """Return manifest info when ``worktree`` is an npm-managed JS project
    Nexus can safely prepare, else None.

    Only ``package-lock.json`` (npm) is recognized. A yarn.lock or
    pnpm-lock.yaml project is reported as not detected rather than as an
    error -- `npm ci` would not honor another package manager's lockfile,
    and guessing at an equivalent command is out of scope for this
    mechanism. An unrelated, non-JS GO mission is unaffected either way.
    """
    root = Path(worktree)
    package_json = root / "package.json"
    lockfile = root / "package-lock.json"
    if not package_json.is_file() or not lockfile.is_file():
        return None
    try:
        manifest = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as error:
        raise JsDependencyPrepError(
            "JS_MANIFEST_UNREADABLE",
            f"package.json exists at {package_json} but could not be read/parsed: {error}",
        ) from error
    if not isinstance(manifest, dict):
        raise JsDependencyPrepError(
            "JS_MANIFEST_UNREADABLE",
            f"package.json at {package_json} did not parse to a JSON object.",
        )
    return {"package_json": str(package_json), "lockfile": str(lockfile), "manifest": manifest}


def _run(command: list[str], cwd: Path, timeout: int | None) -> dict:
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        shell=(os.name == "nt"),  # Windows needs shell=True to resolve npm.cmd
        env=_subprocess_env(),
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
    return {
        "command": " ".join(command),
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "passed": not timed_out and process.returncode == 0,
        "output_tail": (output or "")[-OUTPUT_TAIL_CHARS:],
    }


def install_dependencies(worktree: str, *, timeout: int | None = None) -> dict:
    """Run ``npm ci --ignore-scripts`` in ``worktree``.

    Runs entirely on the host (full network access), never inside the
    Worker/Reviewer Codex sandbox, and only ever against the disposable
    integration worktree -- never the primary repository.
    ``--ignore-scripts`` is unconditional: no repository-declared lifecycle
    script ever runs with host privileges just because a mission opted
    into dependency preparation. Raises JsDependencyPrepError on any
    non-success outcome; never silently continues on failure, timeout, or
    missing network.
    """
    timeout = (
        timeout
        if timeout is not None
        else timeout_from_env("NEXUS_JS_PREP_TIMEOUT_SECONDS", DEFAULT_PREP_TIMEOUT_SECONDS)
    )
    result = _run(
        ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=Path(worktree),
        timeout=timeout,
    )
    if not result["passed"]:
        reason = "timed out" if result["timed_out"] else f"exit code {result['exit_code']}"
        raise JsDependencyPrepError(
            "JS_DEPENDENCY_INSTALL_FAILED",
            f"`npm ci --ignore-scripts` {reason} in {worktree}.",
            output_tail=result["output_tail"],
        )
    return result


def _declared_test_binary(manifest: dict) -> str | None:
    scripts = manifest.get("scripts")
    test_command = scripts.get("test") if isinstance(scripts, dict) else None
    if not isinstance(test_command, str) or not test_command.strip():
        return None
    return test_command.strip().split()[0]


def verify_test_tooling(worktree: str, manifest: dict) -> dict:
    """Confirm the project's declared ``scripts.test`` binary actually
    exists in node_modules/.bin after installation.

    This is the deterministic version of what RUN-E6B89A65's Reviewer had
    to rediscover live, at real LLM cost, via several failed exec attempts:
    "is the test tool even here?" Raises JsDependencyPrepError when a test
    script is declared but its binary is neither a local project dependency
    (node_modules/.bin) nor resolvable on PATH (e.g. `node` itself, or a
    globally installed tool) -- this is the one hard dependency-readiness
    failure this function can produce. A project with no declared ``test``
    script is not an error (there is simply nothing to verify or run);
    returns a dict noting that.
    """
    binary = _declared_test_binary(manifest)
    if binary is None:
        return {"test_script": None, "binary": None, "binary_found": None}
    bin_name = f"{binary}.cmd" if os.name == "nt" else binary
    local_bin_path = Path(worktree) / "node_modules" / ".bin" / bin_name
    found_locally = local_bin_path.exists()
    found_on_path = shutil.which(binary) is not None
    if not found_locally and not found_on_path:
        raise JsDependencyPrepError(
            "JS_TEST_TOOL_UNAVAILABLE",
            f"Declared test command {binary!r} (package.json scripts.test) "
            f"was not found at {local_bin_path} after dependency "
            "installation, and is not resolvable on PATH either.",
            binary=binary,
        )
    return {
        "test_script": manifest["scripts"]["test"],
        "binary": binary,
        "binary_found": True,
        "binary_source": "node_modules/.bin" if found_locally else "PATH",
    }


def run_baseline_tests(worktree: str, *, timeout: int | None = None) -> dict:
    """Run ``npm test`` once against the freshly-prepared, unmodified
    worktree (primary HEAD) to prove the environment itself is healthy
    before the Worker changes anything.

    Returns the result dict; never raises. A failing baseline test run is
    reported as evidence, not treated as a preparation failure -- whether
    the project's own tests currently pass is not this mechanism's call to
    make. Only an unusable *environment* (missing dependency or tool,
    already caught by verify_test_tooling()) is a hard failure.
    """
    timeout = (
        timeout
        if timeout is not None
        else timeout_from_env("NEXUS_JS_PREP_TIMEOUT_SECONDS", DEFAULT_PREP_TIMEOUT_SECONDS)
    )
    return _run(["npm", "test"], cwd=Path(worktree), timeout=timeout)


def test_status_for(result: dict) -> str:
    """Map a ``run_baseline_tests()`` result dict to the Nexus-wide
    TEST_STATUS vocabulary (PASS / FAIL / BLOCKED).

    Never returns NOT_RUN -- that status belongs to the caller, for the
    case where no test command was executed at all (e.g. dependency
    preparation disabled, or no declared test script).
    """
    if result["timed_out"]:
        return "BLOCKED"
    return "PASS" if result["passed"] else "FAIL"


def format_test_evidence(result: dict) -> str:
    """Render a ``run_baseline_tests()`` result as a labeled evidence block
    safe to inject into a Reviewer prompt as data, not instruction.

    Kept distinct, deliberately, from any review verdict: this describes
    only what actually happened when the declared test command was run on
    the host, independent of what any Reviewer concludes from it.
    """
    status = test_status_for(result)
    return (
        f"TEST_STATUS: {status}\n"
        "Command executed (on the host, before this review -- the "
        "Reviewer does not need to re-run it):"
        f" {result['command']}\n"
        f"Exit code: {result['exit_code']}\n"
        f"Timed out: {result['timed_out']}\n"
        f"Output (tail):\n{result['output_tail']}"
    )


def copy_node_modules(source_worktree: str, target_worktree: str) -> None:
    """Copy node_modules from the prepared integration worktree into a
    freshly created per-worker sub-worktree.

    Both worktrees are on the same host, same filesystem, same Node
    installation -- this never crosses machines, operating systems, or Node
    versions, so there is no ABI/platform compatibility concern to resolve.
    node_modules is git-ignored, so `git worktree add` never materializes it
    on its own; this is the one explicit, intentional exception, applied
    only when dependency preparation was explicitly requested. Never
    touches the primary repository or the source (integration) worktree.
    """
    source = Path(source_worktree) / "node_modules"
    target = Path(target_worktree) / "node_modules"
    if not source.is_dir():
        raise JsDependencyPrepError(
            "JS_DEPENDENCY_PROPAGATION_FAILED",
            f"No node_modules to propagate from {source}.",
        )
    try:
        shutil.copytree(source, target)
    except OSError as error:
        raise JsDependencyPrepError(
            "JS_DEPENDENCY_PROPAGATION_FAILED",
            f"Could not copy node_modules from {source} to {target}: {error}",
        ) from error
