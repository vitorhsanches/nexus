"""Intelligent Maestro (P2A): conversation, validated planning, approval,
automatic panes and direct agent messages.

Models are simulated at the Chat/Development executor boundary
(chat_service.send_turn / executor.run_dev_turn). Turn rows, sessions,
worktrees and plans are real: a disposable Git repository, real worktrees
and the real SQLite registry. Nothing here calls a model.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nexus.dev.service import DevServiceError
from nexus.registry import chat_sessions, database, dev_missions, dev_sessions
from nexus.registry import workspace as store
from nexus.workspace import cost_authorizations, maestro, maestro_plan, service

FREE = {"id": "free/model", "cost_status": "free"}
STANDARD = {"id": "std/model", "cost_status": "standard"}
LOW = {"id": "low/model", "cost_status": "low_cost"}
PREMIUM = {"id": "premium/model", "cost_status": "premium"}
UNKNOWN = {"id": "unknown/model", "cost_status": "unknown"}
MODELS = [FREE, STANDARD]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _grant_cost(model: str) -> dict:
    return cost_authorizations.authorize("demo", model, confirm_cost=True, max_uses=20,
                                        validity_seconds=3600, max_concurrent=2)


@pytest.fixture
def env(tmp_path, monkeypatch):
    original_db, original_storage = database.DATABASE_PATH, database.STORAGE_DIR
    database.STORAGE_DIR = tmp_path / "storage"
    database.DATABASE_PATH = database.STORAGE_DIR / "nexus.db"
    database.initialize_database()
    monkeypatch.setenv("NEXUS_DEV_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("NEXUS_DEV_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    for name in ("NEXUS_WORKSPACE_MAX_PANES", "NEXUS_WORKSPACE_MAX_AUTO_PANES"):
        monkeypatch.delenv(name, raising=False)
    catalog = {"models": list(MODELS), "default_model": "free/model", "catalog_error": None}
    monkeypatch.setattr("nexus.dev.models.list_dev_models", lambda: catalog)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "init")
    with database.get_connection() as connection:
        connection.execute("INSERT INTO projects (id, name, path, enabled, dev_enabled) VALUES ('demo','Demo',?,1,1)", (str(repo),))
        connection.execute("INSERT INTO projects (id, name, path, enabled, dev_enabled) VALUES ('locked','Locked',?,1,0)", (str(repo),))

    # Chat boundary: persist real chat session/turn rows, never call a model.
    sent: list[dict] = []

    def fake_create_session(project_id, model, effort=None, allow_unknown_cost=False):
        session_id = chat_sessions.create_session(project_id, str(repo), model, effort)
        return {"session_id": session_id, "model": model}

    def fake_send_turn(session_id, text, *, idempotency_key=None):
        turn_id, _created = chat_sessions.create_turn_if_absent(session_id, idempotency_key, text)
        sent.append({"session_id": session_id, "text": text, "turn_id": turn_id})
        return {"turn_id": turn_id, "status": "PENDING"}

    monkeypatch.setattr("nexus.chat.service.create_session", fake_create_session)
    monkeypatch.setattr("nexus.chat.service.send_turn", fake_send_turn)

    # Development boundary for direct pane messages: real turn rows.
    dev_turns: list[dict] = []

    def fake_run_dev_turn(dev_session_id, prompt, *, model=None):
        turn_id = dev_sessions.create_turn(dev_session_id, prompt, requested_model=model)
        dev_turns.append({"session": dev_session_id, "prompt": prompt, "model": model, "turn_id": turn_id})
        return {"turn_id": turn_id, "status": "PENDING"}

    monkeypatch.setattr("nexus.dev.executor.run_dev_turn", fake_run_dev_turn)
    yield {"repo": repo, "catalog": catalog, "sent": sent, "dev_turns": dev_turns}
    database.DATABASE_PATH, database.STORAGE_DIR = original_db, original_storage


def _answer(plan: dict, explanation: str = "Uma explicação simples.") -> str:
    return f"{explanation}\n{maestro_plan.PLAN_BEGIN}\n{json.dumps(plan)}\n{maestro_plan.PLAN_END}"


SIMPLE = {
    "complexity": "simple", "summary": "multiply", "needs_clarification": None,
    "agents": [{"key": "impl", "role": "implementer", "title": "Calc", "model": "free/model",
                "task": "Implement multiply", "acceptance_criteria": ["tests pass"], "depends_on": [], "touches": ["calc.py"]}],
}
MULTI = {
    "complexity": "multi", "summary": "orders", "needs_clarification": None,
    "agents": [
        {"key": "api", "role": "backend", "title": "API", "model": "free/model", "task": "Build API",
         "acceptance_criteria": ["endpoint works"], "depends_on": [], "touches": ["api/"]},
        {"key": "ui", "role": "frontend", "title": "Tela", "model": "std/model", "task": "Build UI",
         "acceptance_criteria": ["page renders"], "depends_on": ["api"], "touches": ["web/"]},
        {"key": "qa", "role": "tester", "title": "Testes", "model": "free/model", "task": "Write tests",
         "acceptance_criteria": ["coverage for api"], "depends_on": [], "touches": ["tests/"]},
    ],
}


def _complete_last(env, response: str, status: str = "COMPLETED", **extra) -> None:
    chat_sessions.update_turn_status(env["sent"][-1]["turn_id"], status, response_text=response, **extra)


def _ask(text: str) -> dict:
    return maestro.send_message("demo", text)


# ── Plan contract (pure) ────────────────────────────────────────────────


def test_valid_llm_plan_is_parsed_with_dependency_and_conflict_analysis():
    plan = maestro_plan.parse(_answer(MULTI), models=MODELS, limit=3)
    assert plan["source"] == "llm" and plan["complexity"] == "multi"
    assert [agent["key"] for agent in plan["agents"]] == ["api", "ui", "qa"]
    assert plan["agents"][1]["depends_on"] == ["api"] and plan["agents"][1]["cost_status"] == "standard"
    assert plan["agents"][0]["independent"] is True and plan["agents"][1]["independent"] is False
    assert plan["execution"] == {"mode": "sequential", "order": ["api", "ui", "qa"]}
    assert plan["explanation"] == "Uma explicação simples."


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda p: p["agents"][0].update(model="paid/unapproved"), "not an approved"),
        (lambda p: p.update(complexity="simple"), "exactly one agent"),
        (lambda p: p["agents"][1].update(depends_on=["qa"]), "earlier agent"),
        (lambda p: p["agents"][0].update(role="root"), "unknown role"),
        (lambda p: p.update(execute=True), "unknown plan fields"),
        (lambda p: p["agents"][0].update(command="rm -rf /"), "unknown fields"),
        (lambda p: p["agents"][0].update(acceptance_criteria=[]), "acceptance_criteria"),
        (lambda p: p["agents"].append(dict(p["agents"][0], key="extra")), "between 1 and 3"),
    ],
)
def test_malformed_or_unsafe_plans_are_rejected(mutate, reason):
    plan = json.loads(json.dumps(MULTI))
    mutate(plan)
    with pytest.raises(maestro_plan.PlanValidationError, match=reason):
        maestro_plan.parse(_answer(plan), models=MODELS, limit=3)


@pytest.mark.parametrize("text", ["", "no markers at all", f"x {maestro_plan.PLAN_BEGIN} not json {maestro_plan.PLAN_END}",
                                  f"{maestro_plan.PLAN_BEGIN}{{}}{maestro_plan.PLAN_END}{maestro_plan.PLAN_BEGIN}{{}}{maestro_plan.PLAN_END}"])
def test_unparseable_answers_are_rejected(text):
    with pytest.raises(maestro_plan.PlanValidationError):
        maestro_plan.parse(text, models=MODELS, limit=3)


def test_prompt_is_read_only_planning_with_only_approved_models():
    prompt = maestro_plan.build_prompt(user_text="faça X", project={"id": "demo", "name": "Demo"}, models=MODELS,
                                       panes=[], limit=3, previous_summary="antes")
    assert "PLANNING MODE" in prompt and "must not create, modify or delete" in prompt
    assert "free/model (cost: free)" in prompt and "std/model (cost: standard)" in prompt
    assert maestro_plan.PLAN_BEGIN in prompt and "faça X" in prompt and "antes" in prompt


# ── Conversation ────────────────────────────────────────────────────────


def test_without_planning_authorization_the_answer_is_a_labelled_deterministic_proposal(env):
    state = _ask("Implemente multiply em calc.py e faça os testes passarem.")
    assert env["sent"] == []  # no model call
    user, reply = state["messages"]
    assert user["author"] == "user" and reply["author"] == "maestro"
    assert "não foi autorizado" in reply["text"]
    assert reply["plan"]["source"] == "deterministic" and len(reply["plan"]["agents"]) == 1
    assert state["planning"]["authorized"] is False


def test_planning_authorization_requires_cost_confirmation_and_approved_route(env):
    with pytest.raises(DevServiceError) as paid:
        maestro.authorize_planning("demo", "std/model", confirm_cost=False)
    assert paid.value.code == "COST_AUTHORIZATION_REQUIRED"
    with pytest.raises(DevServiceError) as unknown:
        maestro.authorize_planning("demo", "paid/unapproved", confirm_cost=True)
    assert unknown.value.code == "MODEL_NOT_ALLOWED"
    with pytest.raises(DevServiceError) as locked:
        maestro.authorize_planning("locked", "free/model", confirm_cost=False)
    assert locked.value.code == "DEV_MODE_DISABLED"
    _grant_cost("std/model")
    state = maestro.authorize_planning("demo", "std/model", confirm_cost=False)
    assert state["planning"] == {**state["planning"], "authorized": True, "model": "std/model", "cost_status": "standard", "mode": "read-only"}


@pytest.mark.parametrize("route", [LOW, PREMIUM, UNKNOWN, {"id": "unclassified/model"}])
def test_only_explicitly_free_route_can_plan_without_cost_consent(env, route):
    env["catalog"]["models"].append(route)
    with pytest.raises(DevServiceError) as blocked:
        maestro.authorize_planning("demo", route["id"], confirm_cost=False)
    assert blocked.value.code == "COST_AUTHORIZATION_REQUIRED" and env["sent"] == []
    _grant_cost(route["id"])
    state = maestro.authorize_planning("demo", route["id"], confirm_cost=False)
    assert state["planning"]["authorized"] is True
    assert store.get_conversation(state["conversation_id"])["cost_confirmed_model"] is None


def test_legacy_maestro_paid_session_needs_reconfirmation_without_losing_thread(env):
    env["catalog"]["models"].append(LOW)
    grant = _grant_cost("low/model")
    original = maestro.authorize_planning("demo", "low/model", confirm_cost=False)
    conversation_id = original["conversation_id"]
    session_id = store.get_conversation(conversation_id)["chat_session_id"]
    cost_authorizations.revoke("demo", grant["authorization_id"])
    assert maestro.conversation_state("demo")["planning"]["authorized"] is False
    with pytest.raises(DevServiceError) as blocked:
        maestro.send_message("demo", "continue")
    assert blocked.value.code == "COST_AUTHORIZATION_REQUIRED" and env["sent"] == []
    _grant_cost("low/model")
    restored = maestro.authorize_planning("demo", "low/model", confirm_cost=False)
    assert store.get_conversation(conversation_id)["chat_session_id"] == session_id
    assert restored["planning"]["authorized"] is True
    maestro.send_message("demo", "continue")
    assert len(env["sent"]) == 1


def test_legacy_maestro_table_migrates_without_inventing_cost_consent(env):
    env["catalog"]["models"].append(LOW)
    grant = _grant_cost("low/model")
    original = maestro.authorize_planning("demo", "low/model", confirm_cost=False)
    cost_authorizations.revoke("demo", grant["authorization_id"])
    conversation_id = original["conversation_id"]
    with database.get_connection() as connection:
        connection.execute("ALTER TABLE workspace_maestro_conversations DROP COLUMN cost_confirmed_model")
    database.initialize_database()
    record = store.get_conversation(conversation_id)
    assert record["model"] == "low/model" and record["cost_confirmed_model"] is None
    assert maestro.conversation_state("demo")["planning"]["authorized"] is False
    with pytest.raises(DevServiceError) as blocked:
        maestro.send_message("demo", "continue")
    assert blocked.value.code == "COST_AUTHORIZATION_REQUIRED" and env["sent"] == []


def test_llm_conversation_persists_plans_corrections_and_survives_restart(env):
    maestro.authorize_planning("demo", "free/model", confirm_cost=False)
    state = _ask("Implemente multiply.")
    assert state["busy"] is True and len(env["sent"]) == 1
    assert "PLANNING MODE" in env["sent"][0]["text"]
    with pytest.raises(DevServiceError) as busy:
        _ask("outra coisa")
    assert busy.value.code == "MAESTRO_BUSY"

    _complete_last(env, _answer(SIMPLE, "Basta um agente."))
    state = maestro.conversation_state("demo")
    reply = state["messages"][-1]
    assert reply["text"] == "Basta um agente." and reply["plan"]["source"] == "llm"
    first_plan = reply["plan"]["plan_id"]

    # A correction continues the same conversation (same Chat session/thread).
    _ask("Na verdade separe API e tela, e escreva testes.")
    assert env["sent"][1]["session_id"] == env["sent"][0]["session_id"]
    assert "previous plan summary" in env["sent"][1]["text"]
    _complete_last(env, _answer(MULTI, "Três agentes, a tela depende da API."))

    # "Restart": nothing in memory is needed -- a fresh read rebuilds it all.
    state = maestro.conversation_state("demo")
    authors = [message["author"] for message in state["messages"]]
    assert authors == ["user", "maestro", "user", "maestro"]
    assert state["messages"][-1]["plan"]["status"] == "PROPOSED"
    assert service.plan_view(store.get_plan(first_plan))["status"] == "SUPERSEDED"
    # Reconciling again never duplicates a plan.
    assert len(maestro.conversation_state("demo")["messages"]) == 4


def test_invalid_llm_answer_and_failed_turn_fall_back_safely_without_creating_anything(env):
    maestro.authorize_planning("demo", "free/model", confirm_cost=False)
    _ask("Implemente multiply.")
    bad = json.loads(json.dumps(SIMPLE))
    bad["agents"][0]["model"] = "paid/unapproved"
    _complete_last(env, _answer(bad))
    reply = maestro.conversation_state("demo")["messages"][-1]
    assert reply["plan"]["source"] == "deterministic-fallback"
    assert "inválida" in reply["plan"]["fallback_reason"]
    assert "não passou na validação" in reply["text"]
    assert all(agent["model"] != "paid/unapproved" for agent in reply["plan"]["agents"])

    _ask("Tente de novo.")
    _complete_last(env, None, status="FAILED", error_code="MODEL_ACTIVATION_FAILED", error_message="boom")
    state = maestro.conversation_state("demo")
    assert state["messages"][-2]["status"] == "FAILED"
    assert state["messages"][-1]["plan"]["fallback_reason"] == "Maestro indisponível (MODEL_ACTIVATION_FAILED)"
    assert service.list_panes("demo", include_hidden=True) == []
    with database.get_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM dev_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM dev_autonomous_missions").fetchone()[0] == 0


def test_transient_catalog_failure_does_not_discard_a_finished_plan(env):
    maestro.authorize_planning("demo", "free/model", confirm_cost=False)
    _ask("Implemente multiply.")
    _complete_last(env, _answer(SIMPLE))
    saved = env["catalog"]["models"]
    env["catalog"]["models"], env["catalog"]["catalog_error"] = [], "Model discovery is unavailable."
    assert maestro.conversation_state("demo")["busy"] is True
    env["catalog"]["models"], env["catalog"]["catalog_error"] = saved, None
    assert maestro.conversation_state("demo")["messages"][-1]["plan"]["source"] == "llm"


def test_clarification_question_has_no_agents(env):
    maestro.authorize_planning("demo", "free/model", confirm_cost=False)
    _ask("Melhore o sistema.")
    _complete_last(env, _answer({"complexity": "simple", "summary": "?", "needs_clarification": "Qual parte?", "agents": []}, "Preciso entender."))
    reply = maestro.conversation_state("demo")["messages"][-1]
    assert reply["plan"]["agents"] == [] and reply["text"].endswith("Qual parte?")
    with pytest.raises(DevServiceError) as error:
        service.approve_plan(reply["plan"]["plan_id"], agent_models={}, max_turns_per_agent=1, mission_timeout_seconds=600, confirm_costs=False)
    assert error.value.code == "PLAN_HAS_NO_AGENTS"


# ── Approval and automatic panes ────────────────────────────────────────


def _llm_plan(env, plan: dict) -> dict:
    maestro.authorize_planning("demo", "free/model", confirm_cost=False)
    _ask("tarefa")
    _complete_last(env, _answer(plan))
    return maestro.conversation_state("demo")["messages"][-1]["plan"]


def test_simple_plan_approval_creates_exactly_one_pane_without_starting_work(env):
    plan = _llm_plan(env, SIMPLE)
    result = service.approve_plan(plan["plan_id"], agent_models={"impl": "free/model"}, max_turns_per_agent=2,
                                  mission_timeout_seconds=600, confirm_costs=False)
    assert result["plan"]["status"] == "APPROVED" and len(result["panes"]) == 1
    pane = result["panes"][0]
    assert pane["origin"] == "maestro" and pane["model"] == "free/model" and pane["session"]["status"] == "ACTIVE"
    assert pane["brief"]["task"] == "Implement multiply" and pane["brief"]["authorization"]["applies"] is True
    with database.get_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM dev_autonomous_missions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM dev_turns").fetchone()[0] == 0
    with pytest.raises(DevServiceError) as again:
        service.approve_plan(plan["plan_id"], agent_models={"impl": "free/model"}, max_turns_per_agent=2,
                             mission_timeout_seconds=600, confirm_costs=False)
    assert again.value.code == "PLAN_NOT_PROPOSED"


def test_planned_panes_can_be_approved_on_codex_native_with_explicit_cost_consent(env, monkeypatch):
    native = {"id": "gpt-native", "cost_status": "known"}
    monkeypatch.setattr("nexus.chat.service.list_models", lambda **kwargs: {
        "backend": "codex_native", "models": [native], "catalog_error": None,
    })
    monkeypatch.setattr("nexus.dev.executor.validate_native_choice", lambda model, effort: None)
    plan = _llm_plan(env, SIMPLE)

    with pytest.raises(DevServiceError) as unpaid:
        service.approve_plan(plan["plan_id"], agent_models={"impl": "gpt-native"}, backend="codex_native",
                             max_turns_per_agent=2, mission_timeout_seconds=600, confirm_costs=False)
    assert unpaid.value.code == "COST_CONFIRMATION_REQUIRED"
    result = service.approve_plan(plan["plan_id"], agent_models={"impl": "gpt-native"}, backend="codex_native",
                                  max_turns_per_agent=2, mission_timeout_seconds=600, confirm_costs=True)
    pane = result["panes"][0]
    assert result["plan"]["status"] == "APPROVED"
    assert result["plan"]["approval"]["backend"] == "codex_native"
    assert pane["backend"] == pane["session"]["backend"] == "codex_native"
    assert pane["model"] == "gpt-native" and pane["brief"]["authorization"]["applies"] is True
    assert result["plan"]["approval"]["agent_models"]["impl"].get("effort") is None
    with database.get_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM dev_autonomous_missions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM dev_turns").fetchone()[0] == 0


def test_native_plan_persists_per_agent_efforts_and_survives_database_reinitialization(
    env, monkeypatch,
):
    monkeypatch.setattr(
        "nexus.chat.service.list_models",
        lambda **kwargs: {
            "backend": "codex_native",
            "models": [{"id": "gpt-native", "cost_status": "known"}],
            "catalog_error": None,
        },
    )
    def validate(_model, effort):
        if effort is not None and effort not in {"low", "high"}:
            raise DevServiceError(
                "EFFORT_NOT_SUPPORTED", "unsupported", http_status=422
            )

    monkeypatch.setattr("nexus.dev.executor.validate_native_choice", validate)
    plan = _llm_plan(env, MULTI)
    result = service.approve_plan(
        plan["plan_id"],
        agent_models={"api": "gpt-native", "ui": "gpt-native", "qa": "gpt-native"},
        agent_backends={key: "codex_native" for key in ("api", "ui", "qa")},
        agent_efforts={"api": "high", "ui": "low", "qa": "high"},
        max_turns_per_agent=1, mission_timeout_seconds=600, confirm_costs=True,
    )
    efforts = {
        key: value["effort"]
        for key, value in result["plan"]["approval"]["agent_models"].items()
    }
    assert efforts == {"api": "high", "ui": "low", "qa": "high"}
    pane_efforts = {
        pane["plan_agent_key"]: pane["brief"]["authorization"]["effort"]
        for pane in result["panes"]
    }
    assert pane_efforts == efforts
    database.initialize_database()
    persisted = service.plan_view(store.get_plan(plan["plan_id"]))
    persisted_efforts = {
        key: value["effort"]
        for key, value in persisted["approval"]["agent_models"].items()
    }
    assert persisted_efforts == efforts


def test_native_plan_rejects_effort_unsupported_by_selected_model(env, monkeypatch):
    monkeypatch.setattr("nexus.chat.service.list_models", lambda **kwargs: {
        "backend": "codex_native",
        "models": [{"id": "gpt-native", "cost_status": "known"}],
        "catalog_error": None,
    })

    def validate(_model, effort):
        if effort != "high":
            raise DevServiceError(
                "EFFORT_NOT_SUPPORTED", "unsupported", http_status=422
            )

    monkeypatch.setattr("nexus.dev.executor.validate_native_choice", validate)
    plan = _llm_plan(env, SIMPLE)
    with pytest.raises(DevServiceError) as error:
        service.approve_plan(
            plan["plan_id"], agent_models={"impl": "gpt-native"},
            backend="codex_native",
            agent_efforts={"impl": "ultra"}, max_turns_per_agent=1,
            mission_timeout_seconds=600, confirm_costs=True,
        )
    assert error.value.code == "EFFORT_NOT_SUPPORTED"
    assert service.plan_view(store.get_plan(plan["plan_id"]))["status"] == "PROPOSED"
    assert service.list_panes("demo") == []


def test_mixed_plan_persists_each_agent_backend_and_rejects_incomplete_mapping(
    env, monkeypatch,
):
    monkeypatch.setattr("nexus.chat.service.list_models", lambda **kwargs: {
        "backend": "codex_native",
        "models": [{"id": "gpt-native", "cost_status": "known"}],
        "catalog_error": None,
    })
    monkeypatch.setattr(
        "nexus.dev.executor.validate_native_choice", lambda model, effort: None,
    )
    plan = _llm_plan(env, MULTI)
    models = {"api": "gpt-native", "ui": "std/model", "qa": "free/model"}
    with pytest.raises(DevServiceError) as incomplete:
        service.approve_plan(plan["plan_id"], agent_models=models,
                             agent_backends={"api": "codex_native"},
                             max_turns_per_agent=1, mission_timeout_seconds=600,
                             confirm_costs=True)
    assert incomplete.value.code == "BACKENDS_NOT_CONFIRMED"
    result = service.approve_plan(plan["plan_id"], agent_models=models,
                                  agent_backends={
                                      "api": "codex_native",
                                      "ui": "omniroute",
                                      "qa": "omniroute",
                                  },
                                  max_turns_per_agent=1, mission_timeout_seconds=600,
                                  confirm_costs=True)
    assert result["plan"]["status"] == "APPROVED"
    assert {pane["plan_agent_key"]: pane["backend"] for pane in result["panes"]} == {
        "api": "codex_native", "ui": "omniroute", "qa": "omniroute",
    }
    approved_backends = {
        key: choice["backend"]
        for key, choice in result["plan"]["approval"]["agent_models"].items()
    }
    assert approved_backends == {
        "api": "codex_native", "ui": "omniroute", "qa": "omniroute",
    }


def test_created_native_plan_authorizes_execution_without_omniroute(env, monkeypatch):
    from nexus.workspace import coordinator

    monkeypatch.setattr("nexus.chat.service.list_models", lambda **kwargs: {
        "backend": "codex_native",
        "models": [{"id": "gpt-native", "cost_status": "known"}],
        "catalog_error": None,
    })
    monkeypatch.setattr(
        "nexus.dev.executor.validate_native_choice", lambda model, effort: None,
    )
    plan = _llm_plan(env, SIMPLE)
    response = _client().post(f"/api/workspace/plans/{plan['plan_id']}/approve", json={
        "agent_models": {"impl": "gpt-native"},
        "backend": "codex_native",
        "agent_efforts": {"impl": "high"},
        "max_turns_per_agent": 1,
        "mission_timeout_seconds": 600,
        "confirm_costs": True,
    })
    assert response.status_code == 200, response.text
    approved = response.json()
    monkeypatch.setattr(
        "nexus.dev.models.list_dev_models",
        lambda: pytest.fail("OmniRoute catalog called"),
    )
    authorized = coordinator.authorize_execution(
        plan["plan_id"], agent_keys=["impl"], test_profiles={},
        max_turns_per_agent=1, mission_timeout_seconds=600,
    )
    assert authorized["status"] == "AWAITING_EXECUTION"
    assert approved["panes"][0]["session"]["backend"] == "codex_native"
    assert approved["plan"]["approval"]["agent_models"]["impl"]["effort"] == "high"
    assert authorized["steps"][0]["test_profile"] == ""


def test_multi_plan_needs_cost_confirmation_then_creates_isolated_panes_in_one_step(env):
    plan = _llm_plan(env, MULTI)
    models = {"api": "free/model", "ui": "std/model", "qa": "free/model"}
    with pytest.raises(DevServiceError) as cost:
        service.approve_plan(plan["plan_id"], agent_models=models, max_turns_per_agent=2, mission_timeout_seconds=600, confirm_costs=False)
    assert cost.value.code == "COST_CONFIRMATION_REQUIRED" and "std/model" in str(cost.value)
    with pytest.raises(DevServiceError) as partial:
        service.approve_plan(plan["plan_id"], agent_models={"api": "free/model"}, max_turns_per_agent=2,
                             mission_timeout_seconds=600, confirm_costs=True)
    assert partial.value.code == "MODELS_NOT_CONFIRMED"
    assert service.list_panes("demo") == []

    result = service.approve_plan(plan["plan_id"], agent_models=models, max_turns_per_agent=2, mission_timeout_seconds=600, confirm_costs=True)
    panes = result["panes"]
    assert [pane["plan_agent_key"] for pane in panes] == ["api", "ui", "qa"]
    worktrees = {Path(pane["session"]["worktree_path"]).resolve() for pane in panes}
    assert len(worktrees) == 3
    approval = result["plan"]["approval"]
    assert approval["financial_limit"] is None and approval["cost_confirmed_for"] == ["std/model"]
    ui = next(pane for pane in panes if pane["plan_agent_key"] == "ui")
    assert ui["dependencies"] == [{"key": "api", "title": "API", "ready": False}]


def test_explicit_model_change_at_approval_is_recorded_not_silent(env):
    plan = _llm_plan(env, SIMPLE)
    result = service.approve_plan(plan["plan_id"], agent_models={"impl": "std/model"}, max_turns_per_agent=1,
                                  mission_timeout_seconds=600, confirm_costs=True)
    choice = result["plan"]["approval"]["agent_models"]["impl"]
    assert choice == {
        "model": "std/model", "backend": "omniroute", "cost_status": "standard",
        "recommended": "free/model", "changed_by_user": True,
    }


def test_low_cost_plan_needs_consent_and_legacy_approval_cannot_launch(env):
    env["catalog"]["models"].append(LOW)
    plan = _llm_plan(env, SIMPLE)
    with pytest.raises(DevServiceError) as unpaid:
        service.approve_plan(plan["plan_id"], agent_models={"impl": "low/model"}, max_turns_per_agent=2,
                             mission_timeout_seconds=600, confirm_costs=False)
    assert unpaid.value.code == "COST_CONFIRMATION_REQUIRED"
    result = service.approve_plan(plan["plan_id"], agent_models={"impl": "low/model"}, max_turns_per_agent=2,
                                  mission_timeout_seconds=600, confirm_costs=True)
    pane = result["panes"][0]
    assert result["plan"]["approval"]["cost_confirmed_for"] == ["low/model"]
    assert pane["brief"]["authorization"]["applies"] is True
    with database.get_connection() as connection:
        approval = result["plan"]["approval"]
        approval["cost_confirmed_for"] = []  # persisted by the old low_cost policy
        connection.execute("UPDATE workspace_plans SET approval_json=? WHERE id=?", (json.dumps(approval), plan["plan_id"]))
    assert service.get_pane(pane["pane_id"])["brief"]["authorization"]["applies"] is False
    with pytest.raises(DevServiceError) as blocked:
        service.send_pane_message(pane["pane_id"], "continue", confirm_model="low/model")
    assert blocked.value.code == "COST_CONFIRMATION_REQUIRED" and env["dev_turns"] == []
    service.send_pane_message(pane["pane_id"], "continue", confirm_model="low/model", confirm_cost=True)
    assert len(env["dev_turns"]) == 1


def test_confirmed_ids_cover_same_model_twice_but_not_different_model(env):
    env["catalog"]["models"].extend([LOW, PREMIUM])
    plan = _llm_plan(env, MULTI)
    choices = {"api": "low/model", "ui": "low/model", "qa": "free/model"}
    with pytest.raises(DevServiceError) as blocked:
        service.approve_plan(plan["plan_id"], agent_models=choices, max_turns_per_agent=2,
                             mission_timeout_seconds=600, confirm_costs=False)
    assert blocked.value.code == "COST_CONFIRMATION_REQUIRED"
    result = service.approve_plan(plan["plan_id"], agent_models=choices, max_turns_per_agent=2,
                                  mission_timeout_seconds=600, confirm_costs=True)
    assert result["plan"]["approval"]["cost_confirmed_for"] == ["low/model"]
    ui = next(pane for pane in result["panes"] if pane["plan_agent_key"] == "ui")
    assert ui["brief"]["authorization"]["applies"] is True
    api = next(pane for pane in result["panes"] if pane["plan_agent_key"] == "api")
    service.update_pane(api["pane_id"], model="premium/model")
    assert service.get_pane(api["pane_id"])["brief"]["authorization"]["applies"] is False
    with pytest.raises(DevServiceError) as unpaid:
        service.send_pane_message(api["pane_id"], "continue", confirm_model="premium/model")
    assert unpaid.value.code == "COST_CONFIRMATION_REQUIRED"


def test_two_distinct_paid_models_record_distinct_ids(env):
    env["catalog"]["models"].extend([LOW, PREMIUM])
    plan = _llm_plan(env, MULTI)
    choices = {"api": "low/model", "ui": "premium/model", "qa": "free/model"}
    result = service.approve_plan(plan["plan_id"], agent_models=choices, max_turns_per_agent=1,
                                  mission_timeout_seconds=900, confirm_costs=True)
    assert result["plan"]["approval"]["cost_confirmed_for"] == ["low/model", "premium/model"]
    assert all(pane["brief"]["authorization"]["applies"] for pane in result["panes"])


def test_paid_plan_does_not_accept_a_different_persisted_model_id(env):
    env["catalog"]["models"].append(LOW)
    plan = _llm_plan(env, SIMPLE)
    result = service.approve_plan(plan["plan_id"], agent_models={"impl": "low/model"}, max_turns_per_agent=1,
                                  mission_timeout_seconds=300, confirm_costs=True)
    pane_id = result["panes"][0]["pane_id"]
    approval = result["plan"]["approval"]
    approval["cost_confirmed_for"] = ["std/model"]
    with database.get_connection() as connection:
        connection.execute("UPDATE workspace_plans SET approval_json=? WHERE id=?", (json.dumps(approval), plan["plan_id"]))
    assert service.get_pane(pane_id)["brief"]["authorization"]["applies"] is False
    with pytest.raises(DevServiceError) as blocked:
        service.send_pane_message(pane_id, "work", confirm_model="low/model")
    assert blocked.value.code == "COST_CONFIRMATION_REQUIRED" and env["dev_turns"] == []


def test_direct_api_requires_financial_consent_separately_for_paid_pane(env, monkeypatch):
    env["catalog"]["models"].append(LOW)
    pane = service.create_pane("demo", role="implementer", model="low/model")
    starts = []
    monkeypatch.setattr("nexus.dev.mission_runner.start_mission", lambda *args, **kwargs: starts.append((args, kwargs)) or {
        "mission_id": "SIMULATED", "status": "PENDING",
    })
    client = _client()
    mission_url = f"/api/workspace/panes/{pane['pane_id']}/missions"
    payload = {"description": "work", "test_profile": "unit", "confirm_model": "low/model"}
    denied = client.post(mission_url, json=payload)
    assert denied.status_code == 400 and denied.json()["detail"]["code"] == "COST_CONFIRMATION_REQUIRED"
    assert starts == []
    accepted = client.post(mission_url, json={**payload, "confirm_cost": True})
    assert accepted.status_code == 200 and len(starts) == 1
    message_url = f"/api/workspace/panes/{pane['pane_id']}/messages"
    denied = client.post(message_url, json={"text": "explain", "confirm_model": "low/model"})
    assert denied.status_code == 400 and denied.json()["detail"]["code"] == "COST_CONFIRMATION_REQUIRED"
    assert env["dev_turns"] == []
    accepted = client.post(message_url, json={"text": "explain", "confirm_model": "low/model", "confirm_cost": True})
    assert accepted.status_code == 200 and len(env["dev_turns"]) == 1


def test_approval_respects_open_pane_limit(env, monkeypatch):
    monkeypatch.setenv("NEXUS_WORKSPACE_MAX_PANES", "2")
    plan = _llm_plan(env, MULTI)
    with pytest.raises(DevServiceError) as error:
        service.approve_plan(plan["plan_id"], agent_models={"api": "free/model", "ui": "free/model", "qa": "free/model"},
                             max_turns_per_agent=1, mission_timeout_seconds=600, confirm_costs=False)
    assert error.value.code == "PANE_LIMIT_REACHED"
    assert service.plan_view(store.get_plan(plan["plan_id"]))["status"] == "PROPOSED"


def test_manual_pane_still_works_next_to_planned_panes(env):
    plan = _llm_plan(env, SIMPLE)
    service.approve_plan(plan["plan_id"], agent_models={"impl": "free/model"}, max_turns_per_agent=1,
                         mission_timeout_seconds=600, confirm_costs=False)
    manual = service.create_pane("demo", role="tester", title="QA manual")
    assert manual["origin"] == "manual" and manual["brief"] is None
    assert {pane["origin"] for pane in service.list_panes("demo")} == {"maestro", "manual"}


# ── Missions under plan authorization ───────────────────────────────────


def test_planned_mission_uses_brief_and_authorization_limits_and_dependencies(env, monkeypatch):
    plan = _llm_plan(env, MULTI)
    result = service.approve_plan(plan["plan_id"], agent_models={"api": "free/model", "ui": "free/model", "qa": "free/model"},
                                  max_turns_per_agent=1, mission_timeout_seconds=300, confirm_costs=False)
    api, ui = result["panes"][0], result["panes"][1]
    calls = []
    monkeypatch.setattr("nexus.dev.mission_runner.start_mission",
                        lambda sid, desc, **kw: calls.append((sid, desc, kw)) or {"mission_id": f"M{len(calls)}", "status": "PENDING"})

    with pytest.raises(DevServiceError) as blocked:
        service.start_pane_mission(ui["pane_id"], None, confirm_model=None, test_profile="unit")
    assert blocked.value.code == "DEPENDENCY_PENDING" and calls == []

    service.start_pane_mission(api["pane_id"], None, confirm_model=None, test_profile="unit")
    sid, description, kwargs = calls[0]
    assert sid == api["session"]["session_id"]
    assert "Planned task: Build API" in description and "endpoint works" in description
    assert kwargs["model"] == "free/model" and kwargs["allowed_models"] == ["free/model"] and kwargs["timeout_seconds"] == 300

    # Authorization covered exactly one use; the next one must re-confirm.
    with pytest.raises(DevServiceError) as exhausted:
        service.start_pane_mission(api["pane_id"], "again", confirm_model=None, test_profile="unit")
    assert exhausted.value.code == "MODEL_NOT_CONFIRMED"
    service.start_pane_mission(api["pane_id"], "again", confirm_model="free/model", test_profile="unit")
    assert len(calls) == 2

    # Changing the model voids the plan authorization for future uses.
    qa = result["panes"][2]
    service.update_pane(qa["pane_id"], model="std/model")
    assert service.get_pane(qa["pane_id"])["brief"]["authorization"]["applies"] is False
    with pytest.raises(DevServiceError):
        service.start_pane_mission(qa["pane_id"], None, confirm_model=None, test_profile="unit")


# ── Direct agent messages ───────────────────────────────────────────────


def _finish_dev_turn(env, index: int, response: str, status: str = "COMPLETED") -> None:
    dev_sessions.update_turn_status(env["dev_turns"][index]["turn_id"], status, response_text=response)


def test_direct_messages_are_per_agent_and_classify_conversation_vs_code_changes(env):
    a = service.create_pane("demo", role="implementer", title="A", model="free/model")
    b = service.create_pane("demo", role="tester", title="B", model="free/model")
    with pytest.raises(DevServiceError) as unconfirmed:
        service.send_pane_message(a["pane_id"], "oi")
    assert unconfirmed.value.code == "MODEL_NOT_CONFIRMED" and env["dev_turns"] == []

    service.send_pane_message(a["pane_id"], "Explique calc.py", confirm_model="free/model")
    service.send_pane_message(b["pane_id"], "Crie um teste", confirm_model="free/model")
    assert env["dev_turns"][0]["session"] == a["session"]["session_id"]
    assert env["dev_turns"][1]["session"] == b["session"]["session_id"]

    _finish_dev_turn(env, 0, "calc.py soma dois números.")
    Path(b["session"]["worktree_path"], "test_calc.py").write_text("import calc\n", encoding="utf-8")
    _finish_dev_turn(env, 1, "Criei test_calc.py.")

    a_msgs = service.get_pane(a["pane_id"])["messages"]
    b_msgs = service.get_pane(b["pane_id"])["messages"]
    assert [m["text"] for m in a_msgs] == ["Explique calc.py"] and a_msgs[0]["outcome"] == "conversation"
    assert a_msgs[0]["response_text"] == "calc.py soma dois números."
    assert [m["text"] for m in b_msgs] == ["Crie um teste"] and b_msgs[0]["outcome"] == "code_changed"

    # Editing an already-changed file again is still detected as a change.
    service.send_pane_message(b["pane_id"], "Ajuste o teste", confirm_model="free/model")
    Path(b["session"]["worktree_path"], "test_calc.py").write_text("import calc\nassert calc.add(1, 1) == 2\n", encoding="utf-8")
    _finish_dev_turn(env, 2, "Ajustei.")
    assert service.get_pane(b["pane_id"])["messages"][-1]["outcome"] == "code_changed"


def test_messages_are_refused_while_a_mission_runs_and_model_change_applies_to_future_turns(env, monkeypatch):
    pane = service.create_pane("demo", role="implementer", model="free/model")
    running = [{"mission_id": "M", "status": "RUNNING", "requested_model": "free/model", "attempt_count": 1,
                "error_code": None, "error_message": None, "created_at": "t", "finished_at": None, "tests": [], "result": None}]
    monkeypatch.setattr("nexus.dev.mission_runner.list_missions", lambda _sid: running)
    with pytest.raises(DevServiceError) as busy:
        service.send_pane_message(pane["pane_id"], "oi", confirm_model="free/model")
    assert busy.value.code == "PANE_BUSY"
    monkeypatch.setattr("nexus.dev.mission_runner.list_missions", lambda _sid: [])
    service.send_pane_message(pane["pane_id"], "oi", confirm_model="free/model")
    service.update_pane(pane["pane_id"], model="std/model")
    _finish_dev_turn(env, 0, "olá")
    message = service.get_pane(pane["pane_id"])["messages"][0]
    assert message["model"] == "free/model" and env["dev_turns"][0]["model"] == "free/model"
    service.send_pane_message(pane["pane_id"], "de novo", confirm_model="std/model", confirm_cost=True)
    assert env["dev_turns"][1]["model"] == "std/model"


def test_failed_dev_turn_start_is_recorded_and_nothing_runs(env, monkeypatch):
    pane = service.create_pane("demo", role="implementer", model="free/model")

    def refuse(*_args, **_kwargs):
        raise DevServiceError("DEV_EXECUTION_DISABLED", "disabled", http_status=403)

    monkeypatch.setattr("nexus.dev.executor.run_dev_turn", refuse)
    with pytest.raises(DevServiceError) as error:
        service.send_pane_message(pane["pane_id"], "oi", confirm_model="free/model")
    assert error.value.code == "DEV_EXECUTION_DISABLED"
    assert service.get_pane(pane["pane_id"])["messages"][0]["outcome"] == "failed"


# ── Routes ──────────────────────────────────────────────────────────────


def _client():
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


def test_maestro_and_approval_routes_round_trip_without_apply(env):
    client = _client()
    assert client.post("/api/workspace/maestro/authorize", json={"project_id": "demo", "model": "std/model"}).json()["detail"]["code"] == "COST_AUTHORIZATION_REQUIRED"
    assert client.post("/api/workspace/maestro/authorize", json={"project_id": "demo", "model": "free/model"}).status_code == 200
    assert client.post("/api/workspace/maestro/messages", json={"project_id": "demo", "text": "multiply"}).json()["busy"] is True
    _complete_last(env, _answer(SIMPLE))
    plan = client.get("/api/workspace/maestro", params={"project_id": "demo"}).json()["messages"][-1]["plan"]
    approved = client.post(f"/api/workspace/plans/{plan['plan_id']}/approve", json={
        "agent_models": {"impl": "free/model"}, "max_turns_per_agent": 2, "mission_timeout_seconds": 600})
    assert approved.status_code == 200, approved.text
    pane_id = approved.json()["panes"][0]["pane_id"]
    message = client.post(f"/api/workspace/panes/{pane_id}/messages", json={"text": "oi"})
    assert message.status_code == 200, message.text
    extra = client.post(f"/api/workspace/plans/{plan['plan_id']}/approve", json={
        "agent_models": {"impl": "free/model"}, "max_turns_per_agent": 2, "mission_timeout_seconds": 600, "auto_apply": True})
    assert extra.status_code == 422
    assert client.post("/api/workspace/maestro/messages", json={"project_id": "demo", "text": "x"},
                       headers={"Origin": "http://evil.example"}).status_code == 403
    from nexus.web.workspace_routes import router

    paths = {route.path for route in router.routes}
    assert paths and not any(word in path for path in paths for word in ("apply", "merge", "push", "integrat"))


def _manual_native_plan(*, first_dependencies=None, second_dependencies=None):
    return {
        "project_id": "demo",
        "task_text": "Implementar uma operação e validar seu uso.",
        "agents": [
            {
                "key": "implement",
                "role": "backend",
                "title": "Implementação",
                "task": "Implementar a função solicitada.",
                "acceptance_criteria": ["A função retorna o resultado esperado."],
                "depends_on": (
                    first_dependencies if first_dependencies is not None else []
                ),
                "touches": ["calc.py"],
            },
            {
                "key": "verify",
                "role": "tester",
                "title": "Validação",
                "task": "Adicionar um teste complementar.",
                "acceptance_criteria": ["O teste cobre a função implementada."],
                "depends_on": (
                    second_dependencies
                    if second_dependencies is not None else ["implement"]
                ),
                "touches": ["tests/"],
            },
        ],
        "execution_order": ["implement", "verify"],
    }


def test_manual_native_plan_is_proposed_without_catalog_session_or_execution(
    env, monkeypatch,
):
    client = _client()
    monkeypatch.setattr(
        "nexus.dev.models.list_dev_models",
        lambda: pytest.fail("manual planning must not consult the OmniRoute catalog"),
    )
    monkeypatch.setattr(
        "nexus.chat.service.list_models",
        lambda **kwargs: pytest.fail("manual planning must not load a model catalog"),
    )
    monkeypatch.setattr(
        "nexus.chat.service.send_turn",
        lambda *args, **kwargs: pytest.fail(
            "manual planning must not invoke inference"
        ),
    )
    response = client.post("/api/workspace/plans/manual", json=_manual_native_plan())
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["status"] == "PROPOSED"
    assert plan["source"] == "manual"
    assert plan["execution"] is None and plan["approval"] is None
    assert [agent["key"] for agent in plan["agents"]] == ["implement", "verify"]
    assert plan["agents"][1]["depends_on"] == ["implement"]
    assert all(agent["model"] is None for agent in plan["agents"])
    assert service.list_panes("demo", include_hidden=True) == []
    assert dev_sessions.list_sessions("demo") == []
    assert env["sent"] == [] and env["dev_turns"] == []
    reread = client.get(f"/api/workspace/plans/{plan['plan_id']}")
    assert reread.status_code == 200
    assert reread.json()["plan_id"] == plan["plan_id"]


def test_manual_native_plan_without_dependencies_is_valid(env):
    payload = _manual_native_plan(second_dependencies=[])
    response = _client().post("/api/workspace/plans/manual", json=payload)
    assert response.status_code == 200, response.text
    assert all(not agent["depends_on"] for agent in response.json()["agents"])
    assert response.json()["execution"] is None


@pytest.mark.parametrize(
    "payload,code",
    [
        (
            _manual_native_plan(first_dependencies=["missing"]),
            "DEPENDENCY_NOT_FOUND",
        ),
        (
            _manual_native_plan(
                first_dependencies=["verify"], second_dependencies=["implement"]
            ),
            "DEPENDENCY_CYCLE",
        ),
        (
            _manual_native_plan(second_dependencies=["implement"])
            | {"execution_order": ["verify", "implement"]},
            "DEPENDENCY_CYCLE",
        ),
        (
            _manual_native_plan()
            | {"agents": [
                _manual_native_plan()["agents"][0],
                {**_manual_native_plan()["agents"][1], "key": "implement"},
            ]},
            "INVALID_MANUAL_PLAN",
        ),
    ],
)
def test_manual_native_plan_rejects_invalid_graphs(env, payload, code):
    response = _client().post("/api/workspace/plans/manual", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == code
    assert store.list_plans("demo") == []


def test_manual_native_plan_rejects_unauthorized_project_and_invalid_payload(env):
    client = _client()
    payload = _manual_native_plan() | {"project_id": "locked"}
    denied = client.post("/api/workspace/plans/manual", json=payload)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "DEV_MODE_DISABLED"
    invalid = client.post(
        "/api/workspace/plans/manual",
        json=_manual_native_plan() | {"unapproved_model": "gpt-6.1-sol"},
    )
    assert invalid.status_code == 422
    assert store.list_plans("locked") == [] and store.list_plans("demo") == []


def test_manual_plan_get_rechecks_project_authorization(env):
    client = _client()
    created = client.post(
        "/api/workspace/plans/manual", json=_manual_native_plan()
    )
    assert created.status_code == 200
    plan_id = created.json()["plan_id"]
    with database.get_connection() as connection:
        connection.execute("UPDATE projects SET dev_enabled=0 WHERE id='demo'")
    denied = client.get(f"/api/workspace/plans/{plan_id}")
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "DEV_MODE_DISABLED"


def test_manual_plan_persists_and_can_be_approved_and_authorized_as_native(
    env, monkeypatch,
):
    from nexus.workspace import coordinator

    client = _client()
    payload = _manual_native_plan()
    response = client.post("/api/workspace/plans/manual", json=payload)
    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]
    database.initialize_database()
    persisted = client.get(f"/api/workspace/plans/{plan_id}")
    assert persisted.status_code == 200
    assert persisted.json()["status"] == "PROPOSED"
    assert service.list_panes("demo", include_hidden=True) == []

    monkeypatch.setattr("nexus.chat.service.list_models", lambda **kwargs: {
        "backend": "codex_native",
        "models": [{"id": "gpt-native", "cost_status": "known"}],
        "catalog_error": None,
    })
    monkeypatch.setattr(
        "nexus.dev.executor.validate_native_choice",
        lambda model, effort: None,
    )
    approved = client.post(f"/api/workspace/plans/{plan_id}/approve", json={
        "agent_models": {"implement": "gpt-native", "verify": "gpt-native"},
        "backend": "codex_native",
        "agent_backends": {"implement": "codex_native", "verify": "codex_native"},
        "agent_efforts": {"implement": "high", "verify": "low"},
        "max_turns_per_agent": 1,
        "mission_timeout_seconds": 600,
        "confirm_costs": True,
    })
    assert approved.status_code == 200, approved.text
    assert approved.json()["plan"]["status"] == "APPROVED"
    assert {
        key: choice["effort"]
        for key, choice in approved.json()["plan"]["approval"]["agent_models"].items()
    } == {"implement": "high", "verify": "low"}
    assert [pane["session"]["backend"] for pane in approved.json()["panes"]] == [
        "codex_native", "codex_native",
    ]
    monkeypatch.setattr(
        "nexus.dev.models.list_dev_models",
        lambda: pytest.fail("native plan authorization must not use OmniRoute catalog"),
    )
    execution = coordinator.authorize_execution(
        plan_id, agent_keys=["implement", "verify"], test_profiles={},
        max_turns_per_agent=1, mission_timeout_seconds=600,
    )
    assert execution["status"] == "AWAITING_EXECUTION"
    assert env["dev_turns"] == []
    assert all(step["native_turn_id"] is None for step in execution["steps"])


def test_manual_plan_approval_rejects_mixed_backends(env):
    plan = _client().post(
        "/api/workspace/plans/manual", json=_manual_native_plan()
    ).json()
    response = _client().post(
        f"/api/workspace/plans/{plan['plan_id']}/approve",
        json={
            "agent_models": {"implement": "free/model", "verify": "free/model"},
            "agent_backends": {"implement": "codex_native", "verify": "omniroute"},
            "agent_efforts": {"implement": "high"},
            "max_turns_per_agent": 1,
            "mission_timeout_seconds": 600,
            "confirm_costs": True,
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "MANUAL_PLAN_NATIVE_ONLY"
    assert service.list_panes("demo", include_hidden=True) == []


@pytest.mark.parametrize(
    "agent_models,agent_efforts,expected_code",
    [
        ({"implement": "gpt-native"}, {"implement": "high", "verify": "low"},
         "MODELS_NOT_CONFIRMED"),
        ({"implement": "gpt-native", "verify": "gpt-native"}, {"implement": "high"},
         "EFFORTS_NOT_CONFIRMED"),
    ],
)
def test_manual_plan_approval_requires_explicit_model_and_effort_for_each_agent(
    env, monkeypatch, agent_models, agent_efforts, expected_code,
):
    monkeypatch.setattr("nexus.chat.service.list_models", lambda **kwargs: {
        "backend": "codex_native",
        "models": [{"id": "gpt-native", "cost_status": "known"}],
        "catalog_error": None,
    })
    monkeypatch.setattr(
        "nexus.dev.executor.validate_native_choice", lambda model, effort: None
    )
    plan = _client().post(
        "/api/workspace/plans/manual", json=_manual_native_plan()
    ).json()
    response = _client().post(
        f"/api/workspace/plans/{plan['plan_id']}/approve",
        json={
            "agent_models": agent_models,
            "backend": "codex_native",
            "agent_backends": {
                "implement": "codex_native", "verify": "codex_native",
            },
            "agent_efforts": agent_efforts,
            "max_turns_per_agent": 1,
            "mission_timeout_seconds": 600,
            "confirm_costs": True,
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == expected_code
    assert service.plan_view(store.get_plan(plan["plan_id"]))["status"] == "PROPOSED"
    assert all(
        pane["plan_id"] != plan["plan_id"]
        for pane in service.list_panes("demo", include_hidden=True)
    )


def test_manual_plan_approval_rejects_model_absent_from_native_catalog(
    env, monkeypatch,
):
    plan = _client().post(
        "/api/workspace/plans/manual", json=_manual_native_plan()
    ).json()
    monkeypatch.setattr("nexus.chat.service.list_models", lambda **kwargs: {
        "backend": "codex_native",
        "models": [{"id": "gpt-native", "cost_status": "known"}],
        "catalog_error": None,
    })
    monkeypatch.setattr(
        "nexus.dev.executor.validate_native_choice", lambda model, effort: None
    )
    response = _client().post(
        f"/api/workspace/plans/{plan['plan_id']}/approve",
        json={
            "agent_models": {"implement": "gpt-revoked", "verify": "gpt-native"},
            "backend": "codex_native",
            "agent_backends": {
                "implement": "codex_native", "verify": "codex_native",
            },
            "agent_efforts": {"implement": "high", "verify": "low"},
            "max_turns_per_agent": 1,
            "mission_timeout_seconds": 600,
            "confirm_costs": True,
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "MODEL_NOT_ALLOWED"
    assert service.plan_view(store.get_plan(plan["plan_id"]))["status"] == "PROPOSED"
    assert service.list_panes("demo", include_hidden=True) == []


def test_messages_are_refused_without_a_record_when_execution_is_disabled(env, monkeypatch):
    pane = service.create_pane("demo", role="implementer", model="free/model")
    monkeypatch.setenv("NEXUS_DEV_EXECUTION_ENABLED", "0")
    with pytest.raises(DevServiceError) as error:
        service.send_pane_message(pane["pane_id"], "oi", confirm_model="free/model")
    assert error.value.code == "DEV_EXECUTION_DISABLED"
    assert env["dev_turns"] == [] and service.get_pane(pane["pane_id"])["messages"] == []


# ── P2A.1: shared execution gate ────────────────────────────────────────

ALL_FREE = {"api": "free/model", "ui": "free/model", "qa": "free/model"}


def _approved_multi(env, *, timeout=300, turns=2):
    plan = _llm_plan(env, MULTI)
    result = service.approve_plan(plan["plan_id"], agent_models=ALL_FREE, max_turns_per_agent=turns,
                                  mission_timeout_seconds=timeout, confirm_costs=False)
    return {pane["plan_agent_key"]: pane for pane in result["panes"]}


def _real_mission_start(calls, delay=0.0):
    """Persist a real PENDING mission row (what start_mission does first)."""
    def start(session_id, description, **kwargs):
        if delay:
            time.sleep(delay)
        mission_id = dev_missions.create_mission(session_id, description, requested_model=kwargs["model"],
                                                 allowed_models=kwargs["allowed_models"], test_profile=kwargs["test_profile"])
        calls.append((session_id, kwargs, mission_id))
        return {"mission_id": mission_id, "status": "PENDING"}
    return start


def test_dependent_agent_cannot_run_a_direct_write_turn_before_release(env):
    panes = _approved_multi(env)
    with pytest.raises(DevServiceError) as error:
        service.send_pane_message(panes["ui"]["pane_id"], "comece a tela")
    assert error.value.code == "DEPENDENCY_PENDING"
    assert env["dev_turns"] == [] and service.get_pane(panes["ui"]["pane_id"])["messages"] == []
    with pytest.raises(DevServiceError) as confirmed:
        service.send_pane_message(panes["ui"]["pane_id"], "comece", confirm_model="free/model")
    assert confirmed.value.code == "DEPENDENCY_PENDING", "explicit confirmation never bypasses dependencies"


def test_plan_authorized_mission_cannot_exceed_the_approved_duration(env, monkeypatch):
    panes = _approved_multi(env, timeout=300, turns=3)
    calls = []
    monkeypatch.setattr("nexus.dev.mission_runner.start_mission", _real_mission_start(calls))
    api = panes["api"]["pane_id"]
    with pytest.raises(DevServiceError) as error:
        service.start_pane_mission(api, None, confirm_model=None, test_profile="unit", timeout_seconds=301)
    assert error.value.code == "TIMEOUT_EXCEEDS_AUTHORIZATION" and calls == []
    with pytest.raises(DevServiceError) as confirmed:
        # Re-confirming the model while the plan authorization applies is
        # still a plan-authorized use: the ceiling holds.
        service.start_pane_mission(api, None, confirm_model="free/model", test_profile="unit", timeout_seconds=3600)
    assert confirmed.value.code == "TIMEOUT_EXCEEDS_AUTHORIZATION" and calls == []
    assert service.get_pane(api)["brief"]["authorization"]["turns_used"] == 0
    service.start_pane_mission(api, None, confirm_model=None, test_profile="unit", timeout_seconds=120)
    assert calls[-1][1]["timeout_seconds"] == 120


def test_approved_duration_is_the_default_and_extension_needs_explicit_authorization(env, monkeypatch):
    panes = _approved_multi(env, timeout=300, turns=1)
    calls = []
    monkeypatch.setattr("nexus.dev.mission_runner.start_mission", _real_mission_start(calls))
    api = panes["api"]
    service.start_pane_mission(api["pane_id"], None, confirm_model=None, test_profile="unit")
    assert calls[-1][1]["timeout_seconds"] == 300
    dev_missions.update_status(calls[-1][2], "READY_FOR_REVIEW")
    # Plan authorization exhausted: a longer mission is a new, explicit authorization.
    with pytest.raises(DevServiceError) as unconfirmed:
        service.start_pane_mission(api["pane_id"], "again", confirm_model=None, test_profile="unit", timeout_seconds=900)
    assert unconfirmed.value.code == "MODEL_NOT_CONFIRMED"
    service.start_pane_mission(api["pane_id"], "again", confirm_model="free/model", test_profile="unit", timeout_seconds=900)
    assert calls[-1][1]["timeout_seconds"] == 900


def test_agents_of_one_plan_never_execute_at_the_same_time_even_without_dependencies(env, monkeypatch):
    panes = _approved_multi(env)
    api, qa = panes["api"], panes["qa"]
    assert qa["brief"]["depends_on"] == []  # independent on paper
    calls = []
    monkeypatch.setattr("nexus.dev.mission_runner.start_mission", _real_mission_start(calls))
    service.start_pane_mission(api["pane_id"], None, confirm_model=None, test_profile="unit")
    with pytest.raises(DevServiceError) as mission:
        service.start_pane_mission(qa["pane_id"], None, confirm_model=None, test_profile="unit")
    assert mission.value.code == "PLAN_AGENT_RUNNING" and "API" in str(mission.value)
    with pytest.raises(DevServiceError) as message:
        service.send_pane_message(qa["pane_id"], "oi")
    assert message.value.code == "PLAN_AGENT_RUNNING" and env["dev_turns"] == []
    with pytest.raises(DevServiceError) as own:
        service.send_pane_message(api["pane_id"], "oi")
    assert own.value.code == "PANE_BUSY"

    # Once the running agent finishes, the next one may run...
    dev_missions.update_status(calls[0][2], "READY_FOR_REVIEW")
    service.send_pane_message(qa["pane_id"], "oi")
    # ...and an active direct-message turn blocks the rest of the plan too.
    with pytest.raises(DevServiceError) as by_turn:
        service.start_pane_mission(api["pane_id"], "x", confirm_model="free/model", test_profile="unit")
    assert by_turn.value.code == "PLAN_AGENT_RUNNING"
    # A manual pane outside the plan is not affected by the plan's exclusion.
    manual = service.create_pane("demo", role="tester", model="free/model")
    service.send_pane_message(manual["pane_id"], "oi", confirm_model="free/model")
    assert len(env["dev_turns"]) == 2


def test_concurrent_starts_in_one_plan_admit_exactly_one(env, monkeypatch):
    panes = _approved_multi(env)
    calls = []
    monkeypatch.setattr("nexus.dev.mission_runner.start_mission", _real_mission_start(calls, delay=0.2))
    barrier = threading.Barrier(2)
    outcomes = []

    def run(key):
        barrier.wait()
        try:
            service.start_pane_mission(panes[key]["pane_id"], None, confirm_model=None, test_profile="unit")
            outcomes.append("started")
        except DevServiceError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=run, args=(key,)) for key in ("api", "qa")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(outcomes) == ["PLAN_AGENT_RUNNING", "started"] and len(calls) == 1


# ── P2A.1: Maestro recovery ─────────────────────────────────────────────


def _plans_for(message_id):
    with database.get_connection() as connection:
        return connection.execute("SELECT id FROM workspace_plans WHERE message_id = ?", (message_id,)).fetchall()


def _replies(conversation_id):
    return [row for row in store.list_messages(conversation_id) if row["author"] == "maestro"]


def _pending_message(env):
    maestro.authorize_planning("demo", "free/model", confirm_cost=False)
    _ask("Implemente multiply.")
    conversation = store.get_conversation_for_project("demo")
    message = next(row for row in store.list_messages(conversation["id"]) if row["author"] == "user")
    return conversation, message


def test_message_stuck_in_processing_is_finished_once(env):
    conversation, message = _pending_message(env)
    _complete_last(env, _answer(SIMPLE))
    with database.get_connection() as connection:  # interrupted right after the old "claim" step
        connection.execute("UPDATE workspace_maestro_messages SET status = 'PROCESSING' WHERE id = ?", (message["id"],))
    state = maestro.conversation_state("demo")
    assert state["busy"] is False and state["messages"][-1]["plan"]["source"] == "llm"
    maestro.conversation_state("demo")
    assert len(_plans_for(message["id"])) == 1 and len(_replies(conversation["id"])) == 1


def test_plan_stored_but_reply_missing_is_repaired_without_duplicates(env):
    conversation, message = _pending_message(env)
    _complete_last(env, _answer(SIMPLE))
    # Interrupted after the old create_plan step, before the reply was written.
    orphan = store.create_plan("demo", message["text"], policy_version="maestro-plan/1", complexity="simple",
                               proposal=maestro_plan.parse(_answer(SIMPLE), models=MODELS, limit=3), source="llm",
                               conversation_id=conversation["id"], message_id=message["id"])
    with database.get_connection() as connection:
        connection.execute("UPDATE workspace_maestro_messages SET status = 'PROCESSING' WHERE id = ?", (message["id"],))
    state = maestro.conversation_state("demo")
    assert [row["id"] for row in _plans_for(message["id"])] == [orphan]
    replies = _replies(conversation["id"])
    assert len(replies) == 1 and replies[0]["plan_id"] == orphan
    assert state["messages"][-1]["plan"]["plan_id"] == orphan and state["busy"] is False


def test_turn_id_never_recorded_is_relinked_through_the_idempotency_key(env, monkeypatch):
    maestro.authorize_planning("demo", "free/model", confirm_cost=False)
    original = env["sent"]

    def send_then_crash(session_id, text, *, idempotency_key=None):
        turn_id, _created = chat_sessions.create_turn_if_absent(session_id, idempotency_key, text)
        original.append({"session_id": session_id, "text": text, "turn_id": turn_id})
        raise KeyboardInterrupt  # process dies before set_message_turn

    monkeypatch.setattr("nexus.chat.service.send_turn", send_then_crash)
    with pytest.raises(KeyboardInterrupt):
        _ask("Implemente multiply.")
    conversation = store.get_conversation_for_project("demo")
    message = next(row for row in store.list_messages(conversation["id"]) if row["author"] == "user")
    assert message["chat_turn_id"] is None
    state = maestro.conversation_state("demo")
    assert state["busy"] is True  # still running, now linked
    assert state["chat_session_id"] == conversation["chat_session_id"]
    assert state["active_turn_id"] == original[-1]["turn_id"]
    assert store.list_messages(conversation["id"])[0]["chat_turn_id"] == original[-1]["turn_id"]
    _complete_last(env, _answer(SIMPLE))
    assert maestro.conversation_state("demo")["messages"][-1]["plan"]["source"] == "llm"
    assert maestro.conversation_state("demo")["active_turn_id"] is None


def test_message_never_sent_gets_a_labelled_fallback_only_after_the_grace_period(env, monkeypatch):
    maestro.authorize_planning("demo", "free/model", confirm_cost=False)

    def crash(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("nexus.chat.service.send_turn", crash)
    with pytest.raises(KeyboardInterrupt):
        _ask("Implemente multiply.")
    conversation = store.get_conversation_for_project("demo")
    assert maestro.conversation_state("demo")["busy"] is True  # fresh: maybe still being sent
    old = (datetime.now(UTC) - timedelta(seconds=maestro.SEND_GRACE_SECONDS + 1)).isoformat()
    with database.get_connection() as connection:
        connection.execute("UPDATE workspace_maestro_messages SET created_at = ? WHERE conversation_id = ?", (old, conversation["id"]))
    state = maestro.conversation_state("demo")
    assert state["busy"] is False
    assert state["messages"][0]["status"] == "FAILED" and state["messages"][0]["error_code"] == "MAESTRO_SEND_INTERRUPTED"
    assert state["messages"][-1]["plan"]["source"] == "deterministic-fallback"
    assert service.list_panes("demo", include_hidden=True) == []


def test_unauthorized_answer_interrupted_before_the_plan_is_recovered(env):
    conversation = store.get_conversation(store.create_conversation("demo"))
    message_id = store.add_message(conversation["id"], author="user", text="Implemente multiply.", status="PENDING")
    state = maestro.conversation_state("demo")
    assert state["busy"] is False and state["messages"][-1]["plan"]["source"] == "deterministic"
    assert len(_plans_for(message_id)) == 1


def test_concurrent_reconciliation_and_repeated_answers_never_duplicate(env):
    conversation, message = _pending_message(env)
    _complete_last(env, _answer(SIMPLE))
    barrier = threading.Barrier(4)
    errors = []

    def run():
        barrier.wait()
        try:
            maestro.reconcile(conversation["id"])
        except Exception as error:  # noqa: BLE001 - surface any failure
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    assert len(_plans_for(message["id"])) == 1 and len(_replies(conversation["id"])) == 1
    plan = maestro_plan.parse(_answer(SIMPLE), models=MODELS, limit=3)
    again = store.record_maestro_answer(message["id"], project_id="demo", task_text="x", plan=plan,
                                        policy_version="maestro-plan/1", reply="dup", user_status="COMPLETED")
    assert again == _plans_for(message["id"])[0]["id"] and len(_replies(conversation["id"])) == 1


def test_database_refuses_a_second_plan_for_the_same_message(env):
    conversation, message = _pending_message(env)
    store.create_plan("demo", "x", policy_version="v", complexity="simple", proposal={"agents": []},
                      conversation_id=conversation["id"], message_id=message["id"])
    with pytest.raises(sqlite3.IntegrityError):
        store.create_plan("demo", "x", policy_version="v", complexity="simple", proposal={"agents": []},
                          conversation_id=conversation["id"], message_id=message["id"])
