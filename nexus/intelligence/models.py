"""Validated, deterministic domain models for Nexus intelligence."""

from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import Enum
import json
from typing import Any, Mapping, Optional

from .errors import IntelligenceValidationError


class FindingSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ScanStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


def _timestamp(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntelligenceValidationError(f"{field_name} must be a non-empty timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntelligenceValidationError(f"{field_name} must be ISO-8601") from exc
    return value


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntelligenceValidationError(f"{field_name} must be a non-empty string")
    return value


def _confidence(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise IntelligenceValidationError("confidence must be between 0 and 1")
    return float(value)


def _evidence(value: tuple[str, ...], required: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise IntelligenceValidationError("evidence_references must contain non-empty strings")
    result = tuple(sorted(set(value)))
    if required and not result:
        raise IntelligenceValidationError("at least one evidence reference is required")
    return result


class _Serializable:
    def to_dict(self) -> dict[str, Any]:
        return _normalize({item.name: getattr(self, item.name) for item in fields(self)})

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _normalize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, _Serializable):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(key): _normalize(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (tuple, list)):
        return [_normalize(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class RepositorySnapshot(_Serializable):
    repository_id: str
    revision: str
    captured_at: str
    file_references: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _required_text(self.repository_id, "repository_id")
        _required_text(self.revision, "revision")
        _timestamp(self.captured_at, "captured_at")
        object.__setattr__(self, "file_references", _evidence(self.file_references, required=False))


@dataclass(frozen=True, slots=True)
class IntelligenceFinding(_Serializable):
    finding_id: str
    title: str
    description: str
    severity: FindingSeverity
    confidence: float
    evidence_references: tuple[str, ...]
    detected_at: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (self.finding_id, self.title, self.description)):
            raise IntelligenceValidationError("finding_id, title, and description are required")
        if not isinstance(self.severity, FindingSeverity):
            raise IntelligenceValidationError("severity must be FindingSeverity")
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "evidence_references", _evidence(self.evidence_references))
        _timestamp(self.detected_at, "detected_at")


@dataclass(frozen=True, slots=True)
class IntelligenceScanResult(_Serializable):
    scan_id: str
    snapshot: RepositorySnapshot
    status: ScanStatus
    findings: tuple[IntelligenceFinding, ...] = field(default_factory=tuple)
    started_at: str = ""
    completed_at: Optional[str] = None

    def __post_init__(self) -> None:
        _required_text(self.scan_id, "scan_id")
        if not isinstance(self.snapshot, RepositorySnapshot):
            raise IntelligenceValidationError("snapshot is required")
        if not isinstance(self.status, ScanStatus):
            raise IntelligenceValidationError("status must be ScanStatus")
        _timestamp(self.started_at, "started_at")
        if self.completed_at is not None:
            _timestamp(self.completed_at, "completed_at")
        if not isinstance(self.findings, (tuple, list)) or not all(isinstance(item, IntelligenceFinding) for item in self.findings):
            raise IntelligenceValidationError("findings must contain IntelligenceFinding values")
        object.__setattr__(self, "findings", tuple(self.findings))
        if self.status in (ScanStatus.COMPLETED, ScanStatus.PARTIAL, ScanStatus.FAILED) and not self.completed_at:
            raise IntelligenceValidationError("terminal scans require completed_at")


@dataclass(frozen=True, slots=True)
class MissionRecommendation(_Serializable):
    recommendation_id: str
    mission_id: str
    summary: str
    severity: FindingSeverity
    confidence: float
    evidence_references: tuple[str, ...]
    created_at: str
    # D46 report vocabulary.  The legacy fields above remain the canonical
    # constructor surface for backwards compatibility.
    title: str = ""
    objective: str = ""
    priority: str = ""
    rationale: str = ""
    affected_modules: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _required_text(self.recommendation_id, "recommendation_id")
        _required_text(self.mission_id, "mission_id")
        _required_text(self.summary, "summary")
        if not isinstance(self.severity, FindingSeverity):
            raise IntelligenceValidationError("severity must be FindingSeverity")
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "evidence_references", _evidence(self.evidence_references))
        _timestamp(self.created_at, "created_at")
        for value, name in ((self.title, "title"), (self.objective, "objective"),
                            (self.priority, "priority"), (self.rationale, "rationale")):
            if value and (not isinstance(value, str) or not value.strip()):
                raise IntelligenceValidationError(f"{name} must be a non-empty string")
        object.__setattr__(self, "affected_modules", _evidence(self.affected_modules, required=False))
