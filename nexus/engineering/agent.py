"""Proposal-only controlled engineering agent."""

from dataclasses import dataclass, fields
import json
from hashlib import sha256
from typing import Any

from .change_proposal import ChangeProposal
from .errors import EngineeringProposalValidationError


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EngineeringProposalValidationError(f"{name} must be a non-empty string")
    return value


def _items(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise EngineeringProposalValidationError(f"{name} must contain non-empty strings")
    result = tuple(sorted(set(value)))
    if not result:
        raise EngineeringProposalValidationError(f"missing {name.replace('_', ' ')}")
    return result


@dataclass(frozen=True, slots=True)
class EngineeringExecutionProposal:
    """A deterministic description of a possible execution, never an execution."""

    execution_id: str
    change_id: str
    objective: str
    execution_steps: tuple[str, ...]
    files_to_modify: tuple[str, ...]
    expected_diff: str
    validation_commands: tuple[str, ...]
    rollback_plan: str
    approval_required: bool
    created_at: str

    def __post_init__(self) -> None:
        for name in ("execution_id", "change_id", "objective", "expected_diff",
                     "rollback_plan", "created_at"):
            _text(getattr(self, name), name)
        for name in ("execution_steps", "files_to_modify", "validation_commands"):
            object.__setattr__(self, name, _items(getattr(self, name), name))
        if not isinstance(self.approval_required, bool):
            raise EngineeringProposalValidationError("approval_required must be bool")

    def to_dict(self) -> dict[str, Any]:
        return {field.name: list(value) if isinstance(value, tuple) else value
                for field in fields(self) for value in (getattr(self, field.name),)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class EngineeringAgent:
    """Analyzes change proposals and emits approval-gated execution descriptions only."""

    def prepare_execution(self, change_proposal: ChangeProposal) -> EngineeringExecutionProposal:
        if not isinstance(change_proposal, ChangeProposal):
            raise EngineeringProposalValidationError("missing change proposal")
        steps = ("Review and approve the proposed change.",) + change_proposal.proposed_changes
        payload = {"change_id": change_proposal.change_id, "objective": change_proposal.objective,
                   "steps": steps, "files": change_proposal.target_files,
                   "tests": change_proposal.expected_tests, "rollback": change_proposal.rollback_strategy,
                   "created_at": change_proposal.created_at}
        execution_id = "EE-" + sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12].upper()
        return EngineeringExecutionProposal(
            execution_id, change_proposal.change_id, change_proposal.objective, steps,
            change_proposal.target_files,
            "Apply only the approved proposed changes to the listed files.",
            change_proposal.expected_tests, change_proposal.rollback_strategy, True,
            change_proposal.created_at)


prepare_execution = EngineeringAgent().prepare_execution
