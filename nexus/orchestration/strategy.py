"""Deterministic choice between the existing direct and delegated flows."""

from __future__ import annotations

import re
import unicodedata
from enum import Enum


class ExecutionStrategy(str, Enum):
    DIRECT = "DIRECT"
    DELEGATED = "DELEGATED"


_BOUNDED_MARKERS = (
    "metadata", "config", "configuration", "version", "rename", "format",
    "typo", "documentation", "readme", "mechanical", "dependency",
    "dependencies", "lockfile", "manifest", "package.json",
    "package-lock.json",
)
_NEGATED_HIGH_RISK_ACTIONS = (
    "no commit/push", "do not commit/push",
    "no commit or push", "do not commit or push",
    "no commit and push", "do not commit and push",
    "no commit", "no push", "do not commit", "do not push",
    "without commit", "without push", "sem commit", "sem push",
    "n?o fa?a commit ou push", "n?o fa?a commit e push",
    "n?o fa?a commit", "n?o fa?a push",
    "n?o fazer commit ou push", "n?o fazer commit e push",
    "n?o fazer commit", "n?o fazer push",
    "nao faca commit ou push", "nao faca commit e push",
    "nao faca commit", "nao faca push",
    "nao fazer commit ou push", "nao fazer commit e push",
    "nao fazer commit", "nao fazer push",
)
_DECOMPOSITION_MARKERS = (
    "multiple independent deliverables",
    "multiple independent workstreams",
    "multiple workstreams",
    "coupled implementation workstreams",
    "coupled implementation changes",
)
_HIGH_RISK_MARKERS = (
    "delete", "drop ", "publish", "push", "security", "credential",
    "database", "production", "migration", "permission",
)
_TARGET_PATTERN = re.compile(
    r"(?:[\w.-]+[\\/][\w./-]+|\b[\w-]+\.(?:py|js|ts|tsx|json|toml|md|yaml|yml)\b)",
    re.IGNORECASE,
)


def infer_pre_execution_risk(task_text: str) -> str:
    """Use the existing narrow action guard before Manager risk is available."""

    risk_text = unicodedata.normalize("NFKD", task_text or "")
    risk_text = "".join(
        char for char in risk_text if not unicodedata.combining(char)
    ).lower()
    for negated_action in _NEGATED_HIGH_RISK_ACTIONS:
        risk_text = risk_text.replace(negated_action, "")
    if any(marker in risk_text for marker in _HIGH_RISK_MARKERS):
        return "HIGH"
    return "LOW"


def choose_execution_strategy(
    task_text: str,
    *,
    complexity: str = "LOW",
    risk: str | None = None,
    planning_required: bool = False,
) -> tuple[ExecutionStrategy, str]:
    """Choose conservatively without an LLM or policy bypass."""

    normalized = (task_text or "").lower()
    inferred_risk = infer_pre_execution_risk(task_text)
    provided_risk = (risk or "").upper()
    low_complexity = (complexity or "").upper() == "LOW"
    low_risk = inferred_risk == "LOW" and provided_risk in ("", "LOW")
    planning_required = planning_required or any(
        marker in normalized for marker in _DECOMPOSITION_MARKERS
    )
    bounded = bool(_TARGET_PATTERN.search(task_text or "")) and any(
        marker in normalized for marker in _BOUNDED_MARKERS
    )
    if low_complexity and low_risk and not planning_required and bounded:
        return ExecutionStrategy.DIRECT, "BOUNDED_LOW_COMPLEXITY"
    return ExecutionStrategy.DELEGATED, "REQUIRES_DECOMPOSITION"


def direct_worker_for(task_text: str) -> dict:
    """Build the minimal advisory Worker shape for the existing executor."""

    return {
        "scope": task_text.strip(),
        "reason": "Direct execution for a bounded low-risk task.",
        "route_class": "mechanical",
        "execution_path": "OMNIROUTE",
        "effort": "low",
        "authority": "WORKSPACE_WRITE",
        "deliverable": task_text.strip(),
        "acceptance_criteria": "Requested bounded change is applied and validated.",
        "depends_on": [],
    }
