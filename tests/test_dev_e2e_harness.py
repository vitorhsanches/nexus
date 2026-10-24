"""Automated tests for the Development E2E harness; all agent/network calls are mocked."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from scripts import run_dev_e2e


def test_parse_sse_notifications_and_terminal_events():
    body = (
        'id: DEV-1:TURN-1:1\ndata: {"method":"turn/started"}\n\n'
        'data: {"type":"nexus/turnEnded","status":"COMPLETED"}\n\n'
    )
    assert run_dev_e2e.parse_sse_events(body) == [
        {"method": "turn/started"},
        {"type": "nexus/turnEnded", "status": "COMPLETED"},
    ]


def test_prepare_fixture_creates_clean_disposable_repo_and_refuses_overwrite(tmp_path):
    destination = run_dev_e2e.prepare_fixture(tmp_path / "fixture")
    assert (destination / "calc.py").exists()
    assert (destination / "test_calc.py").exists()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=destination, capture_output=True, text=True, check=True)
    assert status.stdout == ""
    with pytest.raises(FileExistsError):
        run_dev_e2e.prepare_fixture(destination)
    baseline = subprocess.run(
        ["python", "-m", "pytest", "-q", "test_calc.py"], cwd=destination,
        capture_output=True, text=True,
    )
    assert baseline.returncode != 0


def test_smoke_verifies_turn_diff_tests_and_git_isolation_without_real_agent(tmp_path, monkeypatch):
    source = run_dev_e2e.prepare_fixture(tmp_path / "source")
    worktree = tmp_path / "isolated-worktree"
    subprocess.run(["git", "worktree", "add", "-b", "nexus/dev/fixture", str(worktree), "HEAD"], cwd=source, check=True, capture_output=True)
    (worktree / "calc.py").write_text('def add(left: int, right: int) -> int:\n    return left + right\n', encoding="utf-8")
    base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, capture_output=True, text=True, check=True).stdout.strip()

    class FakeApi:
        def __init__(self, *_args, **_kwargs):
            self.calls = []

        def request(self, method, path, payload=None):
            self.calls.append((method, path, payload))
            if path == "/api/projects":
                return {"projects": [{"id": "fixture", "path": str(source), "enabled": 1, "dev_enabled": 1}]}
            if path == "/api/dev/sessions":
                return {"session_id": "DEV-1", "worktree_path": str(worktree), "base_sha": base_sha}
            if path.endswith("/turns") and method == "POST":
                return {"turn_id": "TURN-1", "status": "PENDING"}
            if path.endswith("/events?turn_id=TURN-1"):
                return 'data: {"method":"turn/started"}\n\ndata: {"type":"nexus/turnEnded","status":"COMPLETED"}\n\n'
            if path.endswith("/turns/TURN-1"):
                return {"turn_id": "TURN-1", "status": "COMPLETED"}
            if path.endswith("/changed-files"):
                return {"files": [{"path": "calc.py", "status": "modified"}]}
            if path.endswith("/diff"):
                return {"files": [{"path": "calc.py"}]}
            raise AssertionError(f"Unexpected API call: {method} {path}")

    monkeypatch.setattr(run_dev_e2e, "NexusApi", FakeApi)
    args = SimpleNamespace(
        confirm_agent_call=True, base_url="http://127.0.0.1:8000", http_timeout=1,
        project_id="fixture", fixture=source, model="codex-test", prompt="Fix add",
        turn_timeout=5, poll_interval=0, test_timeout=15,
    )
    result = run_dev_e2e.run_smoke(args)
    assert result["turn_status"] == "COMPLETED"
    assert result["sse_events"] == 2
    assert result["diff_files"] == [{"path": "calc.py"}]
    assert result["fixture_head_unchanged"] is True
    assert result["integration_applied"] is False


def test_smoke_refuses_real_agent_call_without_confirmation():
    args = SimpleNamespace(confirm_agent_call=False)
    with pytest.raises(RuntimeError, match="real agent"):
        run_dev_e2e.run_smoke(args)
