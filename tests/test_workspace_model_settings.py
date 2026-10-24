"""Operator model settings persist locally without granting qualification/cost."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from nexus.dev import model_management, models
from nexus.registry import database, workspace_model_settings


@pytest.fixture
def model_settings_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "STORAGE_DIR", tmp_path / "storage")
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "storage" / "nexus.db")
    database.initialize_database()


def test_selection_defaults_to_legacy_behavior_then_persists_across_restart(model_settings_db):
    qualified = {"qualified/a", "qualified/b", "qualified/c"}
    assert workspace_model_settings.effective_active_ids(qualified) == qualified

    workspace_model_settings.set_active("qualified/a", False, qualified)
    assert workspace_model_settings.effective_active_ids(qualified) == {"qualified/b", "qualified/c"}
    database.initialize_database()
    assert workspace_model_settings.effective_active_ids(qualified) == {"qualified/b", "qualified/c"}
    assert workspace_model_settings.read()["selection_configured"] is True


def test_concurrent_changes_are_atomic(model_settings_db):
    qualified = {"qualified/a", "qualified/b", "qualified/c", "qualified/d"}
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda model_id: workspace_model_settings.set_active(model_id, False, qualified), qualified))
    assert workspace_model_settings.effective_active_ids(qualified) == set()


def test_candidate_is_persistent_but_does_not_configure_or_activate_selection(model_settings_db):
    workspace_model_settings.set_candidate("discovered/new", True)
    state = workspace_model_settings.read()
    assert state["selection_configured"] is False
    assert state["models"]["discovered/new"]["candidate"] is True
    assert workspace_model_settings.effective_active_ids({"qualified/a"}) == {"qualified/a"}


def test_model_catalog_exposes_qualification_activation_and_candidate_separately(model_settings_db, monkeypatch):
    monkeypatch.setattr("nexus.chat.service.list_models", lambda refresh=False: {
        "models": [
            {"id": "qualified/a", "name": "A", "provider": "one", "availability": "listed", "selectable": True},
            {"id": "discovered/new", "name": "New", "provider": "two", "availability": "listed", "selectable": False},
            {"id": "gpt-5.6-terra", "name": "Blocked", "provider": "one", "availability": "listed", "selectable": False},
        ], "catalog_error": None,
    })
    monkeypatch.setattr(models, "_qualified_dev_route_ids", lambda: {"qualified/a"})
    workspace_model_settings.set_active("qualified/a", False, {"qualified/a"})
    workspace_model_settings.set_candidate("discovered/new", True)

    result = models.list_dev_model_catalog()
    rows = {row["id"]: row for row in result["models"]}
    assert rows["qualified/a"]["qualification_status"] == "qualified"
    assert rows["qualified/a"]["management_status"] == "inactive"
    assert rows["qualified/a"]["can_execute"] is False
    assert rows["discovered/new"]["management_status"] == "candidate"
    assert rows["discovered/new"]["can_execute"] is False
    assert rows["gpt-5.6-terra"]["management_status"] == "blocked"


def test_activation_cannot_qualify_or_unblock_models(model_settings_db, monkeypatch):
    monkeypatch.setattr(model_management, "_qualified_ids", lambda: {"qualified/a"})
    with pytest.raises(model_management.DevServiceError) as unqualified:
        model_management.set_active("discovered/new", True)
    assert unqualified.value.code == "MODEL_NOT_QUALIFIED"
    with pytest.raises(model_management.DevServiceError) as blocked:
        model_management.set_active("gpt-5.6-terra", True)
    assert blocked.value.code == "MODEL_BLOCKED"
    assert workspace_model_settings.read()["selection_configured"] is False


def test_candidate_requires_discovery_and_never_accepts_qualified_or_blocked(model_settings_db, monkeypatch):
    monkeypatch.setattr("nexus.chat.service.list_models", lambda refresh=False: {"models": [{"id": "discovered/new"}]})
    monkeypatch.setattr(model_management, "_qualified_ids", lambda: {"qualified/a"})
    model_management.set_candidate("discovered/new", True)
    with pytest.raises(model_management.DevServiceError) as qualified:
        model_management.set_candidate("qualified/a", True)
    assert qualified.value.code == "MODEL_ALREADY_QUALIFIED"
    with pytest.raises(model_management.DevServiceError) as blocked:
        model_management.set_candidate("gpt-5.6-terra", True)
    assert blocked.value.code == "MODEL_BLOCKED"
    assert workspace_model_settings.read()["selection_configured"] is False


def test_policy_writes_require_loopback_operator(model_settings_db, monkeypatch):
    monkeypatch.setenv("NEXUS_ADMIN_TOKEN", "x" * 32)
    model_management.require_local_operator(SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")), "Bearer " + "x" * 32)
    with pytest.raises(model_management.DevServiceError) as remote:
        model_management.require_local_operator(SimpleNamespace(client=SimpleNamespace(host="192.168.1.4")), "Bearer " + "x" * 32)
    assert remote.value.code == "LOCAL_OPERATOR_REQUIRED"
    with pytest.raises(model_management.DevServiceError) as unauthenticated:
        model_management.require_local_operator(SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")))
    assert unauthenticated.value.code == "ADMIN_AUTH_REQUIRED"


def test_new_qualifications_require_runtime_metadata_and_start_inactive(model_settings_db, monkeypatch):
    from nexus.registry import workspace_model_qualifications
    from nexus.routing.catalog import default_active_model_ids, default_catalog, production_eligible

    payload = {"catalog_error": None, "models": [{
        "id": "provider/new-code", "provider": "provider", "selectable": True,
        "appserver_compatible": True, "supported_efforts": ["low", "high"],
    }]}
    monkeypatch.setattr("nexus.chat.service.list_models", lambda refresh=False: payload)
    payload["models"][0]["appserver_compatible"] = False
    with pytest.raises(model_management.DevServiceError) as no_appserver_match:
        model_management.qualify("provider/new-code", provider="provider", capabilities=["standard-coding"], efforts=["low"], cost_class="standard", max_risk_level="low")
    assert no_appserver_match.value.code == "EXECUTION_COMPATIBILITY_UNVERIFIED"
    payload["models"][0]["appserver_compatible"] = True
    with pytest.raises(model_management.DevServiceError) as mismatch:
        model_management.qualify("provider/new-code", provider="other", capabilities=["standard-coding"], efforts=["low"], cost_class="standard", max_risk_level="low")
    assert mismatch.value.code == "PROVIDER_MISMATCH"
    with pytest.raises(model_management.DevServiceError) as incompatible:
        model_management.qualify("provider/new-code", provider="provider", capabilities=["standard-coding"], efforts=["xhigh"], cost_class="standard", max_risk_level="low")
    assert incompatible.value.code == "EFFORT_UNSUPPORTED"
    model_management.qualify("provider/new-code", provider="provider", capabilities=["standard-coding"], efforts=["low"], cost_class="standard", max_risk_level="low")
    qualified = workspace_model_qualifications.qualified()
    assert qualified["provider/new-code"]["compatibility"]["inference_performed"] is False
    route = next(route for route in default_catalog() if route.model_id == "provider/new-code")
    assert route.approved is True
    assert route.capabilities == frozenset({"standard-coding"})
    assert "provider/new-code" not in default_active_model_ids()
    assert "provider/new-code" not in workspace_model_settings.effective_active_ids(
        {"provider/new-code", "cc/claude-sonnet-5-low"}, default_active_model_ids()
    )
    model_management.set_active("provider/new-code", True)
    assert "provider/new-code" in workspace_model_settings.effective_active_ids(
        {route.model_id for route in production_eligible(default_catalog())}, default_active_model_ids()
    )


def test_qualification_revocation_clears_activation_and_survives_restart(model_settings_db, monkeypatch):
    from nexus.registry import workspace_model_qualifications

    monkeypatch.setattr("nexus.chat.service.list_models", lambda refresh=False: {"models": [{
        "id": "provider/revoke-me", "provider": "provider", "selectable": True,
        "appserver_compatible": True, "supported_efforts": ["low"],
    }]})
    model_management.qualify("provider/revoke-me", provider="provider", capabilities=["mechanical"], efforts=["low"], cost_class="unknown", max_risk_level="low")
    model_management.set_active("provider/revoke-me", True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _n: workspace_model_qualifications.revoke("provider/revoke-me"), range(6)))
    database.initialize_database()
    assert workspace_model_qualifications.qualified().get("provider/revoke-me") is None
    assert workspace_model_settings.read()["models"]["provider/revoke-me"]["active"] is False


def test_router_rechecks_active_selection_for_each_new_route(model_settings_db, monkeypatch):
    from nexus.routing.catalog import default_catalog, production_eligible
    from nexus.routing.service import AdaptiveRoutingService

    qualified = {route.model_id for route in production_eligible(default_catalog())}
    deactivated = "cc/claude-sonnet-5-low"
    workspace_model_settings.set_active(deactivated, False, qualified)
    router = AdaptiveRoutingService()
    captured = {}
    monkeypatch.setattr(router, "_collect_telemetry", lambda: SimpleNamespace(
        discovered_models=(), provider_telemetry={}, server_health=None, errors=(), warnings=()
    ))
    monkeypatch.setattr(router, "_models_discovery_failed", lambda snapshot: True)

    def capture(*args, approved_catalog=None, **kwargs):
        captured["catalog"] = approved_catalog
        return "route-checked"

    monkeypatch.setattr(router, "_legacy_fallback_decision", capture)
    assert router._select_route("standard-coding", "low") == "route-checked"
    assert deactivated not in {route.model_id for route in captured["catalog"]}
