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


class FakeNexusApi(run_dev_e2e.NexusApi):
    """A scripted Nexus server: routes the harness' HTTP calls, nothing else.

    Any route the harness is not supposed to use (Apply, Reject, an approval
    decision, ...) is not scripted and fails the test.
    """

    def __init__(self, source, worktree, base_sha, *, session_backend=None, execution_enabled=True,
                 review_overrides=None, history=None, on_diff=None, models=None):
        super().__init__("http://nexus.test", timeout=1)
        self.source, self.worktree, self.base_sha = source, worktree, base_sha
        self.session_backend, self.execution_enabled = session_backend, execution_enabled
        self.review_overrides, self.history, self.on_diff = review_overrides or {}, history or [], on_diff
        self.models = models if models is not None else {"backend": "codex_native", "models": [{"id": "gpt-test"}], "catalog_error": None}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.turn_polls = 0
        self.approval_polls = 0
        self.event_calls = 0

    def request(self, method, path, payload=None, *, timeout=None):
        self.calls.append((method, path, payload))
        if (method, path) == ("GET", "/api/projects"):
            return {"projects": [{"id": "fixture", "path": str(self.source), "enabled": 1, "dev_enabled": 1}]}
        if (method, path) == ("GET", "/api/dev/execution-status"):
            return {"execution_enabled": self.execution_enabled}
        if (method, path) == ("GET", "/api/dev/integration-status"):
            return {"integration_enabled": False}
        if method == "GET" and path.startswith("/api/dev/models"):
            return self.models
        if (method, path) == ("POST", "/api/dev/sessions"):
            backend = self.session_backend or (payload or {}).get("backend", "omniroute")
            return {"session_id": "DEV-1", "worktree_path": str(self.worktree), "base_sha": self.base_sha, "backend": backend}
        if (method, path) == ("POST", "/api/dev/sessions/DEV-1/turns"):
            return {"turn_id": "TURN-1", "status": "PENDING"}
        if (method, path) == ("GET", "/api/dev/sessions/DEV-1/turns/TURN-1"):
            self.turn_polls += 1
            return {"turn_id": "TURN-1", "status": "RUNNING" if self.turn_polls == 1 else "COMPLETED"}
        if (method, path) == ("GET", "/api/dev/sessions/DEV-1/approvals"):
            self.approval_polls += 1
            status = "PENDING" if self.approval_polls == 1 else "APPROVED"
            return {"approvals": [{"approval_id": "APPROVAL-1", "kind": "command", "status": status}]}
        if (method, path) == ("GET", "/api/dev/sessions/DEV-1/changed-files"):
            return {"files": [{"path": "calc.py", "status": "modified"}]}
        if (method, path) == ("GET", "/api/dev/sessions/DEV-1/diff"):
            if self.on_diff:
                self.on_diff()
            return {"files": [{"path": "calc.py"}]}
        if (method, path) == ("POST", "/api/dev/sessions/DEV-1/reviews"):
            review = {"review_id": "a" * 64, "status": "REVIEWED",
                      "snapshot": {"approval_ready": True, "base_sha": self.base_sha, "snapshot_hash": "b" * 64,
                                   "diff": {"files": [{"path": "calc.py"}]}}}
            review.update(self.review_overrides)
            return review
        if method == "GET" and path.startswith("/api/dev/sessions/DEV-1/preflight?review_id="):
            return {"ready": False, "issues": [{"code": "INTEGRATION_DISABLED"}]}
        if (method, path) == ("GET", "/api/dev/sessions/DEV-1/integrations"):
            return {"integrations": self.history}
        raise AssertionError(f"The harness must not call {method} {path}")

    def iter_sse_events(self, path, *, cursor, deadline):
        self.event_calls += 1
        if self.event_calls == 1:
            assert cursor == "DEV-1:TURN-1:0"
            yield "DEV-1:TURN-1:1", {"method": "item/commandExecution/requestApproval"}
            raise TimeoutError("simulated quiet SSE read")
        assert cursor == "DEV-1:TURN-1:1"
        yield "DEV-1:TURN-1:2", {"type": "nexus/turnEnded", "status": "COMPLETED"}


def _world(tmp_path, **api_options):
    source = run_dev_e2e.prepare_fixture(tmp_path / "source")
    worktree = tmp_path / "isolated-worktree"
    subprocess.run(["git", "worktree", "add", "-b", "nexus/dev/fixture", str(worktree), "HEAD"], cwd=source, check=True, capture_output=True)
    (worktree / "calc.py").write_text('def add(left: int, right: int) -> int:\n    return left + right\n', encoding="utf-8")
    base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, capture_output=True, text=True, check=True).stdout.strip()
    return source, FakeNexusApi(source, worktree, base_sha, **api_options)


def _args(source, **overrides):
    values = dict(confirm_agent_call=True, base_url="http://127.0.0.1:8000", http_timeout=1, project_id="fixture",
                  fixture=source, model="gpt-test", prompt="Fix add", turn_timeout=5, poll_interval=0, test_timeout=15,
                  backend="codex_native")
    values.update(overrides)
    return SimpleNamespace(**values)


def _run(source, api, backend_class=run_dev_e2e.CodexNativeDevBackend, **arg_overrides):
    return run_dev_e2e.run_smoke(_args(source, **arg_overrides), backend=backend_class(api))


def test_native_smoke_covers_session_turn_tests_diff_and_review_without_applying(tmp_path, capsys):
    source, api = _world(tmp_path)
    result = _run(source, api)

    assert result["backend"] == "codex_native"
    assert api.calls[api.calls.index(("GET", "/api/dev/integration-status", None)) + 1][:2] == ("POST", "/api/dev/sessions")
    [(_m, _p, session_payload)] = [call for call in api.calls if call[:2] == ("POST", "/api/dev/sessions")]
    assert session_payload == {"project_id": "fixture", "requested_model": "gpt-test", "backend": "codex_native"}
    assert [call for call in api.calls if call[1].endswith("/turns") and call[0] == "POST"] == [
        ("POST", "/api/dev/sessions/DEV-1/turns", {"prompt": "Fix add", "model": "gpt-test"})]
    assert result["turn_status"] == "COMPLETED" and result["sse_events"] == 2
    assert result["gates"] == {"execution_enabled": True, "integration_enabled": False}
    assert result["diff_files"] == [{"path": "calc.py"}] and "passed" in result["fixture_tests"]
    assert result["review"] == {"review_id": "a" * 64, "status": "REVIEWED", "files": ["calc.py"], "snapshot_hash": "b" * 64}
    assert result["preflight"] == {"ready": False, "issues": ["INTEGRATION_DISABLED"]}
    assert result["approvals"] == {"APPROVAL-1": {"kind": "command", "status": "APPROVED"}}
    assert result["integration_applied"] is False and result["source_unchanged"] is True
    assert result["fixture_head_unchanged"] is True
    assert "[approval pending] APPROVAL-1" in capsys.readouterr().err


def test_the_harness_never_applies_rejects_or_decides_an_approval(tmp_path):
    source, api = _world(tmp_path)
    _run(source, api)
    for method, path, _payload in api.calls:
        assert not path.endswith(("/apply", "/reject")), path
        assert not (method == "POST" and "/approvals" in path), path
    assert [call for call in api.calls if call[0] == "POST"] == [
        ("POST", "/api/dev/sessions", {"project_id": "fixture", "requested_model": "gpt-test", "backend": "codex_native"}),
        ("POST", "/api/dev/sessions/DEV-1/turns", {"prompt": "Fix add", "model": "gpt-test"}),
        ("POST", "/api/dev/sessions/DEV-1/reviews", None),
    ]


def test_omniroute_backend_remains_available_and_requests_no_native_backend(tmp_path):
    source, api = _world(tmp_path)
    result = _run(source, api, run_dev_e2e.CurrentOmniRouteDevBackend, backend="omniroute")
    [(_m, _p, payload)] = [call for call in api.calls if call[:2] == ("POST", "/api/dev/sessions")]
    assert payload == {"project_id": "fixture", "requested_model": "gpt-test"}
    assert result["backend"] == "omniroute"


def test_a_disabled_execution_gate_stops_before_any_session_is_created(tmp_path):
    source, api = _world(tmp_path, execution_enabled=False)
    with pytest.raises(RuntimeError, match="execution is disabled"):
        _run(source, api)
    assert not [call for call in api.calls if call[0] == "POST"]


def test_a_session_on_another_backend_is_refused_before_any_turn(tmp_path):
    source, api = _world(tmp_path, session_backend="omniroute")
    with pytest.raises(RuntimeError, match="expected 'codex_native'"):
        _run(source, api)
    assert not [call for call in api.calls if call[1].endswith("/turns")]


@pytest.mark.parametrize("override,message", [
    ({"status": "FAILED"}, "not recorded as REVIEWED"),
    ({"review_id": ""}, "not recorded as REVIEWED"),
    ({"snapshot": {"approval_ready": False, "base_sha": "x", "diff": {"files": [{"path": "calc.py"}]}}}, "not approval-ready"),
    ({"snapshot": {"approval_ready": True, "base_sha": "x", "diff": {"files": []}}}, "does not match the diff"),
    ({"snapshot": {"approval_ready": True, "base_sha": "x", "diff": {"files": [{"path": "other.py"}]}}}, "does not match the diff"),
])
def test_an_unusable_review_snapshot_fails_the_smoke(tmp_path, override, message):
    source, api = _world(tmp_path, review_overrides=override)
    with pytest.raises(RuntimeError, match=message):
        _run(source, api)


def test_a_review_bound_to_another_base_commit_fails_the_smoke(tmp_path):
    source, api = _world(tmp_path, review_overrides={"snapshot": {
        "approval_ready": True, "base_sha": "0" * 40, "diff": {"files": [{"path": "calc.py"}]}}})
    with pytest.raises(RuntimeError, match="different base commit"):
        _run(source, api)


@pytest.mark.parametrize("row", [{"status": "APPLIED"}, {"status": "APPLYING"}, {"status": "FAILED", "result_sha": "abc"}])
def test_an_applied_integration_is_never_accepted_as_a_clean_smoke(tmp_path, row):
    source, api = _world(tmp_path, history=[row])
    with pytest.raises(RuntimeError, match="never applies"):
        _run(source, api)


def test_a_change_to_the_source_repository_fails_the_smoke(tmp_path):
    source, api = _world(tmp_path)
    api.on_diff = lambda: (source / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Source fixture repository changed"):
        _run(source, api)


def test_a_dirty_source_repository_is_refused_before_any_session(tmp_path):
    source, api = _world(tmp_path)
    (source / "scratch.txt").write_text("local work", encoding="utf-8")
    with pytest.raises(RuntimeError, match="must be clean"):
        _run(source, api)
    assert not [call for call in api.calls if call[0] == "POST"]


def test_check_is_read_only_needs_no_confirmation_and_never_starts_inference(tmp_path):
    source, api = _world(tmp_path)
    report = run_dev_e2e.check_ready(_args(source, confirm_agent_call=False), backend=run_dev_e2e.CodexNativeDevBackend(api))
    assert report == {"backend": "codex_native", "project_id": "fixture",
                      "gates": {"execution_enabled": True, "integration_enabled": False}, "model_count": 1,
                      "catalog_error": None, "source_clean": True, "ready_for_confirmed_smoke": True}
    assert {call[0] for call in api.calls} == {"GET"}
    assert ("GET", "/api/dev/models?backend=codex_native", None) in api.calls


def test_check_reports_a_catalog_error_and_rejects_unknown_models_and_disabled_gates(tmp_path):
    source, api = _world(tmp_path, models={"models": [], "catalog_error": "Codex is not signed in"})
    report = run_dev_e2e.check_ready(_args(source), backend=run_dev_e2e.CodexNativeDevBackend(api))
    assert report["ready_for_confirmed_smoke"] is False and report["catalog_error"] == "Codex is not signed in"
    source, api = _world(tmp_path / "b")
    with pytest.raises(RuntimeError, match="not offered"):
        run_dev_e2e.check_ready(_args(source, model="other-model"), backend=run_dev_e2e.CodexNativeDevBackend(api))
    source, api = _world(tmp_path / "c", execution_enabled=False)
    with pytest.raises(RuntimeError, match="execution is disabled"):
        run_dev_e2e.check_ready(_args(source), backend=run_dev_e2e.CodexNativeDevBackend(api))


def test_cli_defaults_to_codex_native_and_real_turns_need_explicit_confirmation(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(run_dev_e2e.NexusApi, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")))
    monkeypatch.setattr(run_dev_e2e.sys, "argv", ["run_dev_e2e.py", "--project-id", "fixture", "--fixture", "."])
    assert run_dev_e2e.main() == 1
    assert "--confirm-agent-call" in capsys.readouterr().err
    monkeypatch.setattr(run_dev_e2e, "run_smoke", lambda args: seen.append(args) or {})
    assert run_dev_e2e.main() == 0
    assert seen[0].backend == "codex_native" and seen[0].confirm_agent_call is False
    assert isinstance(run_dev_e2e._backend_for(seen[0]), run_dev_e2e.CodexNativeDevBackend)


def test_smoke_refuses_real_agent_call_without_confirmation():
    args = SimpleNamespace(confirm_agent_call=False)
    with pytest.raises(RuntimeError, match="real agent"):
        run_dev_e2e.run_smoke(args)
