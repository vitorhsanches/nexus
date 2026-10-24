"""AI preferences are operator-owned defaults, never execution grants."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from nexus.dev.service import DevServiceError
from nexus.registry import database, workspace_ai_preferences
from nexus.workspace import ai_settings, cost_authorizations, maestro, service


@pytest.fixture
def ai_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "STORAGE_DIR", tmp_path / "storage")
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "storage" / "nexus.db")
    database.initialize_database()
    with database.get_connection() as connection:
        connection.execute(
            "INSERT INTO projects (id, name, path, enabled, dev_enabled) VALUES (?, ?, ?, 1, 1)",
            ("demo", "Demo", str(tmp_path)),
        )
    monkeypatch.setattr(ai_settings, "validate_dev_project", lambda project_id: str(tmp_path))


@pytest.fixture
def live_models(ai_db, monkeypatch):
    routes = (
        SimpleNamespace(model_id="free/coding", provider="test", effort="low",
                        capabilities={"standard-coding"}, max_risk_level="medium", cost_class="free"),
        SimpleNamespace(model_id="paid/review", provider="test", effort="high",
                        capabilities={"review"}, max_risk_level="high", cost_class="standard"),
        SimpleNamespace(model_id="free/planning", provider="test", effort="medium",
                        capabilities={"planning"}, max_risk_level="low", cost_class="free"),
    )
    monkeypatch.setattr(ai_settings, "default_catalog", lambda: routes)
    monkeypatch.setattr(ai_settings, "production_eligible", lambda catalog: catalog)
    monkeypatch.setattr(ai_settings, "default_active_model_ids", lambda: {route.model_id for route in routes})
    monkeypatch.setattr(ai_settings.workspace_model_settings, "effective_active_ids", lambda qualified, defaults: qualified)
    monkeypatch.setattr(ai_settings, "_codex_readiness", lambda: (True, True, "ready"))
    monkeypatch.setattr(ai_settings.shutil, "which", lambda command: None)
    monkeypatch.setattr(ai_settings.workspace_cost_authorizations, "list_for_project", lambda project_id: [])
    catalog = {"models": [
        {"id": route.model_id, "name": route.model_id, "provider": "test", "selectable": True,
         "appserver_compatible": True, "supported_efforts": [route.effort]}
        for route in routes
    ], "catalog_error": None}
    monkeypatch.setattr("nexus.chat.service.list_models", lambda refresh=False: catalog)
    return catalog


def test_preferences_persist_and_are_project_scoped(live_models):
    saved = ai_settings.save("demo", "development", "codex", "free/coding", "low")
    assert saved["roles"]["development"]["valid"] is True
    database.initialize_database()  # A process restart leaves the same on-disk record.
    assert workspace_ai_preferences.read("demo")["development"]["model_id"] == "free/coding"
    assert workspace_ai_preferences.read("other") == {}
    ai_settings.clear("demo", "development")
    assert workspace_ai_preferences.read("demo") == {}


def test_claude_cannot_be_saved_and_is_never_reported_connected(live_models, monkeypatch):
    monkeypatch.setattr(ai_settings.shutil, "which", lambda command: "claude.exe")
    assert ai_settings.state("demo")["executors"]["claude_code"]["connected"] is False
    with pytest.raises(DevServiceError) as error:
        ai_settings.save("demo", "development", "claude_code", "free/coding", "low")
    assert error.value.code == "AI_EXECUTOR_UNAVAILABLE"


def test_capability_and_cost_are_separate_gates(live_models, monkeypatch):
    state = ai_settings.state("demo")
    assert [row["id"] for row in state["roles"]["development"]["models"]] == ["free/coding"]
    assert [row["id"] for row in state["roles"]["maestro"]["models"]] == ["free/planning"]
    assert state["roles"]["review"]["models"] == []
    with pytest.raises(DevServiceError) as error:
        ai_settings.save("demo", "review", "codex", "paid/review", "high")
    assert error.value.code == "AI_MODEL_NOT_ELIGIBLE"
    monkeypatch.setattr(ai_settings.workspace_cost_authorizations, "list_for_project", lambda project_id: [
        {"model": "paid/review", "status": "ACTIVE", "uses_remaining": 1,
         "active_executions": 0, "max_concurrent": 1}
    ])
    assert ai_settings.save("demo", "review", "codex", "paid/review", "high")["roles"]["review"]["valid"]
    with pytest.raises(DevServiceError) as error:
        ai_settings.save("demo", "development", "codex", "paid/review", "high")
    assert error.value.code == "AI_MODEL_NOT_ELIGIBLE"


def test_revocation_discovery_and_risk_are_revalidated(live_models, monkeypatch):
    ai_settings.save("demo", "maestro", "codex", "free/planning", "medium")
    assert ai_settings.require_saved("demo", "maestro", risk_level="low")["model_id"] == "free/planning"
    with pytest.raises(DevServiceError) as risk:
        ai_settings.require_saved("demo", "maestro", risk_level="high")
    assert risk.value.code == "AI_PREFERENCE_INELIGIBLE"
    live_models["models"] = [row for row in live_models["models"] if row["id"] != "free/planning"]
    assert ai_settings.state("demo", refresh=True)["roles"]["maestro"]["valid"] is False
    with pytest.raises(DevServiceError):
        ai_settings.require_saved("demo", "maestro")
    monkeypatch.setattr(ai_settings, "_codex_readiness", lambda: (False, False, "CLI missing"))
    assert ai_settings.state("demo")["roles"]["maestro"]["models"] == []


def test_concurrent_preference_writes_remain_complete(ai_db):
    choices = [("maestro", "planning"), ("development", "coding"), ("review", "review")]
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda choice: workspace_ai_preferences.save("demo", choice[0], "codex", choice[1], "low"), choices))
    assert set(workspace_ai_preferences.read("demo")) == {"maestro", "development", "review"}


def test_admin_endpoints_require_loopback_and_exact_token(live_models, monkeypatch):
    from nexus.web import routes

    monkeypatch.setenv("NEXUS_ADMIN_TOKEN", "x" * 32)
    body = routes.AiPreferenceRequest(project_id="demo", role="development", executor="codex",
                                      model_id="free/coding", effort="low")

    def request(host, token=None):
        headers = [] if token is None else [(b"authorization", ("Bearer " + token).encode())]
        return Request({"type": "http", "method": "POST", "path": "/api/dev/ai-settings",
                        "client": (host, 50000), "headers": headers})

    with pytest.raises(HTTPException) as missing:
        routes.save_ai_preference(body, request("127.0.0.1"))
    assert missing.value.status_code == 401
    assert routes.ai_settings_admin_check(request("127.0.0.1", "x" * 32)) == {"authenticated": True}
    assert routes.save_ai_preference(body, request("127.0.0.1", "x" * 32))["roles"]["development"]["valid"]
    with pytest.raises(HTTPException) as remote:
        routes.clear_ai_preference(routes.AiPreferenceClearRequest(project_id="demo", role="development"),
                                   request("192.168.1.4", "x" * 32))
    assert remote.value.status_code == 403


def test_manual_pane_uses_revalidated_development_default_without_execution(live_models, monkeypatch):
    ai_settings.save("demo", "development", "codex", "free/coding", "low")
    captured = {}
    monkeypatch.setattr(service.store, "count_open_panes", lambda project_id: 0)
    monkeypatch.setattr(service, "_require_approved_model", lambda model: {"id": model, "cost_status": "free"})
    monkeypatch.setattr("nexus.dev.service.create_dev_session", lambda project_id, requested_model: {
        "session_id": "session-1", "requested_model": requested_model,
    })
    monkeypatch.setattr(service.store, "create_pane", lambda **kwargs: captured.update(kwargs) or "pane-1")
    monkeypatch.setattr(service.store, "get_pane", lambda pane_id: {"model": captured["model"]})
    monkeypatch.setattr(service, "pane_view", lambda row: row)
    assert service.create_pane("demo", role="implementer")["model"] == "free/coding"
    assert captured["model"] == "free/coding"


def test_paid_review_only_route_can_receive_independent_bounded_grant(ai_db, monkeypatch):
    monkeypatch.setattr("nexus.dev.service.validate_dev_project", lambda project_id: "local")
    monkeypatch.setattr("nexus.dev.models.list_dev_models", lambda: {"models": [], "catalog_error": None})
    monkeypatch.setattr("nexus.dev.models.list_dev_model_catalog", lambda: {
        "models": [{"id": "paid/review", "active": True, "discovered": True,
                    "approval_status": "approved", "cost_status": "standard"}],
        "catalog_error": None,
    })
    grant = cost_authorizations.authorize("demo", "paid/review", confirm_cost=True,
                                          max_uses=2, validity_seconds=3600, max_concurrent=1)
    assert grant["uses_remaining"] == 2


def test_maestro_saved_effort_requires_matching_chat_session(ai_db, monkeypatch):
    monkeypatch.setattr("nexus.registry.chat_sessions.get_session", lambda session_id: {"requested_effort": "low"})
    assert maestro._planning_session_matches("session-1", {"effort": "low"})
    assert not maestro._planning_session_matches("session-1", {"effort": "high"})
    assert maestro._planning_session_matches("session-1", {"id": "legacy"})
