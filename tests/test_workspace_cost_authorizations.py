"""Persistent project/model cost grants; all executions are mocked/local."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from nexus.registry import database
from nexus.registry import workspace_cost_authorizations as registry
from nexus.workspace import cost_authorizations, service


@pytest.fixture
def cost_db(tmp_path):
    original_path, original_dir = database.DATABASE_PATH, database.STORAGE_DIR
    database.STORAGE_DIR = tmp_path / "cost-auth-test"
    database.DATABASE_PATH = database.STORAGE_DIR / "nexus.db"
    database.initialize_database()
    with database.get_connection() as connection:
        connection.execute(
            "INSERT INTO projects (id,name,path,enabled,dev_enabled) VALUES ('demo','Demo',?,1,1)",
            (str(database.STORAGE_DIR),),
        )
    try:
        yield
    finally:
        import shutil
        shutil.rmtree(database.STORAGE_DIR, ignore_errors=True)
        database.DATABASE_PATH, database.STORAGE_DIR = original_path, original_dir


def _grant(*, uses=5, concurrent=2, expires=None):
    return registry.create(
        "demo", "paid/model", "premium", max_uses=uses,
        expires_at=(expires or datetime.now(UTC) + timedelta(days=1)).isoformat(),
        max_concurrent=concurrent,
    )


def test_persistent_grant_survives_database_reinitialization(cost_db):
    grant = _grant(uses=7, concurrent=1)
    active_lease = registry.reserve("demo", "paid/model")
    assert active_lease is not None
    database.initialize_database()
    state = registry.list_for_project("demo")
    assert state[0]["authorization_id"] == grant["authorization_id"]
    assert state[0]["uses_remaining"] == 6
    assert state[0]["active_executions"] == 1
    assert state[0]["status"] == "ACTIVE"
    assert registry.reserve("demo", "paid/model") is None
    registry.release(active_lease[0])


def test_reservations_consume_usage_and_enforce_concurrency_atomically(cost_db):
    _grant(uses=10, concurrent=1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: registry.reserve("demo", "paid/model"), range(8)))
    leases = [result for result in results if result is not None]
    assert len(leases) == 1
    assert registry.list_for_project("demo")[0]["uses_reserved"] == 1
    registry.release(leases[0][0])
    second = registry.reserve("demo", "paid/model")
    assert second is not None
    assert registry.list_for_project("demo")[0]["uses_remaining"] == 8


def test_expiration_and_revocation_fail_closed(cost_db):
    expired = _grant(expires=datetime.now(UTC) - timedelta(seconds=1))
    assert registry.reserve("demo", "paid/model") is None
    assert next(row for row in registry.list_for_project("demo")
                if row["authorization_id"] == expired["authorization_id"])["status"] == "EXPIRED"
    active = _grant()
    assert registry.revoke("demo", active["authorization_id"])
    assert registry.reserve("demo", "paid/model") is None
    assert not registry.revoke("demo", active["authorization_id"])


def test_use_limit_is_consumed_even_when_lease_is_released(cost_db):
    _grant(uses=1, concurrent=1)
    lease = registry.reserve("demo", "paid/model")
    assert lease is not None
    registry.release(lease[0])
    assert registry.reserve("demo", "paid/model") is None
    state = registry.list_for_project("demo")[0]
    assert state["uses_remaining"] == 0
    assert state["active_executions"] == 0


def test_reauthorization_keeps_in_flight_execution_inside_concurrency_limit(cost_db):
    old = _grant(uses=3, concurrent=1)
    active = registry.reserve("demo", "paid/model")
    assert active is not None
    registry.revoke("demo", old["authorization_id"])
    _grant(uses=3, concurrent=1)
    assert registry.reserve("demo", "paid/model") is None
    assert registry.list_for_project("demo")[0]["active_executions"] == 1
    registry.release(active[0])
    assert registry.reserve("demo", "paid/model") is not None


def test_execution_rechecks_approved_model_and_holds_a_durable_lease(cost_db, monkeypatch):
    catalog = {"models": [{"id": "paid/model", "cost_status": "premium"}], "catalog_error": None}
    monkeypatch.setattr("nexus.dev.models.list_dev_models", lambda: catalog)
    cost_authorizations.authorize("demo", "paid/model", confirm_cost=True, max_uses=2,
                                  validity_seconds=3600, max_concurrent=1)
    monkeypatch.setattr(service, "_check_execution_gates", lambda *args, **kwargs: False)
    pane = {"id": "pane-1", "plan_id": None, "project_id": "demo", "model": "paid/model"}
    _by_plan, mission = service._run_gated(pane, None, lambda _covered: {
        "mission_id": "mission-1", "status": "PENDING",
    })
    assert mission["mission_id"] == "mission-1"
    state = registry.list_for_project("demo")[0]
    assert state["uses_remaining"] == 1
    assert state["active_executions"] == 1

    catalog["models"] = []
    with pytest.raises(Exception, match="not an approved live route"):
        service._run_gated(pane, None, lambda _covered: {"mission_id": "must-not-start", "status": "PENDING"})
    assert registry.list_for_project("demo")[0]["uses_reserved"] == 1
    registry.release_execution("mission", "mission-1")
    assert registry.list_for_project("demo")[0]["active_executions"] == 0


def test_cost_authorization_routes_query_create_and_revoke(cost_db, monkeypatch):
    monkeypatch.setattr("nexus.dev.service.validate_dev_project", lambda _project: "local-project")
    monkeypatch.setattr("nexus.dev.models.list_dev_models", lambda: {
        "models": [{"id": "paid/model", "cost_status": "standard"}], "catalog_error": None,
    })
    from nexus.web.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    listed = client.get("/api/workspace/cost-authorizations", params={"project_id": "demo"})
    assert listed.status_code == 200 and listed.json()["authorizations"] == []
    created = client.post("/api/workspace/cost-authorizations", json={
        "project_id": "demo", "model": "paid/model", "confirm_cost": True,
        "max_uses": 4, "validity_seconds": 3600, "max_concurrent": 2,
    })
    assert created.status_code == 200
    auth_id = created.json()["authorization_id"]
    assert created.json()["uses_remaining"] == 4
    revoked = client.delete(f"/api/workspace/cost-authorizations/{auth_id}", params={"project_id": "demo"})
    assert revoked.status_code == 200 and revoked.json()["status"] == "REVOKED"
    state = client.get("/api/workspace/cost-authorizations", params={"project_id": "demo"}).json()
    assert state["authorizations"][0]["status"] == "REVOKED"
