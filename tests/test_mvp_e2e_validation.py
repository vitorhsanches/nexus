"""P3.4A: deterministic end-to-end validation of the Nexus MVP.

These tests deliberately keep the orchestration, SQLite registry, Git
worktrees, mission state machine, trusted-test subprocess, and review snapshot
real.  Only external inference/telemetry boundaries are replaced.  No test in
this module can contact Codex App-Server or OmniRoute.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from nexus.dev import integration, mission_runner
from nexus.dev.service import DevServiceError
from nexus.registry import database, dev_missions, dev_sessions
from nexus.workspace import maestro, service

FREE_MODEL = {"id": "mock/free", "cost_status": "free"}
PAID_MODEL = {"id": "mock/paid", "cost_status": "standard"}


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _wait_for_mission(session_id: str, mission_id: str, expected: set[str], timeout: float = 10) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        mission = mission_runner.get_mission(session_id, mission_id)
        if mission["status"] in expected:
            return mission
        time.sleep(0.01)
    raise AssertionError(f"mission {mission_id} did not reach {sorted(expected)}")


@pytest.fixture
def mvp_env(tmp_path, monkeypatch):
    original_db = database.DATABASE_PATH
    original_storage = database.STORAGE_DIR
    database.STORAGE_DIR = tmp_path / "storage"
    database.DATABASE_PATH = database.STORAGE_DIR / "nexus.db"
    database.initialize_database()

    monkeypatch.setenv("NEXUS_DEV_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("NEXUS_DEV_WORKTREE_ROOT", str(tmp_path / "dev-worktrees"))
    monkeypatch.delenv("NEXUS_WORKSPACE_MAX_PANES", raising=False)
    monkeypatch.delenv("NEXUS_WORKSPACE_MAX_AUTO_PANES", raising=False)

    catalog = {
        "models": [FREE_MODEL.copy(), PAID_MODEL.copy()],
        "default_model": FREE_MODEL["id"],
        "catalog_error": None,
    }
    monkeypatch.setattr("nexus.dev.models.list_dev_models", lambda: catalog)

    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "mission_seed.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=Nexus E2E",
        "-c",
        "user.email=nexus-e2e@example.invalid",
        "commit",
        "-q",
        "-m",
        "deterministic fixture",
    )
    with database.get_connection() as connection:
        connection.execute(
            "INSERT INTO projects (id, name, path, enabled, dev_enabled) VALUES ('mvp','MVP',?,1,1)",
            (str(repo),),
        )

    def unexpected_external(*_args, **_kwargs):
        raise AssertionError("an external Codex/OmniRoute/telemetry boundary was called")

    # Guard every external edge that this flow could otherwise reach.  The
    # model catalog and agent turn are injected locally below each scenario.
    monkeypatch.setattr("nexus.chat.service.create_session", unexpected_external)
    monkeypatch.setattr("nexus.chat.service.send_turn", unexpected_external)
    monkeypatch.setattr("nexus.dev.executor.AppServerClient", unexpected_external)
    monkeypatch.setattr("nexus.routing.telemetry.OmniRouteTelemetryClient.fetch_models", unexpected_external)
    monkeypatch.setattr("nexus.routing.telemetry.OmniRouteTelemetryClient.collect", unexpected_external)
    monkeypatch.setattr(mission_runner.executor, "run_dev_turn", unexpected_external)

    passing_script = (
        "from pathlib import Path; "
        "assert Path('mission_result.txt').read_text(encoding='utf-8') == "
        "'simulated mission complete\\n'"
    )
    failing_script = "import sys; print('deterministic failure'); sys.exit(7)"

    def test_profile(name: str, _project_id: str):
        if name == "mvp-pass":
            return [sys.executable, "-c", passing_script], 20
        if name == "mvp-fail":
            return [sys.executable, "-c", failing_script], 20
        return None

    monkeypatch.setattr(mission_runner, "resolve_test_profile", test_profile)
    mission_runner._active_missions.clear()
    yield {"repo": repo, "catalog": catalog, "external_guard": unexpected_external}

    for thread in threading.enumerate():
        if thread.name.startswith("nexus-mission-") and thread is not threading.current_thread():
            thread.join(timeout=5)
    mission_runner._active_missions.clear()
    database.DATABASE_PATH = original_db
    database.STORAGE_DIR = original_storage


def _approved_pane(*, max_turns: int = 3) -> tuple[dict, dict]:
    state = maestro.send_message(
        "mvp",
        "Criar um resultado determinístico dentro da worktree isolada da missão.",
    )
    assert state["planning"]["authorized"] is False
    assert state["chat_session_id"] is None
    plan = next(message["plan"] for message in state["messages"] if message["plan"])
    assert plan["source"] == "deterministic"
    assert [agent["key"] for agent in plan["agents"]] == ["implementer"]

    approved = service.approve_plan(
        plan["plan_id"],
        agent_models={"implementer": FREE_MODEL["id"]},
        max_turns_per_agent=max_turns,
        mission_timeout_seconds=60,
        confirm_costs=False,
    )
    assert approved["plan"]["status"] == "APPROVED"
    return approved["plan"], approved["panes"][0]


def _install_simulated_codex_turn(monkeypatch, calls: list[dict], *, write_result: bool = True):
    def run_turn(session_id: str, prompt: str, *, model: str | None = None) -> dict:
        row = dev_sessions.get_session(session_id)
        assert row is not None
        if write_result:
            Path(row["worktree_path"], "mission_result.txt").write_text(
                "simulated mission complete\n", encoding="utf-8"
            )
        turn_id = dev_sessions.create_turn(session_id, prompt, requested_model=model)
        dev_sessions.update_turn_status(turn_id, "RUNNING")
        dev_sessions.update_turn_status(
            turn_id, "COMPLETED", response_text="deterministic Codex App-Server mock"
        )
        calls.append({"session_id": session_id, "turn_id": turn_id, "model": model})
        return {"turn_id": turn_id, "status": "COMPLETED"}

    monkeypatch.setattr(mission_runner.executor, "run_dev_turn", run_turn)


def test_complete_simulated_mission_reaches_persisted_human_review_without_integration(mvp_env, monkeypatch):
    repo = mvp_env["repo"]
    original_head = _git(repo, "rev-parse", "HEAD")
    original_status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    calls: list[dict] = []
    _install_simulated_codex_turn(monkeypatch, calls)

    plan, pane = _approved_pane()
    session = pane["session"]
    worktree = Path(session["worktree_path"])
    assert worktree.is_dir() and worktree != repo
    assert _git(worktree, "rev-parse", "HEAD") == original_head
    assert _git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == session["branch"]

    started = service.start_pane_mission(
        pane["pane_id"], None, confirm_model=None, test_profile="mvp-pass"
    )["mission"]
    final = _wait_for_mission(
        session["session_id"], started["mission_id"], {"READY_FOR_REVIEW"}
    )

    assert len(calls) == 1
    assert final["attempt_count"] == 1
    assert final["tests"][0]["status"] == "PASSED"
    assert final["result"]["human_approval_required"] is True
    assert final["result"]["integration_applied"] is False
    assert final["result"]["changed_files"] == [
        {"path": "mission_result.txt", "status": "added"}
    ]

    review = integration.create_review(session["session_id"])
    assert review["status"] == "REVIEWED"
    assert review["result_sha"] is None
    assert review["snapshot"]["approval_ready"] is True
    assert [item["path"] for item in review["snapshot"]["diff"]["files"]] == [
        "mission_result.txt"
    ]

    # Human review is persisted, but neither the mission nor review creation
    # changes the destination checkout or performs an integration.
    assert not (repo / "mission_result.txt").exists()
    assert (worktree / "mission_result.txt").read_text(encoding="utf-8") == "simulated mission complete\n"
    assert _git(repo, "rev-parse", "HEAD") == original_head
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == original_status

    # Read the durable state again through fresh SQLite connections, then
    # emulate process recovery.  Recovery marks uncertainty and never replays.
    assert service.list_plans("mvp")[0]["plan_id"] == plan["plan_id"]
    assert mission_runner.get_mission(session["session_id"], final["mission_id"])["status"] == "READY_FOR_REVIEW"
    with database.get_connection() as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "workspace_plans",
                "workspace_panes",
                "dev_sessions",
                "dev_turns",
                "dev_autonomous_missions",
                "dev_mission_test_runs",
                "dev_integration_reviews",
            )
        }
    assert counts == {table: 1 for table in counts}

    orphan = dev_missions.create_mission(
        session["session_id"],
        "mission interrupted by a simulated restart",
        requested_model=FREE_MODEL["id"],
        allowed_models=[FREE_MODEL["id"]],
        test_profile="mvp-pass",
    )
    dev_missions.update_status(orphan, "RUNNING")
    assert dev_missions.recover_orphaned_missions(lambda *_owner: True) == [orphan]
    assert dev_missions.get_mission(orphan)["status"] == "OUTCOME_UNKNOWN"
    assert len(calls) == 1, "recovery must not replay external inference"


def test_authorization_cost_and_unavailable_model_fail_closed_before_mission_persistence(mvp_env):
    _plan, pane = _approved_pane()
    session_id = pane["session"]["session_id"]

    with database.get_connection() as connection:
        connection.execute("UPDATE projects SET dev_enabled=0 WHERE id='mvp'")
    with pytest.raises(DevServiceError) as authorization:
        mission_runner.start_mission(
            session_id,
            "must not run",
            model=FREE_MODEL["id"],
            allowed_models=[FREE_MODEL["id"]],
            test_profile="mvp-pass",
            timeout_seconds=30,
        )
    assert authorization.value.code == "DEV_MODE_DISABLED"

    with database.get_connection() as connection:
        connection.execute("UPDATE projects SET dev_enabled=1 WHERE id='mvp'")
    service.update_pane(pane["pane_id"], model=PAID_MODEL["id"])
    with pytest.raises(DevServiceError) as cost:
        service.start_pane_mission(
            pane["pane_id"],
            "must require cost consent",
            confirm_model=PAID_MODEL["id"],
            confirm_cost=False,
            test_profile="mvp-pass",
        )
    assert cost.value.code == "COST_CONFIRMATION_REQUIRED"

    with pytest.raises(DevServiceError) as unavailable:
        mission_runner.start_mission(
            session_id,
            "must reject unavailable route",
            model="mock/unavailable",
            allowed_models=["mock/unavailable"],
            test_profile="mvp-pass",
            timeout_seconds=30,
        )
    assert unavailable.value.code == "MODEL_NOT_ALLOWED"
    assert dev_missions.list_missions(session_id) == []


def test_failure_repair_limit_and_cancellation_are_durable(mvp_env, monkeypatch):
    _plan, pane = _approved_pane(max_turns=3)
    session_id = pane["session"]["session_id"]
    calls: list[dict] = []
    _install_simulated_codex_turn(monkeypatch, calls, write_result=False)

    failed_start = service.start_pane_mission(
        pane["pane_id"], None, confirm_model=None, test_profile="mvp-fail"
    )["mission"]
    failed = _wait_for_mission(session_id, failed_start["mission_id"], {"FAILED"})
    assert failed["error_code"] == "TESTS_FAILED_AFTER_REPAIR"
    assert failed["attempt_count"] == 2
    assert [run["status"] for run in failed["tests"]] == ["FAILED", "FAILED"]
    assert len(calls) == 2, "exactly one repair turn is allowed"

    running_turn: dict[str, str] = {}

    def run_until_cancel(session: str, prompt: str, *, model: str | None = None) -> dict:
        turn_id = dev_sessions.create_turn(session, prompt, requested_model=model)
        dev_sessions.update_turn_status(turn_id, "RUNNING")
        running_turn["id"] = turn_id
        return {"turn_id": turn_id, "status": "RUNNING"}

    def interrupt_turn(session: str, turn_id: str | None = None) -> dict:
        assert session == session_id and turn_id == running_turn["id"]
        dev_sessions.update_turn_status(turn_id, "CANCELLED")
        return {"status": "INTERRUPT_REQUESTED", "turn_id": turn_id}

    monkeypatch.setattr(mission_runner.executor, "run_dev_turn", run_until_cancel)
    monkeypatch.setattr(mission_runner.executor, "interrupt_dev_turn", interrupt_turn)
    cancelled_start = service.start_pane_mission(
        pane["pane_id"],
        "wait until explicitly cancelled",
        confirm_model=None,
        test_profile="mvp-pass",
    )["mission"]
    _wait_for_mission(session_id, cancelled_start["mission_id"], {"RUNNING"})
    response = mission_runner.interrupt_mission(session_id, cancelled_start["mission_id"])
    assert response["status"] == "INTERRUPT_REQUESTED"
    cancelled = _wait_for_mission(session_id, cancelled_start["mission_id"], {"CANCELLED"})
    assert cancelled["interrupt_requested"] is True
    assert cancelled["error_code"] == "MISSION_INTERRUPTED"
    assert cancelled["tests"] == []

