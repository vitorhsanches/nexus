"""Read-only intelligence pipeline service."""

from pathlib import Path
from .analyzer import IntelligenceAnalyzer
from .recommendations import RecommendationEngine
from .report import IntelligenceReport
from .risk import RiskEngine


class IntelligenceService:
    def __init__(self, analyzer_cls=IntelligenceAnalyzer, risk_engine=None, recommendation_engine=None):
        self.analyzer_cls = analyzer_cls
        self.risk_engine = risk_engine or RiskEngine()
        self.recommendation_engine = recommendation_engine or RecommendationEngine()

    def analyze_repository(self, path: str | Path) -> IntelligenceReport:
        scan = self.analyzer_cls(path).analyze()
        risks = self.risk_engine.assess(scan)
        recommendations = self.recommendation_engine.generate(scan, risks)
        return IntelligenceReport.from_scan(scan, risks, recommendations)
