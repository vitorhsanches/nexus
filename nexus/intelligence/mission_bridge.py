"""Read-only bridge from intelligence reports to Mission Engine proposals."""

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from .errors import IntelligenceValidationError
from .models import MissionRecommendation
from .report import IntelligenceReport


@dataclass(frozen=True, slots=True)
class IntelligenceMissionProposal:
    """Validated input for the existing Mission Engine flow.

    A proposal is deliberately not a ``Mission``: creating, planning, routing,
    reviewing, recovering, and executing a mission remain existing-engine
    responsibilities.
    """

    proposal_id: str
    title: str
    description: str
    source_findings: tuple[str, ...]
    priority: str
    affected_modules: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]

    def __post_init__(self) -> None:
        for value, name in ((self.proposal_id, "proposal_id"),
                            (self.title, "title"),
                            (self.description, "description"),
                            (self.priority, "priority")):
            if not isinstance(value, str) or not value.strip():
                raise IntelligenceValidationError(f"{name} must be a non-empty string")
        for value, name in ((self.source_findings, "source_findings"),
                            (self.affected_modules, "affected_modules"),
                            (self.acceptance_criteria, "acceptance_criteria")):
            if not isinstance(value, (tuple, list)) or any(
                    not isinstance(item, str) or not item.strip() for item in value):
                raise IntelligenceValidationError(f"{name} must contain non-empty strings")
        object.__setattr__(self, "source_findings", tuple(sorted(set(self.source_findings))))
        object.__setattr__(self, "affected_modules", tuple(sorted(set(self.affected_modules))))
        object.__setattr__(self, "acceptance_criteria", tuple(self.acceptance_criteria))

    def to_dict(self) -> dict[str, Any]:
        return {"acceptance_criteria": list(self.acceptance_criteria),
                "affected_modules": list(self.affected_modules),
                "description": self.description, "priority": self.priority,
                "proposal_id": self.proposal_id,
                "source_findings": list(self.source_findings), "title": self.title}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class IntelligenceMissionBridge:
    """Convert an ``IntelligenceReport`` into non-executable proposals."""

    def proposals_from_report(self, report: IntelligenceReport) -> tuple[IntelligenceMissionProposal, ...]:
        if not isinstance(report, IntelligenceReport):
            raise IntelligenceValidationError("report must be an IntelligenceReport")
        proposals = [self.proposal_from_recommendation(recommendation)
                     for recommendation in report.recommendations]
        return tuple(sorted(proposals, key=lambda proposal: proposal.proposal_id))

    def proposal_from_recommendation(self, recommendation: MissionRecommendation) -> IntelligenceMissionProposal:
        if not isinstance(recommendation, MissionRecommendation):
            raise IntelligenceValidationError("recommendation must be a MissionRecommendation")
        title = recommendation.title or recommendation.summary
        description = recommendation.objective or recommendation.summary
        priority = recommendation.priority or recommendation.severity.value
        criteria = (f"Validate remediation for recommendation {recommendation.recommendation_id}.",)
        identity = "\0".join((recommendation.recommendation_id, title, description,
                               priority, *recommendation.evidence_references,
                               *recommendation.affected_modules))
        proposal_id = "IMP-" + hashlib.sha256(identity.encode()).hexdigest()[:12].upper()
        return IntelligenceMissionProposal(
            proposal_id, title, description, recommendation.evidence_references,
            priority, recommendation.affected_modules, criteria)

    # Short aliases make the bridge convenient without coupling callers to a
    # particular orchestration entry point.
    generate = proposals_from_report
    convert = proposals_from_report

    def materialize_intelligence_mission(
        self,
        proposal: IntelligenceMissionProposal,
        run_id: str,
        project_id: str | None = None,
        *,
        approved: bool = False,
    ):
        """Materialize an approved proposal through the standard Mission/Task APIs.

        This only creates durable work; it never dispatches or executes it.
        The deterministic IDs make accidental retries explicit and safe.
        """
        if not isinstance(proposal, IntelligenceMissionProposal):
            raise IntelligenceValidationError("proposal must be an IntelligenceMissionProposal")
        if not isinstance(run_id, str) or not run_id.strip():
            raise IntelligenceValidationError("run_id must be a non-empty string")
        if project_id is not None and (not isinstance(project_id, str) or not project_id.strip()):
            raise IntelligenceValidationError("project_id must be a non-empty string when provided")
        if approved is not True:
            raise IntelligenceValidationError("proposal must be explicitly approved")

        # Validate all materialization inputs before touching either registry.
        capabilities = ("analysis", "architecture", "coding", "analysis", "review")
        titles = (
            "Analyze affected area",
            "Define implementation plan",
            "Implement bounded change",
            "Run validation",
            "Review result",
        )
        identity = proposal.to_json() + "\0" + run_id + "\0" + (project_id or "")
        digest = hashlib.sha256(identity.encode()).hexdigest()[:12].upper()
        mission_id = "MISSION-IMP-" + digest
        task_ids = [f"TASK-IMP-{digest}-{index}" for index in range(1, 6)]

        from nexus.missions import service as mission_service
        from nexus.tasks import registry as task_registry
        from nexus.missions.persistence import persist_mission_bundle

        existing = mission_service._missions.get(mission_id)
        if existing is not None:
            raise IntelligenceValidationError(
                f"proposal already materialized: {proposal.proposal_id!r}"
            )
        if any(task_id in task_registry._tasks for task_id in task_ids):
            raise IntelligenceValidationError("proposal materialization has conflicting task IDs")

        context = (
            f"Source findings: {', '.join(proposal.source_findings)}. "
            f"Affected modules: {', '.join(proposal.affected_modules)}."
        )
        criteria = list(proposal.acceptance_criteria)
        mission = mission_service.create_mission(
            run_id=run_id, title=proposal.title, description=proposal.description,
            project_id=project_id, mission_id=mission_id, persist=False,
        )
        created = []
        try:
            previous = None
            for index, (title, capability) in enumerate(zip(titles, capabilities), 1):
                task = task_registry.create_task(
                    mission_id=mission.mission_id,
                    task_id=task_ids[index - 1],
                    title=title,
                    description=f"{proposal.description} {context}",
                    priority=proposal.priority,
                    dependencies=[] if previous is None else [previous],
                    execution_policy={"required_capabilities": [capability],
                                      **({"project_id": project_id} if project_id else {})},
                    acceptance_criteria=criteria,
                    persist=False,
                )
                mission.tasks.append(task)
                created.append(task.task_id)
                previous = task.task_id
            persist_mission_bundle(mission)
        except Exception:
            mission_service._missions.pop(mission.mission_id, None)
            for task_id in created:
                task_registry._tasks.pop(task_id, None)
            raise
        return mission


def proposals_from_report(report: IntelligenceReport) -> tuple[IntelligenceMissionProposal, ...]:
    return IntelligenceMissionBridge().proposals_from_report(report)


def materialize_intelligence_mission(proposal, run_id, project_id=None, *, approved=False):
    return IntelligenceMissionBridge().materialize_intelligence_mission(
        proposal, run_id, project_id, approved=approved
    )
