"""Architect stage: turn REQUIREMENTS.md into a phased, verifiable spec.

One read-only Codex call reads the requirements from the repository and
returns a NEXUS_SPEC envelope. The spec is validated fail-closed (same
approach as Manager plan validation) because every later stage trusts it:
each phase becomes one ``run_go`` mission, and its verify commands decide
whether the phase is really done.
"""

from __future__ import annotations

import json
import re
import subprocess

from nexus.dispatchers.manager import _find_codex
from nexus.process_env import codex_subprocess_env, with_scoped_git_trust
from nexus.process_timeout import (
    DEFAULT_MANAGER_TIMEOUT_SECONDS,
    ProcessWatchdog,
    timeout_from_env,
)
from nexus.program.acceptance import MAX_CRITERIA, validate_criteria
from nexus.program.bootstrap import REQUIREMENTS_FILE


SPEC_PATTERN = re.compile(
    r"NEXUS_SPEC_BEGIN\s*(\{.*?\})\s*NEXUS_SPEC_END",
    re.DOTALL,
)

ARCHITECT_MODEL = "gpt-5.6-sol"
ARCHITECT_EFFORT = "medium"
MAX_PHASES = 12
MAX_COMMANDS = 8


class ArchitectError(RuntimeError):
    pass


def _string_list(value, field: str, *, allow_empty: bool) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of strings.")
    items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if len(items) != len(value):
        raise ValueError(f"{field} must contain only non-empty strings.")
    if not allow_empty and not items:
        raise ValueError(f"{field} must not be empty.")
    if len(items) > MAX_COMMANDS:
        raise ValueError(f"{field} has more than {MAX_COMMANDS} entries.")
    return items


def validate_spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise ValueError("Spec must be a JSON object.")

    for field in ("summary", "stack", "architecture"):
        if not isinstance(spec.get(field), str) or not spec[field].strip():
            raise ValueError(f"Spec {field} is missing.")
        spec[field] = spec[field].strip()

    spec["setup_commands"] = _string_list(
        spec.get("setup_commands", []), "setup_commands", allow_empty=True
    )
    spec["final_verify_commands"] = _string_list(
        spec.get("final_verify_commands"), "final_verify_commands", allow_empty=False
    )

    seen_criteria_ids: set[str] = set()
    try:
        spec["acceptance_criteria"] = validate_criteria(
            spec.get("acceptance_criteria"),
            field="Spec acceptance_criteria",
            seen_ids=seen_criteria_ids,
            allow_empty=False,
        )
    except ValueError as error:
        raise ValueError(str(error)) from error

    phases = spec.get("phases")
    if not isinstance(phases, list) or not 1 <= len(phases) <= MAX_PHASES:
        raise ValueError(f"Spec phases must be a list of 1..{MAX_PHASES} items.")

    for index, phase in enumerate(phases, start=1):
        if not isinstance(phase, dict):
            raise ValueError(f"Phase {index} is not an object.")
        for field in ("title", "goal", "acceptance_criteria"):
            if not isinstance(phase.get(field), str) or not phase[field].strip():
                raise ValueError(f"Phase {index} {field} is missing.")
            phase[field] = phase[field].strip()
        phase["verify_commands"] = _string_list(
            phase.get("verify_commands"),
            f"Phase {index} verify_commands",
            allow_empty=False,
        )
        try:
            phase["criteria"] = validate_criteria(
                phase.get("criteria", []),
                field=f"Phase {index} criteria",
                seen_ids=seen_criteria_ids,
                allow_empty=True,
            )
        except ValueError as error:
            raise ValueError(str(error)) from error

    return spec


def extract_spec(output: str) -> dict:
    matches = list(SPEC_PATTERN.finditer(output))
    if not matches:
        raise ValueError("Architect did not return a NEXUS_SPEC envelope.")

    errors = []
    # Codex may echo the prompt (which contains an example envelope) first.
    for match in reversed(matches):
        try:
            return validate_spec(json.loads(match.group(1)))
        except (json.JSONDecodeError, ValueError) as error:
            errors.append(str(error))
    raise ValueError("No valid NEXUS_SPEC envelope: " + "; ".join(errors[:3]))


def build_prompt() -> str:
    return f"""
You are the Nexus Architect. Read {REQUIREMENTS_FILE} in the current
repository. It is the complete product request for a brand-new project
(the repository is otherwise empty). Do not modify any files.

Produce an implementation spec that autonomous coding Workers will execute
phase by phase. Each phase is later planned by a Manager into at most 5
parallel Workers, so size every phase so that 1-5 Workers can finish it.

Rules:
- Pick a concrete, conventional stack that satisfies the requirements. If the
  requirements name a stack, use it.
- Phase 1 must scaffold the project (manifests, tooling, test runner, a
  trivial passing test) so every later phase can be verified.
- Every phase must leave the project building and its tests passing.
- Commands run from the repository root in cmd.exe on Windows, must be
  non-interactive, must terminate on their own (never start a dev server or
  watcher), and must not deploy, publish, or push anything.
- setup_commands install dependencies (e.g. "npm install"); they run before
  every verification.
- verify_commands per phase check only what exists after that phase;
  final_verify_commands check the finished product (build + full test suite,
  plus lint/typecheck if the stack has them).
- Order phases so later phases build on earlier ones. At most {MAX_PHASES}.

ACCEPTANCE CRITERIA (structured, not prose):

Besides verify_commands (which only prove the build/test suite passes),
declare structured criteria that prove the actual REQUESTED FEATURES exist
-- a project can build and pass its own tests while a whole requested
feature was never implemented, and structured criteria are what catches
that.

- Top-level "acceptance_criteria": the overall, whole-project requirements
  from {REQUIREMENTS_FILE} that must hold once every phase is done. At
  least one, at most {MAX_CRITERIA}.
- Each phase's "criteria": the subset of those (or phase-local) conditions
  that must already hold once that phase is done. May be empty for a pure
  scaffolding phase, at most {MAX_CRITERIA}.
- Every criterion needs a globally unique "id" (e.g. "AC-1", "P2-AC-1"), a
  short "description", "required" (true/false), and a
  "verification_type" of exactly "command" or "file_exists":
  - "command": exit code 0 proves it. Give the exact command in "command".
    Prefer a narrow, fast command over re-running the whole suite (e.g. a
    single test file or a grep/assert script), since it runs on its own.
  - "file_exists": give a repository-relative "path" that must exist once
    the criterion holds (a generated file, a required module, a config).
  Only declare a criterion you can express this way. Do not invent a
  criterion with no real command or path behind it.

Return exactly one envelope, no markdown fences:

NEXUS_SPEC_BEGIN
{{
  "summary": "one-paragraph product summary",
  "stack": "languages, frameworks, libraries, test tooling",
  "architecture": "markdown: modules, data model, key decisions, folder layout",
  "setup_commands": ["npm install"],
  "final_verify_commands": ["npm run build", "npm test"],
  "acceptance_criteria": [
    {{
      "id": "AC-1",
      "description": "whole-project requirement this proves",
      "verification_type": "command",
      "required": true,
      "command": "npm test -- some.spec.ts"
    }}
  ],
  "phases": [
    {{
      "title": "short phase title",
      "goal": "what this phase implements, concretely",
      "acceptance_criteria": "observable conditions that prove the phase is done",
      "verify_commands": ["npm test"],
      "criteria": [
        {{
          "id": "P1-AC-1",
          "description": "phase-local requirement this proves",
          "verification_type": "file_exists",
          "required": true,
          "path": "src/feature.ts"
        }}
      ]
    }}
  ]
}}
NEXUS_SPEC_END
""".strip()


def run_architect(repo: str, model: str = ARCHITECT_MODEL, effort: str = ARCHITECT_EFFORT) -> dict:
    try:
        codex = _find_codex()
    except (RuntimeError, FileNotFoundError) as error:
        raise ArchitectError(f"Codex not available: {error}") from error

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
        build_prompt(),
    ]

    print()
    print("NEXUS ? ARCHITECT")
    print("=" * 70)
    print(f"Model : {model}")
    print(f"Effort: {effort}")
    print(f"Repo  : {repo}")

    try:
        process = subprocess.Popen(
            command,
            env=with_scoped_git_trust(codex_subprocess_env(), repo),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        raise ArchitectError(f"Architect launch failed: {error}") from error

    lines: list[str] = []
    watchdog = ProcessWatchdog(
        process,
        timeout_from_env(
            "NEXUS_MANAGER_TIMEOUT_SECONDS", DEFAULT_MANAGER_TIMEOUT_SECONDS
        ),
    )
    with watchdog:
        for line in process.stdout:
            lines.append(line)
        exit_code = process.wait()

    output = "".join(lines)
    if watchdog.fired:
        raise ArchitectError("Architect timed out.")
    if exit_code != 0:
        raise ArchitectError(
            f"Architect exited with code {exit_code}: {output[-1500:]}"
        )

    try:
        return extract_spec(output)
    except ValueError as error:
        raise ArchitectError(str(error)) from error


def render_spec_markdown(name: str, spec: dict) -> str:
    parts = [
        f"# {name} — Nexus Spec",
        "",
        "Generated by the Nexus Architect from REQUIREMENTS.md.",
        "",
        "## Summary",
        "",
        spec["summary"],
        "",
        "## Stack",
        "",
        spec["stack"],
        "",
        "## Architecture",
        "",
        spec["architecture"],
        "",
        "## Commands",
        "",
        "Setup: " + (", ".join(f"`{c}`" for c in spec["setup_commands"]) or "none"),
        "",
        "Final verification: "
        + ", ".join(f"`{c}`" for c in spec["final_verify_commands"]),
        "",
        "## Acceptance Criteria",
        "",
        "Whole-project requirements that must hold once every phase is done.",
        "",
    ]
    for criterion in spec["acceptance_criteria"]:
        parts.append(_render_criterion(criterion))
    parts += ["", "## Phases", ""]
    for number, phase in enumerate(spec["phases"], start=1):
        parts += [
            f"### Phase {number}: {phase['title']}",
            "",
            phase["goal"],
            "",
            f"**Acceptance:** {phase['acceptance_criteria']}",
            "",
            "**Verify:** " + ", ".join(f"`{c}`" for c in phase["verify_commands"]),
            "",
        ]
        if phase["criteria"]:
            parts.append("**Criteria:**")
            parts.append("")
            for criterion in phase["criteria"]:
                parts.append(_render_criterion(criterion))
            parts.append("")
    return "\n".join(parts)


def _render_criterion(criterion: dict) -> str:
    proof = (
        f"`{criterion['command']}` exits 0"
        if criterion["verification_type"] == "command"
        else f"`{criterion['path']}` exists"
    )
    required = "required" if criterion["required"] else "optional"
    return f"- **[{criterion['id']}]** ({required}) {criterion['description']} — proof: {proof}"
