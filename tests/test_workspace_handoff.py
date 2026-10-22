"""P2B.2A handoff tests with real disposable Git worktrees, no model calls."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nexus.dev.service import DevServiceError, create_dev_session
from nexus.dev.worktree import read_worktree_changed_files, read_worktree_diff
from nexus.registry import database, dev_missions, dev_sessions
from nexus.registry import workspace as panes
from nexus.registry import workspace_handoff as records
from nexus.workspace import handoff, service


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


def _claim_in_process(db_path, handoff_id, queue):
    database.DATABASE_PATH = Path(db_path)
    database.STORAGE_DIR = Path(db_path).parent
    queue.put(records.claim(handoff_id))


def _race_source_start_in_process(db_path, session_id, handoff_id, action, start, queue):
    database.DATABASE_PATH = Path(db_path)
    database.STORAGE_DIR = Path(db_path).parent
    if not start.wait(timeout=10):
        queue.put((action, "TIMEOUT"))
        return
    if action == "claim":
        queue.put((action, records.claim(handoff_id)))
        return
    try:
        if action == "turn":
            execution_id = dev_sessions.create_turn(session_id, "concurrent edit", requested_model="free/model")
        else:
            execution_id = dev_missions.create_mission(
                session_id, "concurrent mission", requested_model="free/model",
                allowed_models=["free/model"], test_profile="unit",
            )
    except records.HandoffBlocked:
        queue.put((action, "BLOCKED"))
    else:
        queue.put((action, execution_id))


def _claim_after_barrier_in_process(db_path, handoff_id, start, queue):
    database.DATABASE_PATH = Path(db_path)
    database.STORAGE_DIR = Path(db_path).parent
    if not start.wait(timeout=10):
        queue.put((handoff_id, "TIMEOUT"))
        return
    queue.put((handoff_id, records.claim(handoff_id)))


def _transfer_until_first_write_in_process(db_path, handoff_id, target_file, signal, release):
    database.DATABASE_PATH = Path(db_path)
    database.STORAGE_DIR = Path(db_path).parent
    original_bytes = handoff._bytes

    def after_first_target_read(path):
        content = original_bytes(path)
        if Path(path) == Path(target_file):
            signal.send(("first_write", None))
            release.wait(timeout=60)
        return content

    handoff._bytes = after_first_target_read
    try:
        handoff.transfer(handoff_id)
    except Exception as error:
        signal.send(("error", repr(error)))
        raise


@pytest.fixture
def env(tmp_path, monkeypatch):
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
    plan_id = panes.create_plan("demo", "handoff", policy_version="maestro-plan/1", complexity="multi",
                                proposal={"agents": []})
    source = create_dev_session("demo")
    target = create_dev_session("demo")
    source_pane = panes.create_pane(project_id="demo", dev_session_id=source["session_id"], origin="maestro",
                                    role="backend", title="Source", model="free/model", plan_id=plan_id,
                                    plan_agent_key="source", brief={"task": "source", "depends_on": []})
    target_pane = panes.create_pane(project_id="demo", dev_session_id=target["session_id"], origin="maestro",
                                    role="tester", title="Target", model="free/model", plan_id=plan_id,
                                    plan_agent_key="target", brief={"task": "target", "depends_on": ["source"]})
    yield {"repo": repo, "base": base, "plan": plan_id, "source": source, "target": target,
           "source_pane": source_pane, "target_pane": target_pane}


def _ready(env):
    session = env["source"]
    mission_id = dev_missions.create_mission(session["session_id"], "edit calc", requested_model="free/model",
                                            allowed_models=["free/model"], test_profile="unit")
    changed = read_worktree_changed_files(session["worktree_path"])
    review = read_worktree_diff(session["worktree_path"], session["base_sha"])
    dev_missions.update_status(mission_id, "READY_FOR_REVIEW", result={"changed_files": changed, "review": review})
    return mission_id


def _preview(env, mission_id):
    return handoff.preview(env["source_pane"], env["target_pane"], mission_id)


def _approve(env, preview):
    return handoff.approve(preview["handoff_id"], preview["snapshot_fingerprint"],
                           env["source_pane"], env["target_pane"])


def test_modified_and_added_files_transfer_only_to_target(env):
    source = env["source"]["worktree_path"]
    target = env["target"]["worktree_path"]
    from pathlib import Path

    (Path(source) / "calc.py").write_text("def multiply(a, b):\n    return a * b\n", encoding="utf-8")
    (Path(source) / "new.txt").write_text("new\n", encoding="utf-8")
    source_before = {path: (Path(source) / path).read_bytes() for path in ("calc.py", "new.txt")}
    mission = _ready(env)
    before = (Path(target) / "calc.py").read_bytes()
    preview = _preview(env, mission)
    assert preview["status"] == "PREVIEWED"
    assert _preview(env, mission)["handoff_id"] == preview["handoff_id"]
    assert (Path(target) / "calc.py").read_bytes() == before
    assert sorted(file["path"] for file in preview["files"]) == ["calc.py", "new.txt"]
    _approve(env, preview)
    result = handoff.transfer(preview["handoff_id"])
    assert result["status"] == "TRANSFERRED"
    assert handoff.transfer(preview["handoff_id"])["status"] == "TRANSFERRED"
    assert (Path(target) / "calc.py").read_bytes() == (Path(source) / "calc.py").read_bytes()
    assert (Path(target) / "new.txt").read_bytes() == (Path(source) / "new.txt").read_bytes()
    for item in preview["files"]:
        assert hashlib.sha256((Path(target) / item["path"]).read_bytes()).hexdigest() == item["sha256"]
    assert {path: (Path(source) / path).read_bytes() for path in source_before} == source_before
    assert {(row["path"], row["status"]) for row in read_worktree_changed_files(target)} == {
        (item["path"], item["status"]) for item in preview["files"]
    }
    assert (env["repo"] / "calc.py").read_bytes() == before
    assert not (env["repo"] / "new.txt").exists()
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=target, text=True).strip() == env["base"]
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=env["repo"], text=True).strip() == env["base"]
    dependency = service.get_pane(env["target_pane"])["dependencies"][0]
    assert dependency["ready"] and dependency["handoff_id"] == preview["handoff_id"]
    # A completed transfer releases the source barrier.
    turn_id = dev_sessions.create_turn(env["source"]["session_id"], "next edit", requested_model="free/model")
    assert dev_sessions.get_turn(turn_id)["status"] == "PENDING"


def test_active_source_turn_blocks_preview_and_approval(env):
    source = Path(env["source"]["worktree_path"])
    (source / "calc.py").write_text("changed\n", encoding="utf-8")
    mission = _ready(env)
    turn_id = dev_sessions.create_turn(env["source"]["session_id"], "active edit", requested_model="free/model")
    with pytest.raises(DevServiceError) as error:
        _preview(env, mission)
    assert error.value.code == "HANDOFF_SOURCE_BUSY"
    dev_sessions.update_turn_status(turn_id, "COMPLETED")
    preview = _preview(env, mission)
    second_turn = dev_sessions.create_turn(env["source"]["session_id"], "another edit", requested_model="free/model")
    with pytest.raises(DevServiceError) as error:
        _approve(env, preview)
    assert error.value.code == "HANDOFF_SOURCE_BUSY"
    assert records.get(preview["handoff_id"])["status"] == "BLOCKED"
    dev_sessions.update_turn_status(second_turn, "COMPLETED")


def test_source_turn_before_transfer_blocks_without_target_write(env):
    source = Path(env["source"]["worktree_path"])
    target = Path(env["target"]["worktree_path"])
    (source / "calc.py").write_text("changed\n", encoding="utf-8")
    preview = _preview(env, _ready(env))
    _approve(env, preview)
    before = (target / "calc.py").read_bytes()
    turn_id = dev_sessions.create_turn(env["source"]["session_id"], "active edit", requested_model="free/model")
    with pytest.raises(DevServiceError) as error:
        handoff.transfer(preview["handoff_id"])
    assert error.value.code == "HANDOFF_SOURCE_BUSY"
    assert (target / "calc.py").read_bytes() == before
    assert records.get(preview["handoff_id"])["status"] == "BLOCKED"
    dev_sessions.update_turn_status(turn_id, "COMPLETED")


def test_claim_checks_source_and_blocks_both_sessions_until_finished(env):
    source = Path(env["source"]["worktree_path"])
    (source / "calc.py").write_text("changed\n", encoding="utf-8")
    preview = _preview(env, _ready(env))
    _approve(env, preview)
    source_id = env["source"]["session_id"]
    target_id = env["target"]["session_id"]
    turn_id = dev_sessions.create_turn(source_id, "active edit", requested_model="free/model")
    assert records.claim(preview["handoff_id"]) is None
    dev_sessions.update_turn_status(turn_id, "COMPLETED")
    mission_id = dev_missions.create_mission(source_id, "active mission", requested_model="free/model",
                                            allowed_models=["free/model"], test_profile="unit")
    assert records.claim(preview["handoff_id"]) is None
    dev_missions.update_status(mission_id, "CANCELLED")
    token = records.claim(preview["handoff_id"])
    assert token is not None
    with pytest.raises(records.HandoffBlocked, match="Source"):
        dev_sessions.create_turn(source_id, "bypass", requested_model="free/model")
    with pytest.raises(records.HandoffBlocked, match="Source"):
        dev_missions.create_mission(source_id, "bypass", requested_model="free/model",
                                    allowed_models=["free/model"], test_profile="unit")
    with pytest.raises(records.HandoffBlocked, match="Target"):
        dev_sessions.create_turn(target_id, "bypass", requested_model="free/model")
    with pytest.raises(records.HandoffBlocked, match="Target"):
        dev_missions.create_mission(target_id, "bypass", requested_model="free/model",
                                    allowed_models=["free/model"], test_profile="unit")
    assert not records.finish(preview["handoff_id"], "old-token", "TRANSFERRED")
    assert records.owns(preview["handoff_id"], token)
    assert records.finish(preview["handoff_id"], token, "RESULT_UNCERTAIN")
    # Uncertain target remains blocked, but source can start a new turn.
    assert dev_sessions.create_turn(source_id, "safe next edit", requested_model="free/model")
    with pytest.raises(records.HandoffBlocked):
        dev_sessions.create_turn(target_id, "still blocked", requested_model="free/model")


@pytest.mark.parametrize("action", ["turn", "mission"])
def test_source_start_races_claim_across_processes(env, action):
    source = Path(env["source"]["worktree_path"])
    (source / "calc.py").write_text("changed\n", encoding="utf-8")
    preview = _preview(env, _ready(env))
    _approve(env, preview)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    queue = context.Queue()
    processes = [context.Process(target=_race_source_start_in_process,
                                 args=(str(database.DATABASE_PATH), env["source"]["session_id"],
                                       preview["handoff_id"], action, start, queue))
                 for action in ("claim", action)]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    results = dict(queue.get(timeout=2) for _ in processes)
    assert results["claim"] != "TIMEOUT" and results[action] != "TIMEOUT"
    if results["claim"] is None:
        assert results[action].startswith("DEVTURN-" if action == "turn" else "DEVMISSION-")
        assert records.get(preview["handoff_id"])["status"] == "APPROVED"
    else:
        assert results[action] == "BLOCKED"
        assert records.owns(preview["handoff_id"], results["claim"])
        assert records.recover_orphaned() == [preview["handoff_id"]]
        assert records.get(preview["handoff_id"])["status"] == "RESULT_UNCERTAIN"
        with pytest.raises(DevServiceError):
            handoff.transfer(preview["handoff_id"])


def test_two_targets_cannot_claim_same_source_concurrently(env):
    source = Path(env["source"]["worktree_path"])
    (source / "calc.py").write_text("changed\n", encoding="utf-8")
    mission = _ready(env)
    other_session = create_dev_session("demo")
    other_pane = panes.create_pane(
        project_id="demo", dev_session_id=other_session["session_id"], origin="maestro",
        role="tester", title="Other target", model="free/model", plan_id=env["plan"],
        plan_agent_key="other", brief={"task": "other", "depends_on": ["source"]},
    )
    first = _preview(env, mission)
    second = handoff.preview(env["source_pane"], other_pane, mission)
    _approve(env, first)
    handoff.approve(second["handoff_id"], second["snapshot_fingerprint"], env["source_pane"], other_pane)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    queue = context.Queue()
    processes = [context.Process(target=_claim_after_barrier_in_process,
                                 args=(str(database.DATABASE_PATH), preview["handoff_id"], start, queue))
                 for preview in (first, second)]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    outcomes = dict(queue.get(timeout=2) for _ in processes)
    assert sum(token not in {None, "TIMEOUT"} for token in outcomes.values()) == 1
    winner = next(handoff_id for handoff_id, token in outcomes.items() if token)
    loser = next(handoff_id for handoff_id, token in outcomes.items() if token is None)
    assert records.get(winner)["status"] == "TRANSFERRING"
    assert records.get(loser)["status"] == "APPROVED"
    assert records.recover_orphaned() == [winner]
    assert records.get(winner)["status"] == "RESULT_UNCERTAIN"
    assert records.claim(loser) is None


def test_dead_transfer_owner_leaves_partial_target_uncertain_without_replay(env):
    source = Path(env["source"]["worktree_path"])
    target = Path(env["target"]["worktree_path"])
    (source / "calc.py").write_text("changed\n", encoding="utf-8")
    (source / "z.txt").write_text("second\n", encoding="utf-8")
    preview = _preview(env, _ready(env))
    _approve(env, preview)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    release = context.Event()
    process = context.Process(target=_transfer_until_first_write_in_process,
                              args=(str(database.DATABASE_PATH), preview["handoff_id"],
                                    str(target / "calc.py"), sender, release))
    process.start()
    sender.close()
    try:
        assert receiver.poll(120), f"transfer did not report a write (exit={process.exitcode})"
        phase, detail = receiver.recv()
        assert phase == "first_write", detail
        assert records.get(preview["handoff_id"])["status"] == "TRANSFERRING"
    finally:
        process.terminate()
        process.join(timeout=20)
        receiver.close()
    assert process.exitcode is not None
    assert records.recover_orphaned() == [preview["handoff_id"]]
    database.initialize_database()  # Reopening the database never replays the write.
    record = records.get(preview["handoff_id"])
    assert record["status"] == "RESULT_UNCERTAIN"
    assert record["error_code"] == "HANDOFF_OWNER_EXITED"
    assert (target / "calc.py").read_bytes() == (source / "calc.py").read_bytes()
    assert not (target / "z.txt").exists()
    with pytest.raises(DevServiceError):
        handoff.transfer(preview["handoff_id"])
    with pytest.raises(records.HandoffBlocked):
        dev_sessions.create_turn(env["target"]["session_id"], "no replay", requested_model="free/model")
    assert dev_sessions.create_turn(env["source"]["session_id"], "after dead owner", requested_model="free/model")


def test_approval_rejects_fingerprint_and_source_drift(env):
    from pathlib import Path

    path = Path(env["source"]["worktree_path"]) / "calc.py"
    path.write_text("changed\n", encoding="utf-8")
    mission = _ready(env)
    preview = _preview(env, mission)
    with pytest.raises(DevServiceError, match="Approval does not match"):
        handoff.approve(preview["handoff_id"], "0" * 64, env["source_pane"], env["target_pane"])
    with pytest.raises(DevServiceError, match="Approval does not match"):
        handoff.approve(preview["handoff_id"], preview["snapshot_fingerprint"],
                        env["target_pane"], env["source_pane"])
    path.write_text("drift\n", encoding="utf-8")
    with pytest.raises(DevServiceError):
        _approve(env, preview)
    assert records.get(preview["handoff_id"])["status"] == "BLOCKED"


def test_edit_after_mission_completion_is_not_mission_evidence(env):
    from pathlib import Path

    path = Path(env["source"]["worktree_path"]) / "calc.py"
    path.write_text("mission version\n", encoding="utf-8")
    mission = _ready(env)
    path.write_text("later manual edit\n", encoding="utf-8")
    with pytest.raises(DevServiceError, match="differs from the review"):
        _preview(env, mission)


def test_target_drift_after_approval_blocks_without_writing(env):
    from pathlib import Path

    (Path(env["source"]["worktree_path"]) / "calc.py").write_text("changed\n", encoding="utf-8")
    preview = _preview(env, _ready(env))
    _approve(env, preview)
    target = Path(env["target"]["worktree_path"]) / "calc.py"
    target.write_text("user work\n", encoding="utf-8")
    with pytest.raises(DevServiceError, match="target worktree"):
        handoff.transfer(preview["handoff_id"])
    assert target.read_text(encoding="utf-8") == "user work\n"
    assert records.get(preview["handoff_id"])["status"] == "BLOCKED"


@pytest.mark.parametrize("name,content", [(".env", b"TOKEN=x\n"), ("raw.bin", b"\0bad"),
                                          ("large.txt", b"a" * (129 * 1024))], ids=["sensitive", "binary", "large"])
def test_unsupported_content_is_rejected(env, name, content):
    from pathlib import Path

    (Path(env["source"]["worktree_path"]) / name).write_bytes(content)
    with pytest.raises(DevServiceError):
        _preview(env, _ready(env))


def test_missing_dependency_and_uncertain_mission_rejected(env):
    from pathlib import Path

    (Path(env["source"]["worktree_path"]) / "calc.py").write_text("changed\n", encoding="utf-8")
    mission = dev_missions.create_mission(env["source"]["session_id"], "edit", requested_model="free/model",
                                         allowed_models=["free/model"], test_profile="unit")
    with pytest.raises(DevServiceError, match="not conclusively ready"):
        _preview(env, mission)
    dev_missions.update_status(mission, "OUTCOME_UNKNOWN")
    with pytest.raises(DevServiceError):
        _preview(env, mission)
    with database.get_connection() as connection:
        connection.execute("UPDATE workspace_panes SET brief_json=? WHERE id=?",
                           (json.dumps({"task": "target", "depends_on": []}), env["target_pane"]))
    with pytest.raises(DevServiceError, match="dependency"):
        _preview(env, mission)


def test_direct_manual_execution_blocked_until_transfer(env):
    from pathlib import Path

    (Path(env["source"]["worktree_path"]) / "calc.py").write_text("changed\n", encoding="utf-8")
    mission = _ready(env)
    target = panes.get_pane(env["target_pane"])
    with pytest.raises(DevServiceError) as error:
        service._check_execution_gates(target, None)
    assert error.value.code == "DEPENDENCY_WORKTREE_NOT_TRANSFERRED"
    with pytest.raises(records.HandoffBlocked):
        dev_missions.create_mission(env["target"]["session_id"], "bypass", requested_model="free/model",
                                    allowed_models=["free/model"], test_profile="unit")
    with pytest.raises(records.HandoffBlocked):
        dev_sessions.create_turn(env["target"]["session_id"], "bypass", requested_model="free/model")
    preview = _preview(env, mission)
    _approve(env, preview)
    handoff.transfer(preview["handoff_id"])
    assert service.get_pane(env["target_pane"])["dependencies"][0]["ready"]


def test_concurrent_claim_and_uncertain_recovery_never_replays(env):
    from pathlib import Path

    (Path(env["source"]["worktree_path"]) / "calc.py").write_text("changed\n", encoding="utf-8")
    preview = _preview(env, _ready(env))
    _approve(env, preview)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens = list(pool.map(records.claim, [preview["handoff_id"]] * 2))
    assert sum(token is not None for token in tokens) == 1
    assert records.get(preview["handoff_id"])["status"] == "TRANSFERRING"
    with pytest.raises(DevServiceError):
        handoff.transfer(preview["handoff_id"])
    with pytest.raises(DevServiceError):
        _preview(env, records.get(preview["handoff_id"])["source_mission_id"])
    token = next(token for token in tokens if token)
    assert records.owns(preview["handoff_id"], token)
    assert not records.owns(preview["handoff_id"], "stale-token")
    assert not records.finish(preview["handoff_id"], "stale-token", "TRANSFERRED")
    assert records.finish(preview["handoff_id"], token, "RESULT_UNCERTAIN")
    assert not records.owns(preview["handoff_id"], token)
    assert not records.target_has_unresolved_transfer("wrong-session")
    assert records.target_has_unresolved_transfer(env["target"]["session_id"])


def test_partial_write_is_uncertain_and_never_replayed(env, monkeypatch):
    from pathlib import Path

    source = Path(env["source"]["worktree_path"])
    target = Path(env["target"]["worktree_path"])
    (source / "calc.py").write_text("changed\n", encoding="utf-8")
    (source / "z.txt").write_text("added\n", encoding="utf-8")
    preview = _preview(env, _ready(env))
    _approve(env, preview)
    real_safe_path = handoff._safe_path
    target_z_checks = 0

    def interrupted(root, path, *, must_exist):
        nonlocal target_z_checks
        if root == target and path == "z.txt":
            target_z_checks += 1
            if target_z_checks == 3:
                raise RuntimeError("simulated crash after first file")
        return real_safe_path(root, path, must_exist=must_exist)

    monkeypatch.setattr(handoff, "_safe_path", interrupted)
    with pytest.raises(RuntimeError, match="simulated crash"):
        handoff.transfer(preview["handoff_id"])
    assert records.get(preview["handoff_id"])["status"] == "RESULT_UNCERTAIN"
    assert (target / "calc.py").read_bytes() == (source / "calc.py").read_bytes()
    assert not (target / "z.txt").exists()
    with pytest.raises(DevServiceError):
        handoff.transfer(preview["handoff_id"])


def test_divergent_base_and_unsafe_paths_fail_closed(env):
    from pathlib import Path

    source = Path(env["source"]["worktree_path"])
    target = Path(env["target"]["worktree_path"])
    for path in ("../outside.txt", "/absolute.txt", ".git/config", ".github/workflow.yml",
                 "config/projects.json", "CON.txt", "bad:name.txt", "x\\y.txt"):
        with pytest.raises(DevServiceError):
            handoff._safe_path(target, path, must_exist=False)
    (source / "calc.py").write_text("changed\n", encoding="utf-8")
    mission = _ready(env)
    (target / "other.txt").write_text("commit\n", encoding="utf-8")
    _git(target, "add", "other.txt")
    _git(target, "-c", "user.email=test@example.invalid", "-c", "user.name=Test", "commit", "-qm", "diverge")
    with pytest.raises(DevServiceError, match="base commit"):
        _preview(env, mission)


def test_symlink_parent_and_hardlink_are_rejected(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "data.txt").write_text("secret", encoding="utf-8")
    try:
        os.link(outside / "data.txt", root / "linked-file.txt")
    except OSError:
        pytest.skip("This filesystem cannot create hardlinks")
    with pytest.raises(DevServiceError, match="hardlinks"):
        handoff._safe_path(root, "linked-file.txt", must_exist=True)
    try:
        os.symlink(outside, root / "linked", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("This Windows account cannot create symlinks")
    with pytest.raises(DevServiceError, match="Symlink"):
        handoff._safe_path(root, "linked/data.txt", must_exist=True)


def test_case_insensitive_collisions_are_rejected(tmp_path):
    seen = set()
    handoff._check_case_collision("Alpha.txt", "added", False, set(), seen)
    with pytest.raises(DevServiceError) as duplicate:
        handoff._check_case_collision("alpha.TXT", "added", False, set(), seen)
    assert duplicate.value.code == "HANDOFF_CASE_COLLISION"
    with pytest.raises(DevServiceError) as target_conflict:
        handoff._check_case_collision("calc.py", "modified", True, {"Calc.py"}, set())
    assert target_conflict.value.code == "HANDOFF_TARGET_CONFLICT"
    (tmp_path / "Alpha.txt").write_text("ignored", encoding="utf-8")
    with pytest.raises(DevServiceError) as ignored_conflict:
        handoff._safe_path(tmp_path, "alpha.txt", must_exist=False)
    assert ignored_conflict.value.code == "HANDOFF_CASE_COLLISION"


def test_two_processes_cannot_claim_same_target(env):
    from pathlib import Path

    (Path(env["source"]["worktree_path"]) / "calc.py").write_text("changed\n", encoding="utf-8")
    preview = _preview(env, _ready(env))
    _approve(env, preview)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [context.Process(target=_claim_in_process,
                                 args=(str(database.DATABASE_PATH), preview["handoff_id"], queue)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    tokens = [queue.get(timeout=2) for _ in processes]
    assert sum(token is not None for token in tokens) == 1
    assert records.recover_orphaned() == [preview["handoff_id"]]
    assert records.get(preview["handoff_id"])["status"] == "RESULT_UNCERTAIN"
    with pytest.raises(DevServiceError):
        handoff.transfer(preview["handoff_id"])
