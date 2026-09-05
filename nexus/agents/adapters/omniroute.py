"""OmniRoute execution adapter for the Real Agent Adapter Layer V1.

Delegates real work to the existing external worker wrapper
(``omniroute-worker.ps1``). OmniRoute is only provider/model/auth/quota/
health infrastructure here: Nexus still owns orchestration, task/agent
lifecycle, and session tracking through ``AgentExecutor``. This adapter is
only responsible for building the command and interpreting the subprocess
result. It never invokes ``codex`` directly: the wrapper itself owns the
stdin-to-``codex exec -`` transport.

Transport boundary (do not collapse these layers):

    Nexus (this adapter)   -> omniroute-worker.ps1 via its ``-Task`` parameter
    omniroute-worker.ps1   -> pipes ``$Task`` through stdin to ``codex exec -``

The task prompt is therefore passed as the value of the wrapper's ``-Task``
CLI parameter, exactly as the wrapper's own contract requires. Nothing is
piped to the wrapper process's stdin from here.
"""

import shutil
import subprocess

from nexus.agents.adapters.base import AdapterResult, ExecutionAdapter
from nexus.policies.escalation import (
    MECHANICAL_CAPABILITIES,
    validate_route_for_class,
)

# Fixed V1 route policy.
# mechanical work -> oc/big-pickle, standard coding -> cc/claude-sonnet-5-low.
# GPT-5.6 Terra is never used, and there is no automatic Opus escalation.
MECHANICAL_MODEL = "oc/big-pickle"
STANDARD_CODING_MODEL = "cc/claude-sonnet-5-low"
DEFAULT_EFFORT = "low"

DEFAULT_WORKER_SCRIPT = (
    r"C:\Users\Vitor Sanches\.codex\skills\multi-agent-development-manager"
    r"\scripts\omniroute-worker.ps1"
)


def select_route(required_capabilities, route_override=None):
    """Return the (model, effort) pair for a task's required capabilities.

    Mechanical capabilities route to ``oc/big-pickle``; everything else is
    treated as standard coding work and routes to ``cc/claude-sonnet-5-low``.
    Both routes use ``low`` effort in V1. This is the single hook that a
    future Sol escalation policy could extend.

    When ``route_override`` is provided (a dict with ``model`` and optional
    ``effort``), it is used verbatim instead of deriving the route from
    capabilities. Callers (the review/retry service) are responsible for
    validating the override against the approved escalation policy before it
    reaches this adapter; this function does not itself re-validate it.
    """
    if route_override is not None:
        route_class = (
            route_override.get("route_class")
            if isinstance(route_override, dict)
            else None
        )
        validated = validate_route_for_class(
            route_class,
            route_override,
        )
        return validated["model"], validated["effort"]
    capabilities = set(required_capabilities or [])
    if capabilities and capabilities.issubset(MECHANICAL_CAPABILITIES):
        return MECHANICAL_MODEL, DEFAULT_EFFORT
    return STANDARD_CODING_MODEL, DEFAULT_EFFORT


def build_prompt(context):
    """Build the multiline task prompt supplied as the wrapper's -Task value."""
    lines = [
        f"Task ID: {context.task_id}",
        f"Task Title: {context.task_title}",
    ]
    if getattr(context, "task_description", None):
        lines.append(f"Task Description: {context.task_description}")
    if context.required_capabilities:
        lines.append(
            "Required capabilities: " + ", ".join(context.required_capabilities)
        )
    if getattr(context, "acceptance_criteria", None):
        lines.append(
            "Acceptance criteria: " + "; ".join(context.acceptance_criteria)
        )
    if context.mission_context:
        lines.append(f"Mission context: {context.mission_context}")
    if context.execution_policy:
        lines.append(f"Execution policy: {context.execution_policy}")
    return "\n".join(lines)


def resolve_shell():
    """Return the PowerShell executable to invoke, preferring ``pwsh``.

    Falls back to Windows PowerShell (``powershell``) when ``pwsh`` is not on
    PATH. Deterministic and independently testable via ``shutil.which``.
    """
    return "pwsh" if shutil.which("pwsh") else "powershell"


def build_command(context, script_path=DEFAULT_WORKER_SCRIPT, shell=None):
    """Build the full PowerShell command list for a task.

    The task prompt is passed as the value of the wrapper's ``-Task``
    parameter. The wrapper (``omniroute-worker.ps1``) is solely responsible
    for piping that value through stdin to ``codex exec -``; this adapter
    never talks to Codex directly and never pipes anything to the wrapper's
    own stdin.
    """
    model, effort = select_route(
        context.required_capabilities, getattr(context, "route_override", None)
    )
    repo = context.workspace_path or "."
    prompt = build_prompt(context)
    shell = shell or resolve_shell()

    command = [
        shell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-Repo",
        str(repo),
        "-Model",
        model,
        "-Effort",
        effort,
        "-Task",
        prompt,
    ]
    return command


class OmniRouteAdapter(ExecutionAdapter):
    """Adapter that shells out to the OmniRoute worker wrapper."""

    def __init__(self, script_path=DEFAULT_WORKER_SCRIPT, runner=None, shell=None):
        self.script_path = script_path
        self.shell = shell
        # Injectable for tests; defaults to the real subprocess runner.
        self._runner = runner or self._run_subprocess

    def run(self, context):
        command = build_command(
            context, script_path=self.script_path, shell=self.shell
        )
        model, _effort = select_route(
            context.required_capabilities, getattr(context, "route_override", None)
        )

        try:
            completed = self._runner(command)
        except Exception as exc:  # noqa: BLE001 - surfaced as adapter failure
            return AdapterResult(
                success=False, output=None, error=str(exc), routed_model=model
            )

        if completed.returncode != 0:
            return AdapterResult(
                success=False,
                output=completed.stdout,
                error=completed.stderr or f"Worker exited with code {completed.returncode}",
                routed_model=model,
            )

        return AdapterResult(
            success=True, output=completed.stdout, error=None, routed_model=model
        )

    @staticmethod
    def _run_subprocess(command):
        # The task prompt already travels as the -Task argument value, so
        # stdin is explicitly closed/empty here: this process must never
        # rely on stdin to deliver the task to the wrapper.
        return subprocess.run(
            command,
            input="",
            capture_output=True,
            text=True,
            check=False,
        )
