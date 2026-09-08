from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from nexus.routing.capabilities import is_known_capability, is_known_risk_level
from nexus.routing.catalog import is_blocked_model
from nexus.routing.fleet import Modality, NormalizationStatus, RouteKind
from nexus.routing.models import ModelRoute

class QualificationVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    REJECT = "reject"

class QualificationEvidenceType(str, Enum):
    SEMANTIC_REVIEW = "semantic-review"
    HOST_VALIDATION = "host-validation"
    EXACT_IDENTITY_CHECK = "exact-identity-check"
    CAPABILITY_RISK_GATE = "capability-risk-gate"
    FLEET_CLASSIFICATION = "fleet-classification"

class RejectionReason(str, Enum):
    UNRESOLVED = "unresolved"
    POOL = "pool"
    UNKNOWN_ROUTE_KIND = "unknown-route-kind"
    POLICY_BLOCKED = "policy-blocked"
    TERRA = "terra"
    NON_TEXT_MODALITY = "non-text-modality"

_REJECTED_MODALITIES: frozenset[str] = frozenset({Modality.EMBEDDING.value, Modality.IMAGE.value, Modality.AUDIO.value, Modality.RERANK.value, Modality.SAFETY.value})

@dataclass(frozen=True, slots=True)
class QualificationCandidate:
    model_id: str
    provider: str
    execution_path: str
    effort: str
    capability: str
    risk: str
    def as_route(self) -> ModelRoute:
        return ModelRoute(model_id=self.model_id, provider=self.provider, execution_path=self.execution_path, effort=self.effort, capabilities=frozenset({self.capability}), max_risk_level=self.risk, approved=False, enabled=True, experimental=True)

@dataclass(frozen=True, slots=True)
class QualificationEvidence:
    evidence_type: QualificationEvidenceType
    source: str
    passed: bool
    detail: str = ""
    result: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    model_id: str = ""
    provider: str = ""
    execution_path: str = ""
    effort: str = ""
    capability: str = ""
    risk: str = ""
    def binds_to(self, candidate: QualificationCandidate) -> bool:
        return (self.model_id == candidate.model_id and self.provider == candidate.provider and self.execution_path == candidate.execution_path and self.effort == candidate.effort and self.capability == candidate.capability and self.risk == candidate.risk)

def _candidate_rejection_reasons(candidate: QualificationCandidate, *, fleet_modality: str | None = None, route_kind: str | None = None, normalization_status: str | None = None, policy_blocked: bool = False) -> list[RejectionReason]:
    reasons: list[RejectionReason] = []
    if normalization_status is None or route_kind is None:
        if normalization_status is None:
            reasons.append(RejectionReason.UNRESOLVED)
        if route_kind is None:
            reasons.append(RejectionReason.UNKNOWN_ROUTE_KIND)
        return reasons
    if normalization_status == NormalizationStatus.UNRESOLVED.value:
        reasons.append(RejectionReason.UNRESOLVED)
    if candidate.model_id in ("", "unresolved") or candidate.model_id.lower() == "unresolved":
        if RejectionReason.UNRESOLVED not in reasons:
            reasons.append(RejectionReason.UNRESOLVED)
    if candidate.provider in ("", "unknown") or candidate.provider.lower() == "unknown":
        if RejectionReason.UNRESOLVED not in reasons:
            reasons.append(RejectionReason.UNRESOLVED)
    if candidate.effort.lower() == "unresolved":
        if RejectionReason.UNRESOLVED not in reasons:
            reasons.append(RejectionReason.UNRESOLVED)
    if is_blocked_model(candidate.model_id):
        reasons.append(RejectionReason.TERRA)
    if route_kind == RouteKind.POOL.value:
        reasons.append(RejectionReason.POOL)
    if route_kind == RouteKind.UNKNOWN.value:
        reasons.append(RejectionReason.UNKNOWN_ROUTE_KIND)
    if policy_blocked:
        reasons.append(RejectionReason.POLICY_BLOCKED)
    if fleet_modality is not None and fleet_modality in _REJECTED_MODALITIES:
        reasons.append(RejectionReason.NON_TEXT_MODALITY)
    return reasons

def is_candidate_eligible(candidate: QualificationCandidate, *, fleet_modality: str | None = None, route_kind: str | None = None, normalization_status: str | None = None, policy_blocked: bool = False) -> bool:
    return len(_candidate_rejection_reasons(candidate, fleet_modality=fleet_modality, route_kind=route_kind, normalization_status=normalization_status, policy_blocked=policy_blocked)) == 0

_MANDATORY_EVIDENCE_TYPES: frozenset[QualificationEvidenceType] = frozenset({QualificationEvidenceType.EXACT_IDENTITY_CHECK, QualificationEvidenceType.HOST_VALIDATION, QualificationEvidenceType.SEMANTIC_REVIEW})

def _mandatory_evidence_satisfied(candidate: QualificationCandidate, evidence: tuple[QualificationEvidence, ...]) -> tuple[bool, frozenset[QualificationEvidenceType]]:
    by_type: dict[QualificationEvidenceType, list[QualificationEvidence]] = {et: [] for et in _MANDATORY_EVIDENCE_TYPES}
    for item in evidence:
        if item.evidence_type in by_type and item.binds_to(candidate):
            by_type[item.evidence_type].append(item)
    missing: set[QualificationEvidenceType] = set()
    for et, items in by_type.items():
        if not items:
            missing.add(et)
        elif any(not x.passed for x in items):
            missing.add(et)
    return len(missing) == 0, frozenset(missing)

@dataclass(frozen=True, slots=True)
class QualificationRecord:
    candidate: QualificationCandidate
    verdict: QualificationVerdict
    evidence: tuple[QualificationEvidence, ...]
    rejection_reasons: tuple[RejectionReason, ...] = ()
    missing_evidence: frozenset[QualificationEvidenceType] = frozenset()
    @property
    def passed(self) -> bool:
        return self.verdict == QualificationVerdict.PASS

def evaluate_qualification(candidate: QualificationCandidate, evidence: tuple[QualificationEvidence, ...] = (), *, fleet_modality: str | None = None, route_kind: str | None = None, normalization_status: str | None = None, policy_blocked: bool = False) -> QualificationRecord:
    if not is_known_capability(candidate.capability):
        return QualificationRecord(candidate=candidate, verdict=QualificationVerdict.FAIL, evidence=evidence, rejection_reasons=(), missing_evidence=frozenset())
    if not is_known_risk_level(candidate.risk):
        return QualificationRecord(candidate=candidate, verdict=QualificationVerdict.FAIL, evidence=evidence, rejection_reasons=(), missing_evidence=frozenset())
    rejection_reasons = _candidate_rejection_reasons(candidate, fleet_modality=fleet_modality, route_kind=route_kind, normalization_status=normalization_status, policy_blocked=policy_blocked)
    if rejection_reasons:
        return QualificationRecord(candidate=candidate, verdict=QualificationVerdict.REJECT, evidence=evidence, rejection_reasons=tuple(rejection_reasons), missing_evidence=frozenset())
    all_satisfied, missing = _mandatory_evidence_satisfied(candidate, evidence)
    if not all_satisfied:
        return QualificationRecord(candidate=candidate, verdict=QualificationVerdict.FAIL, evidence=evidence, rejection_reasons=(), missing_evidence=missing)
    return QualificationRecord(candidate=candidate, verdict=QualificationVerdict.PASS, evidence=evidence, rejection_reasons=(), missing_evidence=frozenset())
