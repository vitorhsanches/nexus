"""Deterministic universal task intake above existing Nexus orchestration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from nexus.authority import AuthorityResult, resolve_authority
from nexus.capabilities.execution import (
    ExecutionCapability,
    resolve_capabilities,
)
from nexus.dispatchers.triage import classify_mission_scope
from nexus.failure_classification import classify_authority
from nexus.orchestration.go import run_go
from nexus.program import acceptance
from nexus.program.runner import start_program


class TaskType(str, Enum):
    CODE_CHANGE = "CODE_CHANGE"
    CODE_ANALYSIS = "CODE_ANALYSIS"
    SOFTWARE_PROJECT = "SOFTWARE_PROJECT"
    DATA_ANALYSIS = "DATA_ANALYSIS"
    FILE_OPERATION = "FILE_OPERATION"
    RESEARCH = "RESEARCH"
    DOCUMENT_TASK = "DOCUMENT_TASK"
    COMMAND_EXECUTION = "COMMAND_EXECUTION"
    GENERAL_ANALYSIS = "GENERAL_ANALYSIS"


class ExecutionMode(str, Enum):
    TASK = "TASK"
    PROGRAM = "PROGRAM"


@dataclass(frozen=True)
class TaskContract:
    goal: str
    task_type: TaskType
    target: str | None
    scope: str
    constraints: tuple[str, ...]
    validation_requirements: tuple[str, ...]
    project_context: str | None
    project_required: bool
    requires_planning: bool
    long_running: bool
    required_capabilities: tuple[str, ...]
    authority_hint: str | None

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "task_type": self.task_type.value,
            "target": self.target,
            "scope": self.scope,
            "constraints": list(self.constraints),
            "validation_requirements": list(self.validation_requirements),
            "project_context": self.project_context,
            "project_required": self.project_required,
            "requires_planning": self.requires_planning,
            "long_running": self.long_running,
            "required_capabilities": list(self.required_capabilities),
            "authority_hint": self.authority_hint,
        }


class UnsupportedTaskError(RuntimeError):
    pass


_TARGET = re.compile(
    r"(?:[\w.-]+[\\/][\w./-]+|\b[\w-]+\.(?:py|js|ts|tsx|json|toml|md|csv|xlsx|yaml|yml)\b)",
    re.IGNORECASE,
)
_PROGRAM_NOUNS = ("project", "application", "app", "system", "produto", "projeto", "sistema")
_PROGRAM_VERBS = (
    "create", "build", "develop", "execute",
    "crie", "criar", "construa", "desenvolva", "execute", "executar",
)
_PROGRAM_SCOPE = (
    "complete", "entire", "from scratch", "from requirements",
    "multi-phase", "roadmap", "completo", "inteiro", "do zero",
    "continue until complete", "ate finalizar", "até finalizar",
)
_CODE_CHANGE = (
    "fix", "change", "update", "modify", "implement", "refactor",
    "corrija", "alterar", "atualize", "implemente",
)
_CODE_ANALYSIS = ("analyze", "analyse", "review", "inspect", "audit", "analise", "revise")
_CODE_CONTEXT = (
    "code", "repository", "repo", "bug", "config", "package-lock",
    ".py", ".js", ".ts", ".tsx", "código", "repositorio", "repositório",
)
_PLANNING = (
    "multiple workstreams", "coupled implementation", "multi-file",
    "architecture migration", "multiple dependent", "roadmap",
)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _lines_with(text: str, markers: tuple[str, ...]) -> tuple[str, ...]:
    parts = re.split(r"[\r\n;]+", text)
    return tuple(
        part.strip()
        for part in parts
        if part.strip() and _contains_any(part.lower(), markers)
    )


def normalize_task(
    request: str,
    *,
    project: str | None = None,
    requirements_path: str | None = None,
    autonomy: str | None = None,
) -> TaskContract:
    goal = (request or "").strip()
    if not goal:
        raise ValueError("Task request is empty.")

    normalized = goal.lower()
    target_match = _TARGET.search(goal)
    target = target_match.group(0) if target_match else requirements_path
    software_project = (
        _contains_any(normalized, _PROGRAM_VERBS)
        and _contains_any(normalized, _PROGRAM_NOUNS)
        and (
            requirements_path is not None
            or _contains_any(normalized, _PROGRAM_SCOPE)
        )
    )

    if software_project:
        task_type = TaskType.SOFTWARE_PROJECT
    elif "research" in normalized or "pesquise" in normalized:
        task_type = TaskType.RESEARCH
    elif _contains_any(normalized, ("data analysis", "analyze data", "dataset", "csv", "spreadsheet", "planilha")):
        task_type = TaskType.DATA_ANALYSIS
    elif _contains_any(normalized, _CODE_ANALYSIS) and _contains_any(normalized, _CODE_CONTEXT):
        task_type = TaskType.CODE_ANALYSIS
    elif _contains_any(normalized, _CODE_CHANGE) and (
        project is not None
        or _contains_any(normalized, _CODE_CONTEXT)
        or target_match is not None
    ):
        task_type = TaskType.CODE_CHANGE
    elif _contains_any(normalized, ("document", "report", "documento", "relatório", "relatorio")):
        task_type = TaskType.DOCUMENT_TASK
    elif _contains_any(normalized, ("rename file", "move file", "copy file", "renomeie", "mova o arquivo")):
        task_type = TaskType.FILE_OPERATION
    elif _contains_any(normalized, ("run command", "execute command", "execute o comando")):
        task_type = TaskType.COMMAND_EXECUTION
    else:
        task_type = TaskType.GENERAL_ANALYSIS

    project_required = task_type in (TaskType.CODE_CHANGE, TaskType.CODE_ANALYSIS)
    long_running = task_type is TaskType.SOFTWARE_PROJECT
    requires_planning = long_running or _contains_any(normalized, _PLANNING)
    capabilities = {
        TaskType.CODE_CHANGE: (
            ExecutionCapability.REPOSITORY_READ.value,
            ExecutionCapability.FILE_READ.value,
            ExecutionCapability.FILE_WRITE.value,
        ),
        TaskType.CODE_ANALYSIS: (
            ExecutionCapability.REPOSITORY_READ.value,
            ExecutionCapability.FILE_READ.value,
        ),
        TaskType.SOFTWARE_PROJECT: (
            ExecutionCapability.REPOSITORY_READ.value,
            ExecutionCapability.FILE_READ.value,
            ExecutionCapability.FILE_WRITE.value,
            ExecutionCapability.SHELL_EXECUTION.value,
            ExecutionCapability.TEST_EXECUTION.value,
            ExecutionCapability.LOCAL_GIT.value,
        ),
        TaskType.DATA_ANALYSIS: (
            ExecutionCapability.FILE_READ.value,
            ExecutionCapability.DATA_ANALYSIS.value,
        ),
        TaskType.FILE_OPERATION: (
            ExecutionCapability.FILE_READ.value,
            ExecutionCapability.FILE_WRITE.value,
        ),
        TaskType.RESEARCH: (ExecutionCapability.WEB_RESEARCH.value,),
        TaskType.DOCUMENT_TASK: (
            ExecutionCapability.FILE_WRITE.value,
            ExecutionCapability.DOCUMENT_GENERATION.value,
        ),
        TaskType.COMMAND_EXECUTION: (ExecutionCapability.SHELL_EXECUTION.value,),
        TaskType.GENERAL_ANALYSIS: (ExecutionCapability.FILE_READ.value,),
    }[task_type]
    if task_type is TaskType.CODE_CHANGE and _contains_any(
        normalized, ("test", "build", "validate", "check", "install")
    ):
        capabilities += (
            ExecutionCapability.SHELL_EXECUTION.value,
            ExecutionCapability.TEST_EXECUTION.value,
        )
    authority_probe = resolve_authority(goal, explicit_autonomy=autonomy)
    if _contains_any(normalized, ("external api", "api externa", "third-party api")):
        capabilities += (ExecutionCapability.EXTERNAL_API.value,)
    if "EXTERNAL_DEPLOYMENT" in authority_probe.requested_actions:
        capabilities += (ExecutionCapability.EXTERNAL_DEPLOYMENT.value,)
    profile = authority_probe.autonomy_profile
    authority = profile.value if profile.value != "DEFAULT" else (
        "WORKSPACE_WRITE"
        if task_type in (TaskType.CODE_CHANGE, TaskType.SOFTWARE_PROJECT, TaskType.FILE_OPERATION)
        else "READ_ONLY"
    )

    return TaskContract(
        goal=goal,
        task_type=task_type,
        target=target,
        scope=classify_mission_scope(goal).scope.value,
        constraints=_lines_with(
            goal, ("do not", "don't", "without", "must not", "não", "nao")
        ),
        validation_requirements=_lines_with(
            goal, ("test", "build", "lint", "validate", "check", "verifique", "teste")
        ),
        project_context=project,
        project_required=project_required,
        requires_planning=requires_planning,
        long_running=long_running,
        required_capabilities=capabilities,
        authority_hint=authority,
    )


def derive_task_acceptance(contract: TaskContract, result: dict) -> list[dict]:
    """Expose acceptance state for a bounded TASK, without gating it.

    A bounded TASK has no deterministic build/test runner the way a
    Program phase does -- run_go's own Manager Review verdict is the only
    real, deterministic evidence available, so it is the one criterion
    marked SATISFIED. Every free-text validation requirement extracted
    from the request (e.g. "run the tests") has no independent verifier
    behind it here and is honestly reported UNVERIFIED rather than
    assumed true. This never changes ``result["status"]`` -- DIRECT and
    DELEGATED execution behavior is unaffected; this is exposure only.
    """
    criteria = [
        {
            "id": "TASK-EXEC",
            "description": "Execution completed and passed Manager Review.",
            "required": True,
            "status": acceptance.SATISFIED,
            "evidence": f"Run {result.get('run_id')} finished with status "
            f"{result.get('status')}.",
        }
    ]
    for index, line in enumerate(contract.validation_requirements, start=1):
        criteria.append(
            {
                "id": f"TASK-REQ-{index}",
                "description": line,
                "required": True,
                "status": acceptance.UNVERIFIED,
                "evidence": "No deterministic verifier exists for a bounded Task "
                "requirement; not independently confirmed.",
            }
        )
    return criteria


def select_execution_mode(contract: TaskContract) -> ExecutionMode:
    return ExecutionMode.PROGRAM if contract.long_running else ExecutionMode.TASK


def _program_name(name: str | None, requirements_path: str | None) -> str:
    if name and name.strip():
        return name.strip()
    if requirements_path:
        path = Path(requirements_path)
        if path.stem.lower() not in ("requirements", "requirement", "spec"):
            return path.stem
        if path.parent.name:
            return path.parent.name
    return "Nexus Program"


def dispatch_request(
    request: str,
    *,
    project: str | None = None,
    requirements_path: str | None = None,
    name: str | None = None,
    workspace_root: Path | None = None,
    autonomy: str | None = None,
) -> dict:
    contract = normalize_task(
        request,
        project=project,
        requirements_path=requirements_path,
        autonomy=autonomy,
    )
    mode = select_execution_mode(contract)
    contract_payload = contract.to_dict()
    contract_payload["execution_mode"] = mode.value
    capability = resolve_capabilities(contract.required_capabilities)
    authority = resolve_authority(contract.goal, explicit_autonomy=autonomy)
    contract_payload["capability_resolution"] = capability.to_dict()
    contract_payload["authority_resolution"] = authority.to_dict()

    if not capability.all_supported:
        return {
            "task_contract": contract_payload,
            "execution_mode": mode.value,
            "result": {
                "status": "UNSUPPORTED_CAPABILITY",
                "failure_class": "UNSUPPORTED_CAPABILITY",
                "missing": list(capability.unsupported),
            },
        }
    if authority.result is not AuthorityResult.AUTHORIZED:
        return {
            "task_contract": contract_payload,
            "execution_mode": mode.value,
            "result": {
                "status": authority.result.value,
                "failure_class": classify_authority(authority.result.value),
                "reason": authority.reason,
            },
        }

    if mode is ExecutionMode.PROGRAM:
        requirements = (
            Path(requirements_path).read_text(encoding="utf-8-sig")
            if requirements_path
            else contract.goal
        )
        result = start_program(
            _program_name(name, requirements_path),
            requirements,
            workspace_root=workspace_root,
            task_contract=contract_payload,
        )
    else:
        if not contract.project_required:
            raise UnsupportedTaskError(
                f"{contract.task_type.value} execution is not available yet."
            )
        result = run_go(
            contract.goal,
            project_query=project,
            planning_required=contract.requires_planning,
            task_type=contract.task_type.value,
            execution_mode=mode.value,
            required_capabilities=capability.required,
            unsupported_capabilities=capability.unsupported,
            authority_result=authority.result.value,
            autonomy_profile=authority.autonomy_profile.value,
        )
        result["acceptance"] = derive_task_acceptance(contract, result)

    return {
        "task_contract": contract_payload,
        "execution_mode": mode.value,
        "result": result,
    }
