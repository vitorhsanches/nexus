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
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nexus.dev.service import create_dev_session
from nexus.dev.worktree import read_worktree_diff
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


def _save_current_review(session: dict, label: str, base_sha: str) -> str:
    """Persist review evidence from the exact current simulated worktree diff."""
    diff = read_worktree_diff(session["worktree_path"], session["base_sha"])
    session_id = (
        session["session_id"] if "session_id" in session.keys() else session["id"]
    )
    review_id = _review_id(f"{session_id}-{label}")
    dev_integrations.save_review(
        review_id=review_id, dev_session_id=session_id, project_id="demo",
        base_sha=session["base_sha"], target_head=base_sha, target_branch="main",
        snapshot={
            "session_id": session_id,
            "project_id": "demo",
            "base_sha": session["base_sha"],
            "target_head": base_sha,
            "target_branch": "main",
            "diff": diff,
            "approval_ready": bool(diff.get("approval_ready")),
        },
    )
    return review_id


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


def test_manual_two_native_agents_review_handoff_resume_http_e2e(
    native_plan_env, monkeypatch,
):
    """Full two-agent HTTP flow with real disposable worktrees and fake turns."""
    from nexus.workspace import service

    env = native_plan_env
    client = _client()
    repo = env["repo"]
    original_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
    ).strip()
    original_status = subprocess.check_output(
        ["git", "status", "--porcelain=v1"], cwd=repo, text=True,
    )
    original_content = (repo / "api.py").read_text(encoding="utf-8")
    monkeypatch.setattr("nexus.dev.models.list_dev_models", lambda: pytest.fail(
        "Native plan path must not consult the OmniRoute catalog"
    ))
    monkeypatch.setattr("nexus.chat.service.list_models", lambda **kwargs: {
        "backend": "codex_native",
        "models": [{"id": "gpt-native", "cost_status": "known"}],
        "catalog_error": None,
    })
    monkeypatch.setattr(
        "nexus.dev.executor.validate_native_choice", lambda model, effort: None
    )

    proposed = client.post("/api/workspace/plans/manual", json={
        "project_id": "demo",
        "task_text": "Implementar e validar uma pequena alteração.",
        "agents": [
            {"key": "implement", "role": "backend", "title": "Implementação",
             "task": "Alterar api.py para retornar 2.",
             "acceptance_criteria": ["api() retorna 2"], "depends_on": [],
             "touches": ["api.py"]},
            {"key": "verify", "role": "tester", "title": "Validação",
             "task": "Adicionar teste para api().",
             "acceptance_criteria": ["Teste cobre api()"],
             "depends_on": ["implement"], "touches": ["tests/"]},
        ], "execution_order": ["implement", "verify"],
    })
    assert proposed.status_code == 200, proposed.text
    plan_id = proposed.json()["plan_id"]
    assert proposed.json()["status"] == "PROPOSED"
    assert proposed.json()["execution"] is None and proposed.json()["approval"] is None
    assert all(
        pane["plan_id"] != plan_id
        for pane in service.list_panes("demo", include_hidden=True)
    )

    approved = client.post(f"/api/workspace/plans/{plan_id}/approve", json={
        "agent_models": {"implement": "gpt-native", "verify": "gpt-native"},
        "backend": "codex_native",
        "agent_backends": {"implement": "codex_native", "verify": "codex_native"},
        "agent_efforts": {"implement": "high", "verify": "low"},
        "max_turns_per_agent": 1, "mission_timeout_seconds": 60, "confirm_costs": True,
    })
    assert approved.status_code == 200, approved.text
    plan = approved.json()["plan"]
    assert plan["approval"]["agent_models"]["implement"]["effort"] == "high"
    assert plan["approval"]["agent_models"]["verify"]["effort"] == "low"
    panes_by_key = {pane["plan_agent_key"]: pane for pane in approved.json()["panes"]}
    assert {
        pane["session"]["backend"] for pane in approved.json()["panes"]
    } == {"codex_native"}
    assert panes_by_key["verify"]["brief"]["depends_on"] == ["implement"]

    turns: list[dict] = []

    def fake_turn(session_id, prompt, *, model=None, effort=None):
        turn_id = dev_sessions.create_turn(session_id, prompt, requested_model=model)
        turns.append({"turn_id": turn_id, "session_id": session_id,
                      "model": model, "effort": effort})
        return {"turn_id": turn_id, "dev_session_id": session_id, "status": "PENDING"}

    monkeypatch.setattr("nexus.dev.executor.run_dev_turn", fake_turn)
    authorized = client.post(f"/api/workspace/plans/{plan_id}/execute", json={
        "agent_keys": ["implement", "verify"], "test_profiles": {},
        "max_turns_per_agent": 1, "mission_timeout_seconds": 60,
    })
    assert authorized.status_code == 200, authorized.text
    assert authorized.json()["status"] == "RUNNING"
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and len(turns) < 1:
        time.sleep(0.02)
    assert len(turns) == 1
    assert turns[0]["session_id"] == panes_by_key["implement"]["session"]["session_id"]
    assert (turns[0]["model"], turns[0]["effort"]) == ("gpt-native", "high")

    source_session = dev_sessions.get_session(turns[0]["session_id"])
    Path(source_session["worktree_path"], "api.py").write_text(
        "def api():\n    return 2\n", encoding="utf-8",
    )
    dev_sessions.update_turn_status(
        turns[0]["turn_id"], "COMPLETED", response_text="done"
    )
    assert _wait(plan_id, {"BLOCKED"})["error_code"] == "AWAITING_NATIVE_REVIEW"
    review_a = _save_current_review(source_session, "agent-a", original_head)
    bind_a = client.post(
        ROUTE.format(plan_id=plan_id, agent_key="implement"),
        json={"review_id": review_a},
    )
    assert bind_a.status_code == 200, bind_a.text
    blocked = _wait(plan_id, {"BLOCKED"})
    assert blocked["error_code"] == "DEPENDENCY_WORKTREE_NOT_TRANSFERRED"
    assert len(turns) == 1
    assert client.post(f"/api/workspace/plans/{plan_id}/resume").status_code == 409

    preview = client.post("/api/workspace/handoffs/preview", json={
        "source_pane_id": panes_by_key["implement"]["pane_id"],
        "target_pane_id": panes_by_key["verify"]["pane_id"],
        "source_mission_id": review_a,
    })
    assert preview.status_code == 200, preview.text
    handoff = preview.json()
    denied_transfer = client.post(
        f"/api/workspace/handoffs/{handoff['handoff_id']}/transfer"
    )
    assert denied_transfer.status_code == 409
    approved_handoff = client.post(
        f"/api/workspace/handoffs/{handoff['handoff_id']}/approve", json={
            "fingerprint": handoff["snapshot_fingerprint"],
            "source_pane_id": panes_by_key["implement"]["pane_id"],
            "target_pane_id": panes_by_key["verify"]["pane_id"],
        },
    )
    assert approved_handoff.status_code == 200, approved_handoff.text
    assert client.post(f"/api/workspace/plans/{plan_id}/resume").status_code == 409
    transferred = client.post(
        f"/api/workspace/handoffs/{handoff['handoff_id']}/transfer"
    )
    assert transferred.status_code == 200, transferred.text
    assert transferred.json()["status"] == "TRANSFERRED"
    resumed = client.post(f"/api/workspace/plans/{plan_id}/resume")
    assert resumed.status_code == 200, resumed.text
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and len(turns) < 2:
        time.sleep(0.02)
    assert len(turns) == 2
    assert turns[1]["session_id"] == panes_by_key["verify"]["session"]["session_id"]
    assert (turns[1]["model"], turns[1]["effort"]) == ("gpt-native", "low")

    target_session = dev_sessions.get_session(turns[1]["session_id"])
    Path(target_session["worktree_path"], "tests").mkdir()
    Path(target_session["worktree_path"], "tests", "test_api.py").write_text(
        "from api import api\n\ndef test_api():\n    assert api() == 2\n",
        encoding="utf-8",
    )
    dev_sessions.update_turn_status(
        turns[1]["turn_id"], "COMPLETED", response_text="done"
    )
    assert _wait(plan_id, {"BLOCKED"})["error_code"] == "AWAITING_NATIVE_REVIEW"
    review_b = _save_current_review(target_session, "agent-b", original_head)
    bind_b = client.post(
        ROUTE.format(plan_id=plan_id, agent_key="verify"), json={"review_id": review_b},
    )
    assert bind_b.status_code == 200, bind_b.text
    final = _wait(plan_id, {"AWAITING_REVIEW"})
    assert final["status"] == "AWAITING_REVIEW"
    assert final["result"]["human_approval_required"] is True
    assert final["result"]["integration_applied"] is False
    assert len(turns) == 2
    final_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
    ).strip()
    final_status = subprocess.check_output(
        ["git", "status", "--porcelain=v1"], cwd=repo, text=True,
    )
    assert final_head == original_head
    assert final_status == original_status
    assert (repo / "api.py").read_text(encoding="utf-8") == original_content


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
