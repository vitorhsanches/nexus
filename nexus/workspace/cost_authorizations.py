"""Project/model cost consent with server-side approval revalidation."""

from datetime import UTC, datetime, timedelta

from nexus.dev.service import DevServiceError
from nexus.registry import workspace_cost_authorizations as store

MIN_USES, MAX_USES = 1, 10_000
MIN_VALIDITY, MAX_VALIDITY = 60, 365 * 24 * 60 * 60
MIN_CONCURRENCY, MAX_CONCURRENCY = 1, 100


def _project(project_id: str) -> None:
    from nexus.dev.service import validate_dev_project
    validate_dev_project(project_id)


def _approved_model(model: str) -> dict:
    from nexus.dev.models import list_dev_models
    catalog = list_dev_models()
    row = next((item for item in catalog["models"] if item["id"] == model), None)
    if row is None:
        raise DevServiceError("MODEL_NOT_ALLOWED", catalog.get("catalog_error") or
                              "The selected model is not an approved live route.", http_status=403)
    return row


def list_authorizations(project_id: str) -> dict:
    _project(project_id)
    return {"project_id": project_id, "authorizations": store.list_for_project(project_id)}


def authorize(project_id: str, model: str, *, confirm_cost: bool, max_uses: int,
              validity_seconds: int, max_concurrent: int) -> dict:
    _project(project_id)
    if not confirm_cost:
        raise DevServiceError("COST_CONFIRMATION_REQUIRED", "Explicitly authorize this model's cost.", http_status=400)
    if not MIN_USES <= max_uses <= MAX_USES or not MIN_VALIDITY <= validity_seconds <= MAX_VALIDITY or not MIN_CONCURRENCY <= max_concurrent <= MAX_CONCURRENCY:
        raise DevServiceError("INVALID_LIMITS", "Usage, validity or concurrency limit is outside the supported range.", http_status=400)
    row = _approved_model(model)
    cost_status = row.get("cost_status") or "unknown"
    if cost_status == "free":
        raise DevServiceError("COST_AUTHORIZATION_NOT_REQUIRED", "Free models do not need persistent cost authorization.", http_status=400)
    expires_at = (datetime.now(UTC) + timedelta(seconds=validity_seconds)).isoformat()
    return store.create(project_id, model, cost_status, max_uses=max_uses,
                        expires_at=expires_at, max_concurrent=max_concurrent)


def revoke(project_id: str, authorization_id: str) -> dict:
    _project(project_id)
    if not store.revoke(project_id, authorization_id):
        raise DevServiceError("COST_AUTHORIZATION_NOT_ACTIVE", "Authorization is no longer active.", http_status=409)
    return {"authorization_id": authorization_id, "status": "REVOKED"}


def reserve_if_active(project_id: str, model: str) -> tuple[str, dict] | None:
    """Use a configured grant if present; never fall back around its limits."""
    row = _approved_model(model)
    if (row.get("cost_status") or "unknown") == "free":
        return None
    records = store.list_for_project(project_id)
    configured = next((item for item in records if item["model"] == model and item["status"] == "ACTIVE"), None)
    if configured is None:
        return None
    result = store.reserve(project_id, model)
    if result is None:
        raise DevServiceError(
            "COST_AUTHORIZATION_LIMIT_EXCEEDED",
            "The persistent authorization is expired, exhausted, or at its concurrency limit.",
            http_status=403,
        )
    return result


def has_active(project_id: str, model: str) -> bool:
    return any(item["model"] == model and item["status"] == "ACTIVE" and item["uses_remaining"] > 0
               for item in store.list_for_project(project_id))


def bind(lease_id: str | None, kind: str, execution_id: str) -> None:
    if lease_id:
        store.bind_lease(lease_id, kind, execution_id)


def release(lease_id: str | None) -> None:
    if lease_id:
        store.release(lease_id)


def release_execution(kind: str, execution_id: str) -> None:
    store.release_execution(kind, execution_id)
