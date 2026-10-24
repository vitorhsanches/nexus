"""Service-layer tests for nexus.chat.service.

Uses a fake AppServerClient (never a real subprocess) so these are
deterministic and fast, and an isolated SQLite registry (same pattern as
tests/test_chat_sessions_registry.py) so they never touch production
nexus.db. Covers what tests/test_chat_appserver_client.py already proves
at the transport level (OmniRoute enforcement, fail-closed approvals) is
NOT re-tested here -- this file is about the orchestration this task
adds on top: session/turn lifecycle, idempotency, concurrency bounds,
interrupt, and SSE fan-out.
"""

import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.registry import chat_sessions
import pytest

import nexus.chat.service as service
from nexus.chat.appserver_client import AppServerError


# -- fixtures ---------------------------------------------------------


@pytest.fixture
def isolated_registry(tmp_path):
    original_db_path = database.DATABASE_PATH
    original_storage_dir = database.STORAGE_DIR
    original_projects_file = projects.PROJECTS_FILE
    database.STORAGE_DIR = tmp_path / "storage"
    database.DATABASE_PATH = database.STORAGE_DIR / "nexus.db"
    projects.PROJECTS_FILE = tmp_path / "projects.json"
    database.initialize_database()
    try:
        yield tmp_path
    finally:
        # Join every nexus-chat-* background thread before swapping the DB
        # path back: a test that forgot to release a blocked turn (or hit
        # an assertion before doing so) would otherwise leave that thread
        # running against a path the NEXT test's fixture is about to
        # repoint, producing a confusing, misattributed "no such table" /
        # "NoneType is not subscriptable" failure in an unrelated test.
        for thread in threading.enumerate():
            if thread.name.startswith("nexus-chat-") and thread is not threading.current_thread():
                thread.join(timeout=10)
        database.DATABASE_PATH = original_db_path
        database.STORAGE_DIR = original_storage_dir
        projects.PROJECTS_FILE = original_projects_file
        service._active_turns.clear()


def _register_project(tmp_path, project_id="demo", enabled=True, make_dir=True):
    workspace_path = str(tmp_path / project_id)
    if make_dir:
        Path(workspace_path).mkdir(parents=True, exist_ok=True)
    projects.PROJECTS_FILE.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "id": project_id,
                        "name": project_id.title(),
                        "path": workspace_path,
                        "aliases": [],
                        "enabled": enabled,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    projects.sync_projects()
    return workspace_path


def _move_project(tmp_path):
    moved = tmp_path / "moved-workspace"
    moved.mkdir(exist_ok=True)
    payload = json.loads(projects.PROJECTS_FILE.read_text(encoding="utf-8"))
    payload["projects"][0]["path"] = str(moved)
    projects.PROJECTS_FILE.write_text(json.dumps(payload), encoding="utf-8")
    projects.sync_projects()


class FakeAppServerClient:
    """Stands in for nexus.chat.appserver_client.AppServerClient. Records
    everything a test needs to assert on, and supports a controllable
    blocking turn for concurrency/interrupt tests."""

    #: Set per-test to control behavior; reset in the fixture below.
    start_error: Exception | None = None
    block_until_interrupted = False
    response_text = "fake reply"
    turn_error: Exception | None = None
    turn_timeouts = []
    history_item_pages = {}
    history_turn_pages = {}
    history_calls = []
    inference_calls = 0
    redaction_calls = 0
    model_override = None
    start_models = []
    resume_models = []
    reasoning_effort = None
    settings_calls = []
    instances = []

    def __init__(self, repo, *, codex_home=None, on_notification=None, **kwargs):
        self.repo = repo
        self.codex_home = codex_home or "fake-codex-home-" + repo[-4:]
        self._on_notification = on_notification
        FakeAppServerClient.instances.append(self)
        self.started = False
        self.stopped = False
        self.thread_start_kwargs = None
        self.resumed_thread_id = None
        self.interrupt_calls = []
        self._interrupt_event = threading.Event()

    def start(self):
        if FakeAppServerClient.start_error is not None:
            raise FakeAppServerClient.start_error
        self.started = True

    def thread_start(self, **kwargs):
        self.thread_start_kwargs = kwargs
        FakeAppServerClient.start_models.append(kwargs.get("model"))
        self._emit({"method": "thread/started", "params": {}})
        return {
            "thread": {"id": "FAKE-THREAD-1", "modelProvider": "omniroute"},
            "model": FakeAppServerClient.model_override or kwargs.get("model") or "cc/fake-model",
            "modelProvider": "omniroute",
        }

    def thread_resume(self, thread_id, **kwargs):
        self.resumed_thread_id = thread_id
        FakeAppServerClient.resume_models.append(kwargs.get("model"))
        return {
            "thread": {"id": thread_id, "modelProvider": "omniroute"},
            "model": FakeAppServerClient.model_override or kwargs.get("model") or "cc/fake-model",
            "modelProvider": "omniroute",
            "reasoningEffort": FakeAppServerClient.reasoning_effort,
        }

    def thread_settings_update(self, thread_id, *, model=None, effort=None):
        FakeAppServerClient.settings_calls.append((thread_id, model, effort))
        FakeAppServerClient.reasoning_effort = effort
        return {}

    def thread_items_list(self, thread_id, *, cursor=None, limit=100, sort_direction="asc", turn_id=None):
        FakeAppServerClient.history_calls.append(("items", thread_id, cursor, limit, sort_direction, turn_id))
        return FakeAppServerClient.history_item_pages.get(cursor, {"data": [], "nextCursor": None})

    def thread_turns_list(self, thread_id, *, cursor=None, limit=100, sort_direction="desc", items_view="notLoaded"):
        FakeAppServerClient.history_calls.append(("turns", thread_id, cursor, limit, sort_direction, items_view))
        return FakeAppServerClient.history_turn_pages.get(cursor, {"data": [], "nextCursor": None})

    def _redact_event(self, value):
        FakeAppServerClient.redaction_calls += 1
        if isinstance(value, dict):
            return {key: self._redact_event(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_event(item) for item in value]
        if isinstance(value, str):
            return value.replace("KNOWN-SECRET", "<redacted>")
        return value

    def send_message_and_wait(self, thread_id, text, *, timeout_seconds=None, on_turn_started=None):
        FakeAppServerClient.inference_calls += 1
        FakeAppServerClient.turn_timeouts.append(timeout_seconds)
        if on_turn_started:
            on_turn_started("FAKE-CODEX-TURN-1")
        self._emit(
            {
                "method": "item/completed",
                "params": {"item": {"type": "agentMessage", "text": FakeAppServerClient.response_text}},
            }
        )
        if FakeAppServerClient.block_until_interrupted:
            self._interrupt_event.wait(timeout=10)
            raise AppServerError("TURN_FAILED", "Turn ended with status 'interrupted'.")
        if FakeAppServerClient.turn_error is not None:
            raise FakeAppServerClient.turn_error
        return FakeAppServerClient.response_text

    def turn_interrupt(self, thread_id, turn_id):
        self.interrupt_calls.append((thread_id, turn_id))
        self._interrupt_event.set()

    def stop(self):
        self.stopped = True

    def _emit(self, event):
        if self._on_notification:
            self._on_notification(event)


@pytest.fixture(autouse=True)
def _patch_client(monkeypatch):
    FakeAppServerClient.start_error = None
    FakeAppServerClient.block_until_interrupted = False
    FakeAppServerClient.response_text = "fake reply"
    FakeAppServerClient.turn_error = None
    FakeAppServerClient.turn_timeouts = []
    FakeAppServerClient.history_item_pages = {}
    FakeAppServerClient.history_turn_pages = {}
    FakeAppServerClient.history_calls = []
    FakeAppServerClient.inference_calls = 0
    FakeAppServerClient.redaction_calls = 0
    FakeAppServerClient.model_override = None
    FakeAppServerClient.start_models = []
    FakeAppServerClient.resume_models = []
    FakeAppServerClient.reasoning_effort = None
    FakeAppServerClient.settings_calls = []
    FakeAppServerClient.instances = []
    monkeypatch.setattr(service, "AppServerClient", FakeAppServerClient)
    test_catalog = [
        {"id": "cx/gpt-5.6-sol", "name": "GPT Sol", "selectable": True, "availability": "catalog-compatible-unverified", "reason": "test catalog", "cost_status": "free", "supported_efforts": ["low", "high"], "default_effort": "high"},
        {"id": "cx/gpt-5.6-sol-high", "name": "GPT Sol High", "selectable": True, "availability": "catalog-compatible-unverified", "reason": "test catalog", "cost_status": "free", "supported_efforts": ["high"], "default_effort": "high"},
        {"id": "oc/mimo-v2.5-free", "name": "MiMo", "selectable": True, "availability": "catalog-compatible-unverified", "reason": "test catalog", "cost_status": "free", "supported_efforts": [], "default_effort": None},
        {"id": "cc/claude-sonnet-5-low", "name": "Claude", "selectable": True, "availability": "catalog-compatible-unverified", "reason": "test catalog", "cost_status": "free", "supported_efforts": ["low"], "default_effort": "low"},
    ]
    monkeypatch.setattr(service, "_model_catalog_cache", (time.monotonic() + 60, test_catalog))
    monkeypatch.setattr(service, "_native_model_catalog_cache", None)
    # Defensive fallback only: the pre-seeded cache above is what every test in
    # this file relies on, but a background turn-processing thread left running
    # past a prior test's teardown can race in and overwrite/expire it. Without
    # this, that race falls through to a real network call to OmniRoute
    # (nexus.routing.telemetry.OmniRouteTelemetryClient.fetch_models), which is
    # unreachable in CI and makes an unrelated test fail nondeterministically.
    monkeypatch.setattr(
        "nexus.routing.telemetry.OmniRouteTelemetryClient.fetch_models",
        lambda self: (),
    )
    yield


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _wait_for_blocked_client(timeout=5.0):
    """Wait until exactly one active turn has a live client ready to be
    interrupted (added to _active_turns synchronously by send_turn(), but
    its .client is only set once the background thread actually
    constructs it -- waiting on len() alone races with that)."""
    def _ready():
        if len(service._active_turns) != 1:
            return False
        active = next(iter(service._active_turns.values()))
        return (
            active.client is not None
            and active.thread_id is not None
            and active.codex_turn_id is not None
        )

    assert _wait_until(_ready, timeout), "turn never reached a blockable, interruptible state"
    return next(iter(service._active_turns.values()))


# -- session creation / project validation --------------------------------


def test_create_session_success(isolated_registry):
    _register_project(isolated_registry)
    session = service.create_session("demo")
    assert session["project_id"] == "demo"
    assert session["status"] == "CREATED"
    assert session["workspace_path"] == str(isolated_registry / "demo")
    assert session["requested_model"] == service.DEFAULT_CHAT_MODEL


def test_create_session_explicit_model_and_rejects_unknown(isolated_registry):
    _register_project(isolated_registry)
    session = service.create_session("demo", "oc/mimo-v2.5-free")
    assert session["requested_model"] == "oc/mimo-v2.5-free"
    with pytest.raises(service.ChatError) as excinfo:
        service.create_session("demo", "unapproved/model")
    assert excinfo.value.code == "MODEL_NOT_ALLOWED"


def test_codex_native_session_uses_local_runtime_without_omniroute_gate(isolated_registry, monkeypatch):
    _register_project(isolated_registry)

    class FakeNativeClient(FakeAppServerClient):
        def account_read(self, *, refresh_token=False):
            return {"account": {"type": "chatgpt", "email": "must-not-leak@example.test"}, "requiresOpenaiAuth": True}

        def model_list(self, **_kwargs):
            return {"data": [{
                "id": "gpt-native-test", "displayName": "Native Test", "isDefault": True,
                "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
                "defaultReasoningEffort": "low",
            }], "nextCursor": None}

        def thread_start(self, **kwargs):
            self.thread_start_kwargs = kwargs
            FakeAppServerClient.start_models.append(kwargs.get("model"))
            return {"thread": {"id": "NATIVE-THREAD", "modelProvider": "openai"},
                    "model": kwargs.get("model"), "modelProvider": "openai"}

        def thread_resume(self, thread_id, **kwargs):
            self.resumed_thread_id = thread_id
            FakeAppServerClient.resume_models.append(kwargs.get("model"))
            return {"thread": {"id": thread_id, "modelProvider": "openai"},
                    "model": kwargs.get("model"), "modelProvider": "openai",
                    "reasoningEffort": FakeAppServerClient.reasoning_effort}

    monkeypatch.setattr(service, "CodexNativeAppServerClient", FakeNativeClient)
    monkeypatch.setattr(service, "_require_chat_provider", lambda _model: pytest.fail("OmniRoute gate must not run"))

    catalog = service.list_models(runtime="codex-native")
    assert catalog["authenticated"] is True
    assert catalog["auth_type"] == "chatgpt"
    assert "must-not-leak" not in repr(catalog)

    session = service.create_session(
        "demo", "gpt-native-test", "low", allow_unknown_cost=True, runtime="codex-native"
    )
    assert session["runtime"] == "codex-native"
    turn = service.send_turn(session["session_id"], "hello native")
    assert _wait_until(lambda: chat_sessions.get_turn(turn["turn_id"])["status"] == "COMPLETED")
    persisted = chat_sessions.get_session(session["session_id"])
    assert persisted["codex_thread_id"] == "NATIVE-THREAD"
    assert persisted["provider"] == "openai"


def test_codex_native_missing_login_is_clear_and_does_not_create_session(isolated_registry, monkeypatch):
    _register_project(isolated_registry)

    class SignedOutClient(FakeAppServerClient):
        def account_read(self, *, refresh_token=False):
            return {"account": None, "requiresOpenaiAuth": True}

    monkeypatch.setattr(service, "CodexNativeAppServerClient", SignedOutClient)
    with pytest.raises(service.ChatError) as excinfo:
        service.create_session("demo", "gpt-native-test", runtime="codex-native")
    assert excinfo.value.code == "CODEX_AUTH_UNAVAILABLE"
    assert chat_sessions.list_sessions() == []


def test_model_catalog_is_restricted_and_marks_operational_status_unverified():
    result = service.list_models()
    assert {entry["id"] for entry in result["models"]} == {"cx/gpt-5.6-sol", "cx/gpt-5.6-sol-high", "oc/mimo-v2.5-free", "cc/claude-sonnet-5-low"}
    assert result["default_model"] == service.DEFAULT_CHAT_MODEL
    assert all(entry["availability"] == "catalog-compatible-unverified" for entry in result["models"])


def test_list_models_forwards_explicit_refresh_to_catalog(monkeypatch):
    refresh_values = []
    monkeypatch.setattr(
        service,
        "_model_catalog",
        lambda *, refresh=False: refresh_values.append(refresh) or [],
    )
    assert service.list_models(refresh=True)["models"] == []
    assert refresh_values == [True]


def test_model_catalog_keeps_omniroute_ids_when_appserver_ids_do_not_match(monkeypatch):
    from nexus.routing.telemetry import DiscoveredModel

    class CatalogClient:
        def __init__(self, *_args, **_kwargs): pass
        def start(self): pass
        def model_list(self, **_kwargs):
            return {"data": [
                {"id": "gpt-5.6-sol", "displayName": "Unprefixed GPT Sol", "supportedReasoningEfforts": [{"reasoningEffort": "high"}], "defaultReasoningEffort": "high"},
                {"id": "cx/other-exact", "displayName": "Exact metadata route", "supportedReasoningEfforts": [{"reasoningEffort": "low"}], "defaultReasoningEffort": "low"},
            ]}
        def stop(self): pass

    class CatalogTelemetry:
        def fetch_models(self):
            return (
                DiscoveredModel("cx/gpt-5.6-sol", "openai"),
                DiscoveredModel("cx/gpt-5.6-sol-high", "openai"),
                DiscoveredModel("cx/other-exact", "other"),
            )

    monkeypatch.setattr(service, "AppServerClient", CatalogClient)
    monkeypatch.setattr("nexus.routing.telemetry.OmniRouteTelemetryClient", CatalogTelemetry)
    models = service._model_catalog(refresh=True)
    assert {model["id"] for model in models} == {"cx/gpt-5.6-sol", "cx/gpt-5.6-sol-high", "cx/other-exact"}
    by_id = {model["id"]: model for model in models}
    assert by_id["cx/gpt-5.6-sol"]["supported_efforts"] == []
    assert by_id["cx/gpt-5.6-sol-high"]["supported_efforts"] == [], "route suffix does not prove effort support"
    assert by_id["cx/other-exact"]["supported_efforts"] == ["low"]
    assert all(model["availability"] == "omniroute-listed-unverified" for model in models)
    assert all(model["selectable"] for model in models)
    assert all(model["cost_status"] == "unknown" for model in models)


def test_model_catalog_omniroute_failure_is_error_not_empty_catalog(monkeypatch):
    class BrokenTelemetry:
        def fetch_models(self):
            raise RuntimeError("OmniRoute unavailable")

    monkeypatch.setattr("nexus.routing.telemetry.OmniRouteTelemetryClient", BrokenTelemetry)
    monkeypatch.setattr(service, "_model_catalog_cache", None)
    result = service.list_models()
    assert result["models"] == []
    assert result["catalog_error"]


def test_appserver_catalog_failure_does_not_block_omniroute_routes(monkeypatch):
    from nexus.routing.telemetry import DiscoveredModel

    class CatalogTelemetry:
        def fetch_models(self): return (DiscoveredModel("cx/gpt-5.6-sol", "openai"),)

    class BrokenAppServer:
        def __init__(self, *_args, **_kwargs): raise RuntimeError("App-Server unavailable")

    monkeypatch.setattr(service, "AppServerClient", BrokenAppServer)
    monkeypatch.setattr("nexus.routing.telemetry.OmniRouteTelemetryClient", CatalogTelemetry)
    models = service._model_catalog(refresh=True)
    assert [model["id"] for model in models] == ["cx/gpt-5.6-sol"]
    assert models[0]["supported_efforts"] == []


def test_unknown_cost_requires_explicit_acknowledgement(isolated_registry, monkeypatch):
    _register_project(isolated_registry)
    models = [dict(item) for item in service._model_catalog()]
    models[0]["cost_status"] = "unknown"
    monkeypatch.setattr(service, "_model_catalog_cache", (time.monotonic() + 60, models))
    with pytest.raises(service.ChatError) as excinfo:
        service.create_session("demo", "cx/gpt-5.6-sol")
    assert excinfo.value.code == "MODEL_COST_CONFIRMATION_REQUIRED"


def test_explicit_effort_is_applied_and_confirmed_before_inference(isolated_registry):
    _register_project(isolated_registry)
    session_id = service.create_session("demo", "cx/gpt-5.6-sol", "high", allow_unknown_cost=True)["session_id"]
    service.send_turn(session_id, "hello")
    assert _wait_until(lambda: chat_sessions.get_session(session_id)["status"] == "ACTIVE")
    assert FakeAppServerClient.settings_calls == [("FAKE-THREAD-1", "cx/gpt-5.6-sol", "high")]
    assert FakeAppServerClient.inference_calls == 1


def test_effort_activation_unconfirmed_fails_without_inference(isolated_registry, monkeypatch):
    _register_project(isolated_registry)
    session_id = service.create_session("demo", "cx/gpt-5.6-sol", "high", allow_unknown_cost=True)["session_id"]
    monkeypatch.setattr(FakeAppServerClient, "thread_settings_update", lambda self, thread_id, *, model=None, effort=None: {})
    service.send_turn(session_id, "hello")
    assert _wait_until(lambda: bool(chat_sessions.list_turns(session_id, limit=1)))
    turn = chat_sessions.list_turns(session_id, limit=1)[0]
    assert _wait_until(lambda: chat_sessions.get_turn(turn["id"])["status"] == "FAILED")
    assert chat_sessions.get_turn(turn["id"])["error_code"] == "EFFORT_ACTIVATION_FAILED"
    assert FakeAppServerClient.inference_calls == 0


def test_create_session_unknown_project(isolated_registry):
    with pytest.raises(service.ChatError) as excinfo:
        service.create_session("nope")
    assert excinfo.value.code == "PROJECT_NOT_FOUND"
    assert excinfo.value.http_status == 404


def test_create_session_disabled_project(isolated_registry):
    _register_project(isolated_registry, enabled=False)
    with pytest.raises(service.ChatError) as excinfo:
        service.create_session("demo")
    assert excinfo.value.code == "PROJECT_DISABLED"


def test_create_session_workspace_missing_on_disk(isolated_registry):
    # Registered, but its directory does not actually exist -- "approved
    # project path only" is checked against the real filesystem, not just
    # the registry row.
    _register_project(isolated_registry, make_dir=False)
    with pytest.raises(service.ChatError) as excinfo:
        service.create_session("demo")
    assert excinfo.value.code == "WORKSPACE_UNAVAILABLE"


def test_unauthorized_workspace_cannot_be_supplied_by_a_caller(isolated_registry):
    # The public API only ever takes a project_id -- there is no
    # parameter anywhere in create_session()/send_turn() that accepts an
    # arbitrary path, so an "unauthorized workspace" is structurally
    # impossible, not just rejected at runtime.
    import inspect

    assert "path" not in inspect.signature(service.create_session).parameters
    assert "workspace" not in inspect.signature(service.send_turn).parameters


# -- session persistence / detail -----------------------------------------


def test_get_session_detail_includes_turns(isolated_registry):
    _register_project(isolated_registry)
    session = service.create_session("demo")
    service.send_turn(session["session_id"], "hello", idempotency_key="k1")
    assert _wait_until(lambda: service.get_session_detail(session["session_id"])["turns"][0]["status"] == "COMPLETED")

    detail = service.get_session_detail(session["session_id"])
    assert detail["codex_thread_id"] == "FAKE-THREAD-1"
    assert detail["model"] == "cx/gpt-5.6-sol"
    assert detail["provider"] == "omniroute"
    assert len(detail["turns"]) == 1
    assert detail["turns"][0]["response_text"] == "fake reply"


def test_get_session_detail_unknown_session(isolated_registry):
    with pytest.raises(service.ChatError) as excinfo:
        service.get_session_detail("CHAT-NOPE")
    assert excinfo.value.code == "SESSION_NOT_FOUND"


def test_moved_project_rejects_existing_session_before_reservation(isolated_registry):
    _register_project(isolated_registry)
    session_id = service.create_session("demo")["session_id"]
    _move_project(isolated_registry)

    with pytest.raises(service.ChatError) as excinfo:
        service.send_turn(session_id, "hello", idempotency_key="k1")
    assert excinfo.value.code == "WORKSPACE_BINDING_CHANGED"
    assert excinfo.value.http_status == 409
    assert chat_sessions.list_turns(session_id) == []


def test_project_rebind_after_reservation_stops_before_appserver_launch(isolated_registry, monkeypatch):
    _register_project(isolated_registry)
    session_id = service.create_session("demo")["session_id"]
    reserve = chat_sessions.create_turn_if_absent

    def reserve_then_move(*args):
        result = reserve(*args)
        _move_project(isolated_registry)
        return result

    class MustNotLaunch(FakeAppServerClient):
        def start(self):
            raise AssertionError("App-Server launched for a rebound workspace")

    monkeypatch.setattr(chat_sessions, "create_turn_if_absent", reserve_then_move)
    monkeypatch.setattr(service, "AppServerClient", MustNotLaunch)
    turn = service.send_turn(session_id, "hello", idempotency_key="k1")
    assert _wait_until(lambda: chat_sessions.get_turn(turn["turn_id"])["status"] == "FAILED")
    assert chat_sessions.get_turn(turn["turn_id"])["error_code"] == "WORKSPACE_BINDING_CHANGED"


def test_project_rebind_after_launch_stops_before_resume(isolated_registry, monkeypatch):
    _register_project(isolated_registry)
    session_id = service.create_session("demo")["session_id"]
    chat_sessions.update_session_thread(
        session_id, codex_thread_id="OLD-THREAD", codex_home="fake-home", model="cc/fake", provider="omniroute"
    )

    class RebindingClient(FakeAppServerClient):
        def start(self):
            super().start()
            _move_project(isolated_registry)

        def thread_resume(self, thread_id, **kwargs):
            raise AssertionError("Resumed in a rebound workspace")

    monkeypatch.setattr(service, "AppServerClient", RebindingClient)
    turn = service.send_turn(session_id, "hello", idempotency_key="k1")
    assert _wait_until(lambda: chat_sessions.get_turn(turn["turn_id"])["status"] == "FAILED")
    assert chat_sessions.get_turn(turn["turn_id"])["error_code"] == "WORKSPACE_BINDING_CHANGED"


# -- thread resume ----------------------------------------------------------


def test_second_turn_resumes_the_persisted_thread(isolated_registry):
    _register_project(isolated_registry)
    session = service.create_session("demo")
    session_id = session["session_id"]

    service.send_turn(session_id, "first", idempotency_key="k1")
    assert _wait_until(lambda: service.get_session_detail(session_id)["turns"][0]["status"] == "COMPLETED")

    service.send_turn(session_id, "second", idempotency_key="k2")
    assert _wait_until(
        lambda: len([t for t in service.get_session_detail(session_id)["turns"] if t["status"] == "COMPLETED"]) == 2
    )

    # Every FakeAppServerClient instance is a NEW object per turn (the
    # real client is one-process-per-turn, see service.py's module
    # docstring) -- resumption is proven by the session's persisted
    # codex_thread_id being reused, not by client identity.
    row = service._get_session_row(session_id)
    assert FakeAppServerClient.start_models == [service.DEFAULT_CHAT_MODEL]
    assert FakeAppServerClient.resume_models == [service.DEFAULT_CHAT_MODEL]
    assert row["codex_thread_id"] == "FAKE-THREAD-1"


def test_model_activation_mismatch_fails_without_inference(isolated_registry):
    _register_project(isolated_registry)
    session_id = service.create_session("demo", "cx/gpt-5.6-sol") ["session_id"]
    FakeAppServerClient.model_override = "cc/claude-sonnet-5-low"
    turn = service.send_turn(session_id, "hello", idempotency_key="activation-mismatch")
    assert _wait_until(lambda: chat_sessions.get_turn(turn["turn_id"])["status"] == "FAILED")
    failed = chat_sessions.get_turn(turn["turn_id"])
    assert failed["error_code"] == "MODEL_ACTIVATION_FAILED"
    assert FakeAppServerClient.inference_calls == 0
    assert chat_sessions.get_session(session_id)["codex_thread_id"] is None


def test_legacy_session_resumes_without_requested_model(isolated_registry):
    _register_project(isolated_registry)
    session_id = chat_sessions.create_session("demo", str(isolated_registry / "demo"))
    chat_sessions.update_session_thread(
        session_id, codex_thread_id="LEGACY-THREAD", codex_home="isolated", model="cc/claude-sonnet-5-low", provider="omniroute"
    )
    turn = service.send_turn(session_id, "continue", idempotency_key="legacy-resume")
    assert _wait_until(lambda: chat_sessions.get_turn(turn["turn_id"])["status"] == "COMPLETED")
    assert FakeAppServerClient.resume_models == [None]
    assert FakeAppServerClient.inference_calls == 1


# -- message idempotency ----------------------------------------------------


def test_resubmitting_same_idempotency_key_does_not_re_execute(isolated_registry):
    _register_project(isolated_registry)
    session = service.create_session("demo")
    session_id = session["session_id"]

    first = service.send_turn(session_id, "hello", idempotency_key="same-key")
    assert _wait_until(lambda: service.get_session_detail(session_id)["turns"][0]["status"] == "COMPLETED")

    FakeAppServerClient.response_text = "SHOULD NEVER BE SEEN"
    second = service.send_turn(session_id, "goodbye", idempotency_key="same-key")

    assert first["turn_id"] == second["turn_id"]
    assert second["response_text"] == "fake reply"  # from the FIRST execution, not the second call's text


def test_omitted_idempotency_key_generates_one_and_still_executes(isolated_registry):
    _register_project(isolated_registry)
    session = service.create_session("demo")
    turn = service.send_turn(session["session_id"], "hello")
    assert turn["idempotency_key"]


def test_chat_turn_timeout_defaults_to_900_seconds(isolated_registry, monkeypatch):
    monkeypatch.delenv(service.CHAT_TURN_TIMEOUT_ENV, raising=False)
    _register_project(isolated_registry)
    session_id = service.create_session("demo")["session_id"]
    turn = service.send_turn(session_id, "hello", idempotency_key="default-timeout")
    assert _wait_until(lambda: service.get_session_detail(session_id)["turns"][0]["status"] == "COMPLETED")
    assert FakeAppServerClient.turn_timeouts == [900]


def test_chat_turn_timeout_reads_bounded_environment_value(isolated_registry, monkeypatch):
    monkeypatch.setenv(service.CHAT_TURN_TIMEOUT_ENV, "1200")
    _register_project(isolated_registry)
    session_id = service.create_session("demo")["session_id"]
    service.send_turn(session_id, "hello", idempotency_key="configured-timeout")
    assert _wait_until(lambda: service.get_session_detail(session_id)["turns"][0]["status"] == "COMPLETED")
    assert FakeAppServerClient.turn_timeouts == [1200]


@pytest.mark.parametrize("invalid", ["", "abc", "0", "29", "3601", "-10"])
def test_invalid_chat_turn_timeout_fails_closed_before_inference(isolated_registry, monkeypatch, invalid):
    monkeypatch.setenv(service.CHAT_TURN_TIMEOUT_ENV, invalid)
    _register_project(isolated_registry)
    session_id = service.create_session("demo")["session_id"]
    turn = service.send_turn(session_id, "hello", idempotency_key="invalid-timeout")
    assert _wait_until(lambda: service.get_session_detail(session_id)["turns"][0]["status"] == "FAILED")
    row = service.get_session_detail(session_id)["turns"][0]
    assert row["error_code"] == "INVALID_TURN_TIMEOUT_CONFIG"
    assert FakeAppServerClient.inference_calls == 0
    assert FakeAppServerClient.turn_timeouts == []


def test_chat_turn_timeout_is_not_retried_and_history_remains_available(isolated_registry):
    FakeAppServerClient.turn_error = AppServerError("TURN_TIMEOUT", "No turn/completed within 900s.")
    _register_project(isolated_registry)
    session_id = service.create_session("demo")["session_id"]
    prior = service.send_turn(session_id, "first", idempotency_key="first-turn")
    assert _wait_until(lambda: service.get_session_detail(session_id)["turns"][0]["status"] == "FAILED")
    failed = service.get_session_detail(session_id)["turns"][0]
    assert failed["turn_id"] == prior["turn_id"]
    assert failed["error_code"] == "TURN_TIMEOUT"
    # Reusing the idempotency key returns the recorded uncertain result;
    # it does not execute the inference a second time.
    repeated = service.send_turn(session_id, "first", idempotency_key="first-turn")
    assert repeated["turn_id"] == prior["turn_id"]
    assert FakeAppServerClient.inference_calls == 1
    assert service.get_session_detail(session_id)["turns"][0]["error_code"] == "TURN_TIMEOUT"


def test_late_appserver_notification_cannot_rewrite_terminal_timeout(isolated_registry):
    FakeAppServerClient.turn_error = AppServerError("TURN_TIMEOUT", "deadline elapsed")
    _register_project(isolated_registry)
    session_id = service.create_session("demo")["session_id"]
    turn = service.send_turn(session_id, "slow", idempotency_key="late-completion")
    assert _wait_until(lambda: chat_sessions.get_turn(turn["turn_id"])["status"] == "FAILED")
    before = chat_sessions.get_turn(turn["turn_id"])
    client = FakeAppServerClient.instances[0]
    assert client._on_notification is not None
    client._on_notification({"method": "turn/completed", "params": {"turn": {"id": "FAKE-CODEX-TURN-1", "status": "completed"}}})
    after = chat_sessions.get_turn(turn["turn_id"])
    assert after["status"] == "FAILED"
    assert after["error_code"] == before["error_code"] == "TURN_TIMEOUT"
    assert FakeAppServerClient.inference_calls == 1


# -- concurrent requests to one session -------------------------------------


def test_second_distinct_turn_is_rejected_while_one_is_active(isolated_registry):
    FakeAppServerClient.block_until_interrupted = True
    _register_project(isolated_registry)
    session = service.create_session("demo")
    session_id = session["session_id"]

    service.send_turn(session_id, "slow one", idempotency_key="busy-key-1")
    active = _wait_for_blocked_client()

    with pytest.raises(service.ChatError) as excinfo:
        service.send_turn(session_id, "another one", idempotency_key="busy-key-2")
    assert excinfo.value.code == "SESSION_BUSY"
    assert excinfo.value.http_status == 409

    # Clean up: release the blocked turn.
    active.client.turn_interrupt("FAKE-THREAD-1", "FAKE-CODEX-TURN-1")
    assert _wait_until(lambda: len(service._active_turns) == 0)


def test_parallel_send_turns_one_session_start_exactly_one(isolated_registry):
    FakeAppServerClient.block_until_interrupted = True
    _register_project(isolated_registry)
    session_id = service.create_session("demo")["session_id"]
    start = threading.Barrier(3)

    def send(key):
        start.wait(timeout=5)
        try:
            return service.send_turn(session_id, key, idempotency_key=key)
        except service.ChatError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send, "k1")
        second = pool.submit(send, "k2")
        start.wait(timeout=5)
        results = [first.result(timeout=10), second.result(timeout=10)]

    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, service.ChatError) and result.code == "SESSION_BUSY" for result in results) == 1
    assert len(chat_sessions.list_turns(session_id)) == 1
    active = _wait_for_blocked_client()
    active.client.turn_interrupt("FAKE-THREAD-1", "FAKE-CODEX-TURN-1")
    assert _wait_until(lambda: len(service._active_turns) == 0)


def test_too_many_concurrent_turns_is_rejected(isolated_registry, monkeypatch):
    FakeAppServerClient.block_until_interrupted = True
    monkeypatch.setattr(service, "MAX_CONCURRENT_TURNS", 1)
    _register_project(isolated_registry)

    session_a = service.create_session("demo")
    session_b = service.create_session("demo")

    service.send_turn(session_a["session_id"], "slow", idempotency_key="a1")
    active = _wait_for_blocked_client()

    with pytest.raises(service.ChatError) as excinfo:
        service.send_turn(session_b["session_id"], "also slow", idempotency_key="b1")
    assert excinfo.value.code == "TOO_MANY_CONCURRENT_TURNS"
    assert excinfo.value.http_status == 429

    active.client.turn_interrupt("FAKE-THREAD-1", "FAKE-CODEX-TURN-1")
    assert _wait_until(lambda: len(service._active_turns) == 0)


# -- turn cancellation -------------------------------------------------------


def test_interrupt_turn_marks_it_cancelled_not_failed(isolated_registry):
    FakeAppServerClient.block_until_interrupted = True
    _register_project(isolated_registry)
    session = service.create_session("demo")
    session_id = session["session_id"]

    turn = service.send_turn(session_id, "slow one", idempotency_key="k1")
    _wait_for_blocked_client()

    result = service.interrupt_turn(session_id, turn["turn_id"])
    assert result["status"] == "INTERRUPT_REQUESTED"

    def _is_cancelled():
        row = service.chat_sessions.get_turn(turn["turn_id"])
        return row["status"] == "CANCELLED"

    assert _wait_until(_is_cancelled)


def test_interrupt_with_no_active_turn_is_a_clean_no_op(isolated_registry):
    _register_project(isolated_registry)
    session = service.create_session("demo")
    result = service.interrupt_turn(session["session_id"])
    assert result["status"] == "NOT_ACTIVE"


def test_foreign_turn_id_cannot_interrupt_other_session(isolated_registry):
    FakeAppServerClient.block_until_interrupted = True
    _register_project(isolated_registry)
    owner = service.create_session("demo")["session_id"]
    other = service.create_session("demo")["session_id"]
    turn = service.send_turn(owner, "slow", idempotency_key="k1")
    active = _wait_for_blocked_client()

    with pytest.raises(service.ChatError) as excinfo:
        service.interrupt_turn(other, turn["turn_id"])
    assert excinfo.value.code == "TURN_NOT_FOUND"
    assert excinfo.value.http_status == 404
    assert not active.interrupted.is_set()
    assert active.client.interrupt_calls == []

    active.client.turn_interrupt("FAKE-THREAD-1", "FAKE-CODEX-TURN-1")
    assert _wait_until(lambda: len(service._active_turns) == 0)


# -- process failure ---------------------------------------------------------


def test_app_server_launch_failure_marks_turn_failed(isolated_registry):
    FakeAppServerClient.start_error = AppServerError("LAUNCH_FAILED", "could not launch")
    _register_project(isolated_registry)
    session = service.create_session("demo")
    session_id = session["session_id"]

    service.send_turn(session_id, "hello", idempotency_key="k1")
    assert _wait_until(
        lambda: service.get_session_detail(session_id)["turns"][0]["status"] == "FAILED"
    )
    turn = service.get_session_detail(session_id)["turns"][0]
    assert turn["error_code"] == "LAUNCH_FAILED"
    assert service._get_session_row(session_id)["status"] == "ERROR"


# -- SSE event handling / disconnection --------------------------------------


def test_subscribe_events_replays_buffer_then_streams_live(isolated_registry):
    FakeAppServerClient.block_until_interrupted = True
    _register_project(isolated_registry)
    session = service.create_session("demo")
    session_id = session["session_id"]
    turn = service.send_turn(session_id, "slow one", idempotency_key="k1")
    active = _wait_for_blocked_client()

    subscription = service.subscribe_events(turn["turn_id"])
    assert subscription is not None
    subscriber_queue, catch_up, _done, _gap, _oldest, _latest = subscription
    # thread/started + item/completed were already emitted before we
    # subscribed -- both must be in the catch-up replay, not lost.
    assert any(event.get("method") == "thread/started" for _seq, event in catch_up)
    assert any(event.get("method") == "item/completed" for _seq, event in catch_up)

    service.unsubscribe_events(turn["turn_id"], subscriber_queue)

    active.client.turn_interrupt("FAKE-THREAD-1", "FAKE-CODEX-TURN-1")
    assert _wait_until(lambda: len(service._active_turns) == 0)


def test_subscribe_events_on_a_finished_turn_returns_none(isolated_registry):
    _register_project(isolated_registry)
    session = service.create_session("demo")
    turn = service.send_turn(session["session_id"], "hello", idempotency_key="k1")
    assert _wait_until(lambda: service.get_session_detail(session["session_id"])["turns"][0]["status"] == "COMPLETED")

    # Not active any more -- a route handler should fall back to the
    # durable snapshot instead (see turn_snapshot()), never hang waiting
    # on a subscription that will never arrive.
    assert service.subscribe_events(turn["turn_id"]) is None
    snapshot = service.turn_snapshot(turn["turn_id"])
    assert snapshot["status"] == "COMPLETED"
    assert snapshot["response_text"] == "fake reply"


def test_disconnecting_a_subscriber_does_not_stop_the_turn(isolated_registry):
    # "Never lose track of an active turn merely because the browser
    # disconnects": unsubscribe must not touch active.client/interrupted.
    FakeAppServerClient.block_until_interrupted = True
    _register_project(isolated_registry)
    session = service.create_session("demo")
    turn = service.send_turn(session["session_id"], "slow one", idempotency_key="k1")
    active = _wait_for_blocked_client()

    subscriber_queue, _catch_up, _done, _gap, _oldest, _latest = service.subscribe_events(turn["turn_id"])
    service.unsubscribe_events(turn["turn_id"], subscriber_queue)

    assert len(service._active_turns) == 1
    assert not active.interrupted.is_set()
    assert not active.done.is_set()

    active.client.turn_interrupt("FAKE-THREAD-1", "FAKE-CODEX-TURN-1")
    assert _wait_until(lambda: len(service._active_turns) == 0)


def test_event_cursor_replays_only_newer_items_and_marks_expired_cursor(isolated_registry):
    active = service._ActiveTurn()
    active.buffer.extend([
        (10, {"method": "event/10"}),
        (11, {"method": "event/11"}),
    ])
    active.next_seq = 12
    service._active_turns["TURN-CURSOR"] = active

    queue_newer, newer, _done, gap, oldest, latest = service.subscribe_events("TURN-CURSOR", after_seq=10)
    assert [seq for seq, _event in newer] == [11]
    assert gap is False
    assert (oldest, latest) == (10, 11)
    service.unsubscribe_events("TURN-CURSOR", queue_newer)

    queue_expired, expired, _done, gap, _oldest, _latest = service.subscribe_events("TURN-CURSOR", after_seq=3)
    assert [seq for seq, _event in expired] == [10, 11]
    assert gap is True
    service.unsubscribe_events("TURN-CURSOR", queue_expired)


def test_multiple_sse_subscribers_receive_events_and_queue_overflow_is_visible(isolated_registry):
    active = service._ActiveTurn()
    service._active_turns["TURN-FANOUT"] = active
    first, _catchup1, *_ = service.subscribe_events("TURN-FANOUT")
    second, _catchup2, *_ = service.subscribe_events("TURN-FANOUT")

    # Fill one bounded queue, then fan out one more event. Its dropped
    # sequence is retained so the route can emit nexus/replayGap.
    for index in range(service.SUBSCRIBER_QUEUE_LIMIT):
        service._on_event(active, {"method": f"event/{index}"})
    service._on_event(active, {"method": "event/overflow"})

    assert first.qsize() == service.SUBSCRIBER_QUEUE_LIMIT
    assert second.qsize() == service.SUBSCRIBER_QUEUE_LIMIT
    assert first.dropped_through == 1
    assert second.dropped_through == 1
    service.unsubscribe_events("TURN-FANOUT", first)
    service.unsubscribe_events("TURN-FANOUT", second)


# -- OmniRoute enforcement (service layer: sandbox/approval default) --------


def test_new_thread_is_started_read_only_not_workspace_write(isolated_registry):
    # Deliberately NOT Worker's "workspace-write"/approval_policy="never" --
    # the first Chat MVP must enforce read-only + on-request itself, at
    # the service layer, not merely leave it to a caller-supplied default.
    FakeAppServerClient.block_until_interrupted = True
    _register_project(isolated_registry)
    session = service.create_session("demo")
    service.send_turn(session["session_id"], "hello", idempotency_key="k1")
    active = _wait_for_blocked_client()

    assert active.client.thread_start_kwargs["sandbox"] == "read-only"
    assert active.client.thread_start_kwargs["approval_policy"] == "on-request"

    active.client.turn_interrupt("FAKE-THREAD-1", "FAKE-CODEX-TURN-1")
    assert _wait_until(lambda: len(service._active_turns) == 0)


def _bind_fake_history_thread(session_id):
    chat_sessions.update_session_thread(
        session_id, codex_thread_id="FAKE-THREAD-1", codex_home="isolated-home",
        model="cc/fake-model", provider="omniroute",
    )


def test_history_recovers_ordered_user_assistant_and_tool_items_with_opaque_pagination(isolated_registry):
    _register_project(isolated_registry)
    session = service.create_session("demo")
    session_id = session["session_id"]
    _bind_fake_history_thread(session_id)
    FakeAppServerClient.history_item_pages = {
        None: {
            "data": [
                {"turnId": "CODEX-TURN-1", "item": {"id": "ITEM-1", "type": "userMessage", "content": [{"type": "text", "text": "first prompt"}]}},
                {"turnId": "CODEX-TURN-1", "item": {"id": "ITEM-2", "type": "agentMessage", "text": "assistant answer"}},
            ],
            "nextCursor": "opaque/+cursor==",
        },
        "opaque/+cursor==": {
            "data": [{"turnId": "CODEX-TURN-2", "item": {
                "id": "ITEM-3", "type": "commandExecution", "command": "git status",
                "exitCode": 0, "aggregatedOutput": "KNOWN-SECRET", "status": "completed",
            }}],
            "nextCursor": None,
        },
    }
    FakeAppServerClient.history_turn_pages = {
        None: {"data": [{"id": "CODEX-TURN-1", "status": "interrupted"}], "nextCursor": None},
    }

    first = service.get_history(session_id, limit=2)
    second = service.get_history(session_id, cursor=first["next_cursor"], limit=2)

    assert [item["item_id"] for item in first["items"]] == ["ITEM-1", "ITEM-2"]
    assert first["items"][0]["text"] == "first prompt"
    assert first["items"][1]["text"] == "assistant answer"
    assert first["turns"] == [{"turn_id": "CODEX-TURN-1", "status": "interrupted"}]
    assert first["next_cursor"] == "opaque/+cursor=="
    assert second["items"][0]["type"] == "commandExecution"
    assert second["items"][0]["command"] == "git status"
    assert second["items"][0]["exit_code"] == 0
    assert second["items"][0]["output"] == "<redacted>"
    assert FakeAppServerClient.history_calls[0] == (
        "items", "FAKE-THREAD-1", None, 2, "asc", None
    )
    assert FakeAppServerClient.inference_calls == 0
    assert FakeAppServerClient.redaction_calls > 0


def test_history_preserves_redacted_function_and_mcp_tool_outputs(isolated_registry):
    _register_project(isolated_registry)
    session = service.create_session("demo")
    _bind_fake_history_thread(session["session_id"])
    FakeAppServerClient.history_item_pages = {
        None: {"data": [
            {"turnId": "TURN-TOOLS", "item": {
                "id": "FUNCTION-OUTPUT", "type": "functionCallOutput",
                "name": "lookup", "output": "KNOWN-SECRET result",
            }},
            {"turnId": "TURN-TOOLS", "item": {
                "id": "MCP-OUTPUT", "type": "mcpToolCall", "server": "local",
                "tool": "read", "status": "completed",
                "result": {"content": [{"type": "text", "text": "KNOWN-SECRET data"}]},
            }},
        ], "nextCursor": None},
    }
    FakeAppServerClient.history_turn_pages = {
        None: {"data": [{"id": "TURN-TOOLS", "status": "interrupted"}], "nextCursor": None},
    }

    result = service.get_history(session["session_id"])

    assert result["items"][0]["text"] == "<redacted> result"
    assert result["items"][1]["text"] == "<redacted> data"
    assert result["turns"] == [{"turn_id": "TURN-TOOLS", "status": "interrupted"}]
    assert FakeAppServerClient.inference_calls == 0


def test_history_rejects_nonadvancing_cursor(isolated_registry):
    _register_project(isolated_registry)
    session = service.create_session("demo")
    _bind_fake_history_thread(session["session_id"])
    FakeAppServerClient.history_item_pages = {
        "loop": {"data": [], "nextCursor": "loop"},
    }

    with pytest.raises(service.ChatError) as error:
        service.get_history(session["session_id"], cursor="loop")

    assert error.value.code == "HISTORY_UNAVAILABLE"


def test_history_during_active_turn_reports_a_distinguishable_409(isolated_registry, monkeypatch):
    """The real ``codex app-server`` refuses a second concurrent
    ``thread/resume`` of the same non-ephemeral thread while another
    process (the active turn's own AppServerClient) already holds it --
    get_history() must surface this as its own HISTORY_UNAVAILABLE code,
    not silently as empty history and not conflated with a genuinely
    stale workspace (WORKSPACE_BINDING_CHANGED, also 409 -- see
    test_history_stale_workspace_and_missing_thread_are_not_empty_success).
    This distinct code is what the frontend's deferred-reconciliation fix
    (T4.1) keys off of to retry later instead of giving up."""
    _register_project(isolated_registry)
    session = service.create_session("demo")
    _bind_fake_history_thread(session["session_id"])

    def _conflicting_resume(self, thread_id, **kwargs):
        raise AppServerError("THREAD_BUSY", "Thread is already resumed by another process.")

    monkeypatch.setattr(FakeAppServerClient, "thread_resume", _conflicting_resume)

    with pytest.raises(service.ChatError) as error:
        service.get_history(session["session_id"])

    assert error.value.code == "HISTORY_UNAVAILABLE"
    assert error.value.http_status == 409
    assert FakeAppServerClient.inference_calls == 0


def test_history_turn_filter_is_validated_against_resumed_thread(isolated_registry):
    _register_project(isolated_registry)
    session = service.create_session("demo")
    _bind_fake_history_thread(session["session_id"])
    FakeAppServerClient.history_turn_pages = {
        None: {"data": [{"id": "CODEX-TURN-OWNED", "status": "failed"}], "nextCursor": None},
    }
    FakeAppServerClient.history_item_pages = {
        None: {"data": [{"turnId": "CODEX-TURN-OWNED", "item": {"id": "ITEM", "type": "agentMessage", "text": "saved"}}], "nextCursor": None},
    }

    result = service.get_history(session["session_id"], turn_id="CODEX-TURN-OWNED")
    assert result["items"][0]["turn_id"] == "CODEX-TURN-OWNED"
    assert FakeAppServerClient.history_calls[0][0] == "turns"
    assert FakeAppServerClient.history_calls[1][-1] == "CODEX-TURN-OWNED"
    assert FakeAppServerClient.inference_calls == 0


def test_history_rejects_turn_from_another_thread_and_invalid_cursor(isolated_registry):
    _register_project(isolated_registry)
    session = service.create_session("demo")
    _bind_fake_history_thread(session["session_id"])
    FakeAppServerClient.history_turn_pages = {
        None: {"data": [{"id": "OWNED-TURN", "status": "completed"}], "nextCursor": None},
    }
    with pytest.raises(service.ChatError) as missing:
        service.get_history(session["session_id"], turn_id="FOREIGN-TURN")
    assert missing.value.code == "TURN_NOT_FOUND"

    with pytest.raises(service.ChatError) as invalid:
        service.get_history(session["session_id"], cursor="")
    assert invalid.value.code == "INVALID_HISTORY_CURSOR"
    assert FakeAppServerClient.inference_calls == 0


def test_history_stale_workspace_and_missing_thread_are_not_empty_success(isolated_registry):
    _register_project(isolated_registry)
    session = service.create_session("demo")
    session_id = session["session_id"]
    chat_sessions.create_turn_if_absent(session_id, "key", "persisted user prompt")
    with pytest.raises(service.ChatError) as unavailable:
        service.get_history(session_id)
    assert unavailable.value.code == "HISTORY_UNAVAILABLE"

    _bind_fake_history_thread(session_id)
    _move_project(isolated_registry)
    with pytest.raises(service.ChatError) as stale:
        service.get_history(session_id)
    assert stale.value.code == "WORKSPACE_BINDING_CHANGED"
    assert FakeAppServerClient.inference_calls == 0
