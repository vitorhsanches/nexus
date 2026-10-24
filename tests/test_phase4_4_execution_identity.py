"""Phase 4.4 regressions for Worker environment and route identity."""

from pathlib import Path
from unittest.mock import patch

from nexus.orchestration.progressive import execute_progressively
from nexus.routing.service import RoutingDecision


def _worker(route_class="complex-coding"):
    return {
        "route_class": route_class,
        "execution_path": "OMNIROUTE",
        "provider": "manager-advisory",
        "model": "manager-advisory",
        "effort": "high",
        "scope": "bounded change",
        "reason": "manager recommendation",
    }


def _completed():
    return {
        "agent_id": "WORKER-1",
        "status": "COMPLETED",
        "exit_code": 0,
        "worktree": "C:/tmp/worker",
        "branch": "worker-branch",
    }


def _passing_review():
    return {
        "status": "COMPLETED",
        "reviewer_id": "REVIEWER-1",
        "review": {
            "verdict": "PASS",
            "failure_class": None,
            "summary": "pass",
            "evidence": [],
        },
    }


class _RoutingService:
    def __init__(self, decisions):
        self.decisions = iter(decisions)

    def select_route_for_capability(self, *args, **kwargs):
        return next(self.decisions)


def test_complex_coding_executes_exact_selected_identity():
    selected = RoutingDecision(
        model="cc/claude-sonnet-5-low",
        provider="claude",
        effort="low",
        execution_path="OMNIROUTE",
        reason="qualified route",
    )

    with patch("nexus.orchestration.executor.run_omniroute_worker", return_value=_completed()) as launch, \
         patch("nexus.orchestration.progressive.review_worker", return_value=_passing_review()), \
         patch("nexus.orchestration.progressive.update_run_status"), \
         patch("nexus.orchestration.progressive.record_checkpoint"):
        result = execute_progressively(
            run_id="RUN-IDENTITY",
            repo="C:/repo",
            manager_id="MANAGER-1",
            original_task="task",
            planned_worker=_worker(),
            plan_risk="LOW",
            routing_service=_RoutingService([selected]),
        )

    assert result["status"] == "COMPLETED"
    assert (launch.call_args.kwargs["model"], launch.call_args.kwargs["effort"]) == (
        selected.model,
        selected.effort,
    )


def test_transient_fallback_executes_new_routes_own_effort():
    first = RoutingDecision(
        model="route-a", provider="provider-a", effort="low",
        execution_path="OMNIROUTE", reason="initial",
    )
    fallback = RoutingDecision(
        model="route-b", provider="provider-b", effort="high",
        execution_path="OMNIROUTE", reason="fallback",
    )
    failed = {
        "agent_id": "WORKER-FAIL",
        "status": "FAILED",
        "exit_code": 1,
        "reason": "TRANSIENT_PROVIDER_ERROR",
        "worktree": None,
        "branch": None,
    }

    with patch("nexus.orchestration.progressive.execute_worker", side_effect=[failed, failed, _completed()]) as execute, \
         patch("nexus.orchestration.progressive.review_worker", return_value=_passing_review()), \
         patch("nexus.orchestration.progressive.update_run_status"), \
         patch("nexus.orchestration.progressive.record_checkpoint"), \
         patch("nexus.orchestration.progressive.time.sleep"):
        result = execute_progressively(
            run_id="RUN-FALLBACK",
            repo="C:/repo",
            manager_id="MANAGER-1",
            original_task="task",
            planned_worker=_worker(),
            plan_risk="LOW",
            routing_service=_RoutingService([first, fallback]),
        )

    assert result["status"] == "COMPLETED"
    identities = [
        (call.kwargs["worker"]["model"], call.kwargs["worker"]["effort"])
        for call in execute.call_args_list
    ]
    assert identities == [("route-a", "low"), ("route-a", "low"), ("route-b", "high")]


def test_dispatcher_passes_unique_codex_home_to_each_adapter_launch(tmp_path):
    from nexus.dispatchers.omniroute import run_omniroute_worker

    adapter = tmp_path / "adapter.ps1"
    adapter.write_text("# test adapter", encoding="utf-8")
    homes = iter([tmp_path / "worker-a", tmp_path / "worker-b"])
    launched = []

    def isolated_env(profile="default"):
        assert profile == "omniroute"
        home = next(homes)
        home.mkdir()
        (home / "config.toml").write_text(
            'model_provider = "omniroute"\n[model_providers.omniroute]\n',
            encoding="utf-8",
        )
        (home / "auth.json").write_text("{}", encoding="utf-8")
        return {"CODEX_HOME": str(home)}

    class Process:
        stdout = iter(["Branch : branch\n", "Worktree : C:/worker\n", "WORKTREE_PRESERVED=C:/worker\n"])
        returncode = 0

        def wait(self):
            return 0

        def poll(self):
            return 0

    def launch(*args, **kwargs):
        worker_home = Path(kwargs["env"]["CODEX_HOME"])
        launched.append(
            (
                str(worker_home),
                (worker_home / "config.toml").read_text(encoding="utf-8"),
            )
        )
        return Process()

    with patch("nexus.dispatchers.omniroute.ADAPTER_PATH", adapter), \
         patch("nexus.dispatchers.omniroute.codex_subprocess_env", side_effect=isolated_env), \
         patch("nexus.dispatchers.omniroute.subprocess.Popen", side_effect=launch), \
         patch("nexus.dispatchers.omniroute.ProcessWatchdog"), \
         patch("nexus.dispatchers.omniroute.create_agent", side_effect=["A-1", "A-2"]), \
         patch("nexus.dispatchers.omniroute.update_agent_status"), \
         patch("nexus.dispatchers.omniroute.update_agent_execution"):
        for index in range(2):
            run_omniroute_worker(
                run_id=f"RUN-{index}", repo="C:/repo", task="task",
                model="cc/claude-sonnet-5-low", effort="low",
            )

    assert launched[0][0] != launched[1][0]
    assert {Path(value).name for value, _ in launched} == {"worker-a", "worker-b"}
    assert all('model_provider = "omniroute"' in config for _, config in launched)


def test_dispatcher_fails_before_launch_when_omniroute_profile_is_invalid(tmp_path):
    from nexus.dispatchers.omniroute import run_omniroute_worker

    adapter = tmp_path / "adapter.ps1"
    adapter.write_text("# test adapter", encoding="utf-8")

    with patch("nexus.dispatchers.omniroute.ADAPTER_PATH", adapter), \
         patch(
             "nexus.dispatchers.omniroute.codex_subprocess_env",
             side_effect=RuntimeError("OmniRoute Worker configuration is unavailable"),
         ), \
         patch("nexus.dispatchers.omniroute.subprocess.Popen") as launch, \
         patch("nexus.dispatchers.omniroute.create_agent", return_value="A-1"), \
         patch("nexus.dispatchers.omniroute.update_agent_status"):
        result = run_omniroute_worker(
            run_id="RUN-1",
            repo="C:/repo",
            task="task",
            model="cc/claude-sonnet-5-low",
            effort="low",
        )

    assert result["status"] == "LAUNCH_FAILED"
    assert result["error"] == "OmniRoute Worker configuration is unavailable"
    launch.assert_not_called()


def test_repository_adapter_requires_isolated_safe_codex_home():
    from nexus.dispatchers.omniroute import ADAPTER_PATH
    from nexus.runtime_assets import OMNIROUTE_WORKER_SCRIPT

    assert ADAPTER_PATH == OMNIROUTE_WORKER_SCRIPT
    source = ADAPTER_PATH.read_text(encoding="utf-8")

    assert "Nexus Worker requires an isolated CODEX_HOME" in source
    assert 'Join-Path $env:CODEX_HOME "config.toml"' in source
    assert r'Join-Path $env:CODEX_HOME "rules\nexus-worker.rules"' in source
    assert ".codex-omniroute-worker" not in source
