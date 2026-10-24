"""Prepare a disposable Development fixture and run an opt-in live Nexus smoke.

No OmniRoute calls are made directly: turns go through Nexus's Codex App-Server
development executor. The runner never applies integration or removes data.
"""

from __future__ import annotations

import argparse
import http.client
import json
import shutil
import socket
import subprocess
import sys
import time
from typing import Protocol
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "dev_e2e_project"
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}


def prepare_fixture(destination: Path) -> Path:
    """Copy the known-bug project to a new path and initialize a local Git repo."""
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing fixture path: {destination}")
    shutil.copytree(FIXTURE, destination)
    subprocess.run(["git", "init", "-q"], cwd=destination, check=True)
    subprocess.run(["git", "config", "user.name", "Nexus E2E Fixture"], cwd=destination, check=True)
    subprocess.run(["git", "config", "user.email", "nexus-e2e-fixture@localhost"], cwd=destination, check=True)
    subprocess.run(["git", "add", "calc.py", "test_calc.py"], cwd=destination, check=True)
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
    """Semantic harness seam; P4.0B adapter waits for its published contract.

    These are harness operations, not a claim that Nexus has a native-backend
    HTTP API. Only CurrentOmniRouteDevBackend is implemented today.
    """

    def select_project(self, project_id: str) -> dict: ...
    def create_session(self, project_id: str, model: str | None) -> dict: ...
    def start_turn(self, session_id: str, prompt: str, model: str | None) -> dict: ...
    def iter_events(self, session_id: str, turn_id: str, cursor: str | None, deadline: float): ...
    def get_turn(self, session_id: str, turn_id: str, timeout: float) -> dict: ...
    def changed_files(self, session_id: str, timeout: float) -> list[dict]: ...
    def diff(self, session_id: str, timeout: float) -> dict: ...


class CurrentOmniRouteDevBackend:
    """Adapter for current routes; POST turns uses an OmniRoute-only AppServerClient."""

    def __init__(self, api: NexusApi):
        self.api = api

    def select_project(self, project_id: str) -> dict:
        projects = self.api.request("GET", "/api/projects").get("projects", [])
        return next((row for row in projects if row.get("id") == project_id), None)

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

    def changed_files(self, session_id: str, timeout: float) -> list[dict]:
        return self.api.request("GET", f"/api/dev/sessions/{session_id}/changed-files", timeout=timeout).get("files", [])

    def diff(self, session_id: str, timeout: float) -> dict:
        return self.api.request("GET", f"/api/dev/sessions/{session_id}/diff", timeout=timeout)


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


def run_smoke(args, backend: DevelopmentSmokeBackend | None = None) -> dict:
    if not args.confirm_agent_call:
        raise RuntimeError("Live smoke can invoke a real agent. Re-run with --confirm-agent-call after confirming cost and execution.")
    backend = backend or CurrentOmniRouteDevBackend(NexusApi(args.base_url, timeout=args.http_timeout))
    project = backend.select_project(args.project_id)
    if project is None:
        raise RuntimeError(f"Fixture project {args.project_id!r} is not registered in Nexus.")
    if not project.get("enabled") or not project.get("dev_enabled"):
        raise RuntimeError("Fixture project must be enabled and explicitly Development-authorized in Nexus.")
    fixture_path = Path(args.fixture).resolve()
    registered_path = Path(project.get("path", "")).resolve()
    if fixture_path != registered_path:
        raise RuntimeError(f"Selected project does not point to this disposable fixture: {registered_path}")
    source_head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=fixture_path, capture_output=True, text=True, check=True
    ).stdout.strip()
    source_status_before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=fixture_path, capture_output=True, text=True, check=True
    ).stdout
    if source_status_before:
        raise RuntimeError("Fixture source repository must be clean before the smoke; preserve its local changes first.")

    session = backend.create_session(args.project_id, args.model)
    session_id = session["session_id"]
    worktree = Path(session["worktree_path"]).resolve()
    if worktree == fixture_path or fixture_path in worktree.parents or worktree in fixture_path.parents:
        raise RuntimeError(f"Nexus returned a worktree inside the source fixture: {worktree}")

    turn = backend.start_turn(session_id, args.prompt, args.model)
    turn_id = turn["turn_id"]
    deadline = time.monotonic() + args.turn_timeout
    observed_events: list[dict] = []
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

    test_run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_calc.py"],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=args.test_timeout,
    )
    if test_run.returncode:
        raise RuntimeError(f"Fixture pytest failed ({test_run.returncode}):\n{test_run.stdout}\n{test_run.stderr}")

    source_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=fixture_path, capture_output=True, text=True, check=True).stdout.strip()
    source_status = subprocess.run(["git", "status", "--porcelain"], cwd=fixture_path, capture_output=True, text=True, check=True).stdout
    if source_head != source_head_before or source_status != source_status_before:
        raise RuntimeError("Source fixture repository changed during the run; inspect it before continuing.")
    worktree_branch = subprocess.run(["git", "branch", "--show-current"], cwd=worktree, capture_output=True, text=True, check=True).stdout.strip()
    return {
        "project_id": args.project_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "turn_status": last_status,
        "sse_events": len(observed_events),
        "sse_event_methods": sorted({event.get("method", event.get("type", "unknown")) for event in observed_events}),
        "changed_files": changes,
        "diff_files": diff["files"],
        "fixture_tests": test_run.stdout.strip(),
        "fixture_head_unchanged": source_head == session["base_sha"],
        "worktree_branch": worktree_branch,
        "human_review": "Not approved by runner; inspect the diff in the Nexus Development cockpit.",
        "integration_applied": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", type=Path, help="Create a disposable Git fixture at a new path; existing paths are refused.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--project-id", help="Existing Nexus registry ID for the disposable fixture.")
    parser.add_argument("--fixture", type=Path, help="Fixture project path registered in Nexus.")
    parser.add_argument("--model", help="Optional Codex model ID accepted by the running Nexus server.")
    parser.add_argument("--prompt", default="Fix the intentional bug in calc.add. Keep the change small and run the two tests in test_calc.py.")
    parser.add_argument("--confirm-agent-call", action="store_true", help="Confirm that a real Codex turn may run and incur cost.")
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
        print(json.dumps(run_smoke(args), indent=2))
        return 0
    except Exception as error:
        print(f"E2E smoke failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
