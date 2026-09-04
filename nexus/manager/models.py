"""Models for the Nexus Manager Agent Planning Engine V1."""

from dataclasses import dataclass, field

# Capabilities the Manager can assign to generated tasks.
CAPABILITY_ANALYSIS = "analysis"
CAPABILITY_ARCHITECTURE = "architecture"
CAPABILITY_CODING = "coding"

# Every capability the Manager knows how to plan for (matches the default
# Agent Bootstrap pool so the Capability Router can resolve them).
MANAGER_CAPABILITIES = (
    CAPABILITY_ANALYSIS,
    CAPABILITY_ARCHITECTURE,
    CAPABILITY_CODING,
)


@dataclass(slots=True)
class TaskDraft:
    """A deterministic task the Manager plans for a Mission."""

    scope: str
    reason: str
    required_capabilities: list[str]
    priority: str = "MEDIUM"


@dataclass(slots=True)
class ExecutionPlan:
    """A deterministic execution plan derived from a Mission."""

    mission_id: str
    title: str
    description: str | None
    intent: str
    tasks: list[TaskDraft] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize the plan into the Mission Engine worker-plan format."""
        return {
            "mission_id": self.mission_id,
            "title": self.title,
            "description": self.description,
            "intent": self.intent,
            "workers": [
                {
                    "scope": task.scope,
                    "reason": task.reason,
                    "priority": task.priority,
                    "required_capabilities": list(task.required_capabilities),
                }
                for task in self.tasks
            ],
        }
