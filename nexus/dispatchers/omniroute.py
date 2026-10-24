import os
import re
import subprocess
import tempfile
from pathlib import Path

from nexus.failure_contract import MESSAGE_LIMIT, sanitize_error_text
from nexus.registry.agents import (
    create_agent,
    update_agent_execution,
    update_agent_status,
)
from nexus.process_env import codex_subprocess_env, with_scoped_git_trust
from nexus.process_timeout import (
    DEFAULT_WORKER_TIMEOUT_SECONDS,
    ProcessWatchdog,
    timeout_from_env,
)
from nexus.runtime_assets import OMNIROUTE_WORKER_SCRIPT

# Markers that indicate a FAILED worker attempt was caused by a transient
# provider/transport problem (proxy admission shedding, rate limiting,
# dropped connections) rather than a real code/logic failure.  Used to let
# nexus/orchestration/progressive.py retry the same capability tier instead
# of hard-failing the run on infrastructure blips.  Intentionally narrow and
# literal (substring match on the last chunk of the merged stdout/stderr
# transcript) rather than parsing structured logs, since the OmniRoute proxy
# emits plain-text reconnect/HTTP-status lines on stdout.
_TRANSIENT_FAILURE_MARKERS = (
    "503",
    "Service Unavailable",
    "chat_admission_busy",
    "admission capacity",
    "429",
    "Too Many Requests",
    "ECONNRESET",
    "ETIMEDOUT",
    "connection reset",
    "Reconnecting...",
)


_QUOTA_EXHAUSTION_MARKERS = (
    "quota exhausted", "quota exceeded", "usage limit exhausted",
    "usage limit reached", "insufficient quota", "capacity exhausted",
)


def _looks_quota_exhausted(diagnostic_output: str) -> bool:
    """Only explicit account-capacity evidence, never a bare 429 or 5xx."""
    tail = diagnostic_output[-4000:].lower()
    return any(marker in tail for marker in _QUOTA_EXHAUSTION_MARKERS)

def _looks_transient(diagnostic_output: str) -> bool:
    """Best-effort check for a transient provider/transport failure.

    Only looks at the tail of the transcript (where the fatal error that
    caused the process to exit non-zero is most likely to be) to avoid a
    stray marker earlier in a long, otherwise-successful transcript from
    causing a false positive.
    """
    if not diagnostic_output:
        return False
    tail = diagnostic_output[-4000:]
    return any(marker in tail for marker in _TRANSIENT_FAILURE_MARKERS)


ADAPTER_PATH = OMNIROUTE_WORKER_SCRIPT


def _extract(pattern: str, output: str) -> str | None:
    match = re.search(pattern, output, re.MULTILINE)

    if not match:
        return None

    return match.group(1).strip()


# Reason persisted for a Worker that never reached the wrapper. The run-level
# outcome is still WORKER_EXECUTION_FAILED; this only explains the Worker.
LAUNCH_FAILED_REASON = "WORKER_LAUNCH_FAILED"

_LAUNCH_MESSAGE_LIMIT = MESSAGE_LIMIT

def _safe_launch_message(error: BaseException) -> str:
    """Single-line, bounded, credential-redacted text for an exception.

    Delegates to the one shared redaction implementation in
    nexus.failure_contract, so launch and run failures use the same rules.
    """
    return sanitize_error_text(str(error), _LAUNCH_MESSAGE_LIMIT) or ""


def _record_launch_failure(
    *,
    agent_id: str,
    phase: str,
    error: BaseException,
    repo: str,
    model: str,
) -> dict:
    """Persist a bounded diagnostic on the agent row and return LAUNCH_FAILED.

    Only the phase, exception type, redacted message and routing identity are
    stored. Environment variables, CODEX_HOME contents and auth files are never
    read or serialized here.
    """
    diagnostic = "\n".join(
        [
            LAUNCH_FAILED_REASON,
            f"phase={phase}",
            f"exception_type={type(error).__name__}",
            f"message={_safe_launch_message(error)}",
            f"agent_id={agent_id}",
            f"worktree={repo}",
            f"model={model}",
            "provider=omniroute",
            "execution_path=OMNIROUTE",
        ]
    )
    update_agent_execution(agent_id, worktree=repo, result=diagnostic)
    update_agent_status(agent_id, "FAILED")

    return {
        "agent_id": agent_id,
        "exit_code": None,
        "status": "LAUNCH_FAILED",
        "reason": LAUNCH_FAILED_REASON,
        "phase": phase,
        "exception_type": type(error).__name__,
        "branch": None,
        "worktree": repo,
        "error": _safe_launch_message(error),
    }


def _persist_unexpected_launch_failure(
    *,
    agent_id: str,
    phase: str,
    error: BaseException,
    repo: str,
    model: str,
) -> None:
    """Best-effort diagnostic for an exception that is re-raised unchanged.

    Failures here must never mask the original exception, which keeps its
    existing propagation to the integration round.
    """
    try:
        _record_launch_failure(
            agent_id=agent_id,
            phase=phase,
            error=error,
            repo=repo,
            model=model,
        )
    except Exception:  # noqa: BLE001 - the original exception must propagate
        pass


def run_omniroute_worker(
    run_id: str,
    repo: str,
    task: str,
    model: str,
    effort: str = "low",
    parent_agent_id: str | None = None,
    sandbox_mode: str = "workspace-write",
    role: str = "Worker",
    route_class: str | None = None,
) -> dict:
    if sandbox_mode not in {"workspace-write", "read-only"}:
        raise ValueError(f"Unsupported Worker sandbox mode: {sandbox_mode!r}")

    agent_id = create_agent(
        run_id=run_id,
        role=role,
        provider="omniroute",
        model=model,
        effort=effort,
        status="RUNNING",
        parent_agent_id=parent_agent_id,
        route_class=route_class,
    )

    # Record the assigned worktree before any launch preparation so a failed
    # launch still shows where the Worker was meant to run.
    update_agent_execution(agent_id, worktree=repo)

    from nexus.dev.service import DevServiceError
    from nexus.workspace.omniroute_oauth import require_connected_route
    try:
        require_connected_route(model)
    except DevServiceError as error:
        return _record_launch_failure(
            agent_id=agent_id, phase="PROVIDER_CHECK", error=error, repo=repo, model=model,
        )

    phase = "WRAPPER_PREP"
    if not ADAPTER_PATH.exists():
        return _record_launch_failure(
            agent_id=agent_id,
            phase=phase,
            error=FileNotFoundError(
                f"OmniRoute Worker adapter not found: {ADAPTER_PATH}"
            ),
            repo=repo,
            model=model,
        )

    try:
        # Phase 4.1H-C: create a unique per-execution result file path.
        # mkstemp() gives a concurrency-safe unique name with no shared state;
        # each ThreadPoolExecutor worker holds its own path.  We close and
        # remove the placeholder immediately so codex creates the file fresh via
        # --output-last-message; absence after process exit → WORKER_RESULT_MISSING.
        tmp_fd, result_file = tempfile.mkstemp(
            prefix="nexus_worker_",
            suffix=".txt",
            text=True,
        )
        os.close(tmp_fd)
        try:
            Path(result_file).unlink()
        except OSError:
            pass  # Already absent or racy removal — fine; codex creates fresh

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
            # Phase 4.1H-C: unique result file path; wrapper passes this to
            # `codex exec --output-last-message <ResultFile>` so the semantic
            # last-message is written to a separate file, isolated from the
            # merged diagnostic transcript on stdout/stderr.
            "-ResultFile",
            result_file,
            # Phase 4.2: structural read-only enforcement for scouts.
            # sandbox_mode="workspace-write" for normal workers (default, unchanged).
            # sandbox_mode="read-only" for scouts.
            # The wrapper wires this to codex exec --sandbox $Sandbox.
            "-Sandbox",
            sandbox_mode,
        ]

        print()
        # Keep this operational banner safe on legacy Windows cp1252 consoles.
        print("NEXUS -> OMNIROUTE WORKER")
        print("=" * 70)
        print(f"Agent : {agent_id}")
        print(f"Model : {model}")
        print(f"Repo  : {repo}")
        print()
    except Exception as error:
        _persist_unexpected_launch_failure(
            agent_id=agent_id,
            phase=phase,
            error=error,
            repo=repo,
            model=model,
        )
        raise

    # ENVIRONMENT_SETUP covers codex_subprocess_env(), including CODEX_HOME
    # creation; PROCESS_START covers the Popen call itself.
    phase = "ENVIRONMENT_SETUP"
    try:
        # Trust only this Worker's assigned worktree for Git.
        launch_env = with_scoped_git_trust(
            codex_subprocess_env(profile="omniroute"), repo
        )
        phase = "PROCESS_START"
        process = subprocess.Popen(
            command,
            env=launch_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, RuntimeError) as error:
        # Clean up result file slot before early return.
        try:
            Path(result_file).unlink(missing_ok=True)
        except OSError:
            pass
        return _record_launch_failure(
            agent_id=agent_id,
            phase=phase,
            error=error,
            repo=repo,
            model=model,
        )
    except Exception as error:
        _persist_unexpected_launch_failure(
            agent_id=agent_id,
            phase=phase,
            error=error,
            repo=repo,
            model=model,
        )
        raise

    # Initialise before try so post-finally code can always reference them.
    diagnostic_output: str = ""
    worker_output: str | None = None
    final_status: str = "FAILED"
    failure_reason: str | None = None
    exit_code: int | None = None
    branch: str | None = None
    worktree: str | None = None

    try:
        output_lines: list[str] = []

        assert process.stdout is not None

        # Keep the transcript for diagnostics and result extraction, but do
        # not mirror an unbounded model/tool log into the web server console.
        watchdog = ProcessWatchdog(
            process,
            timeout_from_env(
                "NEXUS_WORKER_TIMEOUT_SECONDS",
                DEFAULT_WORKER_TIMEOUT_SECONDS,
            ),
        )
        with watchdog:
            for line in process.stdout:
                output_lines.append(line)

            exit_code = process.wait()
        diagnostic_output = "".join(output_lines)

        # Wrapper-emitted metadata is in the diagnostic stream.
        branch = _extract(r"^Branch\s*:\s*(.+)$", diagnostic_output)

        worktree = _extract(
            r"^WORKTREE_PRESERVED=(.+)$",
            diagnostic_output,
        )

        if worktree is None:
            worktree = _extract(
                r"^Worktree\s*:\s*(.+)$",
                diagnostic_output,
            )

        # The wrapper runs codex inside the assigned linked worktree (`repo`)
        # and no longer prints WORKTREE metadata; downstream review and
        # integration need the worktree the Worker actually modified.
        if worktree is None:
            worktree = repo

        # Phase 4.1H-C: result contract.
        #
        # exit_code != 0            → FAILED (existing behaviour, unchanged).
        #
        # exit_code == 0
        #   result file missing     → FAILED / WORKER_RESULT_MISSING.
        #     Codex never wrote the last-message file.  Transport noise /
        #     reconnect spam can cause exit 0 without any model response;
        #     this case is now structurally distinguished from a real result.
        #
        #   result file empty / ws  → FAILED / WORKER_RESULT_EMPTY.
        #     Codex wrote the file but the model produced no usable content.
        #
        #   result file non-empty   → COMPLETED.
        #     Authoritative worker output = file content exclusively.
        #     Transport logs / reconnect spam remain in diagnostic_output
        #     and are never propagated as the worker result to consumers.
        if watchdog.fired:
            final_status = "FAILED"
            failure_reason = "WORKER_TIMEOUT"
        elif exit_code != 0:
            final_status = "FAILED"
            if _looks_quota_exhausted(diagnostic_output):
                failure_reason = "QUOTA_EXHAUSTED"
            elif _looks_transient(diagnostic_output):
                failure_reason = "TRANSIENT_PROVIDER_ERROR"
            else:
                # Phase 4.6: a Codex process exiting nonzero with no
                # transient marker means the agent runtime/tool layer
                # itself broke (e.g. "Failed to create unified exec
                # process") -- never the repository content the Worker
                # was asked to write. Named explicitly so Program
                # correction can fail fast instead of repeating an
                # identical implementation attempt against it.
                failure_reason = "WORKER_PROCESS_FAILURE"
        else:
            result_path = Path(result_file)
            if not result_path.exists():
                final_status = "FAILED"
                failure_reason = "WORKER_RESULT_MISSING"
            else:
                content = result_path.read_text(encoding="utf-8", errors="replace")
                stripped = content.strip()
                if not stripped:
                    final_status = "FAILED"
                    failure_reason = "WORKER_RESULT_EMPTY"
                else:
                    final_status = "COMPLETED"
                    worker_output = stripped

    finally:
        # Remove result file in all outcomes: success, failure, exception.
        # OSError from cleanup is suppressed so any original exception
        # re-raises cleanly.
        try:
            Path(result_file).unlink(missing_ok=True)
        except OSError:
            pass

    # Persist execution metadata.
    # Phase 4.1H-C: store semantic worker output (COMPLETED path) so that
    # dependency-context readers and the UI receive the real last-message
    # content, not the merged diagnostic transcript.  Fall back to the
    # diagnostic tail only for failure inspection.
    # The bounded `result` column stays a tail preview, unchanged, for every
    # existing reader (dependency context fallback, CLI reports, legacy UI).
    # `full_result` is the separate, untruncated copy of the real semantic
    # deliverable -- only set on the COMPLETED path, never for a diagnostic
    # transcript, so a failed/empty Worker never fakes having a full report.
    if worker_output is not None:
        stored_result: str | None = worker_output[-8000:]
        full_result: str | None = worker_output
    elif diagnostic_output:
        stored_result = diagnostic_output[-8000:]
        full_result = None
    else:
        stored_result = None
        full_result = None

    update_agent_execution(
        agent_id=agent_id,
        branch=branch,
        worktree=worktree,
        result=stored_result,
        full_result=full_result,
    )

    update_agent_status(
        agent_id,
        final_status,
    )

    result: dict = {
        "agent_id": agent_id,
        "exit_code": exit_code,
        "status": final_status,
        "branch": branch,
        "worktree": worktree,
        # Phase 4.1H-C: authoritative worker output is the semantic
        # last-message content from --output-last-message file.
        # Downstream consumers (dependency context, sink aggregation)
        # receive this clean semantic content; the diagnostic transcript
        # (transport logs, reconnect spam, wrapper lines) is never
        # surfaced here.
        "output": worker_output,
    }
    if failure_reason is not None:
        result["reason"] = failure_reason
    if final_status != "COMPLETED" and diagnostic_output:
        # Phase 4.6: preserve real, non-secret diagnostic evidence for
        # every failure mode (root-cause loss point closed) -- the merged
        # stdout/stderr transcript never contains codex_subprocess_env()'s
        # values (tokens/auth), only process/tool output text. Bounded and
        # further clipped again by fc.describe_go_error() downstream.
        result["error"] = diagnostic_output[-2000:]
    return result
