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
                "confirmed_efforts": list(discovered[route.model_id].get("supported_efforts") or [])
                if discovered[route.model_id].get("appserver_compatible") is True else [],
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
    """Return only live, qualified, operator-active Development routes."""
    from nexus.chat.service import list_models
    from nexus.registry import workspace_model_settings

    discovered_payload = list_models(refresh=True) if refresh else list_models()
    catalog = _approved_dev_models(discovered_payload)
    qualified_ids = _qualified_dev_route_ids()
    from nexus.routing.catalog import default_active_model_ids
    active_ids = workspace_model_settings.effective_active_ids(qualified_ids, default_active_model_ids())
    catalog["models"] = [row for row in catalog["models"] if row["id"] in active_ids]
    if catalog["default_model"] not in active_ids:
        catalog["default_model"] = next(
            (row["id"] for row in catalog["models"] if row["cost_status"] == "free"),
            None,
        )
    return catalog


def _qualified_model_ids() -> set[str]:
    """Return every model with an explicit, non-revoked qualification."""
    return {
        route.model_id for route in production_eligible(default_catalog())
    }


def _qualified_dev_route_ids() -> set[str]:
    """Restrict Development execution to qualified coding-capable routes."""
    return {
        route.model_id
        for route in production_eligible(default_catalog())
        if _DEVELOPMENT_CAPABILITIES & set(route.capabilities)
    }


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
    from nexus.registry import workspace_model_settings

    approved_models = _approved_dev_models(discovered_payload)
    qualified_ids = _qualified_model_ids()
    settings = workspace_model_settings.read()
    from nexus.routing.catalog import default_active_model_ids
    active_ids = workspace_model_settings.effective_active_ids(qualified_ids, default_active_model_ids())
    approved_ids = {item["id"] for item in approved_models["models"]}
    from nexus.registry import workspace_model_qualifications
    qualifications = workspace_model_qualifications.qualified()
    rows = []
    for discovered in discovered_payload.get("models", []):
        model_id = discovered.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        routes = routes_by_id.get(model_id, [])
        if is_blocked_model(model_id):
            approval_status = "blocked"
            approval_reason = "This model is globally blocked by Nexus policy."
        elif model_id in qualified_ids:
            approval_status = "approved"
            approval_reason = "Explicitly qualified for its configured capabilities."
        else:
            approval_status = "not_qualified"
            if routes and any(route.approved for route in routes):
                approval_reason = "Known to Nexus, but not enabled and qualified for Development coding."
            else:
                approval_reason = "Discovered by OmniRoute; Nexus has not approved it for execution."
        dynamic_qualification = qualifications.get(model_id)
        cost_status = dynamic_qualification["cost_class"] if dynamic_qualification else next(
            (route.cost_class for route in routes if route.cost_class), "unknown"
        )
        saved = settings["models"].get(model_id, {})
        active = model_id in active_ids
        management_status = (
            "blocked" if approval_status == "blocked"
            else "active" if active
            else "inactive" if model_id in qualified_ids
            else "candidate" if saved.get("candidate")
            else "not_qualified"
        )
        rows.append(
            {
                "id": model_id,
                "name": str(discovered.get("name") or model_id),
                "provider": str(discovered.get("provider") or "unknown"),
                "supported_efforts": list(discovered.get("supported_efforts") or []),
                "appserver_compatible": bool(discovered.get("appserver_compatible")),
                "availability": str(
                    discovered.get("availability") or "omniroute-listed-unverified"
                ),
                "cost_status": cost_status,
                "approval_status": approval_status,
                "approval_reason": approval_reason,
                "qualification_status": "qualified" if model_id in qualified_ids else approval_status,
                "qualification": dynamic_qualification,
                "active": active,
                "candidate": bool(saved.get("candidate")),
                "management_status": management_status,
                "discovered": True,
                # The cockpit catalog represents general qualification and
                # activation; this selector is still limited to Development
                # coding routes, so review/planning-only models stay excluded.
                "can_execute": active and model_id in approved_ids,
            }
        )
    discovered_ids = {row["id"] for row in rows}
    for model_id, saved in settings["models"].items():
        if model_id in discovered_ids:
            continue
        dynamic_qualification = qualifications.get(model_id)
        blocked = is_blocked_model(model_id)
        qualified = model_id in qualified_ids
        rows.append({
            "id": model_id,
            "name": model_id,
            "provider": "unknown",
            "availability": "omniroute-unavailable" if discovered_payload.get("catalog_error") else "not-discovered",
            "cost_status": "unknown",
            "approval_status": "blocked" if blocked else "approved" if qualified else "not_qualified",
            "approval_reason": "Model is no longer advertised by OmniRoute; saved policy is retained.",
            "qualification_status": "qualified" if qualified else "not_qualified",
            "qualification": dynamic_qualification,
            "active": model_id in active_ids and qualified and not blocked,
            "candidate": bool(saved.get("candidate")),
            "management_status": "blocked" if blocked else "inactive" if qualified else "candidate" if saved.get("candidate") else "not_qualified",
            "discovered": None if discovered_payload.get("catalog_error") else False,
            "can_execute": False,
        })
    rows.sort(
        key=lambda item: (
            item["management_status"] not in {"active", "inactive"},
            item["management_status"] != "active",
            item["id"].casefold(),
        )
    )
    executable_models = [item for item in approved_models["models"] if item["id"] in active_ids]
    return {
        "models": rows,
        "approved_models": executable_models,
        "default_model": next(
            (item["id"] for item in executable_models if item["cost_status"] == "free"),
            executable_models[0]["id"] if executable_models else None,
        ),
        "catalog_error": discovered_payload.get("catalog_error"),
        "selection_configured": settings["selection_configured"],
    }

