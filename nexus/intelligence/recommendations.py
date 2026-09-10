"""Deterministic, read-only mission recommendations from intelligence."""

from hashlib import sha256
from .errors import IntelligenceValidationError
from .models import (FindingSeverity, IntelligenceScanResult, MissionRecommendation)
from .risk import RiskAssessment


class RecommendationEngine:
    def generate(self, scan: IntelligenceScanResult,
                 risks: tuple[RiskAssessment, ...]) -> tuple[MissionRecommendation, ...]:
        if not isinstance(scan, IntelligenceScanResult):
            raise IntelligenceValidationError("scan must be an IntelligenceScanResult")
        if not isinstance(risks, (tuple, list)) or not all(isinstance(r, RiskAssessment) for r in risks):
            raise IntelligenceValidationError("risks must contain RiskAssessment values")
        findings = {f.finding_id: f for f in scan.findings}
        result = []
        for risk in sorted(risks, key=lambda r: (-r.score, r.finding_id)):
            finding = findings[risk.finding_id]
            priority = "CRITICAL" if risk.severity is FindingSeverity.CRITICAL else \
                       "HIGH" if risk.severity is FindingSeverity.HIGH else \
                       "MEDIUM" if risk.severity is FindingSeverity.MEDIUM else "OPTIONAL"
            title = f"Address: {finding.title}"
            objective = f"Reduce the risk represented by {finding.title.lower()}."
            affected = tuple(sorted({ref.rsplit(":", 1)[0] if ref.rsplit(":", 1)[-1].isdigit() else ref
                                     for ref in finding.evidence_references}))
            key = finding.finding_id + "\0" + priority + "\0" + "\0".join(affected)
            rec_id = "MR-" + sha256(key.encode()).hexdigest()[:12]
            result.append(MissionRecommendation(
                rec_id, "INTELLIGENCE-" + finding.finding_id, title, finding.severity,
                finding.confidence, finding.evidence_references, scan.snapshot.captured_at,
                title, objective, priority, risk.rationale, affected))
        return tuple(result)

    recommend = generate
