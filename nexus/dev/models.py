"""Server-owned Development model allowlist built from live OmniRoute discovery."""

from __future__ import annotations

from nexus.routing.catalog import default_catalog, production_eligible

_COST_ORDER = {"free": 0, "low_cost": 1, "standard": 2, "premium": 3, "unknown": 4}


def list_dev_models() -> dict:
    """Return live, approved coding routes with transparent cost metadata."""
    from nexus.chat.service import list_models

    discovered_payload = list_models()
    discovered = {
        item["id"]: item
        for item in discovered_payload.get("models", [])
        if item.get("selectable") is True and isinstance(item.get("id"), str)
    }
    approved: dict[str, dict] = {}
    for route in production_eligible(default_catalog()):
        if not (
            {"standard-coding", "advanced-coding", "mechanical"}
            & set(route.capabilities)
        ):
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

