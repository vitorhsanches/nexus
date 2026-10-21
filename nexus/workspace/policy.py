"""Deterministic initial pane policy for the multi-pane workspace (P1).

Pure and network-free: given the task text and the live approved model
rows (nexus.dev.models.list_dev_models()["models"]), return which agent
panes the Maestro proposes and why. Every decision lists the exact signals
that matched so the proposal is auditable and reproducible. No model is
ever called here; adaptive planning is P2.

Rules (policy_version below):

* One implementer pane is always proposed.
* A responsibility gets its own pane only when the text asks for it as a
  separate deliverable (e.g. "escrever testes", "atualizar a documentação").
  "faça os testes passarem" is part of implementing, not a tester pane.
* Frontend and backend work mentioned together split the implementer.
* The proposal is capped at max_auto_panes (configurable, hard cap 4).
* The suggested model is the first free/low-cost approved route; when none
  exists no model is pre-selected (cost_authorization_required=True), so a
  cost-bearing route is only ever chosen explicitly by the operator.
"""

from __future__ import annotations

import os
import re
import unicodedata

POLICY_VERSION = "workspace-policy/1"
DEFAULT_MAX_AUTO_PANES = 3
HARD_MAX_AUTO_PANES = 4
DEFAULT_MAX_OPEN_PANES = 6
HARD_MAX_OPEN_PANES = 8
FREE_COST_STATUSES = frozenset({"free", "low_cost"})

ROLES = {
    "implementer": "Implementação",
    "frontend": "Frontend",
    "backend": "Backend",
    "tester": "Testes",
    "documenter": "Documentação",
}

_FRONTEND = (r"\bfrontend\b", r"\binterface\b", r"\bui\b", r"\bcss\b", r"\btela\b", r"\bhtml\b")
_BACKEND = (r"\bbackend\b", r"\bapi\b", r"\bendpoint\b", r"\bbanco de dados\b", r"\bdatabase\b", r"\bservidor\b")
_TESTER = (
    r"\b(escrev\w*|cri[ae]\w*|adicion\w*|ampli\w*|cobrir com)\s+(novos\s+|mais\s+)?testes\b",
    r"\b(write|add|create)\s+(new\s+)?tests\b",
    r"\bcobertura de testes\b",
    r"\btest coverage\b",
    r"\bsuite de testes\b",
)
_DOCS = (
    r"\bdocumenta(r|cao)\b",
    r"\breadme\b",
    r"\b(update|write)\s+(the\s+)?docs\b",
    r"\bdocumentation\b",
)


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _matches(patterns, text: str) -> list[str]:
    return [pattern for pattern in patterns if re.search(pattern, text)]


def _env_int(name: str, default: int, hard_max: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        value = default
    return max(1, min(value, hard_max))


def max_auto_panes() -> int:
    return _env_int("NEXUS_WORKSPACE_MAX_AUTO_PANES", DEFAULT_MAX_AUTO_PANES, HARD_MAX_AUTO_PANES)


def max_open_panes() -> int:
    return _env_int("NEXUS_WORKSPACE_MAX_PANES", DEFAULT_MAX_OPEN_PANES, HARD_MAX_OPEN_PANES)


def suggest_model(models: list[dict]) -> dict:
    """Pick a suggested route from approved, live, cost-sorted model rows."""
    for row in models:
        if row.get("cost_status") in FREE_COST_STATUSES:
            return {"model": row["id"], "cost_status": row["cost_status"], "cost_authorization_required": False}
    # No free route: never pre-select a cost-bearing one. The operator picks
    # the route explicitly (and re-confirms it for every mission).
    return {"model": None, "cost_status": None, "cost_authorization_required": True}


def propose(task_text: str, models: list[dict], *, limit: int | None = None) -> dict:
    text = _normalize(task_text or "")
    limit = max_auto_panes() if limit is None else max(1, min(limit, HARD_MAX_AUTO_PANES))
    suggestion = suggest_model(models)

    frontend, backend = _matches(_FRONTEND, text), _matches(_BACKEND, text)
    tester, docs = _matches(_TESTER, text), _matches(_DOCS, text)

    candidates: list[tuple[str, list[str]]] = []
    if frontend and backend:
        candidates += [("backend", backend), ("frontend", frontend)]
    else:
        candidates.append(("implementer", ["default: every task gets one implementer"]))
    if tester:
        candidates.append(("tester", tester))
    if docs:
        candidates.append(("documenter", docs))

    panes = [
        {"role": role, "title": ROLES[role], "signals": signals, **suggestion}
        for role, signals in candidates[:limit]
    ]
    dropped = [role for role, _signals in candidates[limit:]]
    complexity = "simple" if len(panes) == 1 else "multi"
    return {
        "policy_version": POLICY_VERSION,
        "complexity": complexity,
        "limit": limit,
        "panes": panes,
        "dropped_by_limit": dropped,
        "auto_start": False,
    }
