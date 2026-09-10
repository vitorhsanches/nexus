"""Serializable architecture intelligence reports."""

from dataclasses import dataclass, fields, is_dataclass
import json
from typing import Any
from .errors import IntelligenceValidationError
from .models import IntelligenceScanResult, MissionRecommendation, RepositorySnapshot
from .risk import RiskAssessment


@dataclass(frozen=True, slots=True)
class IntelligenceReport:
    snapshot: RepositorySnapshot
    scan_timestamp: str
    findings: tuple[Any, ...]
    risks: tuple[RiskAssessment, ...]
    recommendations: tuple[MissionRecommendation, ...]
    health_indicators: dict[str, Any]

    def __post_init__(self):
        if not isinstance(self.snapshot, RepositorySnapshot) or not isinstance(self.scan_timestamp, str):
            raise IntelligenceValidationError("snapshot and scan_timestamp are required")
        if not isinstance(self.findings, tuple) or not isinstance(self.risks, tuple) or not isinstance(self.recommendations, tuple):
            raise IntelligenceValidationError("report collections must be tuples")
        if not isinstance(self.health_indicators, dict):
            raise IntelligenceValidationError("health_indicators must be a dictionary")

    @classmethod
    def from_scan(cls, scan: IntelligenceScanResult, risks, recommendations):
        if not isinstance(scan, IntelligenceScanResult):
            raise IntelligenceValidationError("scan must be an IntelligenceScanResult")
        findings = tuple(scan.findings)
        counts = {severity.value: sum(f.severity.value == severity.value for f in findings)
                  for severity in __import__('nexus.intelligence.models', fromlist=['FindingSeverity']).FindingSeverity}
        return cls(scan.snapshot, scan.completed_at or scan.started_at, findings, tuple(risks),
                   tuple(recommendations), {"finding_count": len(findings), "risk_count": len(risks),
                                            "recommendation_count": len(recommendations), "severity_counts": counts})

    def to_dict(self):
        def norm(value):
            if hasattr(value, "to_dict"): return value.to_dict()
            if isinstance(value, dict): return {k: norm(value[k]) for k in sorted(value)}
            if isinstance(value, (tuple, list)): return [norm(v) for v in value]
            if hasattr(value, "value"): return value.value
            if is_dataclass(value): return {f.name: norm(getattr(value, f.name)) for f in fields(value)}
            return value
        return {"findings": norm(self.findings), "health_indicators": norm(self.health_indicators),
                "recommendations": norm(self.recommendations), "risks": norm(self.risks),
                "scan_timestamp": self.scan_timestamp, "snapshot": norm(self.snapshot)}

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def __repr__(self):
        return f"IntelligenceReport(snapshot={self.snapshot.repository_id!r}, findings={len(self.findings)}, recommendations={len(self.recommendations)})"
