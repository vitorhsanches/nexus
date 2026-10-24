"""F3.6A -- Automated Integration Safety Smoke.

Acceptance-level, real-repository demonstrations of the four Development
Mode Git integration safety behaviors (nexus/dev/integration.py) that used
to require a human to exercise by hand: a clean Apply, a real integration
conflict, a stale review, and a duplicate Apply attempt. Each scenario
below works against disposable, real `git init` repositories (never a
mock of Git) and never calls a model/App-Server -- the behaviors under
test are deterministic Git plumbing, not inference.

This file intentionally does not duplicate the fine-grained, code-path
level coverage already in tests/test_dev_executor.py (snapshot hashing,
WinError 5 recovery, lock contention, etc.); it instead exercises each of
the four scenarios end-to-end exactly once, with assertions written to
double as human-readable evidence for the mission report, and prints a
compact PASS/BLOCKED/FAIL summary via conftest's terminal summary hook.

Run with: scripts/run_integration_safety_smoke.ps1 (or .py / .sh)
See that script's header for the single-command contract.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import nexus.registry.database as database
import nexus.registry.projects as projects
import pytest
from nexus.dev import feature_gate, integration, integration_gate, service
from nexus.registry import dev_integrations


# -- fixtures: disposable registry + disposable Git repos -----------------


@pytest.fixture
def smoke(tmp_path, monkeypatch):
    """Isolated DB/registry/worktree-root, mirroring the pattern already
    proven in tests/test_dev_executor.py's `isolated_registry`, with the
    Dev integration gate opted in (explicit, same as an operator would)."""
    original_db_path = database.DATABASE_PATH
    original_storage_dir = database.STORAGE_DIR
    original_projects_file = projects.PROJECTS_FILE
    database.STORAGE_DIR = tmp_path / "storage"
    database.DATABASE_PATH = database.STORAGE_DIR / "nexus.db"
    projects.PROJECTS_FILE = tmp_path / "projects.json"
    database.initialize_database()
    monkeypatch.setenv("NEXUS_DEV_WORKTREE_ROOT", str(tmp_path / "dev-worktree-root"))
    monkeypatch.setenv(feature_gate.ENV_VAR, "1")
    monkeypatch.setenv(integration_gate.ENV_VAR, "1")
    try:
        yield tmp_path
    finally:
        for thread in threading.enumerate():
            if thread.name.startswith("nexus-dev-") and thread is not threading.current_thread():
                thread.join(timeout=10)
        database.DATABASE_PATH = original_db_path
        database.STORAGE_DIR = original_storage_dir
        projects.PROJECTS_FILE = original_projects_file


def _git(args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check
    ).stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    _git(["config", "user.email", "f36a-smoke@example.com"], path)
    _git(["config", "user.name", "f36a-smoke"], path)
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", "init"], path)


def _register_project(tmp_path, project_id: str):
    import os

    workspace_path = tmp_path / project_id
    _init_repo(workspace_path)
    resolved = os.path.realpath(str(workspace_path))
    existing = []
    if projects.PROJECTS_FILE.exists():
        existing = json.loads(projects.PROJECTS_FILE.read_text(encoding="utf-8")).get("projects", [])
    existing = [entry for entry in existing if entry["id"] != project_id]
    existing.append({"id": project_id, "name": project_id, "path": resolved, "aliases": [], "enabled": True})
    projects.PROJECTS_FILE.write_text(json.dumps({"projects": existing}), encoding="utf-8")
    projects.sync_projects()
    projects.set_dev_enabled(project_id, True)
    return resolved


def _active_session(tmp_path, project_id: str):
    _register_project(tmp_path, project_id)
    return service.create_dev_session(project_id)


# -- Scenario A: successful Apply ------------------------------------------


def test_scenario_a_successful_apply_end_to_end(smoke):
    """A real worktree edit survives create_review -> preflight -> apply,
    lands as the exact reviewed content in a single new destination
    commit, and the destination is left perfectly clean."""
    session = _active_session(smoke, "scenario-a")
    worktree = Path(session["worktree_path"])
    project_path = projects.get_project("scenario-a")["path"]
    original_head = _git(["rev-parse", "HEAD"], project_path)

    (worktree / "README.md").write_text("approved content for scenario A\n", encoding="utf-8")
    (worktree / "new_file.txt").write_text("brand new file\n", encoding="utf-8")

    review = integration.create_review(session["session_id"])
    preflight = integration.integration_preflight(session["session_id"], review["review_id"])
    assert preflight["ready"] is True, f"preflight unexpectedly blocked: {preflight['issues']}"

    applied = integration.apply_review(session["session_id"], review["review_id"], confirmed=True)

    assert applied["status"] == "APPLIED"
    assert applied["commit_sha"] != original_head
    assert _git(["rev-parse", "HEAD"], project_path) == applied["commit_sha"]
    assert (Path(project_path) / "README.md").read_text(encoding="utf-8") == "approved content for scenario A\n"
    assert (Path(project_path) / "new_file.txt").read_text(encoding="utf-8") == "brand new file\n"
    # Repository integrity: exactly one new commit, parent is the base, no dirt.
    assert _git(["rev-parse", applied["commit_sha"] + "^"], project_path) == original_head
    assert _git(["status", "--porcelain"], project_path) == ""
    history = integration.list_history(session["session_id"])
    assert history[0]["status"] == "APPLIED"
    assert history[0]["result_sha"] == applied["commit_sha"]


# -- Scenario B: real integration conflict ---------------------------------


def test_scenario_b_integration_conflict_is_rejected_and_destination_preserved(smoke):
    """Two incompatible changes diverge from one common base: the reviewed
    worktree change, and an independent commit landed directly on the
    destination. Exercises the real preflight/apply validation path (not a
    mocked Git failure) and distinguishes two distinct, real rejection
    reasons:

    - BASE_MISMATCH / TARGET_MOVED with the SAME file touched on both
      sides (a genuine content conflict) -- the destination's own content
      must survive untouched, byte for byte.
    - TARGET_MOVED with a DIFFERENT, non-overlapping file touched on the
      destination (a plain HEAD advance, no content overlap) -- also
      rejected, proving the system never silently merges just because the
      changes don't textually overlap. Nexus integration is fast-forward
      -only by design (see nexus/dev/integration.py's module docstring);
      it does not attempt a 3-way content merge, so both cases are
      rejected the same safe way, and this test asserts that explicitly
      rather than assuming it.

    Note on the exact surfaced code: the review snapshot's hash binds the
    destination's target_head/target_branch into the same hash used for
    SNAPSHOT_STALE, so once the destination has moved, both SNAPSHOT_STALE
    and TARGET_MOVED/BASE_MISMATCH are independently true; preflight
    reports every applicable issue, while apply_review() fails closed on
    whichever issue it detects first (SNAPSHOT_STALE, by check order).
    This test verifies both: the full preflight issue set always contains
    the HEAD-divergence code, and apply_review() still refuses to touch
    the destination regardless of which single code it reports.
    """
    # -- sub-case 1: destination diverges on the SAME file (content conflict)
    session = _active_session(smoke, "scenario-b1")
    worktree = Path(session["worktree_path"])
    project_path = projects.get_project("scenario-b1")["path"]
    base_sha = session["base_sha"]

    (worktree / "README.md").write_text("reviewed branch content\n", encoding="utf-8")
    review = integration.create_review(session["session_id"])

    # Independent, incompatible commit lands directly on the destination,
    # touching the very same file the review also changed.
    (Path(project_path) / "README.md").write_text("destination-side content\n", encoding="utf-8")
    _git(["add", "README.md"], project_path)
    _git(["commit", "-q", "-m", "incompatible destination-side edit"], project_path)
    conflicting_head = _git(["rev-parse", "HEAD"], project_path)
    assert conflicting_head != base_sha

    preflight = integration.integration_preflight(session["session_id"], review["review_id"])
    assert preflight["ready"] is False
    codes = {issue["code"] for issue in preflight["issues"]}
    assert codes & {"TARGET_MOVED", "BASE_MISMATCH"}, f"expected a HEAD-divergence rejection, got {codes}"

    with pytest.raises(service.DevServiceError) as error:
        integration.apply_review(session["session_id"], review["review_id"], confirmed=True)
    assert error.value.code in {"TARGET_MOVED", "BASE_MISMATCH", "SNAPSHOT_STALE"}

    # Destination keeps exactly its own content -- no partial merge, no
    # conflict markers, no mutation of any kind.
    assert _git(["rev-parse", "HEAD"], project_path) == conflicting_head
    assert (Path(project_path) / "README.md").read_text(encoding="utf-8") == "destination-side content\n"
    assert "<<<<<<<" not in (Path(project_path) / "README.md").read_text(encoding="utf-8")
    assert _git(["status", "--porcelain"], project_path) == ""

    # -- sub-case 2: destination diverges on a DIFFERENT, non-overlapping file
    session2 = _active_session(smoke, "scenario-b2")
    worktree2 = Path(session2["worktree_path"])
    project_path2 = projects.get_project("scenario-b2")["path"]
    base_sha2 = session2["base_sha"]

    (worktree2 / "README.md").write_text("reviewed branch content\n", encoding="utf-8")
    review2 = integration.create_review(session2["session_id"])

    (Path(project_path2) / "unrelated.txt").write_text("unrelated destination commit\n", encoding="utf-8")
    _git(["add", "unrelated.txt"], project_path2)
    _git(["commit", "-q", "-m", "unrelated destination-side commit, no overlap"], project_path2)
    advanced_head = _git(["rev-parse", "HEAD"], project_path2)
    assert advanced_head != base_sha2

    preflight2 = integration.integration_preflight(session2["session_id"], review2["review_id"])
    assert preflight2["ready"] is False
    codes2 = {issue["code"] for issue in preflight2["issues"]}
    assert codes2 & {"TARGET_MOVED", "BASE_MISMATCH"}, f"expected a HEAD-divergence rejection, got {codes2}"

    with pytest.raises(service.DevServiceError) as error2:
        integration.apply_review(session2["session_id"], review2["review_id"], confirmed=True)
    assert error2.value.code in {"TARGET_MOVED", "BASE_MISMATCH", "SNAPSHOT_STALE"}

    assert _git(["rev-parse", "HEAD"], project_path2) == advanced_head
    assert (Path(project_path2) / "unrelated.txt").read_text(encoding="utf-8") == "unrelated destination commit\n"
    assert not (Path(project_path2) / "README.md").exists() or \
        (Path(project_path2) / "README.md").read_text(encoding="utf-8") == "baseline\n"
    assert _git(["status", "--porcelain"], project_path2) == ""


# -- Scenario C: stale review -----------------------------------------------


def test_scenario_c_stale_review_is_blocked_before_any_commit(smoke):
    """A review snapshot is taken, then the worktree content it describes
    changes again before Apply. The stale review must never be applicable
    -- not even once -- and the destination must receive zero commits."""
    session = _active_session(smoke, "scenario-c")
    worktree = Path(session["worktree_path"])
    project_path = projects.get_project("scenario-c")["path"]
    original_head = _git(["rev-parse", "HEAD"], project_path)

    (worktree / "README.md").write_text("version one, reviewed\n", encoding="utf-8")
    stale_review = integration.create_review(session["session_id"])

    # Content changes again after the review snapshot was taken -- the old
    # review_id now describes worktree content that no longer exists.
    (worktree / "README.md").write_text("version two, never reviewed\n", encoding="utf-8")

    preflight = integration.integration_preflight(session["session_id"], stale_review["review_id"])
    assert preflight["ready"] is False
    assert any(issue["code"] == "SNAPSHOT_STALE" for issue in preflight["issues"])

    with pytest.raises(service.DevServiceError) as error:
        integration.apply_review(session["session_id"], stale_review["review_id"], confirmed=True)
    assert error.value.code == "SNAPSHOT_STALE"

    # No commit of any kind was created at the destination.
    assert _git(["rev-parse", "HEAD"], project_path) == original_head
    assert _git(["status", "--porcelain"], project_path) == ""
    assert not (Path(project_path) / "README.md").exists() or \
        (Path(project_path) / "README.md").read_text(encoding="utf-8") == "baseline\n"

    # A fresh review of the current (version two) content is unaffected
    # and remains genuinely applicable -- staleness is per-review, not a
    # blanket lockout of the session.
    fresh_review = integration.create_review(session["session_id"])
    fresh_preflight = integration.integration_preflight(session["session_id"], fresh_review["review_id"])
    assert fresh_preflight["ready"] is True


# -- Scenario D: duplicate Apply ---------------------------------------------


def test_scenario_d_duplicate_apply_is_rejected_idempotently(smoke):
    """A successful Apply, immediately repeated with the identical
    review_id, must never create a second commit and must fail with a
    stable, documented contract code rather than silently no-op-ing or
    corrupting history."""
    session = _active_session(smoke, "scenario-d")
    worktree = Path(session["worktree_path"])
    project_path = projects.get_project("scenario-d")["path"]

    (worktree / "README.md").write_text("applied exactly once\n", encoding="utf-8")
    review = integration.create_review(session["session_id"])

    first = integration.apply_review(session["session_id"], review["review_id"], confirmed=True)
    assert first["status"] == "APPLIED"
    head_after_first = _git(["rev-parse", "HEAD"], project_path)
    assert head_after_first == first["commit_sha"]
    log_after_first = _git(["log", "--oneline"], project_path)

    # Repeat with the exact same review_id -- the documented idempotency
    # contract is an explicit error, not a silent success and not a
    # second integration commit.
    with pytest.raises(service.DevServiceError) as error:
        integration.apply_review(session["session_id"], review["review_id"], confirmed=True)
    assert error.value.code == "APPLY_ALREADY_ATTEMPTED"

    assert _git(["rev-parse", "HEAD"], project_path) == head_after_first
    assert _git(["log", "--oneline"], project_path) == log_after_first
    assert _git(["status", "--porcelain"], project_path) == ""

    history = integration.list_history(session["session_id"])
    applied_records = [row for row in history if row["status"] == "APPLIED"]
    assert len(applied_records) == 1
    assert applied_records[0]["result_sha"] == first["commit_sha"]

    # The project lock taken for the first (successful) Apply must have
    # been released, not leaked -- a later, unrelated review for the same
    # project must not be blocked by this already-finished attempt.
    assert dev_integrations.get_project_lock(session["project_id"]) is None
