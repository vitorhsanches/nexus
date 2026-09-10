"""Deterministic, non-executable change proposals derived from implementation plans."""

from dataclasses import dataclass, fields
from enum import Enum
from hashlib import sha256
import json
from typing import Any

from .errors import EngineeringProposalValidationError
from .plan import ImplementationPlan


class ChangeStatus(str, Enum):
    CREATED = "CREATED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


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
class ChangeProposal:
    change_id: str
    plan_id: str
    objective: str
    target_files: tuple[str, ...]
    proposed_changes: tuple[str, ...]
    implementation_strategy: str
    expected_tests: tuple[str, ...]
    risk_level: str
    rollback_strategy: str
    approval_required: bool
    created_at: str
    status: ChangeStatus = ChangeStatus.CREATED

    def __post_init__(self) -> None:
        for name in ("change_id", "plan_id", "objective", "implementation_strategy",
                     "risk_level", "rollback_strategy", "created_at"):
            _text(getattr(self, name), name)
        for name in ("target_files", "proposed_changes", "expected_tests"):
            object.__setattr__(self, name, _items(getattr(self, name), name))
        if not isinstance(self.approval_required, bool):
            raise EngineeringProposalValidationError("approval_required must be bool")
        if isinstance(self.status, str):
            try:
                object.__setattr__(self, "status", ChangeStatus(self.status))
            except ValueError as exc:
                raise EngineeringProposalValidationError("status must be ChangeStatus") from exc
        elif not isinstance(self.status, ChangeStatus):
            raise EngineeringProposalValidationError("status must be ChangeStatus")

    def to_dict(self) -> dict[str, Any]:
        result = {}
        for item in fields(self):
            value = getattr(self, item.name)
            result[item.name] = value.value if isinstance(value, Enum) else list(value) if isinstance(value, tuple) else value
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class ChangeProposalGenerator:
    """Creates proposals only; it never edits files, executes code, or commits."""

    def generate(self, plan: ImplementationPlan) -> ChangeProposal:
        if not isinstance(plan, ImplementationPlan):
            raise EngineeringProposalValidationError("missing plan")
        proposed = tuple(plan.ordered_steps)
        tests = tuple(plan.validation_steps) + tuple(plan.acceptance_criteria)
        payload = {"plan_id": plan.plan_id, "objective": plan.objective,
                   "target_files": plan.affected_files, "proposed_changes": proposed,
                   "expected_tests": tests, "strategy": plan.dependencies,
                   "risk": plan.estimated_risk, "rollback": plan.rollback_strategy,
                   "created_at": plan.created_at}
        change_id = "CP-" + sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12].upper()
        return ChangeProposal(change_id, plan.plan_id, plan.objective, plan.affected_files,
            proposed, "Execute the approved plan steps in order after review.", tests,
            plan.estimated_risk, plan.rollback_strategy, True, plan.created_at)


generate_change_proposal = ChangeProposalGenerator().generate
