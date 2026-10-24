"""Server-owned Development model allowlist built from live OmniRoute discovery."""

from __future__ import annotations

from nexus.routing.catalog import default_catalog, is_blocked_model, production_eligible

_COST_ORDER = {"free": 0, "low_cost": 1, "standard": 2, "premium": 3, "unknown": 4}
_DEVELOPMENT_CAPABILITIES = {"standard-coding", "advanced-coding", "mechanical"}


def _approved_dev_models(discovered_payload: dict) -> dict:
    """Project discovered routes onto the existing Development allowlist."""
    discovered = {
        item["id"]: item
        for item in discovered_payload.get("models", [])
        if item.get("selectable") is True and isinstance(item.get("id"), str)
    }
    approved: dict[str, dict] = {}
    for route in production_eligible(default_catalog()):
        if not (_DEVELOPMENT_CAPABILITIES & set(route.capabilities)):
            continue
        if route.model_id not in discovered:
            continue
        row = approved.setdefault(
            route.model_id,
            {
                **discovered[route.model_id],
                "cost_status": route.cost_class or "unknown",
                "supported_efforts": [],
            },
        )
        if route.effort and route.effort not in row["supported_efforts"]:
            row["supported_efforts"].append(route.effort)
    rows = list(approved.values())
    rows.sort(
        key=lambda item: (
            _COST_ORDER.get(item["cost_status"], 5),
            item["id"].casefold(),
        )
    )
    preferred = next(
        (item["id"] for item in rows if item["cost_status"] == "free"),
        None,
    )
    error = discovered_payload.get("catalog_error")
    if not rows and not error:
        error = "No approved coding route is currently advertised by OmniRoute."
    return {"models": rows, "default_model": preferred, "catalog_error": error}


def list_dev_models(*, refresh: bool = False) -> dict:
    """Return only live, explicitly approved Development coding routes."""
    from nexus.chat.service import list_models

    discovered_payload = list_models(refresh=True) if refresh else list_models()
    return _approved_dev_models(discovered_payload)


def list_dev_model_catalog(*, refresh: bool = False) -> dict:
    """Return all discovered models with explicit, non-granting policy labels.

    Discovery is informational. ``approved_models`` is the same narrowly
    qualified allowlist used by Development execution; unknown discoveries
    never become selectable just by appearing in OmniRoute.
    """
    from nexus.chat.service import list_models

    discovered_payload = list_models(refresh=True) if refresh else list_models()
    routes_by_id: dict[str, list] = {}
    for route in default_catalog():
        routes_by_id.setdefault(route.model_id, []).append(route)
    approved_models = _approved_dev_models(discovered_payload)
    approved_ids = {item["id"] for item in approved_models["models"]}
    rows = []
    for discovered in discovered_payload.get("models", []):
        model_id = discovered.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        routes = routes_by_id.get(model_id, [])
        if is_blocked_model(model_id):
            approval_status = "blocked"
            approval_reason = "This model is globally blocked by Nexus policy."
        elif model_id in approved_ids:
            approval_status = "approved"
            approval_reason = "Explicitly qualified for Development coding and currently selectable."
        else:
            approval_status = "not_qualified"
            if routes and any(route.approved for route in routes):
                approval_reason = "Known to Nexus, but not enabled and qualified for Development coding."
            else:
                approval_reason = "Discovered by OmniRoute; Nexus has not approved it for execution."
        cost_status = next(
            (route.cost_class for route in routes if route.cost_class), "unknown"
        )
        rows.append(
            {
                "id": model_id,
                "name": str(discovered.get("name") or model_id),
                "provider": str(discovered.get("provider") or "unknown"),
                "availability": str(
                    discovered.get("availability") or "omniroute-listed-unverified"
                ),
                "cost_status": cost_status,
                "approval_status": approval_status,
                "approval_reason": approval_reason,
                "can_execute": model_id in approved_ids,
            }
        )
    rows.sort(
        key=lambda item: (
            item["approval_status"] != "approved",
            item["id"].casefold(),
        )
    )
    return {
        "models": rows,
        "approved_models": approved_models["models"],
        "default_model": approved_models["default_model"],
        "catalog_error": discovered_payload.get("catalog_error"),
    }

