"""Mocked protocol tests for nexus.chat.appserver_client.AppServerClient.

No real `codex app-server` subprocess is spawned in these tests: a small
request-driven fake server (`FakeAppServer`) stands in for it -- it only
emits a response line for a given JSON-RPC method once the client has
actually written that request to (fake) stdin, so test timing matches a
real server instead of blindly replaying a canned transcript. This makes
the suite deterministic and fast.

The one live, real-protocol smoke test is gated behind
NEXUS_CHAT_LIVE_SMOKE=1 and skips cleanly (never fails, never retries)
when that is unset or Codex/OmniRoute is not actually available -- see
test_live_two_message_continuity_and_resume at the bottom.

Deliberately avoids pytest's `tmp_path`/`tmp_path_factory` fixtures: this
environment has a pre-existing, pre-diagnosed Windows ACL block on the
shared `%TEMP%\\pytest-of-<user>` base directory (unrelated to this task;
see prior validation reports). Plain `tempfile.TemporaryDirectory()`
(used only by the gated live test) is unaffected because it never touches
that pytest-managed directory.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import time

import pytest

from nexus.chat.appserver_client import AppServerClient, AppServerError, CodexNativeAppServerClient


# -- fakes -------------------------------------------------------------


class FakeAppServer:
    """Emits a response for a method only after that method is actually
    written to stdin -- never a blind, timing-independent canned replay.
    """

    def __init__(self, responder: dict | None = None, close_after=frozenset()):
        self._responder = responder or {}
        self._close_after = set(close_after)
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self.sent: list[dict] = []
        self._closed = False

    def handle_write(self, request: dict) -> None:
        self.sent.append(request)
        method = request.get("method")
        for line in self._responder.get(method, []):
            self.push(line)
        if method in self._close_after:
            self.close()

    def push(self, line) -> None:
        if self._closed:
            return
        self._queue.put(line if isinstance(line, str) else json.dumps(line))

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put(None)

    def __iter__(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            yield item + "\n"


class FakeStdin:
    def __init__(self, server: FakeAppServer):
        self._server = server
        self.closed = False

    def write(self, text: str) -> None:
        if self.closed:
            raise ValueError("write to closed stdin")
        for line in text.splitlines():
            line = line.strip()
            if line:
                self._server.handle_write(json.loads(line))

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    """Stand-in for subprocess.Popen. No real OS process is involved."""

    def __init__(self, server: FakeAppServer, terminates_on_stdin_close: bool = True):
        self.server = server
        self.stdin = FakeStdin(server)
        self.stdout = iter(server)
        self.stderr = iter(())
        self._terminates_on_stdin_close = terminates_on_stdin_close
        self.returncode: int | None = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self._terminates_on_stdin_close and self.stdin.closed:
            self.returncode = 0
            return self.returncode
        raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)

    def kill(self):
        self.returncode = -9


def _ok(request_id, result=None):
    return json.dumps({"id": request_id, "result": result or {}})


def _notification(method, params=None):
    return json.dumps({"method": method, "params": params or {}})


def _thread_result(thread_id="THREAD-1", provider="omniroute", model="cc/test"):
    return {
        "thread": {"id": thread_id, "modelProvider": provider, "model": model},
        "model": model,
        "modelProvider": provider,
    }


def _make_client(
    responder,
    *,
    close_after=frozenset(),
    terminates_on_stdin_close: bool = True,
    request_timeout_seconds: float = 2,
    client_class=AppServerClient,
    **client_kwargs,
):
    captured = {}

    def launcher(command, *, env, cwd):
        captured["command"] = command
        captured["env"] = env
        captured["cwd"] = cwd
        server = FakeAppServer(responder, close_after=close_after)
        process = FakeProcess(server, terminates_on_stdin_close=terminates_on_stdin_close)
        captured["process"] = process
        captured["server"] = server
        return process

    client = client_class(
        r"C:\fake\repo",
        codex_path=r"C:\fake\codex.exe",
        process_launcher=launcher,
        request_timeout_seconds=request_timeout_seconds,
        **client_kwargs,
    )
    return client, captured


@pytest.fixture(autouse=True)
def _fake_omniroute_env(request, monkeypatch):
    """Every mocked test gets a harmless, fast codex_subprocess_env/
    git-trust stand-in unless it overrides one explicitly. The gated live
    test needs the REAL helpers (that is the whole point of that test), so
    it opts out by name."""
    if request.node.name == "test_live_two_message_continuity_and_resume":
        return

    import nexus.chat.appserver_client as mod

    monkeypatch.setattr(
        mod,
        "codex_subprocess_env",
        lambda profile, worker_home=None, include_auth=True: {"FAKE_ENV": "1", "CODEX_HOME": worker_home or "fake-home"},
    )
    monkeypatch.setattr(mod, "with_scoped_git_trust", lambda env, repo: dict(env))


# -- request/response correlation ---------------------------------------


def test_request_response_correlation_matches_by_id():
    client, captured = _make_client(
        {"initialize": [_ok(1, {})], "thread/start": [_ok(2, _thread_result())]}
    )
    client.start()
    result = client.thread_start()
    assert result["thread"]["id"] == "THREAD-1"
    sent = captured["server"].sent
    assert sent[0]["method"] == "initialize"
    assert sent[1] == {"method": "initialized", "params": {}}
    assert sent[2]["method"] == "thread/start"
    assert sent[2]["id"] == 2


def test_an_unmatched_stray_response_id_does_not_break_later_correlation():
    # A response the client never asked for (id=999) arrives mixed in --
    # it must be silently dropped, not confused with a real pending
    # request.
    client, _ = _make_client(
        {
            "initialize": [_ok(1, {}), _ok(999, {"unexpected": True})],
            "thread/start": [_ok(2, _thread_result())],
        }
    )
    client.start()
    result = client.thread_start()
    assert result["thread"]["id"] == "THREAD-1"


# -- notification handling ------------------------------------------------


def test_notifications_are_delivered_to_callback_and_buffered():
    received = []
    client, _ = _make_client(
        {
            "initialize": [_ok(1, {}), _notification("thread/started", {"threadId": "T1"})],
            "thread/start": [_ok(2, _thread_result())],
        },
        on_notification=received.append,
    )
    client.start()
    client.thread_start()
    assert any(event.get("method") == "thread/started" for event in received)
    assert any(
        event.get("method") == "thread/started" for event in client.recent_events
    )


def test_notifications_redact_process_credentials_before_delivery():
    received = []
    client, _ = _make_client(
        {"initialize": [_ok(1, {})]}, on_notification=received.append
    )
    secret = "sk-or-live-secret-value"
    client.start()
    client._sensitive_values = client._collect_sensitive_values(
        {"OMNIROUTE_API_KEY": secret}
    )

    client._deliver_notification(
        {
            "method": "item/completed",
            "params": {
                "item": {"type": "commandExecution", "output": f"key was {secret}"},
                "api_key": secret,
            },
        }
    )

    assert secret not in json.dumps(received)
    assert secret not in json.dumps(list(client.recent_events))
    assert "<redacted>" in json.dumps(received)


def test_a_broken_notification_callback_does_not_kill_the_pump_thread():
    def boom(_event):
        raise RuntimeError("callback bug")

    client, _ = _make_client(
        {
            "initialize": [_ok(1, {}), _notification("thread/started", {})],
            "thread/start": [_ok(2, _thread_result())],
        },
        on_notification=boom,
    )
    client.start()
    # Must still succeed even though the notification callback raised.
    result = client.thread_start()
    assert result["thread"]["id"] == "THREAD-1"


# -- malformed messages -----------------------------------------------------


def test_malformed_json_line_is_skipped_not_fatal():
    client, _ = _make_client(
        {
            "initialize": [_ok(1, {}), "{not valid json"],
            "thread/start": [_ok(2, _thread_result())],
        }
    )
    client.start()
    result = client.thread_start()
    assert result["thread"]["id"] == "THREAD-1"
    assert any(
        event.get("method") == "nexus/malformedLine" for event in client.recent_events
    )


# -- timeout ----------------------------------------------------------------


def test_request_without_a_response_times_out():
    client, _ = _make_client(
        {"initialize": [_ok(1, {})]}, request_timeout_seconds=0.3
    )
    client.start()
    with pytest.raises(AppServerError) as excinfo:
        client.thread_start()
    assert excinfo.value.code == "REQUEST_TIMEOUT"


def test_turn_completes_before_total_deadline():
    client, _captured = _make_client(
        {
            "initialize": [_ok(1, {})],
            "turn/start": [
                _ok(2, {"turn": {"id": "TURN-1"}}),
                _notification("item/completed", {"turnId": "TURN-1", "item": {"type": "agentMessage", "text": "done"}}),
                _notification("turn/completed", {"turn": {"id": "TURN-1", "status": "completed"}}),
            ],
        }
    )
    client.start()
    assert client.send_message_and_wait("THREAD-1", "hello", timeout_seconds=1) == "done"


def test_turn_without_completion_hits_total_deadline():
    client, _captured = _make_client(
        {"initialize": [_ok(1, {})], "turn/start": [_ok(2, {"turn": {"id": "TURN-1"}})]}
    )
    client.start()
    with pytest.raises(AppServerError) as excinfo:
        client.send_message_and_wait("THREAD-1", "hello", timeout_seconds=0.02)
    assert excinfo.value.code == "TURN_TIMEOUT"


def test_late_completion_after_deadline_cannot_complete_timed_out_call():
    import threading

    client, captured = _make_client(
        {"initialize": [_ok(1, {})], "turn/start": [_ok(2, {"turn": {"id": "TURN-1"}})]}
    )
    client.start()
    late_notification = _notification("turn/completed", {"turn": {"id": "TURN-1", "status": "completed"}})
    timer = threading.Timer(0.05, lambda: captured["server"].push(late_notification))
    timer.start()
    with pytest.raises(AppServerError) as excinfo:
        client.send_message_and_wait("THREAD-1", "hello", timeout_seconds=0.01)
    timer.join(timeout=1)
    assert excinfo.value.code == "TURN_TIMEOUT"
    client.stop()


def test_turn_failed_notification_is_explicit_failure():
    client, _captured = _make_client(
        {
            "initialize": [_ok(1, {})],
            "turn/start": [
                _ok(2, {"turn": {"id": "TURN-1"}}),
                _notification("turn/completed", {
                    "turn": {"id": "TURN-1", "status": "failed", "error": "provider failed"}
                }),
            ],
        }
    )
    client.start()
    with pytest.raises(AppServerError) as excinfo:
        client.send_message_and_wait("THREAD-1", "hello", timeout_seconds=1)
    assert excinfo.value.code == "TURN_FAILED"


# -- process exit -------------------------------------------------------


def test_process_exit_fails_pending_requests_immediately():
    # The server answers `initialize` and then its stdout stream ends
    # (process exited) before `thread/start` is ever sent.
    client, _ = _make_client(
        {"initialize": [_ok(1, {})]},
        close_after={"initialize"},
        request_timeout_seconds=5,
    )
    client.start()
    start_time = time.monotonic()
    with pytest.raises(AppServerError):
        client.thread_start()
    # Must fail promptly on EOF, not wait out the 5s request timeout.
    assert time.monotonic() - start_time < 2


# -- initialization failure --------------------------------------------


def test_launch_failure_raises_app_server_error():
    def failing_launcher(command, *, env, cwd):
        raise OSError("executable not found")

    client = AppServerClient(
        r"C:\fake\repo", codex_path=r"C:\fake\codex.exe", process_launcher=failing_launcher
    )
    with pytest.raises(AppServerError) as excinfo:
        client.start()
    assert excinfo.value.code == "LAUNCH_FAILED"


def test_initialize_failure_stops_the_process_and_raises():
    # Nothing ever answers `initialize`, and the stream closes immediately.
    client, captured = _make_client(
        {}, close_after={"initialize"}, request_timeout_seconds=2
    )
    with pytest.raises(AppServerError):
        client.start()
    assert captured["process"].stdin.closed


def test_omniroute_profile_unavailable_raises_before_launching(monkeypatch):
    import nexus.chat.appserver_client as mod

    def boom(profile, worker_home=None, include_auth=True):
        raise RuntimeError("OmniRoute Worker configuration is unavailable or invalid")

    monkeypatch.setattr(mod, "codex_subprocess_env", boom)
    launched = {"called": False}

    def launcher(command, *, env, cwd):
        launched["called"] = True
        return FakeProcess(FakeAppServer({}))

    client = AppServerClient(
        r"C:\fake\repo", codex_path=r"C:\fake\codex.exe", process_launcher=launcher
    )
    with pytest.raises(AppServerError) as excinfo:
        client.start()
    assert excinfo.value.code == "OMNIROUTE_PROFILE_UNAVAILABLE"
    assert launched["called"] is False


# -- stable codex_home for resume across a process restart -----------------


def test_start_without_codex_home_uses_a_fresh_one_each_time(monkeypatch):
    import nexus.chat.appserver_client as mod

    seen_worker_homes = []

    def fake_codex_subprocess_env(profile, worker_home=None, include_auth=True):
        resolved = worker_home or f"fresh-{len(seen_worker_homes)}"
        seen_worker_homes.append(resolved)
        return {"CODEX_HOME": resolved}

    monkeypatch.setattr(mod, "codex_subprocess_env", fake_codex_subprocess_env)
    monkeypatch.setattr(mod, "with_scoped_git_trust", lambda env, repo: dict(env))

    first, _ = _make_client({"initialize": [_ok(1, {})]})
    first.start()
    second, _ = _make_client({"initialize": [_ok(1, {})]})
    second.start()

    assert first.codex_home != second.codex_home


def test_start_with_explicit_codex_home_pins_it_for_resume(monkeypatch):
    import nexus.chat.appserver_client as mod

    calls = []

    def fake_codex_subprocess_env(profile, worker_home=None, include_auth=True):
        calls.append(worker_home)
        return {"CODEX_HOME": worker_home or "generated"}

    monkeypatch.setattr(mod, "codex_subprocess_env", fake_codex_subprocess_env)
    monkeypatch.setattr(mod, "with_scoped_git_trust", lambda env, repo: dict(env))

    client, _ = _make_client(
        {"initialize": [_ok(1, {})]}, codex_home=r"C:\fake\stable-codex-home"
    )
    client.start()

    assert calls == [r"C:\fake\stable-codex-home"]
    assert client.codex_home == r"C:\fake\stable-codex-home"


# -- thread creation / resume contract ------------------------------------


def test_thread_start_sends_required_omniroute_fields():
    client, captured = _make_client(
        {"initialize": [_ok(1, {})], "thread/start": [_ok(2, _thread_result())]}
    )
    client.start()
    client.thread_start(model="cc/claude-sonnet-5-low", cwd=r"C:\fake\repo")
    sent = captured["server"].sent[2]
    assert sent["method"] == "thread/start"
    assert sent["params"]["modelProvider"] == "omniroute"
    assert sent["params"]["model"] == "cc/claude-sonnet-5-low"
    assert sent["params"]["approvalPolicy"] == "on-request"
    assert sent["params"]["sandbox"] == "workspace-write"


def test_thread_resume_sends_thread_id_and_omniroute():
    client, captured = _make_client(
        {
            "initialize": [_ok(1, {})],
            "thread/resume": [_ok(2, _thread_result(thread_id="THREAD-RESUMED"))],
        }
    )
    client.start()
    result = client.thread_resume("THREAD-RESUMED")
    sent = captured["server"].sent[2]
    assert sent["method"] == "thread/resume"
    assert sent["params"]["threadId"] == "THREAD-RESUMED"
    assert sent["params"]["modelProvider"] == "omniroute"
    assert result["thread"]["id"] == "THREAD-RESUMED"


def test_thread_resume_passes_explicit_selected_model():
    client, captured = _make_client(
        {"initialize": [_ok(1, {})], "thread/resume": [_ok(2, _thread_result(thread_id="THREAD-SELECTED"))]}
    )
    client.start()
    client.thread_resume("THREAD-SELECTED", model="cx/gpt-5.6-sol-high")
    sent = captured["server"].sent[2]
    assert sent["params"]["model"] == "cx/gpt-5.6-sol-high"
    assert sent["params"]["modelProvider"] == "omniroute"


def test_codex_native_pins_openai_profile_auth_and_provider(monkeypatch):
    import nexus.chat.appserver_client as mod

    env_calls = []
    monkeypatch.setattr(
        mod,
        "codex_subprocess_env",
        lambda profile, worker_home=None, include_auth=True: env_calls.append(
            (profile, worker_home, include_auth)
        ) or {"CODEX_HOME": worker_home or "native-home"},
    )
    client, captured = _make_client(
        {
            "initialize": [_ok(1, {})],
            "account/read": [_ok(2, {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True})],
            "thread/start": [_ok(3, _thread_result(provider="openai", model="gpt-test"))],
        },
        client_class=CodexNativeAppServerClient,
    )
    client.start()
    account = client.account_read()
    result = client.thread_start(model="gpt-test")

    assert env_calls == [("native", None, True)]
    assert account["account"]["type"] == "chatgpt"
    assert result["modelProvider"] == "openai"
    assert 'model_provider="openai"' in captured["command"]
    assert captured["server"].sent[1] == {"method": "initialized", "params": {}}
    assert captured["server"].sent[3]["params"]["modelProvider"] == "openai"


def test_codex_native_rejects_cross_runtime_override_and_response():
    client, _ = _make_client(
        {"initialize": [_ok(1, {})]}, client_class=CodexNativeAppServerClient
    )
    client.start()
    with pytest.raises(AppServerError) as excinfo:
        client.thread_start(extra_params={"modelProvider": "omniroute"})
    assert excinfo.value.code == "CODEX_NATIVE_OVERRIDE_REJECTED"

    client2, _ = _make_client(
        {
            "initialize": [_ok(1, {})],
            "thread/start": [_ok(2, _thread_result(provider="omniroute"))],
        },
        client_class=CodexNativeAppServerClient,
    )
    client2.start()
    with pytest.raises(AppServerError) as response_error:
        client2.thread_start()
    assert response_error.value.code == "CODEX_NATIVE_NOT_ENFORCED"


def test_model_list_and_thread_effort_settings_use_appserver_protocol():
    client, captured = _make_client({
        "initialize": [_ok(1, {})],
        "model/list": [_ok(2, {"data": [{"id": "cx/gpt-5.6-sol", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]})],
        "thread/settings/update": [_ok(3, {"threadId": "T1"})],
    })
    client.start()
    models = client.model_list()
    client.thread_settings_update("T1", model="cx/gpt-5.6-sol", effort="high")
    model_request, effort_request = captured["server"].sent[2:]
    assert model_request["method"] == "model/list"
    assert model_request["params"] == {"limit": 100, "includeHidden": False}
    assert models["data"][0]["supportedReasoningEfforts"][0]["reasoningEffort"] == "high"
    assert effort_request["method"] == "thread/settings/update"
    assert effort_request["params"] == {"threadId": "T1", "model": "cx/gpt-5.6-sol", "effort": "high"}


def test_thread_history_wrappers_preserve_opaque_cursor_and_redact_response():
    cursor = "cursor-from-app-server+/opaque=="
    client, captured = _make_client({
        "initialize": [_ok(1, {})],
        "thread/items/list": [_ok(2, {"data": [{"item": {"token": "sensitive"}}], "nextCursor": "next"})],
        "thread/turns/list": [_ok(3, {"data": [{"id": "TURN-1"}], "nextCursor": None})],
    })
    client.start()
    client._sensitive_values = ("sensitive",)
    items = client.thread_items_list("THREAD-1", cursor=cursor, limit=17, turn_id="TURN-1")
    turns = client.thread_turns_list("THREAD-1", cursor="turn-cursor", limit=9, sort_direction="asc")

    item_request, turn_request = captured["server"].sent[2:]
    assert item_request["method"] == "thread/items/list"
    assert item_request["params"] == {
        "threadId": "THREAD-1", "cursor": cursor, "limit": 17,
        "sortDirection": "asc", "turnId": "TURN-1",
    }
    assert items["data"][0]["item"]["token"] == "<redacted>"
    assert turn_request["method"] == "thread/turns/list"
    assert turn_request["params"] == {
        "threadId": "THREAD-1", "cursor": "turn-cursor", "limit": 9,
        "sortDirection": "asc", "itemsView": "notLoaded",
    }
    assert turns["data"][0]["id"] == "TURN-1"


@pytest.mark.parametrize("kwargs", [
    {"limit": 0}, {"limit": 1001}, {"sort_direction": "sideways"},
])
def test_thread_items_list_rejects_invalid_pagination(kwargs):
    client, _ = _make_client({"initialize": [_ok(1, {})]})
    with pytest.raises(AppServerError) as excinfo:
        client.thread_items_list("THREAD-1", **kwargs)
    assert excinfo.value.code == "HISTORY_REQUEST_INVALID"


# -- OmniRoute profile selection -------------------------------------------


def test_start_uses_the_omniroute_profile_and_scoped_git_trust(monkeypatch):
    import nexus.chat.appserver_client as mod

    calls = {}

    def fake_codex_subprocess_env(profile, worker_home=None, include_auth=True):
        calls["profile"] = profile
        calls["include_auth"] = include_auth
        return {"CODEX_HOME": worker_home or "fake-home"}

    def fake_with_scoped_git_trust(env, repo):
        calls["env"] = env
        calls["repo"] = repo
        return {**env, "GIT_SCOPED": "1"}

    monkeypatch.setattr(mod, "codex_subprocess_env", fake_codex_subprocess_env)
    monkeypatch.setattr(mod, "with_scoped_git_trust", fake_with_scoped_git_trust)

    client, captured = _make_client({"initialize": [_ok(1, {})]})
    client.start()

    assert calls["profile"] == "omniroute"
    assert calls["include_auth"] is False
    assert calls["repo"] == r"C:\fake\repo"
    assert captured["env"]["GIT_SCOPED"] == "1"


def test_caller_cannot_override_model_provider_away_from_omniroute():
    client, _ = _make_client({"initialize": [_ok(1, {})]})
    client.start()
    with pytest.raises(AppServerError) as excinfo:
        client.thread_start(extra_params={"modelProvider": "openai"})
    assert excinfo.value.code == "OMNIROUTE_OVERRIDE_REJECTED"


# -- authentication fallback prevention ------------------------------------


def test_thread_start_rejects_a_response_not_confirming_omniroute():
    client, _ = _make_client(
        {
            "initialize": [_ok(1, {})],
            "thread/start": [_ok(2, _thread_result(provider="openai"))],
        }
    )
    client.start()
    with pytest.raises(AppServerError) as excinfo:
        client.thread_start()
    assert excinfo.value.code == "OMNIROUTE_NOT_ENFORCED"


def test_thread_resume_rejects_a_response_not_confirming_omniroute():
    client, _ = _make_client(
        {
            "initialize": [_ok(1, {})],
            "thread/resume": [_ok(2, _thread_result(provider=None))],
        }
    )
    client.start()
    with pytest.raises(AppServerError) as excinfo:
        client.thread_resume("THREAD-1")
    assert excinfo.value.code == "OMNIROUTE_NOT_ENFORCED"


# -- fail-closed approval default -----------------------------------------


def test_unhandled_approval_request_is_denied_by_default():
    client, captured = _make_client(
        {
            "initialize": [
                _ok(1, {}),
                json.dumps(
                    {
                        "id": 99,
                        "method": "item/commandExecution/requestApproval",
                        "params": {"command": "rm -rf /"},
                    }
                ),
            ],
            "thread/start": [_ok(2, _thread_result())],
        }
    )
    client.start()
    client.thread_start()
    responses = captured["server"].sent
    approval_response = next(line for line in responses if line.get("id") == 99)
    assert approval_response["result"]["decision"]["denied"]["rejection"]


def test_permissions_request_is_denied_with_empty_permissions():
    client, captured = _make_client(
        {
            "initialize": [
                _ok(1, {}),
                json.dumps(
                    {
                        "id": 42,
                        "method": "item/permissions/requestApproval",
                        "params": {"requested": ["network"]},
                    }
                ),
            ],
            "thread/start": [_ok(2, _thread_result())],
        }
    )
    client.start()
    client.thread_start()
    responses = captured["server"].sent
    response = next(line for line in responses if line.get("id") == 42)
    assert response["result"]["permissions"] == []


def test_unknown_server_request_gets_a_jsonrpc_error_not_a_guess():
    client, captured = _make_client(
        {
            "initialize": [
                _ok(1, {}),
                json.dumps({"id": 7, "method": "some/futureMethod", "params": {}}),
            ],
            "thread/start": [_ok(2, _thread_result())],
        }
    )
    client.start()
    client.thread_start()
    responses = captured["server"].sent
    response = next(line for line in responses if line.get("id") == 7)
    assert "error" in response
    assert "result" not in response


# -- safe process shutdown -------------------------------------------------


def test_stop_closes_stdin_and_resolves_pending_requests():
    client, captured = _make_client({"initialize": [_ok(1, {})]})
    client.start()
    # Simulate an in-flight request whose response never arrives, then stop.
    request_queue: "queue.Queue[dict]" = queue.Queue(maxsize=1)
    with client._pending_lock:
        client._pending[999] = request_queue
    client.stop()
    assert captured["process"].stdin.closed is True
    message = request_queue.get_nowait()
    assert "error" in message


def test_stop_kills_the_process_tree_if_it_does_not_exit_gracefully(monkeypatch):
    import nexus.chat.appserver_client as mod

    killed = {"pid": None}
    monkeypatch.setattr(
        mod, "kill_process_tree", lambda process: killed.__setitem__("pid", id(process))
    )

    client, captured = _make_client(
        {"initialize": [_ok(1, {})]}, terminates_on_stdin_close=False
    )
    client.start()
    client.stop()
    assert killed["pid"] == id(captured["process"])


def test_stop_is_idempotent_and_safe_before_start():
    client = AppServerClient(
        r"C:\fake\repo", process_launcher=lambda *a, **k: FakeProcess(FakeAppServer({}))
    )
    client.stop()  # must not raise even though start() was never called
    client.stop()


def test_stop_never_kills_a_different_process():
    # kill_process_tree is passed the *one* process this client owns --
    # never looked up by name/pattern, so another Codex/app-server
    # process elsewhere on the machine can never be affected.
    import nexus.chat.appserver_client as mod

    seen = []
    monkeypatch_target = mod.kill_process_tree
    try:
        mod.kill_process_tree = lambda process: seen.append(process)
        client, captured = _make_client(
            {"initialize": [_ok(1, {})]}, terminates_on_stdin_close=False
        )
        client.start()
        client.stop()
        assert seen == [captured["process"]]
    finally:
        mod.kill_process_tree = monkeypatch_target


# -- live, real-protocol smoke (gated, never retried, skips cleanly) ------


@pytest.mark.skipif(
    os.environ.get("NEXUS_CHAT_LIVE_SMOKE") != "1",
    reason="Set NEXUS_CHAT_LIVE_SMOKE=1 to run the real codex app-server "
    "smoke test; requires the real Codex CLI and a reachable OmniRoute "
    "server with a callable route.",
)
def test_live_two_message_continuity_and_resume():
    import shutil

    from nexus.dispatchers.manager import _find_codex

    try:
        _find_codex()
    except (RuntimeError, FileNotFoundError):
        pytest.skip("Codex CLI is not installed on this machine.")

    # Deliberately NOT tempfile.TemporaryDirectory(): the OmniRoute profile's
    # sandbox policy excludes %TEMP% from writable roots
    # (exclude_tmpdir_env_var/exclude_slash_tmp in
    # nexus.process_env._safe_omniroute_config), which this exact test
    # exposed turns into a confusing "Model provider `omniroute` not found"
    # error from Codex rather than a clear sandbox message. A disposable
    # sibling directory next to this repo is outside that exclusion, same
    # as the disposable project used in the T0 engine validation.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tmp = os.path.join(os.path.dirname(repo_root), "nexus-chat-live-smoke-tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)

    try:
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        subprocess.run(
            ["git", "config", "user.email", "nexus-chat-test@example.com"],
            cwd=tmp,
            check=True,
        )
        subprocess.run(["git", "config", "user.name", "nexus-chat-test"], cwd=tmp, check=True)
        with open(os.path.join(tmp, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("placeholder\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp, check=True)

        client = AppServerClient(tmp)
        try:
            client.start()
        except AppServerError as error:
            pytest.skip(f"OmniRoute profile not usable here: {error}")

        try:
            thread = client.thread_start(model="cc/claude-sonnet-5-low", ephemeral=False)
            thread_id = thread["thread"]["id"]

            first_reply = client.send_message_and_wait(
                thread_id, "Reply with exactly the single word: PONG"
            )
            assert "PONG" in first_reply

            second_reply = client.send_message_and_wait(
                thread_id,
                "What single word did you just reply with? Answer with ONLY that word.",
            )
            assert "PONG" in second_reply
            codex_home = client.codex_home
        finally:
            client.stop()

        # Real process restart, then thread/resume -- the actual claim
        # under test, not just same-process continuity. A fresh
        # AppServerClient defaults to its own brand-new CODEX_HOME (right
        # for one-shot Manager/Worker-style calls), so resuming the same
        # conversation requires pinning the previous client's codex_home.
        resumed_client = AppServerClient(tmp, codex_home=codex_home)
        try:
            resumed_client.start()
            resumed_client.thread_resume(thread_id)
            resume_reply = resumed_client.send_message_and_wait(
                thread_id,
                "What single word did we agree on earlier in this "
                "conversation? Answer with ONLY that word.",
            )
            assert "PONG" in resume_reply
        finally:
            resumed_client.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_turn_interrupt_sends_both_thread_and_turn_id():
    client, captured = _make_client(
        {
            "initialize": [_ok(1, {})],
            "turn/interrupt": [_ok(2, {})],
        }
    )
    client.start()
    client.turn_interrupt("THREAD-1", "CODEX-TURN-1")
    sent = captured["server"].sent[2]
    assert sent["method"] == "turn/interrupt"
    assert sent["params"]["threadId"] == "THREAD-1"
    assert sent["params"]["turnId"] == "CODEX-TURN-1"


def test_sandbox_mode_override_is_added_to_the_command_line():
    captured = {}

    def launcher(command, *, env, cwd):
        captured["command"] = command
        return FakeProcess(FakeAppServer({"initialize": [_ok(1, {})]}))

    client = AppServerClient(
        r"C:\fake\repo",
        codex_path=r"C:\fake\codex.exe",
        process_launcher=launcher,
        sandbox_mode_override="read-only",
    )
    client.start()
    assert captured["command"] == [
        r"C:\fake\codex.exe", "app-server", "--listen", "stdio://",
        "-c", 'sandbox_mode="read-only"',
    ]


def test_no_sandbox_mode_override_by_default():
    captured = {}

    def launcher(command, *, env, cwd):
        captured["command"] = command
        return FakeProcess(FakeAppServer({"initialize": [_ok(1, {})]}))

    client = AppServerClient(
        r"C:\fake\repo", codex_path=r"C:\fake\codex.exe", process_launcher=launcher
    )
    client.start()
    assert captured["command"] == [r"C:\fake\codex.exe", "app-server", "--listen", "stdio://"]


def test_approval_policy_override_is_added_to_the_command_line():
    captured = {}

    def launcher(command, *, env, cwd):
        captured["command"] = command
        return FakeProcess(FakeAppServer({"initialize": [_ok(1, {})]}))

    client = AppServerClient(
        r"C:\fake\repo",
        codex_path=r"C:\fake\codex.exe",
        process_launcher=launcher,
        approval_policy_override="on-request",
    )
    client.start()
    assert captured["command"] == [
        r"C:\fake\codex.exe", "app-server", "--listen", "stdio://",
        "-c", 'approval_policy="on-request"',
    ]


def test_no_approval_policy_override_by_default():
    captured = {}

    def launcher(command, *, env, cwd):
        captured["command"] = command
        return FakeProcess(FakeAppServer({"initialize": [_ok(1, {})]}))

    client = AppServerClient(
        r"C:\fake\repo", codex_path=r"C:\fake\codex.exe", process_launcher=launcher
    )
    client.start()
    assert captured["command"] == [r"C:\fake\codex.exe", "app-server", "--listen", "stdio://"]
    assert "approval_policy" not in " ".join(captured["command"])


def test_sandbox_and_approval_policy_overrides_combine_in_order():
    captured = {}

    def launcher(command, *, env, cwd):
        captured["command"] = command
        return FakeProcess(FakeAppServer({"initialize": [_ok(1, {})]}))

    client = AppServerClient(
        r"C:\fake\repo",
        codex_path=r"C:\fake\codex.exe",
        process_launcher=launcher,
        sandbox_mode_override="workspace-write",
        approval_policy_override="on-request",
    )
    client.start()
    assert captured["command"] == [
        r"C:\fake\codex.exe", "app-server", "--listen", "stdio://",
        "-c", 'sandbox_mode="workspace-write"',
        "-c", 'approval_policy="on-request"',
    ]
