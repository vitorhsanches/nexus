"""Project-scoped AI defaults backed by live Codex/OmniRoute eligibility.

Preferences never qualify a model, activate it, or authorize a paid use. A
saved choice is rechecked against current discovery and policy before it can
be applied to a new execution.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from nexus.dev.service import DevServiceError, validate_dev_project
from nexus.registry import (
    workspace_ai_preferences,
    workspace_cost_authorizations,
    workspace_model_settings,
)
from nexus.routing.catalog import (
    default_active_model_ids,
    default_catalog,
    production_eligible,
)

ROLE_CAPABILITIES = {
    "maestro": frozenset({"planning"}),
    "development": frozenset({"mechanical", "standard-coding", "advanced-coding"}),
    "review": frozenset({"review"}),
}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
COST_ORDER = {"free": 0, "low_cost": 1, "standard": 2, "premium": 3, "unknown": 4}


def _codex_readiness() -> tuple[bool, bool, str]:
    """Inspect the actual CLI and the dedicated profile, without launching a turn."""
    from nexus.dispatchers.manager import _find_codex
    from nexus.process_env import _safe_omniroute_config

    try:
        executable = _find_codex()
        if not executable.is_file():
            raise FileNotFoundError
    except (OSError, RuntimeError):
        return False, False, "Codex CLI não encontrado neste computador."
    home = os.environ.get("NEXUS_OMNIROUTE_CODEX_HOME")
    if not home:
        user_home = os.environ.get("USERPROFILE") or str(Path.home())
        home = str(Path(user_home) / ".codex-omniroute-worker")
    try:
        _safe_omniroute_config(str(Path(home) / "config.toml"))
    except RuntimeError:
        return True, False, "Perfil dedicado do OmniRoute indisponível ou inválido."
    return True, True, "Codex CLI e perfil OmniRoute disponíveis."


def _executor_state(*, installed: bool, profile_ready: bool, catalog: dict) -> dict:
    compatible = any(row.get("appserver_compatible") for row in catalog.get("models", []))
    connected = installed and profile_ready and not catalog.get("catalog_error") and compatible
    codex_reason = (
        "Codex App-Server confirmou modelos do OmniRoute por metadados; inferência não verificada."
        if connected else
        catalog.get("catalog_error") or "Codex App-Server não confirmou modelos e esforços do OmniRoute."
    )
    claude_installed = bool(shutil.which("claude"))
    return {
        "codex": {
            "name": "OpenAI Codex", "installed": installed, "connected": bool(connected),
            "status": "Conectado" if connected else "Não conectado",
            "detail": codex_reason, "inference_verified": False,
        },
        "claude_code": {
            "name": "Claude Code", "installed": claude_installed, "connected": False,
            "status": "Não conectado",
            "detail": "CLI detectada, mas o Nexus ainda não possui adaptador nativo validado."
            if claude_installed else "CLI não detectada; adaptador nativo do Nexus indisponível.",
            "inference_verified": False,
        },
    }


def _eligible_rows(project_id: str, catalog: dict) -> dict[str, list[dict]]:
    discovered = {
        row["id"]: row for row in catalog.get("models", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
        and row.get("selectable") is True and row.get("appserver_compatible") is True
    }
    routes = production_eligible(default_catalog())
    qualified_ids = {route.model_id for route in routes}
    active_ids = workspace_model_settings.effective_active_ids(qualified_ids, default_active_model_ids())
    grants = {
        row["model"] for row in workspace_cost_authorizations.list_for_project(project_id)
        if row["status"] == "ACTIVE" and row["uses_remaining"] > 0
        and row["active_executions"] < row["max_concurrent"]
    }
    result: dict[str, dict[str, dict]] = {role: {} for role in ROLE_CAPABILITIES}
    for route in routes:
        entry = discovered.get(route.model_id)
        if route.model_id not in active_ids or not entry or route.provider != entry.get("provider"):
            continue
        if route.effort not in entry.get("supported_efforts", []):
            continue
        if route.cost_class != "free" and route.model_id not in grants:
            continue
        for role, capabilities in ROLE_CAPABILITIES.items():
            matched = capabilities.intersection(route.capabilities)
            if not matched:
                continue
            option = result[role].setdefault(route.model_id, {
                "id": route.model_id, "name": entry.get("name") or route.model_id,
                "provider": route.provider, "cost_status": route.cost_class,
                "efforts": [], "capabilities": set(), "risk_by_effort": {},
            })
            option["capabilities"].update(matched)
            if route.effort not in option["efforts"]:
                option["efforts"].append(route.effort)
            current = option["risk_by_effort"].get(route.effort)
            if current is None or RISK_ORDER[route.max_risk_level] > RISK_ORDER[current]:
                option["risk_by_effort"][route.effort] = route.max_risk_level
            if COST_ORDER.get(route.cost_class, 4) > COST_ORDER.get(option["cost_status"], 4):
                option["cost_status"] = route.cost_class
    return {
        role: sorted(({
            **option, "efforts": sorted(option["efforts"]),
            "capabilities": sorted(option["capabilities"]),
        } for option in options.values()), key=lambda row: row["id"].casefold())
        for role, options in result.items()
    }


def state(project_id: str, *, refresh: bool = False) -> dict:
    validate_dev_project(project_id)
    installed, profile_ready, readiness_reason = _codex_readiness()
    catalog = {"models": [], "catalog_error": readiness_reason}
    if installed and profile_ready:
        from nexus.chat.service import list_models
        catalog = list_models(refresh=refresh)
    executors = _executor_state(installed=installed, profile_ready=profile_ready, catalog=catalog)
    if not profile_ready:
        executors["codex"]["detail"] = readiness_reason
    choices = _eligible_rows(project_id, catalog) if executors["codex"]["connected"] else {role: [] for role in ROLE_CAPABILITIES}
    saved = workspace_ai_preferences.read(project_id)
    roles = {}
    for role, options in choices.items():
        selection = saved.get(role)
        valid = bool(selection and selection["executor"] == "codex" and any(
            option["id"] == selection["model_id"] and selection["effort"] in option["efforts"]
            for option in options
        ))
        roles[role] = {"selection": selection, "valid": valid, "models": options}
    return {"project_id": project_id, "executors": executors, "roles": roles,
            "catalog_error": catalog.get("catalog_error")}


def save(project_id: str, role: str, executor: str, model_id: str, effort: str) -> dict:
    if role not in ROLE_CAPABILITIES:
        raise DevServiceError("AI_ROLE_INVALID", "Selecione uma função válida.", http_status=422)
    if executor != "codex":
        raise DevServiceError("AI_EXECUTOR_UNAVAILABLE", "Claude Code ainda não possui adaptador nativo validado no Nexus.", http_status=409)
    if not all(isinstance(value, str) and value and len(value) <= 128 for value in (model_id, effort)):
        raise DevServiceError("AI_MODEL_INVALID", "Modelo e esforço explícitos são necessários.", http_status=422)
    current = state(project_id, refresh=True)
    if not current["executors"]["codex"]["connected"]:
        raise DevServiceError("AI_EXECUTOR_UNAVAILABLE", "Codex/OmniRoute não está conectado.", http_status=503)
    if not any(row["id"] == model_id and effort in row["efforts"] for row in current["roles"][role]["models"]):
        raise DevServiceError("AI_MODEL_NOT_ELIGIBLE", "Modelo ou esforço não é elegível para esta função e projeto.", http_status=403)
    workspace_ai_preferences.save(project_id, role, executor, model_id, effort)
    return state(project_id)


def clear(project_id: str, role: str) -> dict:
    if role not in ROLE_CAPABILITIES:
        raise DevServiceError("AI_ROLE_INVALID", "Selecione uma função válida.", http_status=422)
    validate_dev_project(project_id)
    workspace_ai_preferences.clear(project_id, role)
    return state(project_id)


def require_saved(project_id: str, role: str, *, risk_level: str = "low") -> dict | None:
    """Revalidate a saved default just before a new execution is admitted."""
    selected = workspace_ai_preferences.read(project_id).get(role)
    if selected is None:
        return None
    current = state(project_id, refresh=True)
    option = next((row for row in current["roles"][role]["models"]
                   if row["id"] == selected["model_id"] and selected["effort"] in row["efforts"]), None)
    if option is None or risk_level not in RISK_ORDER or (
        RISK_ORDER[risk_level] > RISK_ORDER[option["risk_by_effort"][selected["effort"]]]
    ):
        raise DevServiceError("AI_PREFERENCE_INELIGIBLE", "A preferência de IA perdeu qualificação, ativação, compatibilidade, autorização de custo ou teto de risco.", http_status=409)
    return {**selected, "cost_status": option["cost_status"], "max_risk_level": option["risk_by_effort"][selected["effort"]]}
