"""Automated tests for the Development E2E harness; all agent/network calls are mocked."""

from __future__ import annotations

import subprocess
import socket
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


def test_sse_transport_yields_frames_before_read_timeout_and_sends_cursor(monkeypatch):
    class FakeSocket:
        timeouts = []

        def settimeout(self, value):
            self.timeouts.append(value)

    class FakeRaw:
        def __init__(self, sock):
            self._sock = sock

    class FakeFp:
        def __init__(self, sock):
            self.raw = FakeRaw(sock)

    class FakeResponse:
        def __init__(self):
            self.fp = FakeFp(FakeSocket())
            self.lines = iter([
                b"id: DEV-1:TURN-1:1\n",
                b'data: {"method":"turn/started"}\n',
                b"\n",
            ])

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self):
            try:
                return next(self.lines)
            except StopIteration as exc:
                raise socket.timeout("quiet stream") from exc

    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(run_dev_e2e.urllib.request, "urlopen", fake_urlopen)
    api = run_dev_e2e.NexusApi("http://localhost", timeout=5)
    stream = api.iter_sse_events("/events", cursor="DEV-1:TURN-1:0", deadline=run_dev_e2e.time.monotonic() + 1)
    event_id, event = next(stream)
    assert event_id == "DEV-1:TURN-1:1"
    assert event == {"method": "turn/started"}
    with pytest.raises(TimeoutError, match="SSE connection timed out"):
        next(stream)
    assert captured["request"].get_header("Last-event-id") == "DEV-1:TURN-1:0"
    assert captured["timeout"] <= 1
    assert captured["request"].full_url.endswith("/events")


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

    class FakeBackend:
        def __init__(self):
            self.turn_start_calls = 0
            self.event_calls = 0
            self.status_calls = 0

        def select_project(self, project_id):
            assert project_id == "fixture"
            return {"id": "fixture", "path": str(source), "enabled": 1, "dev_enabled": 1}

        def create_session(self, _project_id, _model):
            return {"session_id": "DEV-1", "worktree_path": str(worktree), "base_sha": base_sha}

        def start_turn(self, _session_id, _prompt, _model):
            self.turn_start_calls += 1
            return {"turn_id": "TURN-1", "status": "PENDING"}

        def iter_events(self, session_id, turn_id, cursor, _deadline):
            self.event_calls += 1
            if self.event_calls == 1:
                assert cursor == f"{session_id}:{turn_id}:0"
                yield f"{session_id}:{turn_id}:1", {"method": "turn/started"}
                raise TimeoutError("simulated quiet SSE read")
            assert cursor == f"{session_id}:{turn_id}:1"
            yield f"{session_id}:{turn_id}:2", {"type": "nexus/turnEnded", "status": "COMPLETED"}

        def get_turn(self, *_args):
            self.status_calls += 1
            status = "RUNNING" if self.status_calls == 1 else "COMPLETED"
            return {"turn_id": "TURN-1", "status": status}

        def changed_files(self, *_args):
            return [{"path": "calc.py", "status": "modified"}]

        def diff(self, *_args):
            return {"files": [{"path": "calc.py"}]}

    backend = FakeBackend()
    args = SimpleNamespace(
        confirm_agent_call=True, base_url="http://127.0.0.1:8000", http_timeout=1,
        project_id="fixture", fixture=source, model="codex-test", prompt="Fix add",
        turn_timeout=5, poll_interval=0, test_timeout=15,
    )
    result = run_dev_e2e.run_smoke(args, backend=backend)
    assert result["turn_status"] == "COMPLETED"
    assert result["sse_events"] == 2
    assert result["diff_files"] == [{"path": "calc.py"}]
    assert result["fixture_head_unchanged"] is True
    assert result["integration_applied"] is False
    assert backend.turn_start_calls == 1
    assert backend.event_calls == 2


def test_smoke_refuses_real_agent_call_without_confirmation():
    args = SimpleNamespace(confirm_agent_call=False)
    with pytest.raises(RuntimeError, match="real agent"):
        run_dev_e2e.run_smoke(args)
