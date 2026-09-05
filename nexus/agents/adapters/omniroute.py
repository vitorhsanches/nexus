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

Adaptive routing (Nexus v2.0-C)
--------------------------------
For a NEW execution with no explicit ``route_override``, ``OmniRouteAdapter``
delegates initial route selection to an injected
``AdaptiveRoutingService`` (runtime telemetry + resource-aware scoring).
When ``route_override`` is present -- the retry/escalation path -- routing
bypasses telemetry completely and continues through the existing v1.9
``validate_route_for_class`` ladder, exactly as before. ``select_route()``
and ``build_command()`` remain deterministic and network-free on their own
for standalone/backward-compatible use.
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
    Both routes use ``low`` effort in V1. This function is deterministic and
    network-free: it is the legacy static policy, used directly when a
    ``route_override`` is present (the retry/escalation path) and available
    standalone for backward compatibility.

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


def build_command(context, script_path=DEFAULT_WORKER_SCRIPT, shell=None, route=None):
    """Build the full PowerShell command list for a task.

    ``route``, when provided, is a pre-resolved ``(model, effort)`` pair
    (e.g. from adaptive routing) that is used verbatim instead of deriving
    the route here via ``select_route()``. When omitted, the legacy static
    ``select_route()`` policy is used directly, preserving standalone,
    network-free, backward-compatible behavior for existing callers/tests.

    The task prompt is passed as the value of the wrapper's ``-Task``
    parameter. The wrapper (``omniroute-worker.ps1``) is solely responsible
    for piping that value through stdin to ``codex exec -``; this adapter
    never talks to Codex directly and never pipes anything to the wrapper's
    own stdin.
    """
    if route is not None:
        model, effort = route
    else:
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
    """Adapter that shells out to the OmniRoute worker wrapper.

    ``routing_service``, when supplied (or lazily constructed on first use),
    is only consulted for NEW executions with no ``route_override``. The
    retry/escalation ``route_override`` path always bypasses it and uses the
    existing v1.9 ``select_route()``/``validate_route_for_class()`` policy,
    exactly as before.
    """

    def __init__(
        self,
        script_path=DEFAULT_WORKER_SCRIPT,
        runner=None,
        shell=None,
        routing_service=None,
    ):
        self.script_path = script_path
        self.shell = shell
        # Injectable for tests; defaults to the real subprocess runner.
        self._runner = runner or self._run_subprocess
        # Injectable for tests. Constructing an AdaptiveRoutingService
        # performs no network I/O; telemetry is only collected lazily
        # inside select_route_for_task(), and only for the adaptive path.
        self.routing_service = routing_service

    def _resolve_routing_service(self):
        if self.routing_service is None:
            # Imported lazily to keep this module importable/network-free
            # even if routing.service ever grows heavier dependencies.
            from nexus.routing.service import AdaptiveRoutingService

            self.routing_service = AdaptiveRoutingService()
        return self.routing_service

    def _select_route(self, context):
        route_override = getattr(context, "route_override", None)

        if route_override is not None:
            # Explicit retry/escalation route: bypass telemetry completely
            # and validate against the approved v1.9 ladder.
            return select_route(context.required_capabilities, route_override)

        service = self._resolve_routing_service()
        decision = service.select_route_for_task(
            required_capabilities=context.required_capabilities,
            execution_policy=context.execution_policy,
        )
        return decision.model, decision.effort

    def run(self, context):
        model, effort = self._select_route(context)
        command = build_command(
            context,
            script_path=self.script_path,
            shell=self.shell,
            route=(model, effort),
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
