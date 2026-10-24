"""Operator controls for model activation, separate from qualification/cost consent."""

from __future__ import annotations

import hmac
import ipaddress
import os
from datetime import UTC, datetime

from nexus.dev.service import DevServiceError
from nexus.registry import workspace_model_qualifications, workspace_model_settings
from nexus.routing.catalog import is_blocked_model


def admin_auth_configured() -> bool:
    """Expose only whether administrative authentication is enabled."""
    return len(os.environ.get("NEXUS_ADMIN_TOKEN", "")) >= 32


def require_local_operator(request, authorization: str | None = None) -> None:
    """Require both a local connection and an explicit operator bearer token."""
    client = getattr(request, "client", None)
    try:
        address = ipaddress.ip_address(client.host) if client and client.host else None
    except ValueError:
        address = None
    if address is None or not address.is_loopback:
        raise DevServiceError(
            "LOCAL_OPERATOR_REQUIRED",
            "Model policy changes are restricted to a local operator connection.",
            http_status=403,
        )
    configured = os.environ.get("NEXUS_ADMIN_TOKEN", "")
    if not admin_auth_configured():
        raise DevServiceError(
            "ADMIN_AUTH_NOT_CONFIGURED",
            "Administrative model changes are disabled until NEXUS_ADMIN_TOKEN is configured with at least 32 characters.",
            http_status=503,
        )
    supplied = authorization[7:].strip() if isinstance(authorization, str) and authorization.lower().startswith("bearer ") else ""
    if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), configured.encode("utf-8")):
        raise DevServiceError(
            "ADMIN_AUTH_REQUIRED",
            "A valid Nexus administrator bearer token is required.",
            http_status=401,
        )


def _qualified_ids() -> set[str]:
    """Return all qualified models; callers still enforce route capability."""
    from nexus.dev.models import _qualified_model_ids

    return _qualified_model_ids()


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
    from nexus.routing.catalog import default_active_model_ids
    state = workspace_model_settings.set_active(model_id, active, qualified_ids, default_active_model_ids())
    from nexus.dev.models import list_dev_model_catalog

    return {**list_dev_model_catalog(), "selection_configured": state["selection_configured"]}


def qualify(model_id: str, *, provider: str, capabilities: list[str], efforts: list[str],
            cost_class: str, max_risk_level: str) -> dict:
    model_id = model_id.strip() if isinstance(model_id, str) else ""
    if not model_id or len(model_id) > 128 or is_blocked_model(model_id):
        raise DevServiceError("MODEL_BLOCKED", "This model cannot be qualified under Nexus policy.", http_status=403)
    from nexus.chat.service import list_models
    discovered_payload = list_models(refresh=True)
    model = next((item for item in discovered_payload.get("models", []) if item.get("id") == model_id), None)
    if model is None:
        raise DevServiceError("MODEL_NOT_DISCOVERED", "The model must be currently advertised by OmniRoute.", http_status=409)
    if not provider or provider != model.get("provider"):
        raise DevServiceError("PROVIDER_MISMATCH", "Provider must exactly match the current OmniRoute discovery metadata.", http_status=409)
    supported = set(model.get("supported_efforts") or [])
    if not model.get("appserver_compatible") or not supported:
        raise DevServiceError("EXECUTION_COMPATIBILITY_UNVERIFIED", "Codex App-Server did not advertise this exact OmniRoute model and its supported efforts; qualification is blocked.", http_status=409)
    if not efforts or not set(efforts).issubset(supported):
        raise DevServiceError("EFFORT_UNSUPPORTED", "Choose one or more reasoning efforts explicitly advertised by Codex App-Server.", http_status=422)
    allowed_capabilities = {"mechanical", "standard-coding", "advanced-coding", "review", "planning"}
    if not capabilities or len(set(capabilities)) != len(capabilities) or not set(capabilities).issubset(allowed_capabilities):
        raise DevServiceError("CAPABILITIES_INVALID", "At least one explicit supported capability is required.", http_status=422)
    if cost_class not in {"free", "low_cost", "standard", "premium", "unknown"}:
        raise DevServiceError("COST_CLASS_INVALID", "An explicit cost classification is required.", http_status=422)
    if max_risk_level not in {"low", "medium", "high"}:
        raise DevServiceError("RISK_LEVEL_INVALID", "Qualification risk ceiling must be low, medium, or high.", http_status=422)
    compatibility = {
        "method": "omniroute_catalog+codex_model_list",
        "model_id": model_id,
        "provider": provider,
        "supported_efforts": sorted(supported),
        "checked_at": datetime.now(UTC).isoformat(),
        "inference_performed": False,
        "result": "protocol_metadata_compatible; inference_unverified",
    }
    workspace_model_qualifications.save(
        model_id, provider=provider, capabilities=capabilities, efforts=efforts,
        cost_class=cost_class, max_risk_level=max_risk_level, compatibility=compatibility,
    )
    from nexus.dev.models import list_dev_model_catalog
    return list_dev_model_catalog(refresh=True)


def revoke_qualification(model_id: str) -> dict:
    model_id = model_id.strip() if isinstance(model_id, str) else ""
    if not model_id or len(model_id) > 128:
        raise DevServiceError("INVALID_MODEL", "A valid model identifier is required.", http_status=400)
    workspace_model_qualifications.revoke(model_id)
    from nexus.dev.models import list_dev_model_catalog
    return list_dev_model_catalog(refresh=True)


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
