"""Deterministic, non-executable implementation plans."""

from dataclasses import dataclass, fields
import json
from typing import Any

from .errors import EngineeringPlanValidationError


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EngineeringPlanValidationError(f"{name} must be a non-empty string")
    return value


def _items(value: Any, name: str, *, preserve_order: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise EngineeringPlanValidationError(f"{name} must contain non-empty strings")
    values = tuple(value)
    if preserve_order:
        return values
    return tuple(sorted(set(values)))


@dataclass(frozen=True, slots=True)
class ImplementationPlan:
    """Validated planning data linked to an EngineeringProposal; never executes work."""

    plan_id: str
    proposal_id: str
    objective: str
    ordered_steps: tuple[str, ...]
    affected_files: tuple[str, ...]
    dependencies: tuple[str, ...]
    validation_steps: tuple[str, ...]
    rollback_strategy: str
    acceptance_criteria: tuple[str, ...]
    estimated_risk: str
    created_at: str

    def __post_init__(self) -> None:
        for name in ("plan_id", "proposal_id", "objective", "rollback_strategy",
                     "estimated_risk", "created_at"):
            _text(getattr(self, name), name)
        object.__setattr__(self, "ordered_steps",
                           _items(self.ordered_steps, "ordered_steps", preserve_order=True))
        object.__setattr__(self, "affected_files", _items(self.affected_files, "affected_files"))
        object.__setattr__(self, "dependencies", _items(self.dependencies, "dependencies"))
        object.__setattr__(self, "validation_steps", _items(self.validation_steps, "validation_steps"))
        object.__setattr__(self, "acceptance_criteria",
                           _items(self.acceptance_criteria, "acceptance_criteria", preserve_order=True))
        if not self.ordered_steps:
            raise EngineeringPlanValidationError("missing ordered steps")
        for name in ("affected_files", "validation_steps", "acceptance_criteria"):
            if not getattr(self, name):
                raise EngineeringPlanValidationError(f"missing {name.replace('_', ' ')}")

    def to_dict(self) -> dict[str, Any]:
        return {f.name: list(getattr(self, f.name)) if isinstance(getattr(self, f.name), tuple)
                else getattr(self, f.name) for f in fields(self)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
