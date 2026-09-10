"""High-level, non-executing intelligence-to-mission orchestration."""

from dataclasses import dataclass
from pathlib import Path

from .errors import IntelligenceValidationError
from .mission_bridge import IntelligenceMissionBridge, IntelligenceMissionProposal
from .report import IntelligenceReport
from .service import IntelligenceService


@dataclass(frozen=True, slots=True)
class IntelligenceOrchestrationResult:
    """The complete read-only intelligence result and prepared work graph."""

    report: IntelligenceReport
    proposal: IntelligenceMissionProposal
    mission: object
    tasks: tuple[object, ...]


class IntelligenceOrchestrator:
    """Coordinate intelligence, proposal generation, and mission preparation.

    No worker, reviewer, router, or network component is referenced here. The
    existing Mission/Task lifecycle remains the sole materialization boundary.
    """

    def __init__(self, intelligence_service=None, mission_bridge=None):
        self.intelligence_service = intelligence_service or IntelligenceService()
        self.mission_bridge = mission_bridge or IntelligenceMissionBridge()

    def analyze(self, repository_path: str | Path):
        """Run analysis and produce proposals without touching mission state."""
        path = Path(repository_path)
        if not path.is_dir():
            raise IntelligenceValidationError(f"invalid repository path: {path}")
        report = self.intelligence_service.analyze_repository(path)
        if not isinstance(report, IntelligenceReport):
            raise IntelligenceValidationError("intelligence service returned an invalid report")
        return report, self.mission_bridge.proposals_from_report(report)

    def analyze_and_prepare_mission(self, repository_path: str | Path, run_id: str):
        if not isinstance(run_id, str) or not run_id.strip():
            raise IntelligenceValidationError("run_id must be a non-empty string")
        # All validation and recommendation conversion happen before the
        # bridge is allowed to touch the Mission or Task registries.
        report, proposals = self.analyze(repository_path)
        if len(proposals) != 1:
            raise IntelligenceValidationError(
                "exactly one recommendation is required to prepare a mission"
            )
        proposal = proposals[0]
        mission = self.mission_bridge.materialize_intelligence_mission(
            proposal, run_id, approved=True
        )
        return IntelligenceOrchestrationResult(
            report=report, proposal=proposal, mission=mission,
            tasks=tuple(mission.tasks),
        )


def analyze_and_prepare_mission(repository_path: str | Path, run_id: str):
    """Convenience entry point for :class:`IntelligenceOrchestrator`."""
    return IntelligenceOrchestrator().analyze_and_prepare_mission(repository_path, run_id)
