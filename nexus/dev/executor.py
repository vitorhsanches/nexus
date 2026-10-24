"""Interactive Development Agent executor (F2.1).

Scope, strictly: run ONE Codex turn inside an already-created, authorized
development session's isolated worktree, track it to completion/failure/
interruption, and persist the result. No public route, no write UI, no
branch integration -- this module is callable only from Python, by
whatever later phase adds an endpoint on top of it.

Security posture (see module docstring sections below for the
investigation this is based on):

* Write capability is never implicit. A turn only ever runs if the
  session's project has BOTH project.enabled AND dev_enabled set, and
  that is re-checked on every single turn, not just at session creation.
* Sandbox: workspace-write is set via AppServerClient's PROCESS-LEVEL
  ``sandbox_mode_override`` (the ``-c sandbox_mode="..."`` App-Server
  command-line flag), not only via the per-thread ``thread_start(sandbox=
  ...)`` JSON-RPC parameter. This distinction is load-bearing, not
  cosmetic: nexus/chat/appserver_client.py's own docstring documents a
  LIVE-TESTED finding that the OmniRoute profile's config.toml (shared
  with Manager/Worker/Reviewer, which hardcodes sandbox_mode=
  "workspace-write") wins over the thread-level parameter alone -- a
  thread started with sandbox="read-only" still executed a real write
  with no prompt, until the process-level override was added. The same
  profile also hardcodes approval_policy="never"; this module additionally
  passes the matching PROCESS-LEVEL ``approval_policy_override`` (added
  in F2.3), mirroring the sandbox override for consistency and
  defense-in-depth.
* Approval (F2.3 live finding, superseding the earlier F2.2 "unconfirmed"
  note): whether ``approval_policy`` is "never" or "on-request" -- at
  thread level, at process level, or both -- turned out NOT to be what
  determines if a protocol-level approval request fires. A live,
  disposable-repo diagnostic found that an ordinary sandboxed action that
  is simply blocked (e.g. a network call denied at the OS boundary)
  produces zero approval requests under ANY of these settings, but a
  genuine MODEL-INITIATED escalation request (the agent explicitly
  retrying a blocked command with escalated permissions) reliably
  produces a real ``item/commandExecution/requestApproval`` request --
  observed identically with and without the process-level override. No
  ``on_server_request`` callback is passed to AppServerClient here --
  there is no approval UI yet -- so AppServerClient's own,
  already-existing fail-closed default (``_fail_closed_decision``) denies
  every such request; this was verified live to actually deny a real
  escalation attempt (the file targeted outside the worktree was never
  created). It also denies on any exception raised while handling a
  request, and returns a JSON-RPC error (never a grant) for a request
  method it does not recognize. This module adds no approval logic of its
  own because none is needed: Chat already relies on this exact same
  default for its own on-request policy, and reusing it here (rather than
  writing a second implementation) is both the smallest change and the
  one with an established, tested, and now live-verified precedent.
* The worktree is re-verified (nexus.dev.worktree.verify_dev_worktree)
  immediately before every turn -- never assumed still valid from
  session-creation time -- and the App-Server process's cwd is always
  that exact, re-verified worktree path, never the project's own
  directory.
* CODEX_HOME isolation and OAuth-credential exclusion are entirely
  AppServerClient's own job (codex_subprocess_env(..., include_auth=
  False)) -- unchanged here, exactly as Chat already relies on it.
"""

from __future__ import annotations

import queue
import threading
from collections import deque

from nexus.chat.appserver_client import AppServerClient, AppServerError
from nexus.dev import feature_gate
from nexus.dev.service import DevServiceError, validate_dev_project
from nexus.dev.worktree import (
    DevWorktreeError,
    read_worktree_changed_files,
    read_worktree_diff,
    verify_dev_worktree,
)
from nexus.failure_contract import sanitize_error_text
from nexus.registry import dev_sessions, workspace_handoff

# Same bounds as nexus.chat.service -- a bounded per-turn event buffer
# (for a reconnecting subscriber to catch up from) and a bounded
# per-subscriber delivery queue (so one slow/stuck SSE client can never
# grow without limit or block turn execution).
EVENT_BUFFER_LIMIT = 200
SUBSCRIBER_QUEUE_LIMIT = 200

# Deliberately NOT "never" (that is the existing, reviewed, non-interactive
# Worker/Manager/Reviewer convention) -- see the module docstring for why
# this value's real-world effectiveness is unconfirmed, and why that does
# not weaken the actual security guarantee (no on_server_request handler
# is ever wired, so AppServerClient's own fail-closed default denies
# regardless of whether this is honored).
DEV_APPROVAL_POLICY = "on-request"

# The process-level override that nexus.chat.appserver_client's own
# docstring documents as the one mechanism actually found, live, to
# control sandboxing -- never inferred, never left as the OmniRoute
# profile's own hardcoded default (which happens to already be
# "workspace-write", but relying on that silently would mean this
# module's write capability depends on an unrelated profile's default
# never changing).
DEV_SANDBOX = "workspace-write"

DEFAULT_DEV_TURN_TIMEOUT_SECONDS = 180
MAX_CONCURRENT_DEV_TURNS = 2

_active_lock = threading.Lock()
_active_dev_turns: dict[str, "_ActiveDevTurn"] = {}


class _ActiveDevTurn:
    def __init__(self) -> None:
        self.client: AppServerClient | None = None
        self.thread_id: str | None = None
        self.codex_turn_id: str | None = None
        self.lock = threading.Lock()
        self.buffer: "deque[tuple[int, dict]]" = deque(maxlen=EVENT_BUFFER_LIMIT)
        self.subscribers: list["queue.Queue"] = []
        self.next_seq = 1
        self.interrupted = threading.Event()
        self.done = threading.Event()


class _SubscriberQueue(queue.Queue):
    """Bounded subscriber queue that records the highest silently evicted ID."""

    def __init__(self) -> None:
        super().__init__(maxsize=SUBSCRIBER_QUEUE_LIMIT)
        self.dropped_through = 0


def _on_event(active: _ActiveDevTurn, event: dict) -> None:
    with active.lock:
        if active.done.is_set():
            return
        seq = active.next_seq
        active.next_seq += 1
        active.buffer.append((seq, event))
        subscribers = list(active.subscribers)
    for subscriber_queue in subscribers:
        try:
            subscriber_queue.put_nowait((seq, event))
        except Exception:
            try:
                dropped_seq, _dropped_event = subscriber_queue.get_nowait()
                if isinstance(subscriber_queue, _SubscriberQueue):
                    subscriber_queue.dropped_through = max(subscriber_queue.dropped_through, dropped_seq)
                subscriber_queue.put_nowait((seq, event))
            except Exception:
                pass


def _mark_active_done(active: _ActiveDevTurn) -> None:
    with active.lock:
        active.done.set()


def _turn_dict(row) -> dict:
    return {
        "turn_id": row["id"],
        "dev_session_id": row["dev_session_id"],
        "prompt": row["prompt"],
        "requested_model": row["requested_model"],
        "status": row["status"],
        "response_text": row["response_text"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def _require_active_session(dev_session_id: str):
    """Steps 1-6 of the executor contract: a valid session, belonging to
    an authorized project, in a state that can run a turn, with a
    worktree that is confirmed to still exist and belong to that exact
    project/session. Re-run on every single turn -- never cached from an
    earlier check."""
    session = dev_sessions.get_session(dev_session_id)
    if session is None:
        raise DevServiceError(
            "SESSION_NOT_FOUND", f"Unknown development session: {dev_session_id!r}", http_status=404
        )

    # Re-validates project.enabled AND project.dev_enabled fresh, every
    # call -- revoking authorization takes effect on the very next turn,
    # including for a session that was legitimately created earlier.
    workspace_path = validate_dev_project(session["project_id"])

    if session["status"] != dev_sessions.STATUS_ACTIVE:
        raise DevServiceError(
            "SESSION_NOT_ACTIVE",
            f"Development session {dev_session_id!r} is {session['status']!r}, not ACTIVE; cannot run a turn.",
            http_status=409,
        )

    if not session["worktree_path"]:
        raise DevServiceError(
            "WORKTREE_MISSING", f"Development session {dev_session_id!r} has no worktree recorded.", http_status=409
        )

    try:
        verified_worktree = verify_dev_worktree(workspace_path, session["worktree_path"], dev_session_id)
    except DevWorktreeError as error:
        raise DevServiceError(
            "WORKTREE_INVALID",
            f"Development session {dev_session_id!r}'s worktree could not be verified: {error}",
            http_status=409,
        ) from error

    return session, workspace_path, str(verified_worktree)


def run_dev_turn(dev_session_id: str, prompt: str, *, model: str | None = None,
                 effort: str | None = None) -> dict:
    """Start one Codex turn inside dev_session_id's isolated worktree.

    Returns immediately with the new turn's PENDING record (the App-Server
    subprocess runs on a background thread, exactly mirroring
    nexus.chat.service.send_turn()'s architecture, so interrupt_dev_turn()
    can reach it while it is still running). Raises DevServiceError
    without creating any turn record if the session/project/worktree
    checks in _require_active_session() fail, or if this session already
    has a PENDING/RUNNING turn (exactly one active turn per development
    session, ever). Also enforced here, not only at the (future) route
    layer, as defense in depth: feature_gate.execution_enabled() must be
    true -- see nexus/dev/feature_gate.py for why it defaults off.
    """
    if not feature_gate.execution_enabled():
        raise DevServiceError(
            "DEV_EXECUTION_DISABLED",
            "Development Mode execution is disabled on this server "
            f"(set {feature_gate.ENV_VAR}=1 to enable it).",
            http_status=403,
        )
    if not isinstance(prompt, str) or not prompt.strip():
        raise DevServiceError("EMPTY_PROMPT", "prompt must not be empty.", http_status=400)

    session, _workspace_path, _worktree = _require_active_session(dev_session_id)
    selected_model = model or session["requested_model"]
    if selected_model:
        from nexus.workspace.omniroute_oauth import require_connected_route
        require_connected_route(selected_model)
    if selected_model and selected_model.startswith(("cc/", "cx/")) and not effort:
        raise DevServiceError("AI_EFFORT_REQUIRED", "Escolha o esforço da rota antes de executar.", http_status=422)
    if effort is not None:
        from nexus.dev.models import list_dev_models
        row = next((item for item in list_dev_models(refresh=True)["models"]
                    if item["id"] == selected_model), None)
        if row is None or effort not in row.get("supported_efforts", []) or (
            selected_model and selected_model.startswith(("cc/", "cx/"))
            and effort not in row.get("confirmed_efforts", [])
        ):
            raise DevServiceError("EFFORT_NOT_SUPPORTED", "Esforço não confirmado para a rota exata de Development.", http_status=422)

    try:
        turn_id = dev_sessions.create_turn(dev_session_id, prompt, requested_model=model)
    except workspace_handoff.HandoffBlocked as error:
        raise DevServiceError("DEPENDENCY_WORKTREE_NOT_TRANSFERRED", str(error), http_status=409) from error
    except dev_sessions.DevTurnBusyError as error:
        raise DevServiceError(
            "SESSION_BUSY",
            f"Development session {dev_session_id!r} already has an active turn "
            f"({error.turn_id!r}); wait for it to finish or interrupt it first.",
            http_status=409,
        ) from error

    with _active_lock:
        if len(_active_dev_turns) >= MAX_CONCURRENT_DEV_TURNS:
            dev_sessions.update_turn_status(
                turn_id,
                "FAILED",
                error_code="TOO_MANY_CONCURRENT_DEV_TURNS",
                error_message=f"At most {MAX_CONCURRENT_DEV_TURNS} development turns may run at once.",
            )
            raise DevServiceError(
                "TOO_MANY_CONCURRENT_DEV_TURNS",
                f"At most {MAX_CONCURRENT_DEV_TURNS} development turns may run at once; try again shortly.",
                http_status=429,
            )
        active = _ActiveDevTurn()
        _active_dev_turns[turn_id] = active

    thread = threading.Thread(
        target=_run_dev_turn_body,
        args=(dev_session_id, turn_id, prompt, model, effort, active),
        daemon=True,
        name=f"nexus-dev-{turn_id}",
    )
    try:
        thread.start()
    except RuntimeError as error:
        with _active_lock:
            _active_dev_turns.pop(turn_id, None)
        dev_sessions.update_turn_status(
            turn_id,
            "FAILED",
            error_code="TURN_START_FAILED",
            error_message=sanitize_error_text(str(error)) or "Development turn could not be started.",
        )
        raise DevServiceError(
            "TURN_START_FAILED", "Development turn could not be started.", http_status=503
        ) from error

    return _turn_dict(dev_sessions.get_turn(turn_id))


def _run_dev_turn_body(
    dev_session_id: str, turn_id: str, prompt: str, model: str | None, effort: str | None, active: _ActiveDevTurn
) -> None:
    dev_sessions.update_turn_status(turn_id, "RUNNING")
    client = None
    try:
        # Re-verify, not re-trust: the session/project/worktree could have
        # changed in the moment between run_dev_turn()'s own check and
        # this background thread actually starting.
        session, _workspace_path, worktree_path = _require_active_session(dev_session_id)
        from nexus.workspace.omniroute_oauth import require_connected_route
        require_connected_route(model or session["requested_model"] or "")

        client = AppServerClient(
            worktree_path,
            codex_home=session["codex_home"],
            sandbox_mode_override=DEV_SANDBOX,
            approval_policy_override=DEV_APPROVAL_POLICY,
            on_notification=lambda event: _on_event(active, event),
            # No on_server_request handler: see module docstring --
            # AppServerClient's own fail-closed default is the entire
            # approval story for this MVP, deliberately.
        )
        active.client = client
        client.start()

        if session["codex_thread_id"]:
            thread_result = client.thread_resume(session["codex_thread_id"], model=model)
        else:
            thread_result = client.thread_start(
                model=model,
                cwd=worktree_path,
                sandbox=DEV_SANDBOX,
                approval_policy=DEV_APPROVAL_POLICY,
                ephemeral=False,
            )
        thread_info = thread_result.get("thread") or {}
        thread_id = thread_info.get("id") or session["codex_thread_id"]
        active.thread_id = thread_id
        dev_sessions.update_thread(dev_session_id, codex_thread_id=thread_id, codex_home=client.codex_home)

        if effort is not None:
            client.thread_settings_update(thread_id, model=model or session["requested_model"], effort=effort)
            confirmed = client.thread_resume(thread_id, model=model or session["requested_model"])
            if confirmed.get("reasoningEffort") != effort or confirmed.get("model") != (model or session["requested_model"]):
                raise DevServiceError("EFFORT_ACTIVATION_FAILED", "App Server não confirmou modelo e esforço antes da execução.", http_status=503)

        response_text = client.send_message_and_wait(
            thread_id,
            prompt,
            timeout_seconds=DEFAULT_DEV_TURN_TIMEOUT_SECONDS,
            on_turn_started=lambda codex_turn_id: setattr(active, "codex_turn_id", codex_turn_id),
        )

        dev_sessions.update_turn_status(turn_id, "COMPLETED", response_text=response_text)
        _mark_active_done(active)

    except (AppServerError, DevServiceError, DevWorktreeError) as error:
        status = "CANCELLED" if active.interrupted.is_set() else "FAILED"
        code = getattr(error, "code", type(error).__name__)
        dev_sessions.update_turn_status(
            turn_id, status, error_code=code, error_message=sanitize_error_text(str(error)) or str(error)
        )
        _mark_active_done(active)
    except Exception as error:  # pragma: no cover -- defensive, unexpected failure
        dev_sessions.update_turn_status(
            turn_id,
            "FAILED",
            error_code="UNEXPECTED_ERROR",
            error_message=sanitize_error_text(str(error)) or "Unexpected error.",
        )
        _mark_active_done(active)
    finally:
        _mark_active_done(active)
        if client is not None:
            try:
                client.stop()
            except Exception:
                pass
        with _active_lock:
            _active_dev_turns.pop(turn_id, None)


def interrupt_dev_turn(dev_session_id: str, turn_id: str | None = None) -> dict:
    """Mirrors nexus.chat.service.interrupt_turn() exactly."""
    if dev_sessions.get_session(dev_session_id) is None:
        raise DevServiceError("SESSION_NOT_FOUND", f"Unknown development session: {dev_session_id!r}", http_status=404)

    if turn_id is None:
        active_row = dev_sessions.get_active_turn_for_session(dev_session_id)
        if active_row is None:
            return {"status": "NOT_ACTIVE", "turn": None}
        turn_id = active_row["id"]

    turn_row = dev_sessions.get_turn_for_session(dev_session_id, turn_id)
    if turn_row is None:
        raise DevServiceError("TURN_NOT_FOUND", "Turn not found in this development session.", http_status=404)

    with _active_lock:
        active = _active_dev_turns.get(turn_id)

    if active is None or active.client is None or active.thread_id is None:
        return {"status": "NOT_ACTIVE", "turn": _turn_dict(turn_row)}

    if active.codex_turn_id is None:
        return {"status": "STARTING", "turn_id": turn_id}

    active.interrupted.set()
    try:
        active.client.turn_interrupt(active.thread_id, active.codex_turn_id)
    except AppServerError as error:
        return {"status": "INTERRUPT_FAILED", "error": str(error)}

    return {"status": "INTERRUPT_REQUESTED", "turn_id": turn_id}


def get_turn_detail(dev_session_id: str, turn_id: str) -> dict:
    row = dev_sessions.get_turn_for_session(dev_session_id, turn_id)
    if row is None:
        raise DevServiceError("TURN_NOT_FOUND", "Turn not found in this development session.", http_status=404)
    return _turn_dict(row)


def list_turns(dev_session_id: str) -> list[dict]:
    return [_turn_dict(row) for row in dev_sessions.list_turns(dev_session_id)]


def changed_files(dev_session_id: str) -> list[dict]:
    """Read-only Git status summary for a session's worktree (F2.4A).

    Reuses the exact same project-authorization + worktree-identity
    checks as a real turn (_require_active_session) -- this never reads
    an unverified or foreign path, and revoked authorization hides it
    just as it would block a turn. Deliberately NOT gated by
    feature_gate.execution_enabled(): inspecting what a worktree already
    contains is not itself an execution action, and a reviewer must be
    able to see a session's changes even with the execution gate off.
    Unlike run_dev_turn, this does not require the session to be
    ACTIVE -- a finished/interrupted/failed session's worktree is still
    readable until its worktree is removed.
    """
    session = dev_sessions.get_session(dev_session_id)
    if session is None:
        raise DevServiceError("SESSION_NOT_FOUND", f"Unknown development session: {dev_session_id!r}", http_status=404)
    workspace_path = validate_dev_project(session["project_id"])
    if not session["worktree_path"]:
        raise DevServiceError("WORKTREE_MISSING", f"Development session {dev_session_id!r} has no worktree recorded.", http_status=409)
    try:
        verified_worktree = verify_dev_worktree(workspace_path, session["worktree_path"], dev_session_id)
    except DevWorktreeError as error:
        raise DevServiceError(
            "WORKTREE_INVALID",
            f"Development session {dev_session_id!r}'s worktree could not be verified: {error}",
            http_status=409,
        ) from error
    return read_worktree_changed_files(str(verified_worktree))


def review_diff(dev_session_id: str) -> dict:
    """Read-only, base-SHA review; applies the same fresh authorization and
    worktree ownership checks as the Changed Files endpoint."""
    session = dev_sessions.get_session(dev_session_id)
    if session is None:
        raise DevServiceError("SESSION_NOT_FOUND", f"Unknown development session: {dev_session_id!r}", http_status=404)
    workspace_path = validate_dev_project(session["project_id"])
    if not session["worktree_path"]:
        raise DevServiceError("WORKTREE_MISSING", f"Development session {dev_session_id!r} has no worktree recorded.", http_status=409)
    try:
        verified_worktree = verify_dev_worktree(workspace_path, session["worktree_path"], dev_session_id)
        if not session["base_sha"]:
            raise DevWorktreeError("The saved base commit is missing.")
        return read_worktree_diff(str(verified_worktree), session["base_sha"])
    except DevWorktreeError as error:
        raise DevServiceError("DIFF_UNAVAILABLE", f"Development session diff is unavailable: {error}", http_status=409) from error


# -- SSE ------------------------------------------------------------------
#
# Mirrors nexus.chat.service's SSE functions exactly (same buffer/
# subscriber-queue/replay-gap semantics), applied to development turns.


def require_dev_session_turn(dev_session_id: str, turn_id: str):
    turn_row = dev_sessions.get_turn_for_session(dev_session_id, turn_id)
    if turn_row is None:
        raise DevServiceError("TURN_NOT_FOUND", "Turn not found in this development session.", http_status=404)
    return turn_row


def subscribe_events(turn_id: str, after_seq: int | None = None):
    """Atomically attach a bounded subscriber and replay retained newer events.

    Returns ``(queue, catch_up, active_done, replay_gap, oldest, latest)``,
    or None if the turn is not (or no longer) actively running -- the
    caller falls back to turn_snapshot() for the durable final state.
    """
    with _active_lock:
        active = _active_dev_turns.get(turn_id)
    if active is None:
        return None

    subscriber_queue: "queue.Queue" = _SubscriberQueue()
    with active.lock:
        retained = list(active.buffer)
        oldest = retained[0][0] if retained else active.next_seq
        latest = active.next_seq - 1
        replay_gap = (
            after_seq < oldest - 1
            if after_seq is not None
            else oldest > 1
        )
        catch_up = [(seq, event) for seq, event in retained if after_seq is None or seq > after_seq]
        active.subscribers.append(subscriber_queue)
    return subscriber_queue, catch_up, active.done, replay_gap, oldest, latest


def active_event_latest_seq(turn_id: str) -> int | None:
    with _active_lock:
        active = _active_dev_turns.get(turn_id)
    if active is None:
        return None
    with active.lock:
        return active.next_seq - 1


def unsubscribe_events(turn_id: str, subscriber_queue) -> None:
    with _active_lock:
        active = _active_dev_turns.get(turn_id)
    if active is None:
        return
    with active.lock:
        try:
            active.subscribers.remove(subscriber_queue)
        except ValueError:
            pass


def latest_turn_id_for_session(dev_session_id: str) -> str | None:
    turns = dev_sessions.list_turns(dev_session_id, limit=1)
    return turns[0]["id"] if turns else None


def turn_snapshot(turn_id: str) -> dict | None:
    row = dev_sessions.get_turn(turn_id)
    return _turn_dict(row) if row else None


def recover_orphaned_dev_turns() -> list[str]:
    """Mark RUNNING/PENDING turns from dead Nexus processes outcome-unknown.

    Pure metadata bookkeeping -- never touches the filesystem, Git, or an
    App-Server process, and never replays or retries anything. Reuses the
    same Windows-correct liveness check as
    nexus.dev.service.recover_orphaned_sessions().
    """
    from nexus.orchestration.ownership import classify_ownership

    return dev_sessions.recover_orphaned_turns(
        lambda host, pid, acquired_at: classify_ownership(host, pid, acquired_at) == "dead_same_host"
    )
