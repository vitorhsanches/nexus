"""Harness Evaluation (F3.4): reproducible behavioral checks for whether
Nexus's own verification machinery relies on real, independently-gathered
evidence rather than trusting an agent's or a stale record's own claim.

Scope, deliberately narrow (see docs/NEXUS_ARCHITECTURE_CONSOLIDATION_PLAN.md
section 4.1, itself informed by
https://github.com/soumatheusgomes/vibe-coding-toolkit/blob/main/docs/07-harness-evaluation.md):
this module does not introduce a new test framework, a new fixture
mechanism, or any paid LLM call. It composes already-existing, already-unit-
tested production functions (nexus.program.acceptance, nexus.dev.worktree,
nexus.dev.executor) into three disposable-fixture scenarios that were not
previously exercised *together* as an end-to-end "evidence over appearance"
story, reusing this repository's own existing test fixtures and helper
functions by import rather than by copy:

  - tests.test_acceptance's `_repo`/`_criterion` (disposable Git repo +
    acceptance-criterion builder).
  - tests.test_dev_executor's `isolated_registry`/`_patch_client` fixtures,
    `_active_session`, `_wait_until`, and `FakeAppServerClient` (isolated
    Dev Mode registry + a fake, non-network App-Server client -- no real
    Codex subprocess, no network, no cost).

Each scenario below states, in its own docstring, exactly which existing
test file already covers the adjacent unit-level behavior, and what new
ground this scenario covers that those did not (so this file is additive,
not a duplicate).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.dev import executor, service
from nexus.dev.worktree import read_worktree_changed_files
from nexus.program import acceptance, bootstrap
from nexus.registry import dev_sessions

from tests.test_acceptance import _criterion, _repo
from tests.test_dev_executor import (  # noqa: F401 -- _patch_client is autouse, reused not copied
    FakeAppServerClient,
    _active_session,
    _patch_client,
    _wait_until,
    isolated_registry,
)


# ---------------------------------------------------------------------------
# Scenario 1 -- independent evidence verification before accepting a claim
# ---------------------------------------------------------------------------
#
# tests/test_acceptance.py already proves evaluate_criteria() mechanics in
# isolation (file_exists/command evaluation, never dropping a criterion,
# fabricating no evidence for UNVERIFIED). What it does not exercise is the
# toolkit's own "stale handoff" case: an external claim (a Worker/Program
# handoff asserting a criterion is done) that disagrees with the real,
# independently-gathered evidence -- and proving the claim has zero
# influence on the result in *either* direction.


def test_a_stale_completion_claim_never_overrides_the_real_repository_state(tmp_path):
    repo = _repo(tmp_path)
    head = bootstrap.git(["rev-parse", "HEAD"], repo).stdout.strip()

    # A handoff/self-report claiming success for a file that was never
    # actually created. This "claim" is intentionally never passed to
    # evaluate_criteria() at all -- only the criterion (what to check) is.
    handoff_claim = {"AC-NEW": "DONE", "note": "added new_feature.py per the request"}
    criteria = [_criterion(id="AC-NEW", description="new_feature.py exists", path="new_feature.py")]

    results = acceptance.evaluate_criteria(str(repo), head, criteria, 30)

    assert handoff_claim["AC-NEW"] == "DONE"  # the claim itself, unchanged -- for contrast below
    assert results[0]["status"] == acceptance.UNSATISFIED, (
        "a handoff's own claim of completion must never be trusted over the real, "
        "independently-checked repository state"
    )
    assert acceptance.gate_passed(results) is False

    # Inverse control: now the file genuinely exists, but the claim is
    # withheld/absent entirely. The evaluator must still independently find
    # it -- proving its SATISFIED verdict is anchored to real state, not to
    # ever having seen a claim at all (positive or negative).
    (repo / "new_feature.py").write_text("def f(): pass\n", encoding="utf-8")
    bootstrap.commit_all(repo, "add new_feature.py")
    new_head = bootstrap.git(["rev-parse", "HEAD"], repo).stdout.strip()
    results_after = acceptance.evaluate_criteria(str(repo), new_head, criteria, 30)
    assert results_after[0]["status"] == acceptance.SATISFIED
    assert acceptance.gate_passed(results_after) is True


# ---------------------------------------------------------------------------
# Scenario 2 -- unauthorized changes by a "read-only"-claimed agent are
# independently detected, never trusted
# ---------------------------------------------------------------------------
#
# tests/test_phase4_3_authority_sandbox.py already proves Nexus *requests*
# the correct sandbox_mode=read-only flag for Scout/Review roles (unit
# level: is the right flag passed). It does not prove what Nexus does if a
# self-report from such an agent claims "no changes were made" -- this
# scenario proves the system never takes that claim at face value and
# always re-derives the real answer from Git itself, using the exact
# function (read_worktree_changed_files) Dev Mode's own Cockpit relies on.


def test_a_self_reported_no_changes_claim_is_independently_verified_against_real_git_state(isolated_registry):
    session = _active_session(isolated_registry)
    worktree = session["worktree_path"]

    # Baseline: nothing changed yet, independently confirmed.
    assert read_worktree_changed_files(worktree) == []

    # An agent that was supposed to run read-only reports success with no
    # changes -- exactly the self-report a compromised or misconfigured
    # "read-only" role might produce.
    agent_self_report = {"role": "scout", "sandbox_requested": "read-only", "changes_made": False}

    # It actually wrote a file anyway (simulating a sandbox bypass or a
    # misclassified role -- the scenario this check exists to catch).
    (Path(worktree) / "unauthorized.txt").write_text("should not be trusted blindly\n", encoding="utf-8")

    real_changes = read_worktree_changed_files(worktree)

    assert agent_self_report["changes_made"] is False  # the claim, unchanged -- for contrast
    assert real_changes == [{"path": "unauthorized.txt", "status": "added"}], (
        "the independent Git-based check must detect an unauthorized change "
        "even when the agent's own self-report claims none occurred"
    )


# ---------------------------------------------------------------------------
# Scenario 3 -- safe recovery after an interruption with a stale record
# ---------------------------------------------------------------------------
#
# tests/test_dev_executor.py already proves one half of this (a dead-owner
# turn is reconciled to FAILED/TURN_OUTCOME_UNKNOWN, never silently marked
# COMPLETED, and the session+worktree survive untouched -- see
# test_recover_orphaned_dev_turns_marks_a_dead_owner_outcome_unknown). The
# toolkit's "stop and resume" case asks for more: that *resuming* actual
# work afterward goes through the full, fresh authorization path again
# (never inherits trust from the dead turn), and that the dead turn's own
# record can never later be reported as if it had succeeded, even after a
# real resumed turn on the same session does succeed.


def test_resuming_after_a_dead_owner_turn_requires_fresh_authorization_and_never_rewrites_the_stale_record(isolated_registry):
    session = _active_session(isolated_registry)
    stuck_turn_id = dev_sessions.create_turn(session["session_id"], "stuck before a crash")
    dev_sessions.update_turn_status(stuck_turn_id, "RUNNING")
    with database.get_connection() as connection:
        connection.execute("UPDATE dev_turns SET owner_pid = 999999999 WHERE id = ?", (stuck_turn_id,))

    # Pause/interruption recovery: the dead-owner turn must be reconciled
    # to an honest "outcome unknown" failure -- not silently resumed, not
    # fabricated as a pass.
    recovered = executor.recover_orphaned_dev_turns()
    assert stuck_turn_id in recovered
    stale = executor.get_turn_detail(session["session_id"], stuck_turn_id)
    assert stale["status"] == "FAILED"
    assert stale["error_code"] == "TURN_OUTCOME_UNKNOWN"

    # Resume: starting real new work on the same session must go through
    # the exact same fresh authorization/worktree verification as any
    # first-time turn -- revoking authorization now must still block it,
    # proving nothing about the dead turn was trusted or carried forward.
    projects.set_dev_enabled("demo", False)
    with pytest.raises(service.DevServiceError) as blocked:
        executor.run_dev_turn(session["session_id"], "resume attempt while revoked")
    assert blocked.value.code == "DEV_MODE_DISABLED"

    # Re-authorize and resume for real -- this must succeed independently,
    # not because of anything the dead turn left behind.
    projects.set_dev_enabled("demo", True)
    result = executor.run_dev_turn(session["session_id"], "resume attempt after reauthorization")
    assert _wait_until(lambda: executor.get_turn_detail(session["session_id"], result["turn_id"])["status"] == "COMPLETED")

    # The stale turn's record must never be retroactively rewritten to
    # look like it succeeded, even after a later turn on the same session
    # legitimately completed.
    still_stale = executor.get_turn_detail(session["session_id"], stuck_turn_id)
    assert still_stale["status"] == "FAILED"
    assert still_stale["error_code"] == "TURN_OUTCOME_UNKNOWN"
