"""Deterministic risk prioritization for read-only intelligence scans."""

from dataclasses import dataclass
from .errors import IntelligenceValidationError
from .models import FindingSeverity, IntelligenceScanResult


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    risk_id: str
    finding_id: str
    severity: FindingSeverity
    score: float
    rationale: str
    evidence: tuple[str, ...]


class RiskEngine:
    _severity_weight = {FindingSeverity.LOW: 25, FindingSeverity.MEDIUM: 50,
                        FindingSeverity.HIGH: 75, FindingSeverity.CRITICAL: 100}

    def assess(self, scan: IntelligenceScanResult) -> tuple[RiskAssessment, ...]:
        if not isinstance(scan, IntelligenceScanResult):
            raise IntelligenceValidationError("scan must be an IntelligenceScanResult")
        assessments = [self._assess_finding(f) for f in scan.findings]
        return tuple(sorted(assessments, key=lambda item: (-item.score, item.finding_id)))

    prioritize = assess

    def _assess_finding(self, finding):
        refs = tuple(sorted(finding.evidence_references))
        modules = {ref.rsplit(":", 1)[0] if ref.rsplit(":", 1)[-1].isdigit() else ref for ref in refs}
        text = (finding.title + " " + finding.description).lower()
        dependency = 1 if any(word in text for word in ("depend", "coupling", "architecture")) else 0
        complexity = 1 if "complex" in text or "large" in text else 0
        module_factor = min(len(modules), 5) / 5
        score = round(min(100.0, self._severity_weight[finding.severity] * (.7 + .3 * finding.confidence)
                          + module_factor * 10 + dependency * 7 + complexity * 8), 2)
        rationale = (f"severity={finding.severity.value}; confidence={finding.confidence:.2f}; "
                     f"affected_modules={len(modules)}; dependency_impact={dependency}; "
                     f"complexity_indicators={complexity}")
        return RiskAssessment("R-" + finding.finding_id, finding.finding_id, finding.severity,
                              score, rationale, refs)


def assess_risks(scan: IntelligenceScanResult) -> tuple[RiskAssessment, ...]:
    return RiskEngine().assess(scan)
