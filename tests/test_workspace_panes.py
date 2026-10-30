"""Multi-pane workspace (P1): policy, persistence, isolation and routes.

Panes are created through the real Development service, so these tests use
a real disposable Git repository and real worktrees under a temporary
NEXUS_DEV_WORKTREE_ROOT. No model is ever called: mission start is
observed at the mission_runner.start_mission boundary.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nexus.dev.service import DevServiceError
from nexus.registry import database, dev_sessions, workspace as workspace_store
from nexus.workspace import maestro, policy, service

FREE = {"id": "free/model", "cost_status": "free"}
STANDARD = {"id": "std/model", "cost_status": "standard"}
LOW = {"id": "low/model", "cost_status": "low_cost"}
NATIVE = {"id": "gpt-native", "cost_status": "known"}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    original_db, original_storage = database.DATABASE_PATH, database.STORAGE_DIR
    database.STORAGE_DIR = tmp_path / "storage"
    database.DATABASE_PATH = database.STORAGE_DIR / "nexus.db"
    database.initialize_database()
    monkeypatch.setenv("NEXUS_DEV_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("NEXUS_DEV_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.delenv("NEXUS_WORKSPACE_MAX_PANES", raising=False)
    monkeypatch.delenv("NEXUS_WORKSPACE_MAX_AUTO_PANES", raising=False)
    catalog = {"models": [FREE, STANDARD], "default_model": "free/model", "catalog_error": None}
    monkeypatch.setattr("nexus.dev.models.list_dev_models", lambda: catalog)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "init")
    with database.get_connection() as connection:
        connection.execute(
            "INSERT INTO projects (id, name, path, enabled, dev_enabled) VALUES ('demo','Demo',?,1,1)", (str(repo),)
        )
        connection.execute(
            "INSERT INTO projects (id, name, path, enabled, dev_enabled) VALUES ('locked','Locked',?,1,0)", (str(repo),)
        )
    yield {"repo": repo, "catalog": catalog}
    database.DATABASE_PATH, database.STORAGE_DIR = original_db, original_storage


# ── Policy ──────────────────────────────────────────────────────────────


def test_simple_task_keeps_a_single_implementer_and_test_passing_is_not_a_tester_role():
    proposal = policy.propose("Implemente multiply(a, b) e faça os testes passarem.", [FREE, STANDARD])
    assert proposal["complexity"] == "simple"
    assert [pane["role"] for pane in proposal["panes"]] == ["implementer"]
    assert proposal["panes"][0]["model"] == "free/model"
    assert proposal["panes"][0]["cost_authorization_required"] is False
    assert proposal["auto_start"] is False


def test_multi_responsibility_task_is_split_with_auditable_signals():
    proposal = policy.propose(
        "Crie a API de pedidos no backend, a tela no frontend, escreva testes e atualize a documentação.",
        [FREE],
        limit=4,
    )
    assert [pane["role"] for pane in proposal["panes"]] == ["backend", "frontend", "tester", "documenter"]
    assert all(pane["signals"] for pane in proposal["panes"])
    assert proposal["policy_version"] == policy.POLICY_VERSION


def test_policy_limit_is_enforced_and_reported(monkeypatch):
    monkeypatch.setenv("NEXUS_WORKSPACE_MAX_AUTO_PANES", "2")
    proposal = policy.propose("backend api e frontend ui, escrever testes, readme", [FREE])
    assert len(proposal["panes"]) == 2
    assert proposal["dropped_by_limit"] == ["tester", "documenter"]
    monkeypatch.setenv("NEXUS_WORKSPACE_MAX_AUTO_PANES", "99")
    assert policy.max_auto_panes() == policy.HARD_MAX_AUTO_PANES


def test_policy_never_suggests_a_paid_route_without_flagging_cost_authorization():
    proposal = policy.propose("implemente algo", [STANDARD])
    assert proposal["panes"][0]["model"] is None
    assert proposal["panes"][0]["cost_authorization_required"] is True
    empty = policy.propose("implemente algo", [])
    assert empty["panes"][0]["model"] is None
    assert policy.suggest_model([LOW, STANDARD])["model"] is None


# ── Panes: real sessions, isolation, persistence ────────────────────────


def test_each_pane_owns_its_own_real_session_and_isolated_worktree(ws_env):
    first = service.create_pane("demo", role="backend", model="free/model")
    second = service.create_pane("demo", role="frontend", title="UI agent")
    a, b = first["session"], second["session"]
    assert a["status"] == b["status"] == "ACTIVE"
    assert a["session_id"] != b["session_id"]
    assert a["branch"] != b["branch"]
    assert Path(a["worktree_path"]).resolve() != Path(b["worktree_path"]).resolve()
    assert Path(a["worktree_path"], "calc.py").is_file()
    assert first["origin"] == second["origin"] == "manual"
    assert second["title"] == "UI agent" and second["model"] is None
    # UNIQUE(dev_session_id): one session never backs two panes.
    with pytest.raises(sqlite3.IntegrityError), database.get_connection() as connection:
            connection.execute(
                "INSERT INTO workspace_panes (id, project_id, dev_session_id, origin, role, title) VALUES ('P2','demo',?,'manual','tester','x')",
                (a["session_id"],),
            )


def test_unauthorized_project_and_unapproved_model_create_nothing(ws_env):
    with pytest.raises(DevServiceError) as locked:
        service.create_pane("locked", role="implementer", model="free/model")
    assert locked.value.code == "DEV_MODE_DISABLED"
    with pytest.raises(DevServiceError) as model:
        service.create_pane("demo", role="implementer", model="not/approved")
    assert model.value.code == "MODEL_NOT_ALLOWED"
    with pytest.raises(DevServiceError) as role:
        service.create_pane("demo", role="root")
    assert role.value.code == "INVALID_ROLE"
    assert service.list_panes("demo", include_hidden=True) == []
    with database.get_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM dev_sessions").fetchone()[0] == 0


def test_codex_native_pane_uses_native_catalog_and_persists_backend(ws_env, monkeypatch):
    native_catalog = {"backend": "codex_native", "models": [NATIVE], "catalog_error": None}
    monkeypatch.setattr("nexus.chat.service.list_models", lambda **kwargs: native_catalog)
    validated = []
    monkeypatch.setattr("nexus.dev.executor.validate_native_choice", lambda model, effort: validated.append((model, effort)))
    monkeypatch.setattr("nexus.dev.models.list_dev_models", lambda **kwargs: pytest.fail("OmniRoute catalog must not be used"))

    pane = service.create_pane("demo", role="implementer", model="gpt-native", backend="codex_native")
    session = dev_sessions.get_session(pane["session"]["session_id"])

    assert validated == [("gpt-native", None), ("gpt-native", None)]
    assert pane["backend"] == pane["session"]["backend"] == "codex_native"
    assert pane["model"] == session["requested_model"] == "gpt-native"
    assert session["status"] == "ACTIVE"
    assert Path(session["worktree_path"]).is_dir()


def test_codex_native_invalid_model_and_session_failure_do_not_persist_panes(ws_env, monkeypatch):
    native_catalog = {"backend": "codex_native", "models": [], "catalog_error": None}
    monkeypatch.setattr("nexus.chat.service.list_models", lambda **kwargs: native_catalog)

    def reject_model(model, effort):
        raise DevServiceError("MODEL_NOT_ALLOWED", "not offered", http_status=422)

    monkeypatch.setattr("nexus.dev.executor.validate_native_choice", reject_model)
    with pytest.raises(DevServiceError) as invalid:
        service.create_pane("demo", role="implementer", model="gpt-invalid", backend="codex_native")
    assert invalid.value.code == "MODEL_NOT_ALLOWED"
    assert service.list_panes("demo") == []
    native_catalog["models"] = [NATIVE]
    monkeypatch.setattr("nexus.dev.executor.validate_native_choice", lambda model, effort: None)

    def fail_session(*args, **kwargs):
        raise DevServiceError("WORKTREE_CREATE_FAILED", "simulated failure", http_status=500)

    monkeypatch.setattr("nexus.dev.service.create_dev_session", fail_session)
    with pytest.raises(DevServiceError) as failed:
        service.create_pane("demo", role="implementer", model="gpt-native", backend="codex_native")
    assert failed.value.code == "WORKTREE_CREATE_FAILED"
    assert service.list_panes("demo") == []


def test_codex_native_pane_can_be_promoted_without_recreating_identity(ws_env, monkeypatch):
    native_catalog = {
        "backend": "codex_native", "models": [{**NATIVE, "supported_efforts": ["high"]}],
        "catalog_error": None, "account": {"authenticated": True},
    }
    monkeypatch.setattr("nexus.chat.service.list_models", lambda **kwargs: native_catalog)
    monkeypatch.setattr("nexus.dev.executor.validate_native_choice", lambda model, effort: None)
    pane = service.create_pane("demo", role="implementer", model="gpt-native", backend="codex_native", effort="high")
    session_id = pane["session"]["session_id"]
    worktree = pane["session"]["worktree_path"]

    promoted = service.promote_pane_to_maestro(pane["pane_id"])
    assert promoted["pane"]["pane_id"] == pane["pane_id"]
    assert promoted["pane"]["session"]["session_id"] == session_id
    assert promoted["pane"]["session"]["worktree_path"] == worktree
    assert promoted["pane"]["model"] == "gpt-native"
    assert promoted["pane"]["is_maestro"] is True
    assert promoted["maestro"]["maestro_pane_id"] == pane["pane_id"]
    assert promoted["maestro"]["planning"]["backend"] == "codex_native"

    second = service.create_pane("demo", role="tester", model="gpt-native", backend="codex_native")
    with pytest.raises(DevServiceError) as conflict:
        service.promote_pane_to_maestro(second["pane_id"])
    assert conflict.value.code == "MAESTRO_ALREADY_ASSIGNED"


def test_native_maestro_authorization_uses_existing_pane_backend(ws_env, monkeypatch):
    native_catalog = {
        "backend": "codex_native", "models": [{**NATIVE, "cost_status": "free", "supported_efforts": ["high"]}],
        "catalog_error": None, "account": {"authenticated": True},
    }
    monkeypatch.setattr("nexus.chat.service.list_models", lambda **kwargs: native_catalog)
    monkeypatch.setattr("nexus.dev.executor.validate_native_choice", lambda model, effort: None)
    pane = service.create_pane("demo", role="implementer", model="gpt-native", backend="codex_native")
    service.promote_pane_to_maestro(pane["pane_id"])
    calls = []

    def fake_create_session(project_id, model, effort=None, allow_unknown_cost=False, backend="omniroute"):
        calls.append({"project_id": project_id, "model": model, "backend": backend, "effort": effort})
        return {"session_id": "CHAT-NATIVE", "model": model}

    monkeypatch.setattr("nexus.chat.service.create_session", fake_create_session)
    state = maestro.authorize_planning("demo", "gpt-native", confirm_cost=False, effort="high")
    assert calls == [{"project_id": "demo", "model": "gpt-native", "backend": "codex_native", "effort": "high"}]
    assert state["maestro_pane_id"] == pane["pane_id"]
    assert state["planning"]["backend"] == "codex_native"
    assert state["planning"]["authorized"] is True


def test_approved_maestro_plan_persists_parent_pane_on_each_agent(ws_env, monkeypatch):
    monkeypatch.setattr("nexus.chat.service.list_models", lambda **kwargs: {
        "backend": "codex_native", "models": [{**NATIVE, "supported_efforts": ["high"]}],
        "catalog_error": None, "account": {"authenticated": True},
    })
    monkeypatch.setattr("nexus.dev.executor.validate_native_choice", lambda model, effort: None)
    maestro_pane = service.create_pane("demo", role="implementer", model="gpt-native", backend="codex_native")
    state = service.promote_pane_to_maestro(maestro_pane["pane_id"])["maestro"]
    plan_id = workspace_store.create_plan(
        "demo", "Implement a feature", policy_version="test", complexity="simple",
        proposal={"complexity": "simple", "summary": "Feature", "agents": [{
            "key": "impl", "role": "implementer", "title": "Implementer", "model": "gpt-native",
            "task": "Implement feature", "acceptance_criteria": ["Works"], "depends_on": [],
        }]},
        source="llm", conversation_id=state["conversation_id"],
    )
    result = service.approve_plan(
        plan_id, agent_models={"impl": "gpt-native"}, max_turns_per_agent=1,
        mission_timeout_seconds=60, confirm_costs=True,
    )
    assert result["panes"][0]["maestro_parent_pane_id"] == maestro_pane["pane_id"]
    database.initialize_database()
    assert service.get_pane(result["panes"][0]["pane_id"])["maestro_parent_pane_id"] == maestro_pane["pane_id"]

def test_codex_and_claude_panes_keep_distinct_sessions_backends_and_efforts(
    ws_env, monkeypatch,
):
    catalogs = {
        "codex_native": {"models": [
            {"id": "gpt-native", "supported_efforts": ["high"]},
        ]},
        "claude_native": {"models": [
            {"id": "sonnet", "supported_efforts": ["medium"]},
        ]},
    }
    monkeypatch.setattr(
        "nexus.chat.service.list_models", lambda *, backend: catalogs[backend],
    )
    codex_calls, claude_calls = [], []
    monkeypatch.setattr(
        "nexus.dev.executor.validate_native_choice",
        lambda model, effort: codex_calls.append((model, effort)),
    )
    monkeypatch.setattr(
        "nexus.dev.executor.validate_claude_choice",
        lambda model, effort: claude_calls.append((model, effort)),
    )
    monkeypatch.setattr(
        "nexus.dev.models.list_dev_models",
        lambda: pytest.fail("OmniRoute catalog used"),
    )

    codex = service.create_pane(
        "demo", role="implementer", model="gpt-native",
        backend="codex_native", effort="high",
    )
    claude = service.create_pane(
        "demo", role="implementer", model="sonnet",
        backend="claude_native", effort="medium",
    )
    assert codex["pane_id"] != claude["pane_id"]
    assert codex["session"]["session_id"] != claude["session"]["session_id"]
    assert codex["session"]["worktree_path"] != claude["session"]["worktree_path"]
    assert [(pane["backend"], pane["session"]["backend"], pane["model"], pane["effort"])
            for pane in service.list_panes("demo")] == [
                ("codex_native", "codex_native", "gpt-native", "high"),
                ("claude_native", "claude_native", "sonnet", "medium"),
            ]
    assert ("gpt-native", "high") in codex_calls
    assert ("sonnet", "medium") in claude_calls


def test_native_pane_rejects_unsupported_effort_before_session_creation(
    ws_env, monkeypatch,
):
    monkeypatch.setattr("nexus.chat.service.list_models", lambda *, backend: {
        "models": [{"id": "sonnet", "supported_efforts": ["medium"]}],
        "catalog_error": None,
    })

    def validate(model, effort):
        if effort != "medium":
            raise DevServiceError(
                "INVALID_EFFORT", "unsupported effort", http_status=422,
            )

    monkeypatch.setattr("nexus.dev.executor.validate_claude_choice", validate)
    with pytest.raises(DevServiceError, match="unsupported effort"):
        service.create_pane(
            "demo", role="implementer", model="sonnet",
            backend="claude_native", effort="xhigh",
        )
    assert service.list_panes("demo", include_hidden=True) == []
    with database.get_connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM dev_sessions").fetchone()[0]
        assert count == 0

def test_open_pane_limit_blocks_new_sessions(ws_env, monkeypatch):
    monkeypatch.setenv("NEXUS_WORKSPACE_MAX_PANES", "1")
    service.create_pane("demo", role="implementer")
    with pytest.raises(DevServiceError) as error:
        service.create_pane("demo", role="tester")
    assert error.value.code == "PANE_LIMIT_REACHED"


def test_hiding_a_pane_keeps_session_and_history_and_can_be_restored(ws_env):
    pane = service.create_pane("demo", role="implementer")
    hidden = service.update_pane(pane["pane_id"], layout={"hidden": True})
    assert hidden["layout"]["hidden"] is True
    assert service.list_panes("demo") == []
    state = service.workspace_state("demo")
    assert [item["pane_id"] for item in state["hidden_panes"]] == [pane["pane_id"]]
    assert hidden["session"]["status"] == "ACTIVE"
    restored = service.update_pane(pane["pane_id"], layout={"hidden": False, "collapsed": True, "expanded": True, "position": 5})
    assert restored["layout"] == {"position": 5, "collapsed": True, "expanded": True, "hidden": False}
    kinds = [event["kind"] for event in service.pane_history(pane["pane_id"])["events"]]
    assert kinds[-1] == "CREATED" and "HIDDEN" in kinds and "RESTORED" in kinds


def test_panes_and_plans_survive_reload_and_keep_their_association(ws_env):
    plan = service.plan_task("demo", "backend api e frontend ui")
    pane = service.create_pane("demo", role="backend", model="free/model", plan_id=plan["plan_id"])
    # A fresh read from SQLite (as after a server restart) returns the same identity.
    again = service.get_pane(pane["pane_id"])
    assert again["origin"] == "policy" and again["plan_id"] == plan["plan_id"]
    assert again["session"]["session_id"] == pane["session"]["session_id"]
    plans = service.list_plans("demo")
    assert plans[0]["plan_id"] == plan["plan_id"] and plans[0]["task_text"] == "backend api e frontend ui"
    with pytest.raises(DevServiceError) as other:
        service.create_pane("demo", role="tester", plan_id="WSPLAN-NOPE")
    assert other.value.code == "PLAN_NOT_FOUND"


# ── Models and missions ─────────────────────────────────────────────────


def test_model_change_is_validated_recorded_and_only_affects_future_missions(ws_env, monkeypatch):
    pane = service.create_pane("demo", role="implementer", model="free/model")
    running = [{
        "mission_id": "DEVMISSION-1", "status": "RUNNING", "requested_model": "free/model", "attempt_count": 1,
        "error_code": None, "error_message": None, "created_at": "t", "finished_at": None, "tests": [], "result": None,
    }]
    monkeypatch.setattr("nexus.dev.mission_runner.list_missions", lambda _sid: running)
    with pytest.raises(DevServiceError) as rejected:
        service.update_pane(pane["pane_id"], model="not/approved")
    assert rejected.value.code == "MODEL_NOT_ALLOWED"
    updated = service.update_pane(pane["pane_id"], model="std/model")
    assert updated["model"] == "std/model"
    assert running[0]["requested_model"] == "free/model"  # the active mission is untouched
    change = next(e for e in service.pane_history(pane["pane_id"])["events"] if e["kind"] == "MODEL_CHANGED")
    assert change["detail"] == {
        "from": "free/model", "to": "std/model", "applies_to": "future_turns_and_missions", "active_mission_unchanged": "DEVMISSION-1",
    }


def test_pane_mission_uses_pane_session_and_requires_exact_model_confirmation(ws_env, monkeypatch):
    pane = service.create_pane("demo", role="tester", title="QA", model="std/model")
    calls = []

    def fake_start(session_id, description, **kwargs):
        calls.append((session_id, description, kwargs))
        return {"mission_id": "DEVMISSION-9", "status": "PENDING"}

    monkeypatch.setattr("nexus.dev.mission_runner.start_mission", fake_start)
    with pytest.raises(DevServiceError) as unconfirmed:
        service.start_pane_mission(pane["pane_id"], "do it", confirm_model="free/model", test_profile="unit")
    assert unconfirmed.value.code == "MODEL_NOT_CONFIRMED" and calls == []

    with pytest.raises(DevServiceError) as unpaid:
        service.start_pane_mission(pane["pane_id"], "do it", confirm_model="std/model", test_profile="unit")
    assert unpaid.value.code == "COST_CONFIRMATION_REQUIRED" and calls == []
    result = service.start_pane_mission(pane["pane_id"], "  do it  ", confirm_model="std/model", confirm_cost=True, test_profile="unit")
    session_id, description, kwargs = calls[0]
    assert session_id == pane["session"]["session_id"]
    assert description.endswith("do it") and "QA" in description
    assert kwargs["model"] == "std/model" and kwargs["allowed_models"] == ["std/model"]
    assert result["mission"]["mission_id"] == "DEVMISSION-9"
    assert any(e["kind"] == "MISSION_STARTED" for e in service.pane_history(pane["pane_id"])["events"])

    bare = service.create_pane("demo", role="implementer")
    with pytest.raises(DevServiceError) as no_model:
        service.start_pane_mission(bare["pane_id"], "x", confirm_model="free/model", test_profile="unit")
    assert no_model.value.code == "MODEL_REQUIRED"


def test_pane_view_surfaces_review_state_without_applying(ws_env, monkeypatch):
    pane = service.create_pane("demo", role="implementer", model="free/model")
    ready = [{
        "mission_id": "DEVMISSION-2", "status": "READY_FOR_REVIEW", "interrupt_requested": True,
        "requested_model": "free/model", "attempt_count": 1,
        "error_code": None, "error_message": None, "created_at": "t", "finished_at": "t2",
        "tests": [{"attempt_number": 1, "status": "PASSED", "exit_code": 0}],
        "result": {"changed_files": [{"path": "calc.py", "status": "modified"}], "human_approval_required": True, "integration_applied": False},
    }]
    monkeypatch.setattr("nexus.dev.mission_runner.list_missions", lambda _sid: ready)
    view = service.get_pane(pane["pane_id"])
    latest = view["latest_mission"]
    assert view["busy"] is False
    assert latest["status"] == "READY_FOR_REVIEW" and latest["human_approval_required"] is True
    assert latest["interrupt_requested"] is True
    assert latest["integration_applied"] is False
    assert latest["changed_files"] == [{"path": "calc.py", "status": "modified"}]


def test_pane_view_exposes_only_its_active_dev_turn_for_streaming(ws_env):
    pane = service.create_pane("demo", role="implementer", model="free/model")
    assert pane["active_turn_id"] is None
    session_id = pane["session"]["session_id"]
    turn_id = dev_sessions.create_turn(session_id, "test prompt", requested_model="free/model")
    assert service.get_pane(pane["pane_id"])["active_turn_id"] == turn_id
    dev_sessions.update_turn_status(turn_id, "COMPLETED", response_text="done")
    assert service.get_pane(pane["pane_id"])["active_turn_id"] is None


# ── Routes ──────────────────────────────────────────────────────────────


def _client():
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


def test_workspace_routes_round_trip(ws_env):
    client = _client()
    plan = client.post("/api/workspace/plans", json={"project_id": "demo", "task": "implemente x"}).json()
    assert plan["complexity"] == "simple"
    created = client.post(
        "/api/workspace/panes",
        json={"project_id": "demo", "role": "implementer", "model": "free/model", "plan_id": plan["plan_id"]},
    )
    assert created.status_code == 200, created.text
    pane_id = created.json()["pane_id"]
    state = client.get("/api/workspace/state", params={"project_id": "demo"}).json()
    assert [pane["pane_id"] for pane in state["panes"]] == [pane_id]
    assert state["integration_enabled"] is False
    patched = client.patch(f"/api/workspace/panes/{pane_id}", json={"layout": {"collapsed": True}})
    assert patched.json()["layout"]["collapsed"] is True
    assert client.get(f"/api/workspace/panes/{pane_id}/history").status_code == 200
    assert client.get("/api/workspace/plans", params={"project_id": "demo"}).json()["plans"][0]["plan_id"] == plan["plan_id"]


def test_workspace_pane_route_accepts_codex_native_backend(ws_env, monkeypatch):
    monkeypatch.setattr("nexus.chat.service.list_models", lambda **kwargs: {
        "backend": "codex_native", "models": [NATIVE], "catalog_error": None,
    })
    monkeypatch.setattr("nexus.dev.executor.validate_native_choice", lambda model, effort: None)
    response = _client().post("/api/workspace/panes", json={
        "project_id": "demo", "role": "implementer", "model": "gpt-native", "backend": "codex_native",
    })
    assert response.status_code == 200, response.text
    assert response.json()["backend"] == response.json()["session"]["backend"] == "codex_native"
    assert response.json()["session"]["status"] == "ACTIVE"


def test_workspace_route_promotes_existing_native_pane(ws_env, monkeypatch):
    monkeypatch.setattr("nexus.chat.service.list_models", lambda **kwargs: {
        "backend": "codex_native", "models": [NATIVE], "catalog_error": None,
    })
    monkeypatch.setattr("nexus.dev.executor.validate_native_choice", lambda model, effort: None)
    client = _client()
    created = client.post("/api/workspace/panes", json={
        "project_id": "demo", "role": "implementer", "model": "gpt-native", "backend": "codex_native",
    })
    pane_id = created.json()["pane_id"]
    promoted = client.post(f"/api/workspace/panes/{pane_id}/promote-maestro")
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["pane"]["pane_id"] == pane_id
    assert promoted.json()["maestro"]["maestro_pane_id"] == pane_id


def test_workspace_pane_route_accepts_claude_native_with_explicit_effort(
    ws_env, monkeypatch,
):
    monkeypatch.setattr("nexus.chat.service.list_models", lambda **kwargs: {
        "backend": "claude_native",
        "models": [{"id": "sonnet", "supported_efforts": ["medium"]}],
        "catalog_error": None,
    })
    monkeypatch.setattr(
        "nexus.dev.executor.validate_claude_choice", lambda model, effort: None,
    )
    client = _client()
    response = client.post("/api/workspace/panes", json={
        "project_id": "demo", "role": "implementer", "model": "sonnet",
        "backend": "claude_native",
        "effort": "medium", "title": "Claude Native",
    })
    assert response.status_code == 200, response.text
    pane = response.json()
    assert pane["backend"] == pane["session"]["backend"] == "claude_native"
    assert pane["effort"] == "medium" and pane["model"] == "sonnet"
    loaded = client.get(f"/api/workspace/panes/{pane['pane_id']}").json()
    assert loaded["session"]["session_id"] == pane["session"]["session_id"]
    assert loaded["effort"] == "medium"


def test_workspace_routes_report_errors_and_reject_unknown_fields(ws_env):
    client = _client()
    locked = client.post("/api/workspace/panes", json={"project_id": "locked", "role": "implementer"})
    assert locked.status_code == 403 and locked.json()["detail"]["code"] == "DEV_MODE_DISABLED"
    assert client.get("/api/workspace/panes/PANE-NOPE").json()["detail"]["code"] == "PANE_NOT_FOUND"
    extra = client.post("/api/workspace/panes", json={"project_id": "demo", "role": "implementer", "apply": True})
    assert extra.status_code == 422
    # confirm_model is optional at the route (a plan authorization may cover
    # it); the service still refuses an unconfirmed, unauthorized mission.
    missing = client.post("/api/workspace/panes/PANE-NOPE/missions", json={"description": "x"})
    assert missing.status_code == 422


def test_workspace_api_rejects_cross_origin_requests(ws_env):
    response = _client().post(
        "/api/workspace/plans",
        json={"project_id": "demo", "task": "x"},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "CHAT_ORIGIN_FORBIDDEN"


def test_workspace_page_renders_and_advanced_tools_stay_reachable():
    client = _client()
    page = client.get("/nexus/workspace")
    assert page.status_code == 200
    assert 'id="ws-maestro-form"' in page.text and 'href="/nexus"' in page.text
    assert "/static/js/workspace.js" in page.text
    assert 'href="/nexus/workspace"' in client.get("/nexus").text


def test_cockpit_preview_route_renders_without_replacing_workspace():
    client = _client()
    page = client.get("/nexus/workspace/cockpit")
    assert page.status_code == 200
    assert "/static/css/cockpit.css" in page.text
    assert "/static/js/cockpit.js" in page.text
    assert 'id="pane-grid"' in page.text
    assert "EVENTOS NEXUS" in page.text
    assert client.get("/nexus/workspace").status_code == 200


def test_cockpit_existing_read_apis_expose_project_agent_and_history(ws_env):
    created = service.create_pane("demo", role="implementer", title="Agente de teste", model="free/model")
    client = _client()
    projects = client.get("/api/projects")
    workspace = client.get("/api/workspace/state", params={"project_id": "demo"})
    maestro = client.get("/api/workspace/maestro", params={"project_id": "demo"})
    history = client.get(f"/api/workspace/panes/{created['pane_id']}/history")
    assert all(response.status_code == 200 for response in (projects, workspace, maestro, history))
    assert any(project["id"] == "demo" for project in projects.json()["projects"])
    pane = workspace.json()["panes"][0]
    assert pane["pane_id"] == created["pane_id"]
    assert pane["model"] == "free/model"
    assert pane["session"]["session_id"] == created["session"]["session_id"]
    assert maestro.json()["project_id"] == "demo"
    assert history.json()["pane_id"] == created["pane_id"]
