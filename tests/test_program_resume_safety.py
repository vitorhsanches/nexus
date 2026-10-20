"""Phase 4.5: Program resume safety.

Covers the three P1 findings from the resume durability audit:
  1. no live-ownership guard (Program could be resumed while a live
     process still owns it);
  2. a persistence gap between a successful integration and the durable
     save of its run_id/target_revision (verification could run for many
     minutes with nothing on disk to show the merge already happened);
  3. resume/new-phase-work never compared the primary repo's actual HEAD
     against the Program's last known integrated revision.

Reuses tests/test_program.py's FakeGo/FakeVerify/_spec/program_env so
these exercise the same production Program serialization and
_execute/_run_stage control flow as the rest of the Program test suite,
not reimplemented helpers. No real Codex/OmniRoute/network calls.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from nexus.orchestration.ownership import current_host
from nexus.program import runner, state
from tests.test_program import (  # noqa: F401 -- fixtures reused as-is
    FakeGo,
    FakeVerify,
    _spec,
    isolated,
    program_env,
)


def _minimal_program(program_id: str = "PROGRAM-OWNTEST", **overrides) -> dict:
    program = {
        "program_id": program_id,
        "name": "owner-test",
        "project_id": "owner-test",
        "path": "/fake/repo",
        "status": "RUNNING",
        "created_at": state.now_iso(),
        "stop_after_spec": False,
        "spec": None,
        "phases": [],
        "final": None,
        "events": [],
        "task_contract": None,
    }
    program.update(overrides)
    return program


@pytest.fixture
def program_store(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "PROGRAMS_DIR", tmp_path / "programs")
    return tmp_path


def _dead_pid() -> int:
    """A PID that is guaranteed to no longer be running."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


@pytest.fixture
def live_other_pid():
    """A genuinely different, genuinely alive PID (not this test process's
    own -- classify_ownership special-cases owner_pid == current_pid() as
    "current_process", not "live_same_host")."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        yield process.pid
    finally:
        process.kill()
        process.wait()


# ---------------------------------------------------------------------------
# Part 8: ownership persistence
# ---------------------------------------------------------------------------


def test_ownership_round_trips_through_json(program_store):
    program = _minimal_program()
    state.save_program(program)

    claimed = state.acquire_ownership(program["program_id"])
    assert claimed["owner_host"] == current_host()
    assert claimed["owner_pid"] == os.getpid()
    assert claimed["owner_acquired_at"]

    reloaded = state.load_program(program["program_id"])
    assert reloaded["owner_pid"] == os.getpid()
    assert reloaded["owner_host"] == current_host()

    state.release_ownership(reloaded)
    released = state.load_program(program["program_id"])
    assert released["owner_host"] is None
    assert released["owner_pid"] is None
    assert released["owner_acquired_at"] is None


def test_unowned_program_is_freely_claimed(program_store):
    program = _minimal_program()
    state.save_program(program)
    claimed = state.acquire_ownership(program["program_id"])
    assert claimed["owner_pid"] == os.getpid()


def test_reacquiring_your_own_ownership_is_a_noop(program_store):
    program = _minimal_program()
    state.save_program(program)
    first = state.acquire_ownership(program["program_id"])
    second = state.acquire_ownership(program["program_id"])
    assert second["owner_pid"] == first["owner_pid"] == os.getpid()


# ---------------------------------------------------------------------------
# Parts 1, 5, 6: live / stale / cross-host ownership
# ---------------------------------------------------------------------------


def test_live_same_host_owner_blocks_second_execution(program_store, live_other_pid):
    program = _minimal_program(
        owner_host=current_host(), owner_pid=live_other_pid, owner_acquired_at=state.now_iso(),
    )
    state.save_program(program)

    with pytest.raises(state.ProgramOwnershipError) as info:
        state.acquire_ownership(program["program_id"])
    assert info.value.code == "PROGRAM_OWNER_ACTIVE"
    assert info.value.program_id == program["program_id"]

    # No mutation: the blocked attempt must leave the Program exactly as
    # the live owner left it -- no second Manager/Worker was launched.
    untouched = state.load_program(program["program_id"])
    assert untouched["owner_pid"] == live_other_pid


def test_dead_same_host_owner_is_reconciled_and_claim_allowed(program_store):
    program = _minimal_program(
        owner_host=current_host(), owner_pid=_dead_pid(), owner_acquired_at=state.now_iso(),
    )
    state.save_program(program)

    claimed = state.acquire_ownership(program["program_id"])
    assert claimed["owner_pid"] == os.getpid()
    assert claimed["owner_host"] == current_host()


def test_foreign_host_ownership_is_never_assumed_dead(program_store):
    program = _minimal_program(
        owner_host="some-other-machine-xyz", owner_pid=999999, owner_acquired_at=state.now_iso(),
    )
    state.save_program(program)

    with pytest.raises(state.ProgramOwnershipError) as info:
        state.acquire_ownership(program["program_id"])
    assert info.value.code == "PROGRAM_OWNER_FOREIGN"

    untouched = state.load_program(program["program_id"])
    assert untouched["owner_host"] == "some-other-machine-xyz"


def test_unknown_owner_host_also_fails_closed(program_store):
    # owner_pid present but owner_host empty/unknown -- classify_ownership's
    # "unknown" bucket must be treated exactly like a foreign host, never
    # as a green light.
    program = _minimal_program(owner_host="", owner_pid=1, owner_acquired_at=state.now_iso())
    state.save_program(program)

    with pytest.raises(state.ProgramOwnershipError) as info:
        state.acquire_ownership(program["program_id"])
    assert info.value.code == "PROGRAM_OWNER_FOREIGN"


# ---------------------------------------------------------------------------
# Part 5 (real path): resume_program itself respects ownership
# ---------------------------------------------------------------------------


def test_resume_program_blocked_while_live_owner_holds_it(program_env, live_other_pid):
    program = runner.start_program(
        "Concurrent", "reqs", program_env["workspace"], stop_after_spec=True
    )
    # Simulate a still-alive owner (e.g. a first `--resume` still running)
    # by stamping a genuinely different, genuinely alive process as owner.
    loaded = state.load_program(program["program_id"])
    loaded["owner_host"] = current_host()
    loaded["owner_pid"] = live_other_pid
    loaded["owner_acquired_at"] = state.now_iso()
    state.save_program(loaded)

    with pytest.raises(state.ProgramOwnershipError) as info:
        runner.resume_program(program["program_id"])
    assert info.value.code == "PROGRAM_OWNER_ACTIVE"

    # No second Manager/Worker was ever launched.
    assert program_env["go"].missions == []
    still_specified = state.load_program(program["program_id"])
    assert still_specified["status"] == "SPECIFIED"


def test_resume_program_proceeds_once_owner_is_stale(program_env):
    program = runner.start_program(
        "ConcurrentStale", "reqs", program_env["workspace"], stop_after_spec=True
    )
    loaded = state.load_program(program["program_id"])
    loaded["owner_host"] = current_host()
    loaded["owner_pid"] = _dead_pid()
    loaded["owner_acquired_at"] = state.now_iso()
    state.save_program(loaded)

    result = runner.resume_program(program["program_id"])
    assert result["status"] == "COMPLETED"


# ---------------------------------------------------------------------------
# Part 2 / Part 7 (1-3): post-integration persistence gap
# ---------------------------------------------------------------------------


def test_run_id_and_revision_are_durable_before_verification_runs(program_env, monkeypatch):
    """Real path: run_go() succeeds -> the NEXT thing that must happen is a
    durable save, strictly before the (potentially long) run_verification()
    call -- proven by recording call order, not by reading source."""
    calls: list[tuple] = []

    real_save = state.save_program

    def spy_save(program):
        phases = program.get("phases") or []
        if phases:
            phase0 = phases[0]
            calls.append(("save", list(phase0["run_ids"]), phase0["target_revision"]))
        else:
            calls.append(("save", None, None))
        real_save(program)

    real_verify = program_env["verify"]

    def spy_verify(repo, setup, commands, revision=None):
        calls.append(("verify",))
        return real_verify(repo, setup, commands, revision=revision)

    monkeypatch.setattr(runner, "save_program", spy_save)
    monkeypatch.setattr(runner, "run_verification", spy_verify)

    program = runner.start_program("Gap", "reqs", program_env["workspace"])
    assert program["status"] == "COMPLETED"

    first_verify = next(i for i, c in enumerate(calls) if c[0] == "verify")
    save_immediately_before = calls[first_verify - 1]
    assert save_immediately_before[0] == "save"
    assert save_immediately_before[1] == ["RUN-1"], "run_id must already be durable"
    assert save_immediately_before[2], "target_revision must already be durable"


def test_target_revision_survives_a_crash_during_verification(program_env, monkeypatch):
    """Real path: run_go() succeeds (real integration happened) -> a crash
    during verification -> reload from disk -> the run_id and exact
    integrated revision are still present, so a resume never loses track
    of already-merged work."""

    def crashing_verify(*_args, **_kwargs):
        raise RuntimeError("simulated crash during verification")

    monkeypatch.setattr(runner, "run_verification", crashing_verify)

    with pytest.raises(RuntimeError):
        runner.start_program("Crash", "reqs", program_env["workspace"])

    saved = state.list_programs()
    assert len(saved) == 1
    program = saved[0]
    stage = program["phases"][0]
    assert stage["run_ids"] == ["RUN-1"]
    assert stage["target_revision"], "integrated revision must have survived the crash"
    assert stage["status"] == "RUNNING"  # never reached the DONE transition
    # A caught Python exception is a terminal exit, not a hard process death.
    # Ownership is released while the integrated revision remains resumable.
    assert program["owner_pid"] is None


# ---------------------------------------------------------------------------
# Part 3 / Part 4 / Part 7 (4-6): resume revision guard
# ---------------------------------------------------------------------------


def test_fresh_program_first_phase_is_never_falsely_blocked(program_env):
    program = runner.start_program("Fresh", "reqs", program_env["workspace"])
    assert program["status"] == "COMPLETED"
    assert not any(e.get("kind") == "REVISION_MISMATCH" for e in program["events"])


def test_resume_with_nothing_integrated_yet_proceeds_normally(program_env):
    program = runner.start_program(
        "Match", "reqs", program_env["workspace"], stop_after_spec=True
    )
    result = runner.resume_program(program["program_id"])
    assert result["status"] == "COMPLETED"


def test_resume_with_diverged_repository_head_fails_closed(program_env):
    """Real path: load Program -> ownership decision -> revision guard ->
    execution blocked. Phase 1 is fabricated as already-DONE at a revision
    that no longer matches the repo's actual HEAD (an external checkout/
    reset/rebase/amend while Nexus was stopped) -- Phase 2 must refuse to
    start new Worker implementation work rather than silently continuing."""
    program = runner.start_program(
        "Diverge", "reqs", program_env["workspace"], stop_after_spec=True
    )
    program = state.load_program(program["program_id"])
    fabricated_revision = "0" * 40
    program["phases"][0]["status"] = "DONE"
    program["phases"][0]["target_revision"] = fabricated_revision
    program["phases"][0]["last_verification"] = {"passed": True, "head": fabricated_revision, "results": []}
    state.save_program(program)

    with pytest.raises(runner.ProgramBlockedError) as info:
        runner.resume_program(program["program_id"])
    assert "does not match" in str(info.value)
    assert fabricated_revision in str(info.value)

    # No Worker/Manager was ever launched for phase 2.
    assert program_env["go"].missions == []

    blocked = state.load_program(program["program_id"])
    assert blocked["phases"][1]["last_failure_class"] == "ENVIRONMENT_FAILURE"
    assert blocked["status"] == "BLOCKED"
    assert any(e.get("kind") == "REVISION_MISMATCH" for e in blocked["events"])


def test_own_stage_revision_takes_priority_over_predecessor(program_env):
    """Part 4: a phase that already has its OWN target_revision (a prior
    crashed attempt already integrated real code for this exact phase)
    must be checked against THAT revision, not a matching predecessor's --
    proven by making the two values disagree and asserting the phase's own
    (wrong) revision is the one that triggers the mismatch."""
    program = runner.start_program(
        "OwnRevision", "reqs", program_env["workspace"], stop_after_spec=True
    )
    real_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=program["path"], capture_output=True, text=True,
    ).stdout.strip()

    program = state.load_program(program["program_id"])
    # Phase 1: DONE at the real, actual repo revision -- if the guard used
    # this (the predecessor) it would incorrectly pass.
    program["phases"][0]["status"] = "DONE"
    program["phases"][0]["target_revision"] = real_head
    program["phases"][0]["last_verification"] = {"passed": True, "head": real_head, "results": []}
    # Phase 2: already has its OWN (fabricated, wrong) integrated revision
    # from a prior crashed attempt, interrupted before the next save.
    program["phases"][1]["status"] = "BLOCKED"
    program["phases"][1]["target_revision"] = "1" * 40
    state.save_program(program)

    with pytest.raises(runner.ProgramBlockedError) as info:
        runner.resume_program(program["program_id"])

    message = str(info.value)
    assert ("1" * 40) in message  # phase 2's OWN revision was the expected one
    assert real_head in message   # actual HEAD, unchanged
    assert program_env["go"].missions == []
