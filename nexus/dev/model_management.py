"""Operator controls for model activation, separate from qualification/cost consent."""

from __future__ import annotations

import ipaddress

from nexus.dev.service import DevServiceError
from nexus.registry import workspace_model_settings
from nexus.routing.catalog import is_blocked_model


def require_local_operator(request) -> None:
    """Policy writes are available only to the local Nexus operator."""
    client = getattr(request, "client", None)
    try:
        address = ipaddress.ip_address(client.host) if client and client.host else None
    except ValueError:
        address = None
    if address is None or not address.is_loopback:
        raise DevServiceError(
            "LOCAL_OPERATOR_REQUIRED",
            "Model policy changes are restricted to a local operator session.",
            http_status=403,
        )


def _qualified_ids() -> set[str]:
    from nexus.dev.models import _qualified_dev_route_ids

    return _qualified_dev_route_ids()


def set_active(model_id: str, active: bool) -> dict:
    model_id = model_id.strip() if isinstance(model_id, str) else ""
    if not model_id or len(model_id) > 128:
        raise DevServiceError("INVALID_MODEL", "A valid model identifier is required.", http_status=400)
    if not isinstance(active, bool):
        raise DevServiceError("INVALID_MODEL_STATE", "active must be a boolean.", http_status=400)
    if is_blocked_model(model_id):
        raise DevServiceError("MODEL_BLOCKED", "This model is blocked by Nexus policy.", http_status=403)
    qualified_ids = _qualified_ids()
    if active and model_id not in qualified_ids:
        raise DevServiceError(
            "MODEL_NOT_QUALIFIED",
            "Only models explicitly qualified by Nexus policy can be activated.",
            http_status=403,
        )
    state = workspace_model_settings.set_active(model_id, active, qualified_ids)
    from nexus.dev.models import list_dev_model_catalog

    return {**list_dev_model_catalog(), "selection_configured": state["selection_configured"]}


def set_candidate(model_id: str, candidate: bool) -> dict:
    model_id = model_id.strip() if isinstance(model_id, str) else ""
    if not model_id or len(model_id) > 128:
        raise DevServiceError("INVALID_MODEL", "A valid model identifier is required.", http_status=400)
    if not isinstance(candidate, bool):
        raise DevServiceError("INVALID_CANDIDATE_STATE", "candidate must be a boolean.", http_status=400)
    if is_blocked_model(model_id):
        raise DevServiceError("MODEL_BLOCKED", "Blocked models cannot be marked as qualification candidates.", http_status=403)
    if candidate and model_id in _qualified_ids():
        raise DevServiceError("MODEL_ALREADY_QUALIFIED", "Qualified models do not need a future-qualification candidate mark.", http_status=409)
    from nexus.chat.service import list_models
    discovered_payload = list_models(refresh=True)
    discovered = {
        item.get("id") for item in discovered_payload.get("models", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if candidate and model_id not in discovered:
        raise DevServiceError("MODEL_NOT_DISCOVERED", "Only models currently discovered by OmniRoute can be marked as candidates.", http_status=409)
    state = workspace_model_settings.set_candidate(model_id, candidate)
    from nexus.dev.models import list_dev_model_catalog

    return {**list_dev_model_catalog(), "selection_configured": state["selection_configured"]}
