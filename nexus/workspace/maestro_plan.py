"""Maestro planning contract (P2A): prompt envelope and strict plan validation.

Pure and network-free. The Maestro LLM answers in plain language and
appends one JSON plan between PLAN_BEGIN/PLAN_END markers. Nothing an LLM
writes is ever executed directly: the JSON is parsed and validated against
this schema, every model must be an approved live Development route, and
any deviation raises PlanValidationError so the caller can fall back to the
deterministic policy -- clearly labelled as such.

Plan shape (stored as workspace_plans.proposal_json, schema maestro-plan/1):

    {
      "schema_version": "maestro-plan/1",
      "source": "llm" | "deterministic" | "deterministic-fallback",
      "complexity": "simple" | "multi",
      "summary": str,
      "explanation": str,               # plain language, shown to the user
      "needs_clarification": str | None, # Maestro asks instead of planning
      "agents": [{
          "key", "role", "title", "backend", "model", "effort", "cost_status", "task",
          "acceptance_criteria": [str], "depends_on": [key], "touches": [str],
          "independent": bool, "conflicts_with": [key]
      }],
      "execution": {"mode": "sequential", "order": [key]},
      "limit": int,
      "fallback_reason": str | None
    }

P2A never executes agents in parallel: "execution.mode" is always
"sequential" and the order respects depends_on. "independent" only records
the analysis (no dependency and no overlapping "touches") for P2B.
"""

from __future__ import annotations

import json
import re

from nexus.workspace import policy

SCHEMA_VERSION = "maestro-plan/1"
PLAN_BEGIN = "NEXUS_MAESTRO_PLAN_BEGIN"
PLAN_END = "NEXUS_MAESTRO_PLAN_END"
MAX_RESPONSE_CHARS = 60_000
MAX_EXPLANATION_CHARS = 4_000
_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
_TOP_KEYS = {"complexity", "summary", "agents", "needs_clarification"}
_AGENT_KEYS = {"key", "role", "title", "backend", "model", "effort", "task", "acceptance_criteria", "depends_on", "touches"}

ROLE_GUIDE = {
    "implementer": "implements a self-contained change end to end (default for most tasks)",
    "backend": "server-side/API/data work when it is clearly separate from UI work",
    "frontend": "UI/HTML/CSS/JS work when it is clearly separate from server work",
    "tester": "writes or extends tests as a separate deliverable (not just 'make tests pass')",
    "documenter": "writes or updates documentation as a separate deliverable",
}


class PlanValidationError(ValueError):
    """The Maestro's answer cannot be trusted as a plan."""


def build_prompt(*, user_text: str, project: dict, models: list[dict], panes: list[dict], limit: int, previous_summary: str | None) -> str:
    model_lines = "\n".join(
        f"- {row.get('backend', 'omniroute')}:{row['id']} (cost: {row.get('cost_status') or 'unknown'}; "
        f"efforts: {', '.join(row.get('supported_efforts') or []) or 'model default'})"
        for row in models
    ) or "- (none available)"
    role_lines = "\n".join(f"- {role}: {text}" for role, text in ROLE_GUIDE.items())
    pane_lines = "\n".join(f"- {pane['title']} (role {pane['role']}, model {pane['model'] or 'none'})" for pane in panes) or "- (none)"
    previous = f"\nYour previous plan summary in this conversation: {previous_summary}\n" if previous_summary else ""
    return f"""You are the Nexus Maestro for project "{project['name']}" (id {project['id']}).
You are in PLANNING MODE: you may read the repository to understand it, but you must not create, modify or delete any file, and you must not run commands that change anything.

Decide how many agents this request really needs (at most {limit}). Rules:
- A simple, self-contained task gets exactly ONE agent ("complexity": "simple").
- Split work only into genuinely separate responsibilities. Agents run one after another in isolated worktrees; never assume two agents can edit the same files at the same time. If two agents would touch the same files, make the later one depend on the earlier one.
- Choose each agent's backend, model and effort ONLY from this live approved list. Preserve the backend prefix exactly; never invent a model or switch to an unavailable backend. Prefer the cheapest suitable route:
{model_lines}
- Roles:
{role_lines}
- Give every agent concrete acceptance criteria that can be checked.
- If the request is ambiguous, set "needs_clarification" to one short question and leave "agents" empty.

Agents already open in the workspace:
{pane_lines}
{previous}
Answer in the user's language. First explain your decision in a few short, plain sentences (no JSON there). Then output exactly one JSON object between the markers, with no other keys:
{PLAN_BEGIN}
{{"complexity": "simple", "summary": "one line", "needs_clarification": null,
  "agents": [{{"key": "impl", "role": "implementer", "title": "short name", "backend": "codex_native", "model": "<approved id>", "effort": "<supported effort or null for model default>",
    "task": "what this agent must do", "acceptance_criteria": ["checkable criterion"],
    "depends_on": [], "touches": ["path or area"]}}]}}
{PLAN_END}

User request:
{user_text}
"""


def _text(value, field: str, *, max_len: int, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise PlanValidationError(f"{field} must be a string")
    cleaned = " ".join(value.split()) if max_len <= 300 else value.strip()
    if required and not cleaned:
        raise PlanValidationError(f"{field} must not be empty")
    if len(cleaned) > max_len:
        raise PlanValidationError(f"{field} exceeds {max_len} characters")
    return cleaned


def _str_list(value, field: str, *, max_items: int, max_len: int, min_items: int = 0) -> list[str]:
    if value is None:
        value = []
    if not isinstance(value, list) or not min_items <= len(value) <= max_items:
        raise PlanValidationError(f"{field} must be a list of {min_items}..{max_items} strings")
    return [_text(item, field, max_len=max_len) for item in value]


def extract(response_text: str) -> tuple[str, dict]:
    """Split a Maestro answer into (explanation, raw plan object)."""
    if not isinstance(response_text, str) or not response_text.strip():
        raise PlanValidationError("empty Maestro response")
    if len(response_text) > MAX_RESPONSE_CHARS:
        raise PlanValidationError("Maestro response is too large")
    if response_text.count(PLAN_BEGIN) != 1 or response_text.count(PLAN_END) != 1:
        raise PlanValidationError("response must contain exactly one plan block")
    before, rest = response_text.split(PLAN_BEGIN, 1)
    body, after = rest.split(PLAN_END, 1)
    body = body.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", body, re.DOTALL)
    if fence:
        body = fence.group(1)
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as error:
        raise PlanValidationError(f"plan block is not valid JSON ({error.msg})") from None
    if not isinstance(raw, dict):
        raise PlanValidationError("plan must be a JSON object")
    explanation = "\n".join(part.strip() for part in (before, after) if part.strip())
    return explanation[:MAX_EXPLANATION_CHARS], raw


def validate(
    raw: dict,
    *,
    explanation: str,
    models: list[dict],
    limit: int,
    allow_unselected_models: bool = False,
) -> dict:
    unknown = set(raw) - _TOP_KEYS
    if unknown:
        raise PlanValidationError(f"unknown plan fields: {sorted(unknown)}")
    approved = {
        (row.get("backend", "omniroute"), row["id"]): row
        for row in models
    }
    by_model = {}
    for (backend, model_id), row in approved.items():
        by_model.setdefault(model_id, []).append((backend, row))
    explanation = _text(explanation, "explanation", max_len=MAX_EXPLANATION_CHARS)
    clarification = raw.get("needs_clarification")
    clarification = _text(clarification, "needs_clarification", max_len=500, required=False) or None
    agents_raw = raw.get("agents")
    if not isinstance(agents_raw, list):
        raise PlanValidationError("agents must be a list")
    if clarification:
        if agents_raw:
            raise PlanValidationError("a clarification question must not come with agents")
        return _shape(raw, explanation, [], limit, clarification=clarification, complexity="simple")

    complexity = raw.get("complexity")
    if complexity not in {"simple", "multi"}:
        raise PlanValidationError("complexity must be 'simple' or 'multi'")
    if not 1 <= len(agents_raw) <= limit:
        raise PlanValidationError(f"plan must have between 1 and {limit} agents")
    if complexity == "simple" and len(agents_raw) != 1:
        raise PlanValidationError("a simple task must have exactly one agent")
    if complexity == "multi" and len(agents_raw) < 2:
        raise PlanValidationError("a multi task must have at least two agents")

    agents: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(agents_raw):
        if not isinstance(item, dict):
            raise PlanValidationError(f"agent {index} must be an object")
        extra = set(item) - _AGENT_KEYS
        if extra:
            raise PlanValidationError(f"agent {index} has unknown fields: {sorted(extra)}")
        key = item.get("key")
        if not isinstance(key, str) or not _KEY.match(key) or key in seen:
            raise PlanValidationError(f"agent {index} has an invalid or duplicate key")
        role = item.get("role")
        if role not in policy.ROLES:
            raise PlanValidationError(f"agent {key} has an unknown role")
        model = item.get("model")
        backend = item.get("backend")
        if model is None and allow_unselected_models:
            cost_status = None
            backend = backend or "omniroute"
        elif backend is None:
            matches = by_model.get(model, [])
            if not matches:
                raise PlanValidationError(f"agent {key} uses a model that is not an approved live route")
            if len(matches) != 1:
                raise PlanValidationError(f"agent {key} must name an exact backend for its model")
            backend, model_row = matches[0]
            cost_status = model_row.get("cost_status") or "unknown"
        elif (backend, model) not in approved:
            # Never substitute another route: an unapproved model invalidates the plan.
            raise PlanValidationError(f"agent {key} uses a model/backend combination that is not an approved live route")
        else:
            model_row = approved[(backend, model)]
            cost_status = model_row.get("cost_status") or "unknown"
        if backend not in {"omniroute", "codex_native", "claude_native"}:
            raise PlanValidationError(f"agent {key} uses an unsupported runtime")
        effort = item.get("effort")
        model_row = approved.get((backend, model))
        if effort is not None and (not isinstance(effort, str) or effort not in (model_row or {}).get("supported_efforts", [])):
            raise PlanValidationError(f"agent {key} uses an effort not supported by its selected runtime/model")
        depends = _str_list(item.get("depends_on"), f"{key}.depends_on", max_items=policy.HARD_MAX_AUTO_PANES, max_len=24)
        for dependency in depends:
            if dependency not in seen:
                raise PlanValidationError(f"agent {key} depends on {dependency!r}, which is not an earlier agent")
        agents.append({
            "key": key,
            "role": role,
            "title": _text(item.get("title"), f"{key}.title", max_len=80),
            "backend": backend,
            "model": model,
            "effort": effort,
            "cost_status": cost_status,
            "task": _text(item.get("task"), f"{key}.task", max_len=4000),
            "acceptance_criteria": _str_list(item.get("acceptance_criteria"), f"{key}.acceptance_criteria", max_items=8, max_len=300, min_items=1),
            "depends_on": depends,
            "touches": _str_list(item.get("touches"), f"{key}.touches", max_items=10, max_len=200),
        })
        seen.add(key)
    return _shape(raw, explanation, agents, limit, clarification=None, complexity=complexity)


def _overlap(a: list[str], b: list[str]) -> bool:
    if not a or not b:
        # Unknown footprint: assume it may overlap -- never assume safety.
        return True

    def norm(value):
        return value.strip().strip("/").replace("\\", "/").casefold()

    left, right = {norm(x) for x in a}, {norm(x) for x in b}
    return any(x == y or x.startswith(y + "/") or y.startswith(x + "/") for x in left for y in right)


def _shape(raw: dict, explanation: str, agents: list[dict], limit: int, *, clarification, complexity) -> dict:
    for agent in agents:
        others = [other for other in agents if other["key"] != agent["key"]]
        agent["conflicts_with"] = [other["key"] for other in others if _overlap(agent["touches"], other["touches"])]
        agent["independent"] = not agent["depends_on"] and not agent["conflicts_with"]
    summary = _text(raw.get("summary") or (clarification or ""), "summary", max_len=300)
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "llm",
        "complexity": complexity,
        "summary": summary,
        "explanation": explanation,
        "needs_clarification": clarification,
        "agents": agents,
        "execution": {"mode": "sequential", "order": [agent["key"] for agent in agents]},
        "limit": limit,
        "fallback_reason": None,
    }


def parse(response_text: str, *, models: list[dict], limit: int) -> dict:
    explanation, raw = extract(response_text)
    return validate(raw, explanation=explanation, models=models, limit=limit)


def deterministic(task_text: str, *, models: list[dict], limit: int, reason: str | None) -> dict:
    """The P1 keyword policy in the plan shape, explicitly labelled."""
    proposal = policy.propose(task_text, models, limit=limit)
    agents = []
    for index, pane in enumerate(proposal["panes"]):
        key = f"{pane['role']}{index + 1}" if pane["role"] in {a['role'] for a in agents} else pane["role"]
        agents.append({
            "key": key[:24],
            "role": pane["role"],
            "title": pane["title"],
            "backend": pane.get("backend", "omniroute"),
            "model": pane["model"],
            "effort": None,
            "cost_status": pane["cost_status"],
            "task": " ".join(task_text.split())[:4000],
            "acceptance_criteria": ["A tarefa descrita está implementada e o perfil de testes confiável passa."],
            "depends_on": [agents[-1]["key"]] if agents else [],
            "touches": [],
        })
    plan = _shape({"summary": " ".join(task_text.split())[:300]}, explanation=(
        "Proposta determinística por palavras-chave (sem IA): "
        + ("tarefa simples, um único agente." if len(agents) == 1 else f"{len(agents)} responsabilidades detectadas, executadas em sequência.")
    ), agents=agents, limit=limit, clarification=None, complexity=proposal["complexity"])
    plan["source"] = "deterministic-fallback" if reason else "deterministic"
    plan["fallback_reason"] = reason
    plan["policy_version"] = policy.POLICY_VERSION
    plan["signals"] = {pane["role"]: pane["signals"] for pane in proposal["panes"]}
    return plan
