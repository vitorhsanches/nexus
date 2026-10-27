"""P5.3A: Codex Native source evidence through handoff.py, with real
disposable Git worktrees -- no model calls. Mirrors test_workspace_handoff.py's
fixture style exactly, but the source session is backend='codex_native' and
its evidence is a nexus.dev.integration review instead of a mission.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from nexus.dev.service import DevServiceError, create_dev_session
from nexus.dev.worktree import read_worktree_diff
from nexus.registry import database, dev_integrations
from nexus.registry import workspace as panes
from nexus.workspace import handoff


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


@pytest.fixture
def native_env(tmp_path, monkeypatch):
    db_dir = tmp_path / "storage"
    monkeypatch.setattr(database, "STORAGE_DIR", db_dir)
    monkeypatch.setattr(database, "DATABASE_PATH", db_dir / "nexus.db")
    monkeypatch.setenv("NEXUS_DEV_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    database.initialize_database()
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    with database.get_connection() as connection:
        connection.execute(
            "INSERT INTO projects (id, name, path, enabled, dev_enabled) VALUES ('demo', 'Demo', ?, 1, 1)",
            (str(repo),),
        )
    plan_id = panes.create_plan("demo", "native handoff", policy_version="maestro-plan/1", complexity="multi",
                                proposal={"agents": []})
    source = create_dev_session("demo", backend="codex_native")
    target = create_dev_session("demo")
    source_pane = panes.create_pane(project_id="demo", dev_session_id=source["session_id"], origin="maestro",
                                    role="backend", title="Source", model="gpt-5-codex", plan_id=plan_id,
                                    plan_agent_key="source", brief={"task": "source", "depends_on": []})
    target_pane = panes.create_pane(project_id="demo", dev_session_id=target["session_id"], origin="maestro",
                                    role="tester", title="Target", model="free/model", plan_id=plan_id,
                                    plan_agent_key="target", brief={"task": "target", "depends_on": ["source"]})
    yield {"repo": repo, "base": base, "plan": plan_id, "source": source, "target": target,
           "source_pane": source_pane, "target_pane": target_pane}


def _review_id(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _ready(env):
    """Build a review from the source worktree's real, current diff --
    exactly what nexus.dev.integration.create_review does, without needing
    the source session's project-authorization path re-verified here."""
    session = env["source"]
    diff = read_worktree_diff(session["worktree_path"], session["base_sha"])
    snapshot = {"session_id": session["session_id"], "project_id": "demo",
                "base_sha": session["base_sha"], "target_head": env["base"], "target_branch": "master",
                "diff": diff, "approval_ready": bool(diff.get("approval_ready"))}
    review_id = _review_id(f"{session['session_id']}-{len(dev_integrations.list_reviews(session['session_id']))}")
    dev_integrations.save_review(
        review_id=review_id, dev_session_id=session["session_id"], project_id="demo",
        base_sha=session["base_sha"], target_head=env["base"], target_branch="master", snapshot=snapshot,
    )
    return review_id


def _preview(env, review_id):
    return handoff.preview(env["source_pane"], env["target_pane"], review_id)


def _approve(env, preview):
    return handoff.approve(preview["handoff_id"], preview["snapshot_fingerprint"],
                           env["source_pane"], env["target_pane"])


def _transfer_sequence(env, review_id):
    preview = _preview(env, review_id)
    _approve(env, preview)
    return handoff.transfer(preview["handoff_id"])


# -- identity --------------------------------------------------------------


def test_native_preview_succeeds_with_a_valid_review(native_env):
    (Path(native_env["source"]["worktree_path"]) / "calc.py").write_text(
        "def add(a, b):\n    return a + b + 1\n", encoding="utf-8",
    )
    review_id = _ready(native_env)
    preview = _preview(native_env, review_id)
    assert preview["status"] == "PREVIEWED"
    assert preview["source_backend"] == "codex_native"
    assert preview["source_native_review_id"] == review_id
    assert preview["source_mission_id"] is None
    assert [item["path"] for item in preview["files"]] == ["calc.py"]


def test_native_preview_rejects_a_review_from_another_session(native_env):
    (Path(native_env["source"]["worktree_path"]) / "calc.py").write_text(
        "def add(a, b):\n    return a + b + 1\n", encoding="utf-8",
    )
    other = create_dev_session("demo", backend="codex_native")
    foreign_review = _review_id("foreign")
    dev_integrations.save_review(
        review_id=foreign_review, dev_session_id=other["session_id"], project_id="demo",
        base_sha=other["base_sha"], target_head=native_env["base"], target_branch="master",
        snapshot={"diff": {"files": [], "truncated": False, "review_id": "fp"}, "approval_ready": True},
    )
    with pytest.raises(DevServiceError) as error:
        _preview(native_env, foreign_review)
    assert error.value.code == "HANDOFF_REVIEW_MISMATCH"


def test_native_preview_rejects_a_superseded_review(native_env):
    (Path(native_env["source"]["worktree_path"]) / "calc.py").write_text(
        "def add(a, b):\n    return a + b + 1\n", encoding="utf-8",
    )
    stale_review = _ready(native_env)
    (Path(native_env["source"]["worktree_path"]) / "calc.py").write_text(
        "def add(a, b):\n    return a + b + 2\n", encoding="utf-8",
    )
    _ready(native_env)  # a newer review now exists for the same session
    with pytest.raises(DevServiceError) as error:
        _preview(native_env, stale_review)
    assert error.value.code == "HANDOFF_REVIEW_STALE"


def test_native_preview_rejects_a_rejected_review(native_env):
    (Path(native_env["source"]["worktree_path"]) / "calc.py").write_text(
        "def add(a, b):\n    return a + b + 1\n", encoding="utf-8",
    )
    review_id = _ready(native_env)
    dev_integrations.update_review(review_id, status="REJECTED")
    with pytest.raises(DevServiceError) as error:
        _preview(native_env, review_id)
    assert error.value.code == "HANDOFF_REVIEW_NOT_READY"


def test_native_preview_rejects_an_applied_review(native_env):
    (Path(native_env["source"]["worktree_path"]) / "calc.py").write_text(
        "def add(a, b):\n    return a + b + 1\n", encoding="utf-8",
    )
    review_id = _ready(native_env)
    dev_integrations.update_review(review_id, status="APPLIED", result_sha="deadbeef")
    with pytest.raises(DevServiceError) as error:
        _preview(native_env, review_id)
    assert error.value.code == "HANDOFF_REVIEW_NOT_READY"


def test_native_preview_rejects_an_incomplete_snapshot(native_env):
    (Path(native_env["source"]["worktree_path"]) / "calc.py").write_text(
        "def add(a, b):\n    return a + b + 1\n", encoding="utf-8",
    )
    session = native_env["source"]
    diff = read_worktree_diff(session["worktree_path"], session["base_sha"])
    review_id = _review_id("incomplete")
    dev_integrations.save_review(
        review_id=review_id, dev_session_id=session["session_id"], project_id="demo",
        base_sha=session["base_sha"], target_head=native_env["base"], target_branch="master",
        snapshot={"diff": diff, "approval_ready": False},
    )
    with pytest.raises(DevServiceError) as error:
        _preview(native_env, review_id)
    assert error.value.code == "HANDOFF_REVIEW_EVIDENCE"


def test_diff_fingerprint_is_not_a_valid_review_id(native_env):
    """native_review_id (the persisted record's primary key) and
    diff.review_id (a content fingerprint) must never be interchangeable."""
    (Path(native_env["source"]["worktree_path"]) / "calc.py").write_text(
        "def add(a, b):\n    return a + b + 1\n", encoding="utf-8",
    )
    session = native_env["source"]
    diff = read_worktree_diff(session["worktree_path"], session["base_sha"])
    with pytest.raises(DevServiceError) as error:
        _preview(native_env, diff["review_id"])
    assert error.value.code in {"REVIEW_ID_INVALID", "HANDOFF_REVIEW_MISMATCH"}


# -- snapshot / drift -------------------------------------------------------


def test_native_handoff_transfers_added_and_modified_files(native_env):
    source = native_env["source"]["worktree_path"]
    target = native_env["target"]["worktree_path"]
    (Path(source) / "calc.py").write_text("def multiply(a, b):\n    return a * b\n", encoding="utf-8")
    (Path(source) / "new.txt").write_text("new\n", encoding="utf-8")
    review_id = _ready(native_env)
    result = _transfer_sequence(native_env, review_id)
    assert result["status"] == "TRANSFERRED"
    assert (Path(target) / "calc.py").read_bytes() == (Path(source) / "calc.py").read_bytes()
    assert (Path(target) / "new.txt").read_bytes() == (Path(source) / "new.txt").read_bytes()
    for item in result["files"]:
        assert hashlib.sha256((Path(target) / item["path"]).read_bytes()).hexdigest() == item["sha256"]
    assert result["source_backend"] == "codex_native"
    assert result["source_native_review_id"] == review_id


def test_native_handoff_rejects_a_deleted_file(native_env):
    source = native_env["source"]["worktree_path"]
    (Path(source) / "calc.py").unlink()
    _git(source, "add", "-A")
    review_id = _ready(native_env)
    with pytest.raises(DevServiceError) as error:
        _preview(native_env, review_id)
    assert error.value.code == "HANDOFF_UNSUPPORTED_FILE"


def test_native_handoff_rejects_drift_after_preview(native_env):
    source = native_env["source"]["worktree_path"]
    (Path(source) / "calc.py").write_text("def multiply(a, b):\n    return a * b\n", encoding="utf-8")
    review_id = _ready(native_env)
    preview = _preview(native_env, review_id)
    (Path(source) / "calc.py").write_text("def multiply(a, b):\n    return a * b  # changed\n", encoding="utf-8")
    with pytest.raises(DevServiceError) as error:
        _approve(native_env, preview)
    assert error.value.code == "HANDOFF_SOURCE_DRIFT"


def test_native_handoff_duplicate_transfer_is_idempotent(native_env):
    source = native_env["source"]["worktree_path"]
    (Path(source) / "calc.py").write_text("def multiply(a, b):\n    return a * b\n", encoding="utf-8")
    review_id = _ready(native_env)
    preview = _preview(native_env, review_id)
    _approve(native_env, preview)
    first = handoff.transfer(preview["handoff_id"])
    second = handoff.transfer(preview["handoff_id"])
    assert first["status"] == second["status"] == "TRANSFERRED"
    assert first["finished_at"] == second["finished_at"]


def test_native_handoff_approve_rejects_wrong_fingerprint(native_env):
    source = native_env["source"]["worktree_path"]
    (Path(source) / "calc.py").write_text("def multiply(a, b):\n    return a * b\n", encoding="utf-8")
    review_id = _ready(native_env)
    preview = _preview(native_env, review_id)
    with pytest.raises(DevServiceError) as error:
        handoff.approve(preview["handoff_id"], "0" * 64, native_env["source_pane"], native_env["target_pane"])
    assert error.value.code == "HANDOFF_APPROVAL_MISMATCH"


def test_native_preview_never_writes_to_the_target_worktree(native_env):
    source = native_env["source"]["worktree_path"]
    target = native_env["target"]["worktree_path"]
    (Path(source) / "calc.py").write_text("def multiply(a, b):\n    return a * b\n", encoding="utf-8")
    before = (Path(target) / "calc.py").read_bytes()
    review_id = _ready(native_env)
    _preview(native_env, review_id)
    assert (Path(target) / "calc.py").read_bytes() == before


def test_native_dependency_is_resolvable_end_to_end(native_env):
    """handoff.verified_dependency's native branch, independent of the
    coordinator (which still refuses a native predecessor -- see
    test_workspace_coordinator.py's NATIVE_HANDOFF_NOT_SUPPORTED tests).
    This proves the lower-level machinery itself is correct."""
    source = native_env["source"]["worktree_path"]
    (Path(source) / "calc.py").write_text("def multiply(a, b):\n    return a * b\n", encoding="utf-8")
    review_id = _ready(native_env)
    _transfer_sequence(native_env, review_id)
    source_step = {"pane_id": native_env["source_pane"], "agent_key": "source",
                   "status": "READY_FOR_REVIEW", "error_code": None,
                   "mission_id": None, "native_review_id": review_id}
    target_step = {"pane_id": native_env["target_pane"], "agent_key": "target"}
    record = handoff.verified_dependency(native_env["plan"], source_step, target_step)
    assert record["status"] == "TRANSFERRED"
    assert record["source_backend"] == "codex_native"


def test_native_dependency_fails_closed_without_a_transfer(native_env):
    source = native_env["source"]["worktree_path"]
    (Path(source) / "calc.py").write_text("def multiply(a, b):\n    return a * b\n", encoding="utf-8")
    review_id = _ready(native_env)
    source_step = {"pane_id": native_env["source_pane"], "agent_key": "source",
                   "status": "READY_FOR_REVIEW", "error_code": None,
                   "mission_id": None, "native_review_id": review_id}
    target_step = {"pane_id": native_env["target_pane"], "agent_key": "target"}
    with pytest.raises(DevServiceError) as error:
        handoff.verified_dependency(native_env["plan"], source_step, target_step)
    assert error.value.code == "DEPENDENCY_WORKTREE_NOT_TRANSFERRED"
