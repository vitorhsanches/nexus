"""P5.UIA: HTTP route for binding a Codex Native review to a coordinated
plan step (POST /api/workspace/plans/{plan_id}/steps/{agent_key}/review).

coordinator.bind_native_review already has full Python-level coverage in
test_workspace_coordinator.py (identity, backend, staleness, fencing,
replay). These tests cover the HTTP layer itself -- request validation,
routing, and status/error-code passthrough -- with exactly one full
wire-through per scenario, never re-deriving the lower-level logic.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from nexus.dev.service import create_dev_session
from nexus.registry import database, dev_integrations, dev_sessions
from nexus.registry import workspace as store
from nexus.workspace import coordinator

FREE = {"id": "free/model", "cost_status": "free"}
ROUTE = "/api/workspace/plans/{plan_id}/steps/{agent_key}/review"


def _git(root, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


def _client() -> TestClient:
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def native_plan_env(tmp_path, monkeypatch):
    original_db, original_storage = database.DATABASE_PATH, database.STORAGE_DIR
    database.STORAGE_DIR = tmp_path / "storage"
    database.DATABASE_PATH = database.STORAGE_DIR / "nexus.db"
    monkeypatch.setenv("NEXUS_DEV_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.setenv("NEXUS_DEV_EXECUTION_ENABLED", "1")
    database.initialize_database()
    monkeypatch.setattr("nexus.dev.models.list_dev_models", lambda: {"models": [FREE], "catalog_error": None})
    monkeypatch.setattr("nexus.chat.service.list_models", lambda **kwargs: {
        "backend": "codex_native",
        "models": [{"id": "free/model", "cost_status": "known"}],
        "catalog_error": None,
    })
    monkeypatch.setattr("nexus.dev.executor.validate_native_choice", lambda model, effort: None)
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "api.py").write_text("def api():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    with database.get_connection() as connection:
        connection.execute(
            "INSERT INTO projects (id, name, path, enabled, dev_enabled) VALUES ('demo', 'Demo', ?, 1, 1)",
            (str(repo),),
        )
    proposal = {
        "schema_version": "maestro-plan/1", "source": "llm", "complexity": "simple",
        "summary": "one native agent", "explanation": "one", "needs_clarification": None,
        "agents": [{"key": "api", "role": "backend", "title": "API", "model": "free/model",
                    "task": "API", "acceptance_criteria": ["ok"], "depends_on": [], "touches": ["api.py"]}],
        "execution": {"mode": "sequential", "order": ["api"]}, "limit": 3, "fallback_reason": None,
    }
    plan_id = store.create_plan("demo", "one native agent", policy_version="maestro-plan/1",
                                complexity="simple", proposal=proposal, source="llm")
    approval = {
        "backend": "codex_native",
        "agent_models": {"api": {"model": "free/model", "cost_status": "free", "backend": "codex_native"}},
        "max_turns_per_agent": 1, "mission_timeout_seconds": 60, "cost_confirmed_for": ["free/model"],
    }
    with database.get_connection() as connection:
        connection.execute("UPDATE workspace_plans SET status='APPROVED', approval_json=? WHERE id=?",
                          (json.dumps(approval), plan_id))
    source = create_dev_session("demo", backend="codex_native")
    other = create_dev_session("demo", backend="codex_native")
    authorization = {"model": "free/model", "cost_status": "free", "max_turns": 1, "mission_timeout_seconds": 60}
    api_pane = store.create_pane(
        project_id="demo", dev_session_id=source["session_id"], origin="maestro",
        role="backend", title="API", model="free/model", plan_id=plan_id,
        plan_agent_key="api", brief={"task": "API", "acceptance_criteria": ["ok"],
                                      "depends_on": [], "authorization": authorization},
    )
    yield {"plan_id": plan_id, "repo": repo, "source": source, "other": other, "api_pane": api_pane}
    for worker in list(coordinator._workers.values()):
        worker.join(timeout=5)
        assert not worker.is_alive(), "simulated worker leaked past fixture teardown"
    with coordinator._workers_lock:
        coordinator._workers.clear()
    database.DATABASE_PATH, database.STORAGE_DIR = original_db, original_storage


def _review_id(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _save_review(session_id: str, review_id: str, *, approval_ready: bool = True, status: str = "REVIEWED"):
    record = dev_integrations.save_review(
        review_id=review_id, dev_session_id=session_id, project_id="demo",
        base_sha="base", target_head="base", target_branch="main",
        snapshot={"diff": {"files": [], "truncated": False, "review_id": "fingerprint"},
                 "approval_ready": approval_ready},
    )
    if status != "REVIEWED":
        dev_integrations.update_review(review_id, status=status)
        record = dev_integrations.get_review(review_id)
    return record


def _fake_run_dev_turn(calls: list):
    def run(dev_session_id, prompt, *, model=None, effort=None):
        turn_id = dev_sessions.create_turn(dev_session_id, prompt, requested_model=model)
        calls.append(turn_id)
        return {"turn_id": turn_id, "dev_session_id": dev_session_id, "status": "PENDING"}
    return run


def _wait(plan_id: str, expected: set[str], timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = coordinator.state(plan_id)
        if state["status"] in expected:
            return state
        time.sleep(0.02)
    return coordinator.state(plan_id)


def _launch(env, monkeypatch) -> str:
    """Authorize and start the plan's one native step; return its turn id."""
    turns: list[str] = []
    monkeypatch.setattr("nexus.dev.executor.run_dev_turn", _fake_run_dev_turn(turns))
    coordinator.authorize_execution(env["plan_id"], agent_keys=["api"], test_profiles={},
                                    max_turns_per_agent=1, mission_timeout_seconds=60)
    coordinator.start(env["plan_id"])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not turns:
        time.sleep(0.02)
    assert turns, "the native turn was never launched"
    return turns[0]


def _awaiting_review(env, monkeypatch) -> str:
    """Drive the plan to AWAITING_NATIVE_REVIEW; return the completed turn id."""
    turn_id = _launch(env, monkeypatch)
    dev_sessions.update_turn_status(turn_id, "COMPLETED", response_text="done")
    state = _wait(env["plan_id"], {"BLOCKED"})
    assert state["error_code"] == "AWAITING_NATIVE_REVIEW"
    return turn_id


# -- golden path -------------------------------------------------------------


def test_bind_review_route_accepts_a_valid_review(native_plan_env, monkeypatch):
    env = native_plan_env
    _awaiting_review(env, monkeypatch)
    review_id = _review_id("valid")
    _save_review(env["source"]["session_id"], review_id)
    response = _client().post(
        ROUTE.format(plan_id=env["plan_id"], agent_key="api"),
        json={"review_id": review_id},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["steps"][0]["native_review_id"] == review_id
    assert body["steps"][0]["status"] in {"READY_FOR_REVIEW", "AWAITING_REVIEW"}
    # Never auto-advances past what the sequence itself already does: the
    # route created no review, approved/transferred no handoff, and
    # resumed nothing beyond the one native step it unblocked -- the
    # worker it spawned just carries that single step to its natural end.
    final = _wait(env["plan_id"], {"AWAITING_REVIEW"})
    assert final["status"] == "AWAITING_REVIEW"
    assert final["result"]["human_approval_required"] is True
    assert final["result"]["integration_applied"] is False


# -- negative: identity and freshness ---------------------------------------


def test_bind_review_route_rejects_a_review_from_another_session(native_plan_env, monkeypatch):
    env = native_plan_env
    _awaiting_review(env, monkeypatch)
    review_id = _review_id("other-session")
    _save_review(env["other"]["session_id"], review_id)
    response = _client().post(
        ROUTE.format(plan_id=env["plan_id"], agent_key="api"),
        json={"review_id": review_id},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "REVIEW_NOT_FOUND"
    assert coordinator.state(env["plan_id"])["steps"][0]["status"] == "AWAITING_NATIVE_REVIEW"


def test_bind_review_route_rejects_a_rejected_review(native_plan_env, monkeypatch):
    env = native_plan_env
    _awaiting_review(env, monkeypatch)
    review_id = _review_id("rejected")
    _save_review(env["source"]["session_id"], review_id, status="REJECTED")
    response = _client().post(
        ROUTE.format(plan_id=env["plan_id"], agent_key="api"),
        json={"review_id": review_id},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "NATIVE_REVIEW_NOT_READY"


def test_bind_review_route_rejects_an_incomplete_review(native_plan_env, monkeypatch):
    env = native_plan_env
    _awaiting_review(env, monkeypatch)
    review_id = _review_id("incomplete")
    _save_review(env["source"]["session_id"], review_id, approval_ready=False)
    response = _client().post(
        ROUTE.format(plan_id=env["plan_id"], agent_key="api"),
        json={"review_id": review_id},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "NATIVE_REVIEW_INCOMPLETE"


def test_bind_review_route_rejects_a_stale_review(native_plan_env, monkeypatch):
    env = native_plan_env
    _awaiting_review(env, monkeypatch)
    stale = _review_id("first")
    _save_review(env["source"]["session_id"], stale)
    _save_review(env["source"]["session_id"], _review_id("second"))
    response = _client().post(
        ROUTE.format(plan_id=env["plan_id"], agent_key="api"),
        json={"review_id": stale},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "NATIVE_REVIEW_STALE"


# -- negative: execution state -----------------------------------------------


def test_bind_review_route_rejects_binding_while_the_turn_is_still_active(native_plan_env, monkeypatch):
    env = native_plan_env
    turn_id = _launch(env, monkeypatch)  # turn still PENDING -- not completed yet
    review_id = _review_id("too-early")
    _save_review(env["source"]["session_id"], review_id)
    response = _client().post(
        ROUTE.format(plan_id=env["plan_id"], agent_key="api"),
        json={"review_id": review_id},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "NATIVE_REVIEW_NOT_AWAITED"
    # Let the still-polling worker finish so it does not outlive this test.
    dev_sessions.update_turn_status(turn_id, "COMPLETED", response_text="done")
    _wait(env["plan_id"], {"BLOCKED"})


def test_bind_review_route_rejects_an_unknown_plan(native_plan_env):
    response = _client().post(
        ROUTE.format(plan_id="WSPLAN-DOES-NOT-EXIST", agent_key="api"),
        json={"review_id": _review_id("whatever")},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "EXECUTION_NOT_AUTHORIZED"


def test_bind_review_route_rejects_an_invalid_agent_key(native_plan_env, monkeypatch):
    env = native_plan_env
    _awaiting_review(env, monkeypatch)
    review_id = _review_id("wrong-key")
    _save_review(env["source"]["session_id"], review_id)
    response = _client().post(
        ROUTE.format(plan_id=env["plan_id"], agent_key="not-a-real-agent"),
        json={"review_id": review_id},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "NATIVE_REVIEW_NOT_AWAITED"


def test_bind_review_route_surfaces_a_disabled_project_cleanly(native_plan_env, monkeypatch):
    env = native_plan_env
    _awaiting_review(env, monkeypatch)
    review_id = _review_id("project-disabled")
    _save_review(env["source"]["session_id"], review_id)
    with database.get_connection() as connection:
        connection.execute("UPDATE projects SET dev_enabled = 0 WHERE id = 'demo'")
    response = _client().post(
        ROUTE.format(plan_id=env["plan_id"], agent_key="api"),
        json={"review_id": review_id},
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "DEV_MODE_DISABLED"


def test_bind_review_route_refuses_a_duplicate_bind_attempt(native_plan_env, monkeypatch):
    env = native_plan_env
    _awaiting_review(env, monkeypatch)
    first = _review_id("first-bind")
    _save_review(env["source"]["session_id"], first)
    client = _client()
    ok = client.post(ROUTE.format(plan_id=env["plan_id"], agent_key="api"), json={"review_id": first})
    assert ok.status_code == 200, ok.text
    replay = _review_id("replay-attempt")
    _save_review(env["source"]["session_id"], replay)
    again = client.post(ROUTE.format(plan_id=env["plan_id"], agent_key="api"), json={"review_id": replay})
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["code"] == "NATIVE_REVIEW_NOT_AWAITED"


# -- negative: request shape (no DB access needed) ---------------------------


def test_bind_review_route_rejects_a_malformed_review_id(native_plan_env, monkeypatch):
    env = native_plan_env
    _awaiting_review(env, monkeypatch)
    response = _client().post(
        ROUTE.format(plan_id=env["plan_id"], agent_key="api"),
        json={"review_id": "not-a-sha256"},
    )
    assert response.status_code == 422, response.text


def test_bind_review_route_rejects_unknown_fields(native_plan_env, monkeypatch):
    env = native_plan_env
    _awaiting_review(env, monkeypatch)
    response = _client().post(
        ROUTE.format(plan_id=env["plan_id"], agent_key="api"),
        json={"review_id": _review_id("x"), "apply": True},
    )
    assert response.status_code == 422, response.text


def test_bind_review_route_rejects_a_missing_review_id(native_plan_env, monkeypatch):
    env = native_plan_env
    _awaiting_review(env, monkeypatch)
    response = _client().post(ROUTE.format(plan_id=env["plan_id"], agent_key="api"), json={})
    assert response.status_code == 422, response.text


def test_bind_review_route_rejects_cross_origin_requests(native_plan_env, monkeypatch):
    env = native_plan_env
    _awaiting_review(env, monkeypatch)
    response = _client().post(
        ROUTE.format(plan_id=env["plan_id"], agent_key="api"),
        json={"review_id": _review_id("x")},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "CHAT_ORIGIN_FORBIDDEN"
