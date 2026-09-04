"""Deterministic execution-plan generation for the Nexus Manager Agent.

Takes a Mission, inspects its title and description, and derives an intent
plus a bounded list of TaskDraft objects. Every keyword match is seeded by a
fixed priority order; the description is consumed in a fixed order, so a given
Mission always maps to the same plan (fully reproducible). No external model
calls are made.
"""

import re
import unicodedata

from nexus.manager.models import (
    CAPABILITY_ANALYSIS,
    CAPABILITY_ARCHITECTURE,
    CAPABILITY_CODING,
    ExecutionPlan,
    TaskDraft,
)
from nexus.router.intent import ANALYSIS, EXECUTION, QUESTION, VALID_INTENTS


class MissionError(ValueError):
    """Raised when a Mission is invalid or cannot be planned."""


def build_execution_plan(mission) -> ExecutionPlan:
    """Build a deterministic ExecutionPlan for a Mission.

    The Mission may be a ``nexus.missions.models.Mission`` instance or a dict
    with ``mission_id``, ``title``, and optional ``description`` keys.
    """
    if mission is None:
        raise MissionError("Mission is required to build a plan.")

    if isinstance(mission, dict):
        mission = _mission_from_dict(mission)

    title = mission.title
    if not isinstance(title, str) or not title.strip():
        raise MissionError("Mission title is required to build a plan.")

    description = mission.description if isinstance(mission.description, str) else ""

    text = _combine(title, description)
    intent = _classify(text)

    return ExecutionPlan(
        mission_id=mission.mission_id,
        title=title,
        description=mission.description if mission.description is not None else None,
        intent=intent,
        tasks=_tasks_for_intent(intent),
    )


def _mission_from_dict(mission: dict):
    """Adapt a mission dict into a lightweight object with the needed fields."""
    class _View:
        __slots__ = ("mission_id", "title", "description")

        def __init__(self, mission_id, title, description):
            self.mission_id = mission_id
            self.title = title
            self.description = description

    return _View(
        mission_id=mission.get("mission_id"),
        title=mission.get("title"),
        description=mission.get("description"),
    )


# Fixed priority: coding signals most specific, then architecture, then analysis.
_CODING_TERMS = (
    "fix",
    "implement",
    "create",
    "add",
    "update",
    "modify",
    "refactor",
    "remove",
    "delete",
    "build",
    "write",
    "change",
    "patch",
    "resolve",
    "automate",
    "correct",
    "feature",
    "migrate",
    "integrate",
    "implemente",
    "crie",
    "adicione",
    "modifique",
    "corrija",
    "corrigir",
)

_ARCHITECTURE_TERMS = (
    "architecture",
    "design architecture",
    "architecture proposal",
    "propose an architecture",
    "scalability",
    "mvp",
)

_ANALYSIS_TERMS = (
    "analyze",
    "analyse",
    "analysis",
    "review",
    "assess",
    "audit",
    "evaluate",
    "investigate",
    "requirements",
    "roadmap",
    "risks",
    "risco",
)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _contains(normalized: str, term: str) -> bool:
    return re.search(r"(?<![\w])" + re.escape(term) + r"(?![\w])", normalized) is not None


def _has_coding_signal(normalized: str) -> bool:
    return any(_contains(normalized, term) for term in _CODING_TERMS)


def _has_architecture_signal(normalized: str) -> bool:
    return any(_contains(normalized, term) for term in _ARCHITECTURE_TERMS)


def _has_analysis_signal(normalized: str) -> bool:
    return any(_contains(normalized, term) for term in _ANALYSIS_TERMS)


def _classify(text: str) -> str:
    """Classify a mission into a Nexus intent, most specific priority first."""
    normalized = _normalize(text)

    if _has_coding_signal(normalized):
        return EXECUTION
    if _has_architecture_signal(normalized):
        return ANALYSIS
    if _has_analysis_signal(normalized):
        return ANALYSIS
    if normalized.rstrip().endswith("?"):
        return QUESTION
    return ANALYSIS


# -- deterministic per-intent task generation ---------------------------------


def _tasks_for_intent(intent: str) -> list[TaskDraft]:
    """Return the fixed set of tasks for an intent.

    All returned intents are members of VALID_INTENTS and each task carries an
    explicit required capability so the Capability Router can resolve a
    matching agent for execution.
    """
    if intent == EXECUTION:
        return _coding_tasks()
    if intent == QUESTION:
        return _explanation_tasks()
    return _analysis_tasks()


def _coding_tasks() -> list[TaskDraft]:
    return [
        TaskDraft(
            scope="Analyze requirements and constraints",
            reason="Understand the mission before designing a solution.",
            required_capabilities=[CAPABILITY_ANALYSIS],
            priority="HIGH",
        ),
        TaskDraft(
            scope="Design the implementation architecture",
            reason="Define the structure that the coding task will follow.",
            required_capabilities=[CAPABILITY_ARCHITECTURE],
            priority="HIGH",
        ),
        TaskDraft(
            scope="Implement the solution and verify it",
            reason="Turn the approved design into code and validate it.",
            required_capabilities=[CAPABILITY_CODING],
            priority="HIGH",
        ),
    ]


def _analysis_tasks() -> list[TaskDraft]:
    return [
        TaskDraft(
            scope="Analyze the mission and document findings",
            reason="Establish the baseline analysis for the mission.",
            required_capabilities=[CAPABILITY_ANALYSIS],
        ),
        TaskDraft(
            scope="Propose an architecture recommendation",
            reason="Translate analysis into a concrete architecture direction.",
            required_capabilities=[CAPABILITY_ARCHITECTURE],
        ),
    ]


def _explanation_tasks() -> list[TaskDraft]:
    return [
        TaskDraft(
            scope="Explain the mission intent and implications",
            reason="Provide a clear answer grounded in the mission.",
            required_capabilities=[CAPABILITY_ANALYSIS],
        ),
    ]


def _combine(title: str, description: str) -> str:
    parts = [part for part in (title, description) if part and part.strip()]
    return " ".join(parts)
