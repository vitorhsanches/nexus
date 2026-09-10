"""Read-only intelligence domain for Nexus."""

from .errors import IntelligenceValidationError
from .models import (
    FindingSeverity,
    IntelligenceFinding,
    IntelligenceScanResult,
    MissionRecommendation,
    RepositorySnapshot,
    ScanStatus,
)
from .analyzer import IntelligenceAnalyzer, analyze_repository
from .evidence import Evidence, EvidenceEngine, generate_evidence
from .risk import RiskAssessment, RiskEngine, assess_risks
from .recommendations import RecommendationEngine
from .report import IntelligenceReport
from .service import IntelligenceService
from .orchestrator import IntelligenceOrchestrator, IntelligenceOrchestrationResult, analyze_and_prepare_mission
from .audit import IntelligenceAuditRecord, IntelligenceAuditRegistry, record_analysis, record_mission_creation

__all__ = [
    "FindingSeverity", "IntelligenceFinding", "IntelligenceScanResult",
    "IntelligenceValidationError", "MissionRecommendation", "RepositorySnapshot",
    "ScanStatus",
    "IntelligenceAnalyzer", "analyze_repository",
    "Evidence", "EvidenceEngine", "generate_evidence",
    "RiskAssessment", "RiskEngine", "assess_risks",
    "RecommendationEngine", "IntelligenceReport", "IntelligenceService",
    "IntelligenceOrchestrator", "IntelligenceOrchestrationResult", "analyze_and_prepare_mission",
    "IntelligenceAuditRecord", "IntelligenceAuditRegistry", "record_analysis", "record_mission_creation",
]
