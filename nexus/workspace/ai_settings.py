"""Project-scoped AI defaults backed by live Codex/OmniRoute eligibility.

Preferences never qualify a model, activate it, or authorize a paid use. A
saved choice is rechecked against current discovery and policy before it can
be applied to a new execution.
"""

from __future__ import annotations

import os
from pathlib import Path

from nexus.dev.service import DevServiceError, validate_dev_project
from nexus.registry import (
    workspace_ai_preferences,
    workspace_ai_providers,
    workspace_cost_authorizations,
    workspace_model_settings,
)
from nexus.routing.catalog import (
    default_active_model_ids,
    default_catalog,
    production_eligible,
)
from nexus.workspace import omniroute_oauth

ROLE_CAPABILITIES = {
    "maestro": frozenset({"planning"}),
    "development": frozenset({"mechanical", "standard-coding", "advanced-coding"}),
    "review": frozenset({"review"}),
}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
COST_ORDER = {"free": 0, "low_cost": 1, "standard": 2, "premium": 3, "unknown": 4}
VERIFIED_VARIANTS = {
    "cc/claude-sonnet-5-low": "cc/claude-sonnet-5",
    "cc/claude-sonnet-5-high": "cc/claude-sonnet-5",
}


def _provider(model_id: str) -> str | None:
    if model_id.startswith("cc/"):
        return "claude_code"
    if model_id.startswith("cx/"):
        return "codex"
    return None


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


def _executor_state(*, installed: bool, profile_ready: bool, connections: dict, switches: dict) -> dict:
    result = {}
    for key in ("claude_code", "codex"):
        connection = connections.get(key) or {}
        connected = connection.get("connected") is True
        enabled = switches.get(key, True)
        result[key] = {
            "name": omniroute_oauth.NAMES[key], "connected": connected,
            "enabled": enabled, "executable": connected and enabled and installed and profile_ready,
            "status": "Conectado" if connected else "Não conectado",
            "detail": connection.get("detail") or "Conexão OAuth não confirmada pelo OmniRoute.",
            "adapter": "Codex App Server → OmniRoute", "inference_verified": False,
        }
    result["claude_code"]["native_cli"] = "Execução nativa pela CLI Claude Code não implementada."
    return result


def _eligible_rows(project_id: str, catalog: dict, executors: dict) -> dict[str, list[dict]]:
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
        provider = _provider(route.model_id)
        if not provider or not executors[provider]["executable"]:
            continue
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
            base_id = VERIFIED_VARIANTS.get(route.model_id, route.model_id)
            option = result[role].setdefault(base_id, {
                "id": base_id, "name": base_id if base_id != route.model_id else entry.get("name") or route.model_id,
                "provider": provider, "cost_status": route.cost_class,
                "capabilities": set(), "candidate_routes": {},
            })
            option["capabilities"].update(matched)
            option["candidate_routes"].setdefault(route.effort, []).append(route)
            if COST_ORDER.get(route.cost_class, 4) > COST_ORDER.get(option["cost_status"], 4):
                option["cost_status"] = route.cost_class
    choices = {}
    for role, options in result.items():
        rows = []
        for option in options.values():
            exact, risk, ambiguous = {}, {}, []
            for effort, candidates in option.pop("candidate_routes").items():
                ids = {candidate.model_id for candidate in candidates}
                if len(ids) != 1:
                    # Only this explicitly verified Sonnet pair has a
                    # stable effort-encoded route identity. A qualification
                    # for another effort on the high route cannot silently
                    # replace the low route at LOW/MEDIUM risk.
                    matching = {model_id for model_id in ids
                                if option["id"] == VERIFIED_VARIANTS.get(model_id)
                                and model_id.endswith("-" + effort)}
                    if len(matching) != 1:
                        ambiguous.append(effort)
                        continue
                    ids = matching
                    candidates = [candidate for candidate in candidates if candidate.model_id in ids]
                exact[effort] = next(iter(ids))
                risk[effort] = max((candidate.max_risk_level for candidate in candidates), key=RISK_ORDER.__getitem__)
            if exact:
                rows.append({**option, "efforts": sorted(exact), "routes_by_effort": exact,
                             "risk_by_effort": risk, "ambiguous_efforts": sorted(ambiguous),
                             "capabilities": sorted(option["capabilities"])})
        choices[role] = sorted(rows, key=lambda row: row["id"].casefold())
    return choices


def state(project_id: str, *, refresh: bool = False) -> dict:
    providers = provider_state(project_id)
    executors = providers["executors"]
    catalog = {"models": [], "catalog_error": providers["adapter_error"] or "Nenhum provedor OAuth executável."}
    if any(item["executable"] for item in executors.values()):
        from nexus.chat.service import list_models
        catalog = list_models(refresh=refresh)
    choices = _eligible_rows(project_id, catalog, executors)
    saved = workspace_ai_preferences.read(project_id)
    roles = {}
    for role, options in choices.items():
        selection = saved.get(role)
        if selection:
            selection = {**selection, "model_id": VERIFIED_VARIANTS.get(selection["model_id"], selection["model_id"])}
            selection["provider"] = _provider(selection["model_id"])
        valid = bool(selection and any(
            option["id"] == selection["model_id"] and (not selection["effort"] or selection["effort"] in option["efforts"])
            for option in options
        ))
        roles[role] = {"selection": selection, "valid": valid, "models": options}
    return {"project_id": project_id, "executors": executors, "roles": roles,
            "catalog_error": catalog.get("catalog_error")}


def provider_state(project_id: str) -> dict:
    validate_dev_project(project_id)
    installed, profile_ready, readiness_reason = _codex_readiness()
    connections = omniroute_oauth.connection_status()
    switches = workspace_ai_providers.read()
    executors = _executor_state(installed=installed, profile_ready=profile_ready,
                                connections=connections, switches=switches)
    return {"project_id": project_id, "executors": executors,
            "adapter_error": None if installed and profile_ready else readiness_reason}


def save(project_id: str, role: str, executor: str, model_id: str, effort: str | None = None) -> dict:
    if role not in ROLE_CAPABILITIES:
        raise DevServiceError("AI_ROLE_INVALID", "Selecione uma função válida.", http_status=422)
    if executor not in omniroute_oauth.PROVIDERS:
        raise DevServiceError("AI_EXECUTOR_UNAVAILABLE", "Provedor inválido.", http_status=409)
    if not isinstance(model_id, str) or not model_id or len(model_id) > 128:
        raise DevServiceError("AI_MODEL_INVALID", "Selecione um modelo.", http_status=422)
    current = state(project_id, refresh=True)
    if not current["executors"][executor]["executable"]:
        raise DevServiceError("AI_EXECUTOR_UNAVAILABLE", "Provedor OAuth ou adaptador indisponível.", http_status=503)
    base = VERIFIED_VARIANTS.get(model_id, model_id)
    if not any(row["id"] == base and row["provider"] == executor and
               (not effort or effort in row["efforts"]) for row in current["roles"][role]["models"]):
        raise DevServiceError("AI_MODEL_NOT_ELIGIBLE", "Modelo não elegível para esta função e projeto.", http_status=403)
    # Keep the legacy NOT NULL column; empty means per-execution effort.
    # The Codex App Server is the execution adapter for both OAuth providers.
    workspace_ai_preferences.save(project_id, role, "codex", base, "")
    return state(project_id)


def set_provider_enabled(project_id: str, provider: str, enabled: bool) -> dict:
    if provider not in omniroute_oauth.PROVIDERS or type(enabled) is not bool:
        raise DevServiceError("AI_PROVIDER_INVALID", "Provedor ou estado inválido.", http_status=422)
    validate_dev_project(project_id)
    workspace_ai_providers.set_enabled(provider, enabled)
    return state(project_id, refresh=True)


def clear(project_id: str, role: str) -> dict:
    if role not in ROLE_CAPABILITIES:
        raise DevServiceError("AI_ROLE_INVALID", "Selecione uma função válida.", http_status=422)
    validate_dev_project(project_id)
    workspace_ai_preferences.clear(project_id, role)
    return state(project_id)


def resolve_choice(project_id: str, role: str, model_id: str, effort: str | None,
                   *, risk_level: str = "low") -> dict:
    """Return one exact live route; refuse missing or ambiguous effort."""
    if role not in ROLE_CAPABILITIES or risk_level not in RISK_ORDER:
        raise DevServiceError("AI_ROLE_INVALID", "Função ou risco inválido.", http_status=422)
    base = VERIFIED_VARIANTS.get(model_id, model_id)
    current = state(project_id, refresh=True)
    option = next((row for row in current["roles"][role]["models"] if row["id"] == base), None)
    if option is None:
        raise DevServiceError("AI_MODEL_NOT_ELIGIBLE", "Modelo sem conexão, qualificação, ativação, capacidade ou autorização de custo.", http_status=403)
    requested = effort or (option["efforts"][0] if len(option["efforts"]) == 1 else None)
    if requested is None:
        raise DevServiceError("AI_EFFORT_REQUIRED", "Escolha um esforço suportado antes da execução.", http_status=422)
    exact = option["routes_by_effort"].get(requested)
    if not exact or RISK_ORDER[risk_level] > RISK_ORDER[option["risk_by_effort"][requested]]:
        raise DevServiceError("AI_PREFERENCE_INELIGIBLE", "Esforço sem rota exata ou acima do teto de risco.", http_status=409)
    return {"model_id": exact, "base_model_id": base, "effort": requested,
            "provider": option["provider"], "cost_status": option["cost_status"],
            "max_risk_level": option["risk_by_effort"][requested]}


def require_saved(project_id: str, role: str, *, effort: str | None = None, risk_level: str = "low") -> dict | None:
    """Revalidate a saved default just before a new execution is admitted."""
    selected = workspace_ai_preferences.read(project_id).get(role)
    if selected is None:
        return None
    resolved = resolve_choice(project_id, role, selected["model_id"], effort or selected["effort"] or None,
                              risk_level=risk_level)
    return {**selected, **resolved}
