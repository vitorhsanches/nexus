"""Deterministic, non-executable engineering context models."""

from dataclasses import dataclass, field, fields
from datetime import datetime
from hashlib import sha256
import json
from typing import Any, Mapping

from .errors import EngineeringContextValidationError


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EngineeringContextValidationError(f"{name} must be a non-empty string")
    return value


def _items(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise EngineeringContextValidationError(f"{name} must contain non-empty strings")
    return tuple(sorted(set(value)))


def _timestamp(value: str) -> str:
    _text(value, "created_at")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EngineeringContextValidationError("created_at must be ISO-8601") from exc
    return value


def _normalize(value: Any) -> Any:
    if isinstance(value, EngineeringContext):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(key): _normalize(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (tuple, list)):
        return [_normalize(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class EngineeringContext:
    """Validated context passed between planning stages; never executes work."""

    context_id: str
    mission_id: str
    task_id: str
    objective: str
    affected_files: tuple[str, ...]
    evidence: tuple[str, ...]
    risks: tuple[str, ...] = field(default_factory=tuple)
    constraints: tuple[str, ...] = field(default_factory=tuple)
    validation_requirements: tuple[str, ...] = field(default_factory=tuple)
    created_at: str = ""

    def __post_init__(self) -> None:
        for value, name in ((self.context_id, "context_id"), (self.mission_id, "mission_id"),
                            (self.task_id, "task_id"), (self.objective, "objective")):
            _text(value, name)
        object.__setattr__(self, "affected_files", _items(self.affected_files, "affected_files"))
        object.__setattr__(self, "evidence", _items(self.evidence, "evidence"))
        if not self.evidence:
            raise EngineeringContextValidationError("at least one evidence reference is required")
        for name in ("risks", "constraints", "validation_requirements"):
            object.__setattr__(self, name, _items(getattr(self, name), name))
        _timestamp(self.created_at)

    def to_dict(self) -> dict[str, Any]:
        return _normalize({item.name: getattr(self, item.name) for item in fields(self)})

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @classmethod
    def build(cls, *, mission_id: str, task_id: str, objective: str,
              affected_files: tuple[str, ...], evidence: tuple[str, ...],
              risks: tuple[str, ...] = (), constraints: tuple[str, ...] = (),
              validation_requirements: tuple[str, ...] = (), created_at: str) -> "EngineeringContext":
        payload = dict(mission_id=mission_id, task_id=task_id, objective=objective,
                       affected_files=tuple(sorted(set(affected_files))),
                       evidence=tuple(sorted(set(evidence))), risks=tuple(sorted(set(risks))),
                       constraints=tuple(sorted(set(constraints))),
                       validation_requirements=tuple(sorted(set(validation_requirements))),
                       created_at=created_at)
        context_id = "EC-" + sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12].upper()
        return cls(context_id=context_id, **payload)

