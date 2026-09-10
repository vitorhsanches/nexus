"""Read-only intelligence decision history and traceability persistence."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import uuid
from typing import Any

from nexus.registry.database import get_connection
from .errors import IntelligenceValidationError


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntelligenceValidationError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class IntelligenceAuditRecord:
    audit_id: str
    timestamp: str
    repository: str
    scan_reference: str
    findings_count: int
    recommendation_count: int
    mission_id: str | None = None
    status: str = "ANALYZED"

    def __post_init__(self) -> None:
        for value, name in ((self.audit_id, "audit_id"), (self.repository, "repository"),
                            (self.scan_reference, "scan_reference"), (self.status, "status")):
            _text(value, name)
        if not isinstance(self.timestamp, str) or not self.timestamp.strip():
            raise IntelligenceValidationError("timestamp must be a non-empty ISO-8601 string")
        try:
            datetime.fromisoformat(self.timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise IntelligenceValidationError("timestamp must be ISO-8601") from exc
        for value, name in ((self.findings_count, "findings_count"),
                            (self.recommendation_count, "recommendation_count")):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise IntelligenceValidationError(f"{name} must be a non-negative integer")
        if self.mission_id is not None:
            _text(self.mission_id, "mission_id")

    def to_dict(self) -> dict[str, Any]:
        return {"audit_id": self.audit_id, "timestamp": self.timestamp,
                "repository": self.repository, "scan_reference": self.scan_reference,
                "findings_count": self.findings_count,
                "recommendation_count": self.recommendation_count,
                "mission_id": self.mission_id, "status": self.status}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _ensure() -> None:
    with get_connection() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS intelligence_audits (
            audit_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, repository TEXT NOT NULL,
            scan_reference TEXT NOT NULL, findings_count INTEGER NOT NULL,
            recommendation_count INTEGER NOT NULL, mission_id TEXT, status TEXT NOT NULL
        )""")


class IntelligenceAuditRegistry:
    """Small additive registry; it never creates, updates, or executes missions."""
    def record(self, record: IntelligenceAuditRecord) -> IntelligenceAuditRecord:
        if not isinstance(record, IntelligenceAuditRecord):
            raise IntelligenceValidationError("record must be an IntelligenceAuditRecord")
        _ensure()
        with get_connection() as connection:
            connection.execute("INSERT INTO intelligence_audits VALUES (?,?,?,?,?,?,?,?)",
                               tuple(record.to_dict().values()))
        return record

    def list(self) -> list[IntelligenceAuditRecord]:
        _ensure()
        with get_connection() as connection:
            rows = connection.execute("SELECT * FROM intelligence_audits ORDER BY timestamp, audit_id").fetchall()
        return [IntelligenceAuditRecord(**dict(row)) for row in rows]


def record_analysis(repository: str, scan_reference: str, findings_count: int = 0,
                    recommendation_count: int = 0, *, timestamp: str | None = None,
                    audit_id: str | None = None, status: str = "ANALYZED") -> IntelligenceAuditRecord:
    record = IntelligenceAuditRecord(audit_id or "AUDIT-" + uuid.uuid4().hex.upper(),
        timestamp or datetime.now(timezone.utc).isoformat(), repository, scan_reference,
        findings_count, recommendation_count, None, status)
    return IntelligenceAuditRegistry().record(record)


def record_mission_creation(audit: IntelligenceAuditRecord | str, mission_id: str) -> IntelligenceAuditRecord:
    _text(mission_id, "mission_id")
    _ensure()
    with get_connection() as connection:
        audit_id = audit.audit_id if isinstance(audit, IntelligenceAuditRecord) else _text(audit, "audit_id")
        connection.execute("UPDATE intelligence_audits SET mission_id=?, status=? WHERE audit_id=?",
                           (mission_id, "MISSION_CREATED", audit_id))
        row = connection.execute("SELECT * FROM intelligence_audits WHERE audit_id=?", (audit_id,)).fetchone()
    if row is None:
        raise IntelligenceValidationError(f"unknown audit_id: {audit_id!r}")
    return IntelligenceAuditRecord(**dict(row))
