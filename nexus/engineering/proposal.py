"""Deterministic, non-executable engineering change proposals."""

from dataclasses import dataclass, fields
from enum import Enum
from hashlib import sha256
import json
from typing import Any

from .context import EngineeringContext
from .errors import EngineeringProposalValidationError
from .validation import validate_proposal


class ProposalStatus(str, Enum):
    CREATED = "CREATED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


def _items(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise EngineeringProposalValidationError(f"{name} must contain non-empty strings")
    return tuple(sorted(set(value)))


@dataclass(frozen=True, slots=True)
class EngineeringProposal:
    proposal_id: str
    context_id: str
    objective: str
    change_summary: str
    affected_files: tuple[str, ...]
    implementation_steps: tuple[str, ...]
    expected_tests: tuple[str, ...]
    rollback_plan: str
    risk_assessment: str
    created_at: str
    status: ProposalStatus = ProposalStatus.CREATED

    def __post_init__(self) -> None:
        for name in ("proposal_id", "context_id", "objective", "change_summary", "rollback_plan", "risk_assessment", "created_at"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise EngineeringProposalValidationError(f"{name} must be a non-empty string")
        for name in ("affected_files", "implementation_steps", "expected_tests"):
            object.__setattr__(self, name, _items(getattr(self, name), name))
        if not isinstance(self.status, ProposalStatus):
            raise EngineeringProposalValidationError("status must be ProposalStatus")
        validate_proposal(self)

    def to_dict(self) -> dict[str, Any]:
        result = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, Enum):
                value = value.value
            elif isinstance(value, tuple):
                value = list(value)
            result[f.name] = value
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class ProposalEngine:
    """Builds proposals from an EngineeringContext; it never edits or executes files."""

    def generate(self, context: EngineeringContext) -> EngineeringProposal:
        if not isinstance(context, EngineeringContext):
            raise EngineeringProposalValidationError("missing context")
        steps = ("Review the affected files and applicable constraints.",
                 "Implement the approved change through the normal task and review gates.",
                 "Run the expected validation checks and inspect their results.")
        tests = context.validation_requirements or ("Run the relevant unit and integration tests.",)
        payload = {"context_id": context.context_id, "objective": context.objective,
                   "affected_files": context.affected_files, "steps": steps, "tests": tests}
        proposal_id = "EP-" + sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12].upper()
        return EngineeringProposal(proposal_id, context.context_id, context.objective,
            f"Address the engineering objective: {context.objective}", context.affected_files,
            steps, tests, "Revert the approved change and restore the prior state if validation fails.",
            "; ".join(context.risks) if context.risks else "Assess regression risk during review.",
            context.created_at)


generate_proposal = ProposalEngine().generate
