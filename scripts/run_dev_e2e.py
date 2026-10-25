"""Prepare a disposable Development fixture and run an opt-in live Nexus smoke.

Native-first: by default the smoke drives a ``codex_native`` Development session
(the local Codex login, no OmniRoute); ``--backend omniroute`` keeps the previous
path. No OmniRoute calls are made directly: turns go through Nexus's Development
executor. The runner creates a review snapshot but never decides an approval,
applies/rejects an integration, or removes data.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import shutil
import socket
import os
import subprocess
import sys
import tempfile
import time
from typing import Protocol
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "dev_e2e_project"
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}
# Environment that could change which plugins/options/startup code pytest runs with.
PYTEST_ENV_DROP = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DEBUG", "PYTHONSTARTUP", "PYTHONINSPECT")
OUTPUT_TAIL_CHARS = 4000


def prepare_fixture(destination: Path) -> Path:
    """Copy the known-bug project to a new path and initialize a local Git repo."""
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing fixture path: {destination}")
    # Never copy interpreter/test artifacts a previous import or pytest run left
    # next to the fixture; the repo's own .gitignore keeps new ones out of Git.
    shutil.copytree(FIXTURE, destination, ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]", ".pytest_cache"))
    subprocess.run(["git", "init", "-q"], cwd=destination, check=True)
    subprocess.run(["git", "config", "user.name", "Nexus E2E Fixture"], cwd=destination, check=True)
    subprocess.run(["git", "config", "user.email", "nexus-e2e-fixture@localhost"], cwd=destination, check=True)
    subprocess.run(["git", "add", "calc.py", "test_calc.py", ".gitignore"], cwd=destination, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "Create disposable Nexus E2E fixture"], cwd=destination, check=True)
    return destination


class NexusApi:
    def __init__(self, base_url: str, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, payload: dict | None = None, *, timeout: float | None = None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    return raw.decode("utf-8", "replace")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            raise RuntimeError(f"Nexus returned HTTP {error.code} for {path}: {detail}") from error

    def iter_sse_events(self, path: str, *, cursor: str | None, deadline: float):
        """Yield SSE frames as they arrive; timeout is bounded by the turn deadline."""
        request = urllib.request.Request(self.base_url + path, headers={"Last-Event-ID": cursor} if cursor else {})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout, remaining)) as response:
                event_id = None
                data_lines: list[str] = []
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("SSE read reached the overall turn deadline.")
                    # urllib applies the initial timeout to every read. Tighten it
                    # to the remaining end-to-end budget before each blocking line.
                    socket_obj = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                    if socket_obj is not None:
                        socket_obj.settimeout(min(self.timeout, remaining))
                    line = response.readline()
                    if not line:
                        break
                    decoded = line.decode("utf-8", "replace").rstrip("\r\n")
                    if not decoded:
                        if data_lines:
                            try:
                                payload = json.loads("\n".join(data_lines))
                            except json.JSONDecodeError:
                                payload = None
                            if isinstance(payload, dict):
                                yield event_id, payload
                        event_id, data_lines = None, []
                    elif decoded.startswith(":"):
                        continue
                    elif decoded.startswith("id:"):
                        event_id = decoded[3:].strip()
                    elif decoded.startswith("data:"):
                        data_lines.append(decoded[5:].lstrip())
                if data_lines:
                    try:
                        payload = json.loads("\n".join(data_lines))
                    except json.JSONDecodeError:
                        payload = None
                    if isinstance(payload, dict):
                        yield event_id, payload
        except urllib.error.URLError as error:
            reason = error.reason
            if isinstance(reason, (TimeoutError, socket.timeout, http.client.IncompleteRead)):
                raise TimeoutError("SSE connection timed out before the turn completed.") from error
            raise RuntimeError(f"Could not read Nexus SSE stream: {reason}") from error
        except (socket.timeout, http.client.IncompleteRead) as error:
            raise TimeoutError("SSE connection timed out before the turn completed.") from error


class DevelopmentSmokeBackend(Protocol):
    """Semantic harness seam: the operations the smoke needs from Nexus.

    ``CurrentOmniRouteDevBackend`` and ``CodexNativeDevBackend`` implement it
    over the existing ``/api/dev`` routes. Nothing here decides an approval or
    applies/rejects an integration: the harness has no method for either.
    """

    def select_project(self, project_id: str) -> dict: ...
    def execution_status(self) -> dict: ...
    def integration_status(self) -> dict: ...
    def models(self) -> dict: ...
    def create_session(self, project_id: str, model: str | None) -> dict: ...
    def start_turn(self, session_id: str, prompt: str, model: str | None) -> dict: ...
    def iter_events(self, session_id: str, turn_id: str, cursor: str | None, deadline: float): ...
    def get_turn(self, session_id: str, turn_id: str, timeout: float) -> dict: ...
    def approvals(self, session_id: str, timeout: float) -> list[dict]: ...
    def changed_files(self, session_id: str, timeout: float) -> list[dict]: ...
    def diff(self, session_id: str, timeout: float) -> dict: ...
    def create_review(self, session_id: str, timeout: float) -> dict: ...
    def preflight(self, session_id: str, review_id: str, timeout: float) -> dict: ...
    def integrations(self, session_id: str, timeout: float) -> list[dict]: ...


class CurrentOmniRouteDevBackend:
    """Adapter for the ``/api/dev`` routes; sessions use the OmniRoute backend."""

    backend_id = "omniroute"

    def __init__(self, api: NexusApi):
        self.api = api

    def select_project(self, project_id: str) -> dict:
        projects = self.api.request("GET", "/api/projects").get("projects", [])
        return next((row for row in projects if row.get("id") == project_id), None)

    def execution_status(self) -> dict:
        return self.api.request("GET", "/api/dev/execution-status")

    def integration_status(self) -> dict:
        return self.api.request("GET", "/api/dev/integration-status")

    def models(self) -> dict:
        return self.api.request("GET", "/api/dev/models")

    def create_session(self, project_id: str, model: str | None) -> dict:
        return self.api.request("POST", "/api/dev/sessions", {"project_id": project_id, "requested_model": model})

    def start_turn(self, session_id: str, prompt: str, model: str | None) -> dict:
        return self.api.request("POST", f"/api/dev/sessions/{session_id}/turns", {"prompt": prompt, "model": model})

    def iter_events(self, session_id: str, turn_id: str, cursor: str | None, deadline: float):
        return self.api.iter_sse_events(
            f"/api/dev/sessions/{session_id}/events?turn_id={urllib.parse.quote(turn_id)}",
            cursor=cursor,
            deadline=deadline,
        )

    def get_turn(self, session_id: str, turn_id: str, timeout: float) -> dict:
        return self.api.request("GET", f"/api/dev/sessions/{session_id}/turns/{turn_id}", timeout=timeout)

    def approvals(self, session_id: str, timeout: float) -> list[dict]:
        return self.api.request("GET", f"/api/dev/sessions/{session_id}/approvals", timeout=timeout).get("approvals", [])

    def changed_files(self, session_id: str, timeout: float) -> list[dict]:
        return self.api.request("GET", f"/api/dev/sessions/{session_id}/changed-files", timeout=timeout).get("files", [])

    def diff(self, session_id: str, timeout: float) -> dict:
        return self.api.request("GET", f"/api/dev/sessions/{session_id}/diff", timeout=timeout)

    def create_review(self, session_id: str, timeout: float) -> dict:
        # Records a review snapshot only; Apply/Reject stay manual in the cockpit.
        return self.api.request("POST", f"/api/dev/sessions/{session_id}/reviews", timeout=timeout)

    def preflight(self, session_id: str, review_id: str, timeout: float) -> dict:
        return self.api.request("GET", f"/api/dev/sessions/{session_id}/preflight?review_id={urllib.parse.quote(review_id)}", timeout=timeout)

    def integrations(self, session_id: str, timeout: float) -> list[dict]:
        return self.api.request("GET", f"/api/dev/sessions/{session_id}/integrations", timeout=timeout).get("integrations", [])


class CodexNativeDevBackend(CurrentOmniRouteDevBackend):
    """Codex Native sessions: the local Codex login, no OmniRoute."""

    backend_id = "codex_native"

    def models(self) -> dict:
        return self.api.request("GET", "/api/dev/models?backend=codex_native")

    def create_session(self, project_id: str, model: str | None) -> dict:
        return self.api.request("POST", "/api/dev/sessions",
                                {"project_id": project_id, "requested_model": model, "backend": "codex_native"})


BACKENDS = {backend.backend_id: backend for backend in (CodexNativeDevBackend, CurrentOmniRouteDevBackend)}


def parse_sse_events(body: str) -> list[dict]:
    events = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
        if data:
            try:
                events.append(json.loads(data))
            except json.JSONDecodeError:
                continue
    return events


def fixture_test_environment(base: dict | None = None) -> dict:
    """Environment for the fixture's pytest run: no inherited options/plugins,
    no bytecode, no plugin autoload. PYTHONPATH is kept so the interpreter can
    still find pytest in unusual setups."""
    env = dict(os.environ if base is None else base)
    for name in PYTEST_ENV_DROP:
        env.pop(name, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


def _tail(path: Path) -> str:
    text = path.read_bytes().decode("utf-8", "replace").strip() if path.exists() else ""
    return text[-OUTPUT_TAIL_CHARS:]


def run_fixture_tests(worktree: Path, timeout: float, *, command: list[str] | None = None) -> dict:
    """Run the fixture's own tests in the session worktree, bounded and diagnosable.

    Output goes to files, not pipes, and stdin is closed, so a stray grandchild
    can neither keep this call waiting past ``timeout`` nor block on input; on a
    timeout the whole process tree is killed. Failures name the command, cwd,
    interpreter, elapsed time and the output tail.
    """
    # Same tree-kill the Codex watchdogs use; the script lives inside the repository.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from nexus.process_timeout import kill_process_tree

    command = command or [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                          "--rootdir", str(worktree), "test_calc.py"]
    where = f"command: {' '.join(command)}\ncwd: {worktree}\npython: {sys.executable}"
    with tempfile.TemporaryDirectory(prefix="nexus-e2e-pytest-", ignore_cleanup_errors=True) as scratch:
        out_path, err_path = Path(scratch) / "stdout.txt", Path(scratch) / "stderr.txt"
        started = time.monotonic()
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            process = subprocess.Popen(command, cwd=worktree, env=fixture_test_environment(),
                                       stdin=subprocess.DEVNULL, stdout=out, stderr=err)
            try:
                returncode = process.wait(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                kill_process_tree(process)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                returncode, timed_out = None, True
        elapsed = round(time.monotonic() - started, 1)
        stdout, stderr = _tail(out_path), _tail(err_path)
    if timed_out:
        raise RuntimeError(
            f"Fixture pytest did not finish within {timeout}s (elapsed {elapsed}s); its process tree was killed.\n"
            f"{where}\npartial stdout:\n{stdout or '(none)'}\npartial stderr:\n{stderr or '(none)'}\n"
            "Run the same command by hand in the worktree to compare; a pending approval or sandbox permission "
            "prompt in the Nexus cockpit can also keep a test run from completing.")
    if returncode:
        raise RuntimeError(f"Fixture pytest failed ({returncode}) after {elapsed}s:\n{where}\n{stdout}\n{stderr}")
    return {"returncode": returncode, "stdout": stdout, "stderr": stderr, "elapsed": elapsed}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def _source_state(path: Path) -> dict:
    """Everything an unapproved run must leave untouched in the source repository."""
    calc = path / "calc.py"
    return {
        "head": _git(path, "rev-parse", "HEAD").strip(),
        "branch": _git(path, "branch", "--show-current").strip(),
        "status": _git(path, "status", "--porcelain"),
        "calc_sha256": hashlib.sha256(calc.read_bytes()).hexdigest() if calc.is_file() else None,
    }


def _backend_for(args) -> DevelopmentSmokeBackend:
    backend_class = BACKENDS[getattr(args, "backend", "codex_native")]
    return backend_class(NexusApi(args.base_url, timeout=args.http_timeout))


def _require_fixture_project(backend: DevelopmentSmokeBackend, args) -> Path:
    project = backend.select_project(args.project_id)
    if project is None:
        raise RuntimeError(f"Fixture project {args.project_id!r} is not registered in Nexus.")
    if not project.get("enabled") or not project.get("dev_enabled"):
        raise RuntimeError("Fixture project must be enabled and explicitly Development-authorized in Nexus.")
    fixture_path = Path(args.fixture).resolve()
    registered_path = Path(project.get("path", "")).resolve()
    if fixture_path != registered_path:
        raise RuntimeError(f"Selected project does not point to this disposable fixture: {registered_path}")
    return fixture_path


def _require_execution_gate(backend: DevelopmentSmokeBackend) -> dict:
    """The server-side gates are the operator's; the harness only reads them."""
    gates = {
        "execution_enabled": bool(backend.execution_status().get("execution_enabled")),
        "integration_enabled": bool(backend.integration_status().get("integration_enabled")),
    }
    if not gates["execution_enabled"]:
        raise RuntimeError("Development execution is disabled on this Nexus (start it with -EnableDevelopment or "
                           "NEXUS_DEV_EXECUTION_ENABLED=1); no session was created.")
    return gates


def check_ready(args, backend: DevelopmentSmokeBackend | None = None) -> dict:
    """Read-only readiness report: no session, no turn, no inference, no confirmation needed."""
    backend = backend or _backend_for(args)
    fixture_path = _require_fixture_project(backend, args)
    gates = _require_execution_gate(backend)
    state = _source_state(fixture_path)
    if state["status"]:
        raise RuntimeError("Fixture source repository must be clean before the smoke; preserve its local changes first.")
    catalog = backend.models() or {}
    model_ids = [row.get("id") for row in catalog.get("models", []) if isinstance(row, dict) and row.get("id")]
    if args.model and model_ids and args.model not in model_ids:
        raise RuntimeError(f"Model {args.model!r} is not offered by this Nexus backend.")
    return {
        "backend": getattr(args, "backend", "codex_native"),
        "project_id": args.project_id,
        "gates": gates,
        "model_count": len(model_ids),
        "catalog_error": catalog.get("catalog_error"),
        "source_clean": True,
        "ready_for_confirmed_smoke": not catalog.get("catalog_error"),
    }


def _note_approvals(backend: DevelopmentSmokeBackend, session_id: str, timeout: float, seen: dict) -> None:
    """Record approvals so the operator can decide them in the cockpit; never decides."""
    try:
        rows = backend.approvals(session_id, timeout)
    except RuntimeError:
        return
    for row in rows:
        approval_id = row.get("approval_id")
        if not approval_id:
            continue
        if approval_id not in seen and row.get("status") == "PENDING":
            print(f"[approval pending] {approval_id} ({row.get('kind')}): decide it in the Nexus Development "
                  "cockpit; the harness never approves or declines.", file=sys.stderr, flush=True)
        seen[approval_id] = {"kind": row.get("kind"), "status": row.get("status")}


def run_smoke(args, backend: DevelopmentSmokeBackend | None = None) -> dict:
    if not args.confirm_agent_call:
        raise RuntimeError("Live smoke can invoke a real agent. Re-run with --confirm-agent-call after confirming cost and execution.")
    backend = backend or _backend_for(args)
    expected_backend = getattr(backend, "backend_id", getattr(args, "backend", "omniroute"))
    fixture_path = _require_fixture_project(backend, args)
    gates = _require_execution_gate(backend)
    source_before = _source_state(fixture_path)
    if source_before["status"]:
        raise RuntimeError("Fixture source repository must be clean before the smoke; preserve its local changes first.")

    session = backend.create_session(args.project_id, args.model)
    session_id = session["session_id"]
    if session.get("backend", "omniroute") != expected_backend:
        raise RuntimeError(f"Nexus created a {session.get('backend')!r} session, expected {expected_backend!r}; "
                           "refusing to run on a different backend.")
    worktree = Path(session["worktree_path"]).resolve()
    if worktree == fixture_path or fixture_path in worktree.parents or worktree in fixture_path.parents:
        raise RuntimeError(f"Nexus returned a worktree inside the source fixture: {worktree}")

    turn = backend.start_turn(session_id, args.prompt, args.model)
    turn_id = turn["turn_id"]
    deadline = time.monotonic() + args.turn_timeout
    observed_events: list[dict] = []
    approvals_seen: dict[str, dict] = {}
    # Sequence 0 asks the current event route to replay from the first buffered
    # event, so a quiet initial stream can reconnect without losing early events.
    cursor = f"{session_id}:{turn_id}:0"
    last_status = turn.get("status")
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            for event_id, event in backend.iter_events(session_id, turn_id, cursor, deadline):
                observed_events.append(event)
                if event_id:
                    cursor = event_id
                if str(event.get("method", "")).endswith("requestApproval"):
                    _note_approvals(backend, session_id, args.http_timeout, approvals_seen)
                if event.get("type") == "nexus/turnEnded":
                    break
        except TimeoutError:
            # A quiet but still-running turn may outlast one socket read. Reconnect
            # from the last event ID; the POST is never repeated.
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        detail = backend.get_turn(session_id, turn_id, min(args.http_timeout, remaining))
        last_status = detail.get("status")
        _note_approvals(backend, session_id, min(args.http_timeout, remaining), approvals_seen)
        if last_status in TERMINAL:
            break
        time.sleep(min(max(args.poll_interval, 0), max(deadline - time.monotonic(), 0)))
    else:
        raise TimeoutError(f"Turn {turn_id} did not finish before {args.turn_timeout}s; inspect it in Nexus before retrying.")
    if last_status != "COMPLETED":
        raise RuntimeError(f"Agent turn ended as {last_status}: {detail.get('error_code')} {detail.get('error_message')}")
    if not observed_events:
        raise RuntimeError("Turn completed, but no SSE events were observed; streaming was not verified.")

    changes = backend.changed_files(session_id, args.http_timeout)
    diff = backend.diff(session_id, args.http_timeout)
    if not changes or not diff.get("files"):
        raise RuntimeError("Completed turn produced no inspectable worktree changes.")
    if not (worktree / "calc.py").is_file() or not (worktree / "test_calc.py").is_file():
        raise RuntimeError("Expected fixture files are missing from the session worktree.")

    test_run = run_fixture_tests(worktree, args.test_timeout)

    # Review: a snapshot of exactly what the human would approve. Apply/Reject
    # are deliberately not part of the harness.
    review = backend.create_review(session_id, args.http_timeout)
    snapshot = review.get("snapshot") or {}
    review_paths = sorted(row.get("path") for row in (snapshot.get("diff") or {}).get("files", []))
    if review.get("status") != "REVIEWED" or not review.get("review_id"):
        raise RuntimeError(f"Integration review was not recorded as REVIEWED: {review.get('status')}")
    if not snapshot.get("approval_ready"):
        raise RuntimeError("The review snapshot is not approval-ready; the change cannot be completely reviewed.")
    if not review_paths or review_paths != sorted(row.get("path") for row in diff["files"]):
        raise RuntimeError("The review snapshot does not match the diff that was inspected.")
    if snapshot.get("base_sha") != session.get("base_sha"):
        raise RuntimeError("The review snapshot is bound to a different base commit than the session.")
    preflight = backend.preflight(session_id, review["review_id"], args.http_timeout)
    history = backend.integrations(session_id, args.http_timeout)
    if any(row.get("result_sha") or row.get("status") in {"APPLIED", "APPLYING"} for row in history):
        raise RuntimeError("An integration was applied for this session; the harness never applies changes.")
    _note_approvals(backend, session_id, args.http_timeout, approvals_seen)

    source_after = _source_state(fixture_path)
    if source_after != source_before:
        raise RuntimeError("Source fixture repository changed during the run; inspect it before continuing.")
    worktree_branch = _git(worktree, "branch", "--show-current").strip()
    return {
        "backend": expected_backend,
        "project_id": args.project_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "turn_status": last_status,
        "gates": gates,
        "sse_events": len(observed_events),
        "sse_event_methods": sorted({event.get("method", event.get("type", "unknown")) for event in observed_events}),
        "approvals": approvals_seen,
        "changed_files": changes,
        "diff_files": diff["files"],
        "fixture_tests": test_run["stdout"],
        "review": {"review_id": review["review_id"], "status": review["status"], "files": review_paths,
                   "snapshot_hash": snapshot.get("snapshot_hash")},
        "preflight": {"ready": bool(preflight.get("ready")),
                      "issues": [issue.get("code") for issue in preflight.get("issues", [])]},
        "fixture_head_unchanged": source_after["head"] == session["base_sha"],
        "source_unchanged": True,
        "worktree_branch": worktree_branch,
        "human_review": "Not approved by runner; inspect the diff in the Nexus Development cockpit.",
        "integration_applied": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", type=Path, help="Create a disposable Git fixture at a new path; existing paths are refused.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--backend", choices=sorted(BACKENDS), default="codex_native",
                        help="Development backend for the session (default: codex_native, no OmniRoute).")
    parser.add_argument("--project-id", help="Existing Nexus registry ID for the disposable fixture.")
    parser.add_argument("--fixture", type=Path, help="Fixture project path registered in Nexus.")
    parser.add_argument("--model", help="Optional model ID offered by the selected backend on the running Nexus server.")
    parser.add_argument("--prompt", default="Fix the intentional bug in calc.add. Keep the change small and run the two tests in test_calc.py.")
    parser.add_argument("--check", action="store_true",
                        help="Read-only readiness report (project, gates, catalog, clean source). No session, no inference.")
    parser.add_argument("--confirm-agent-call", action="store_true",
                        help="Confirm that a real turn may run with the selected backend and incur cost.")
    parser.add_argument("--turn-timeout", type=int, default=900)
    parser.add_argument("--http-timeout", type=float, default=30)
    parser.add_argument("--test-timeout", type=int, default=120)
    parser.add_argument("--poll-interval", type=float, default=2)
    args = parser.parse_args()

    try:
        if args.prepare:
            print(prepare_fixture(args.prepare))
            return 0
        if not args.project_id or not args.fixture:
            parser.error("--project-id and --fixture are required unless using --prepare")
        print(json.dumps(check_ready(args) if args.check else run_smoke(args), indent=2))
        return 0
    except Exception as error:
        print(f"E2E smoke failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
