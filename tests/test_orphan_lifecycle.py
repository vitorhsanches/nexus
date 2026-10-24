"""Tests for the NEXUS orphan lifecycle recovery feature.

Tests A-O as specified. Each test uses an isolated temporary SQLite database;
none of them touch the real nexus.db.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import nexus.registry.database as database
import nexus.registry.projects as projects

from nexus.orchestration.ownership import (
    classify_ownership,
    ensure_ownership_columns,
    get_process_created_at,
)
from nexus.orchestration.recovery import (
    reconcile_orphan_run,
    reconcile_stale_ownership,
    recover_all,
)
from nexus.registry.agents import create_agent, list_agents_for_run
from nexus.registry.checkpoints import list_checkpoints
from nexus.registry.database import get_connection
from nexus.registry.runs import create_run, get_run


FIXTURE_PROJECTS = {
    "projects": [
        {
            "id": "norte",
            "name": "Norte",
            "path": r"C:\fake\interface-life",
            "aliases": ["norte"],
            "enabled": True,
        }
    ]
}

# A PID that is almost certainly dead (PID 1 on Linux is always init/systemd,
# but we want a PID that is DEAD; use a very large number instead).
DEAD_PID = 999_999_999
DEAD_HOST = "test-host"
# Timestamp well before 'now'; any live process was started after this.
ANCIENT_ACQUIRED_AT = "2000-01-01T00:00:00+00:00"


class OrphanLifecycleTestCase(unittest.TestCase):
    """Base class: set up an isolated temp DB before each test."""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp_dir.name)

        self._original_db_path = database.DATABASE_PATH
        self._original_storage_dir = database.STORAGE_DIR
        self._original_projects_file = projects.PROJECTS_FILE

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus-test.db"

        fixture_path = tmp_path / "projects.json"
        fixture_path.write_text(json.dumps(FIXTURE_PROJECTS), encoding="utf-8")
        projects.PROJECTS_FILE = fixture_path

        database.initialize_database()
        projects.sync_projects()
        ensure_ownership_columns()

    def tearDown(self):
        database.DATABASE_PATH = self._original_db_path
        database.STORAGE_DIR = self._original_storage_dir
        projects.PROJECTS_FILE = self._original_projects_file
        self._tmp_dir.cleanup()

    # ------------------------------------------------------------------ helpers

    def _make_run(self, status="ROUTING"):
        return create_run(
            project_id="norte",
            input_text="test request",
            intent="GO",
            status=status,
        )

    def _set_owner(self, run_id, host, pid, acquired_at=ANCIENT_ACQUIRED_AT):
        with get_connection() as conn:
            conn.execute(
                "UPDATE runs SET owner_host=?, owner_pid=?, owner_acquired_at=? WHERE id=?",
                (host, pid, acquired_at, run_id),
            )
            conn.commit()

    def _run_status(self, run_id):
        with get_connection() as conn:
            row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        return row["status"] if row else None

    def _agent_statuses(self, run_id):
        agents = list_agents_for_run(run_id)
        return {a["id"]: a["status"] for a in agents}

    def _checkpoints_for(self, run_id):
        return list_checkpoints(run_id)

    def _has_orphan_checkpoint(self, run_id):
        cps = self._checkpoints_for(run_id)
        return any(c["boundary"] == "LIFECYCLE_ORPHAN_DETECTED" for c in cps)


# ==============================================================================
# A: ROUTING run + Manager RUNNING + dead owner → orphan detected
# ==============================================================================
class TestA_RoutingRunDeadOwner(OrphanLifecycleTestCase):
    def test_orphan_detected_run_and_agent_failed_checkpoint_written(self):
        run_id = self._make_run(status="ROUTING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)
        agent_id = create_agent(run_id, role="Manager", provider="p", model="m", status="RUNNING")

        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=False),
        ):
            reconciled = reconcile_stale_ownership()

        self.assertIn(run_id, reconciled)
        self.assertEqual(self._run_status(run_id), "FAILED")
        self.assertEqual(self._agent_statuses(run_id)[agent_id], "FAILED")
        self.assertTrue(self._has_orphan_checkpoint(run_id))


# ==============================================================================
# B: Active process really alive (os.getpid()) → run untouched
# ==============================================================================
class TestB_LiveProcess(OrphanLifecycleTestCase):
    def test_live_process_run_is_not_reconciled(self):
        run_id = self._make_run(status="ROUTING")
        self._set_owner(run_id, DEAD_HOST, os.getpid())

        with patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST):
            reconciled = reconcile_stale_ownership()

        self.assertNotIn(run_id, reconciled)
        self.assertEqual(self._run_status(run_id), "ROUTING")


# ==============================================================================
# C: Terminal run (COMPLETED) → untouched
# ==============================================================================
class TestC_TerminalRunUntouched(OrphanLifecycleTestCase):
    def test_completed_run_is_not_reconciled(self):
        run_id = self._make_run(status="COMPLETED")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)

        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=False),
        ):
            reconciled = reconcile_stale_ownership()

        self.assertNotIn(run_id, reconciled)
        self.assertEqual(self._run_status(run_id), "COMPLETED")


# ==============================================================================
# D: Terminal agent (COMPLETED) → untouched
# ==============================================================================
class TestD_TerminalAgentUntouched(OrphanLifecycleTestCase):
    def test_completed_agent_is_not_touched_during_reconciliation(self):
        run_id = self._make_run(status="ROUTING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)
        agent_id_done = create_agent(run_id, role="Manager", provider="p", model="m", status="COMPLETED")
        agent_id_live = create_agent(run_id, role="Worker", provider="p", model="m", status="RUNNING")

        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=False),
        ):
            reconciled = reconcile_stale_ownership()

        self.assertIn(run_id, reconciled)
        statuses = self._agent_statuses(run_id)
        self.assertEqual(statuses[agent_id_done], "COMPLETED")  # untouched
        self.assertEqual(statuses[agent_id_live], "FAILED")     # reconciled


# ==============================================================================
# E: Multiple non-terminal agents (RUNNING + QUEUED + REVIEWING) → all reconciled
# ==============================================================================
class TestE_MultipleNonTerminalAgents(OrphanLifecycleTestCase):
    def test_all_active_agent_statuses_are_reconciled(self):
        run_id = self._make_run(status="RUNNING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)
        a1 = create_agent(run_id, role="Manager", provider="p", model="m", status="RUNNING")
        a2 = create_agent(run_id, role="Worker1", provider="p", model="m", status="QUEUED")
        a3 = create_agent(run_id, role="Worker2", provider="p", model="m", status="REVIEWING")

        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=False),
        ):
            reconciled = reconcile_stale_ownership()

        self.assertIn(run_id, reconciled)
        statuses = self._agent_statuses(run_id)
        for aid in (a1, a2, a3):
            self.assertEqual(statuses[aid], "FAILED")
        self.assertEqual(self._run_status(run_id), "FAILED")


# ==============================================================================
# F: PID reuse / identity mismatch
# ==============================================================================
class TestF_PidReuse(OrphanLifecycleTestCase):
    def test_get_process_created_at_returns_none_on_nonwindows(self):
        """On non-Windows, get_process_created_at should return None."""
        if os.name == "nt":
            self.skipTest("Non-Windows only test")
        result = get_process_created_at(os.getpid())
        self.assertIsNone(result)

    def test_pid_reuse_detected_when_created_after_acquired(self):
        """If get_process_created_at returns a time after acquired_at, classify as dead."""
        future_time = datetime(2099, 1, 1, tzinfo=timezone.utc)
        # Use a fake PID (not os.getpid()) so classify_ownership reaches the is_pid_alive branch.
        FAKE_OTHER_PID = 88888  # not current process PID
        # When get_process_created_at returns None (non-Windows), no reuse detected
        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.current_pid", return_value=99999),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=True),
            patch("nexus.orchestration.ownership.get_process_created_at", return_value=None),
        ):
            cls_no_reuse = classify_ownership(
                owner_host=DEAD_HOST,
                owner_pid=FAKE_OTHER_PID,
                owner_acquired_at="2000-01-01T00:00:00+00:00",
            )
        # When get_process_created_at returns a future time, PID reuse is detected
        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.current_pid", return_value=99999),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=True),
            patch("nexus.orchestration.ownership.get_process_created_at", return_value=future_time),
        ):
            cls_reuse = classify_ownership(
                owner_host=DEAD_HOST,
                owner_pid=FAKE_OTHER_PID,
                owner_acquired_at="2000-01-01T00:00:00+00:00",
            )
        self.assertEqual(cls_no_reuse, "live_same_host")  # No reuse when None returned
        self.assertEqual(cls_reuse, "dead_same_host")     # Reuse detected when future time

    def test_classify_ownership_pid_reuse_triggers_dead_same_host(self):
        """When get_process_created_at returns time after acquired_at, returns dead_same_host."""
        future_time = datetime(2099, 1, 1, tzinfo=timezone.utc)
        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=True),
            patch("nexus.orchestration.ownership.get_process_created_at", return_value=future_time),
        ):
            cls = classify_ownership(
                owner_host=DEAD_HOST,
                owner_pid=12345,
                owner_acquired_at="2000-01-01T00:00:00+00:00",
            )
        self.assertEqual(cls, "dead_same_host")


# ==============================================================================
# G: Same-host dead process → reconciled
# ==============================================================================
class TestG_SameHostDeadProcess(OrphanLifecycleTestCase):
    def test_same_host_dead_pid_is_reconciled(self):
        run_id = self._make_run(status="RUNNING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)

        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=False),
        ):
            reconciled = reconcile_stale_ownership()

        self.assertIn(run_id, reconciled)
        self.assertEqual(self._run_status(run_id), "FAILED")


# ==============================================================================
# H: Foreign-host ownership → not reconciled
# ==============================================================================
class TestH_ForeignHost(OrphanLifecycleTestCase):
    def test_foreign_host_run_is_not_reconciled(self):
        run_id = self._make_run(status="ROUTING")
        self._set_owner(run_id, "foreign-host-X", DEAD_PID)

        with patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST):
            # current_host is DEAD_HOST; owner is "foreign-host-X" → foreign_host classification
            reconciled = reconcile_stale_ownership()

        self.assertNotIn(run_id, reconciled)
        self.assertEqual(self._run_status(run_id), "ROUTING")


# ==============================================================================
# I: recover_all dry_run still works after reconciliation on dead-owner runs
# ==============================================================================
class TestI_RecoverAllDryRunStillWorks(OrphanLifecycleTestCase):
    def test_recover_all_dry_run_on_dead_owner_run(self):
        """recover_all(dry_run=True) should find the dead-owner run before reconciliation."""
        run_id = self._make_run(status="ROUTING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)

        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=False),
        ):
            result = recover_all(dry_run=True)

        self.assertIn(run_id, result["recovered"])

    def test_recover_all_after_reconciliation_skips_terminal_run(self):
        """After reconcile_stale_ownership marks a run FAILED, recover_all skips it."""
        run_id = self._make_run(status="ROUTING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)

        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=False),
        ):
            reconcile_stale_ownership()
            result = recover_all(dry_run=True)

        # run is now FAILED (terminal) → recover_all should skip it
        run_found_in_recovered = run_id in result["recovered"]
        run_found_in_skipped = any(run_id == s[0] for s in result["skipped"])
        self.assertFalse(run_found_in_recovered)
        self.assertTrue(run_found_in_skipped)


# ==============================================================================
# J: New GO entry can proceed after stale ownership reconciliation
# ==============================================================================
class TestJ_GoCanProceedAfterReconciliation(OrphanLifecycleTestCase):
    def test_reconcile_stale_returns_list_and_db_consistent(self):
        """reconcile_stale_ownership() returns a list and leaves DB consistent."""
        run1 = self._make_run(status="ROUTING")
        self._set_owner(run1, DEAD_HOST, DEAD_PID)

        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=False),
        ):
            reconciled = reconcile_stale_ownership()

        self.assertIsInstance(reconciled, list)
        self.assertIn(run1, reconciled)
        # After reconciliation a new run can be created without issues
        run2 = self._make_run(status="ROUTING")
        self.assertNotEqual(run1, run2)
        self.assertEqual(self._run_status(run2), "ROUTING")  # untouched; it has no dead owner


# ==============================================================================
# K: No destructive git command is executed
# ==============================================================================
class TestK_NoDestructiveGitCommands(OrphanLifecycleTestCase):
    def test_reconcile_does_not_call_git_reset_or_clean(self):
        """reconcile_orphan_run should not invoke any git subprocess commands."""
        import subprocess
        run_id = self._make_run(status="ROUTING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)
        create_agent(run_id, role="Manager", provider="p", model="m", status="RUNNING")

        called_cmds = []

        original_run = subprocess.run
        original_popen = subprocess.Popen

        def mock_run(cmd, *args, **kwargs):
            called_cmds.append(cmd)
            return original_run(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            reconcile_orphan_run(run_id, DEAD_PID, DEAD_HOST)

        # Verify no git reset/clean commands were called
        for cmd in called_cmds:
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            self.assertNotIn("git reset", cmd_str)
            self.assertNotIn("git clean", cmd_str)
            self.assertNotIn("git checkout .", cmd_str)
            self.assertNotIn("git restore", cmd_str)


# ==============================================================================
# L: Phase 4.4 integration state not deleted/reset
# ==============================================================================
class TestL_IntegrationStatePreserved(OrphanLifecycleTestCase):
    def test_reconcile_does_not_delete_worktrees(self):
        """reconcile_orphan_run sets statuses; it does NOT delete worktrees from the DB."""
        run_id = self._make_run(status="RUNNING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)
        # Simulate a worker with a worktree recorded
        agent_id = create_agent(
            run_id, role="Worker", provider="p", model="m",
            status="RUNNING",
            worktree="/tmp/some-worktree",
        )

        reconcile_orphan_run(run_id, DEAD_PID, DEAD_HOST)

        agents = list_agents_for_run(run_id)
        agent = next(a for a in agents if a["id"] == agent_id)
        # worktree column preserved; only status changed
        self.assertEqual(agent["worktree"], "/tmp/some-worktree")
        self.assertEqual(agent["status"], "FAILED")


# ==============================================================================
# M: Orphan checkpoint/audit evidence is recorded
# ==============================================================================
class TestM_OrphanCheckpointRecorded(OrphanLifecycleTestCase):
    def test_lifecycle_orphan_detected_checkpoint_payload(self):
        run_id = self._make_run(status="ROUTING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)
        agent_id = create_agent(run_id, role="Manager", provider="p", model="m", status="RUNNING")

        reconcile_orphan_run(run_id, DEAD_PID, DEAD_HOST)

        cps = list_checkpoints(run_id)
        orphan_cp = [c for c in cps if c["boundary"] == "LIFECYCLE_ORPHAN_DETECTED"]
        self.assertEqual(len(orphan_cp), 1)
        payload = orphan_cp[0]["payload"]
        self.assertEqual(payload["reason"], "owner_process_dead")
        self.assertIn(agent_id, payload["stale_agent_ids"])
        self.assertEqual(payload["owner_pid"], DEAD_PID)
        self.assertEqual(payload["owner_host"], DEAD_HOST)
        self.assertEqual(payload["previous_run_status"], "ROUTING")


# ==============================================================================
# N: RUN-515B85EA-style: ROUTING + two managers (one RUNNING, one COMPLETED)
# ==============================================================================
class TestN_RunWith515BA_Style(OrphanLifecycleTestCase):
    def test_non_terminal_agents_reconciled_terminal_agents_untouched(self):
        run_id = self._make_run(status="ROUTING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)
        mgr_done = create_agent(run_id, role="Manager", provider="p", model="m", status="COMPLETED")
        mgr_live = create_agent(run_id, role="Manager", provider="p", model="m", status="RUNNING")

        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=False),
        ):
            reconciled = reconcile_stale_ownership()

        self.assertIn(run_id, reconciled)
        statuses = self._agent_statuses(run_id)
        self.assertEqual(statuses[mgr_done], "COMPLETED")  # terminal → untouched
        self.assertEqual(statuses[mgr_live], "FAILED")     # non-terminal → reconciled
        self.assertEqual(self._run_status(run_id), "FAILED")

        # Check checkpoint records only the non-terminal agent
        cps = list_checkpoints(run_id)
        orphan_cp = next(c for c in cps if c["boundary"] == "LIFECYCLE_ORPHAN_DETECTED")
        self.assertIn(mgr_live, orphan_cp["payload"]["stale_agent_ids"])
        self.assertNotIn(mgr_done, orphan_cp["payload"]["stale_agent_ids"])


# ==============================================================================
# O: RUN-4A2EA6EF-style: ROUTING + Manager RUNNING, only MISSION_TRIAGE checkpoint
# ==============================================================================
class TestO_RunWith4A2EA6EF_Style(OrphanLifecycleTestCase):
    def test_routing_run_single_manager_reconciled(self):
        from nexus.registry.checkpoints import record_checkpoint

        run_id = self._make_run(status="ROUTING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)
        # Simulate what the real RUN-4A2EA6EF had: only a MISSION_TRIAGE checkpoint
        record_checkpoint(
            run_id=run_id,
            boundary="MISSION_TRIAGE",
            payload={"classification": "BOUNDED", "reason_codes": []},
        )
        agent_id = create_agent(run_id, role="Manager", provider="p", model="m", status="RUNNING")

        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=False),
        ):
            reconciled = reconcile_stale_ownership()

        self.assertIn(run_id, reconciled)
        self.assertEqual(self._run_status(run_id), "FAILED")
        self.assertEqual(self._agent_statuses(run_id)[agent_id], "FAILED")

        cps = list_checkpoints(run_id)
        boundaries = [c["boundary"] for c in cps]
        self.assertIn("MISSION_TRIAGE", boundaries)
        self.assertIn("LIFECYCLE_ORPHAN_DETECTED", boundaries)


if __name__ == "__main__":
    unittest.main()


# ==============================================================================
# P: CASE A — orphan before PLAN_APPROVED → terminal FAILED
# ==============================================================================
class TestP_CaseA_NoApprovedPlan_Terminal(OrphanLifecycleTestCase):
    def test_orphan_no_plan_approved_becomes_terminal(self):
        """An orphaned run that never reached PLAN_APPROVED must be marked FAILED (terminal)."""
        run_id = self._make_run(status="ROUTING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)
        agent_id = create_agent(run_id, role="Manager", provider="p", model="m", status="RUNNING")

        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=False),
        ):
            reconciled = reconcile_stale_ownership()

        self.assertIn(run_id, reconciled)
        self.assertEqual(self._run_status(run_id), "FAILED")
        self.assertEqual(self._agent_statuses(run_id)[agent_id], "FAILED")

        cps = self._checkpoints_for(run_id)
        orphan_cp = next(c for c in cps if c["boundary"] == "LIFECYCLE_ORPHAN_DETECTED")
        self.assertEqual(orphan_cp["payload"]["case"], "A_terminal_failed")


# ==============================================================================
# Q: CASE B — orphan after PLAN_APPROVED → run stays non-terminal, ownership released
# ==============================================================================
class TestQ_CaseB_HasApprovedPlan_ReleasedNotTerminal(OrphanLifecycleTestCase):
    def _insert_approved_plan(self, run_id):
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO approved_plans (run_id, plan_json, created_at) VALUES (?, ?, ?)",
                (run_id, '{"steps":[]}', datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()

    def test_orphan_with_plan_approved_stays_non_terminal(self):
        """An orphaned run that reached PLAN_APPROVED must NOT be made terminal."""
        run_id = self._make_run(status="RUNNING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)
        self._insert_approved_plan(run_id)
        stale_agent_id = create_agent(run_id, role="Worker", provider="p", model="m", status="RUNNING")

        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=False),
        ):
            reconciled = reconcile_stale_ownership()

        self.assertIn(run_id, reconciled)

        # Run must remain non-terminal
        status_after = self._run_status(run_id)
        from nexus.orchestration.recovery import RECOVERABLE_RUN_STATUSES
        self.assertIn(status_after, RECOVERABLE_RUN_STATUSES,
                      f"Run became terminal ({status_after}) but should stay non-terminal (CASE B)")

        # Stale agent must be failed
        self.assertEqual(self._agent_statuses(run_id)[stale_agent_id], "FAILED")

        # Ownership must be released (NULL)
        with get_connection() as conn:
            row = conn.execute(
                "SELECT owner_host, owner_pid FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        self.assertIsNone(row["owner_host"])
        self.assertIsNone(row["owner_pid"])

        cps = self._checkpoints_for(run_id)
        orphan_cp = next(c for c in cps if c["boundary"] == "LIFECYCLE_ORPHAN_DETECTED")
        self.assertEqual(orphan_cp["payload"]["case"], "B_release_ownership")


# ==============================================================================
# R: explicit recover after reconciliation sees recoverable (unowned) run
# ==============================================================================
class TestR_RecoverAfterReconciliationSeesUnownedRun(OrphanLifecycleTestCase):
    def _insert_approved_plan(self, run_id):
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO approved_plans (run_id, plan_json, created_at) VALUES (?, ?, ?)",
                (run_id, '{"steps":[]}', datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()

    def test_recover_can_reacquire_unowned_run_after_case_b_reconciliation(self):
        """After CASE B reconciliation ownership is NULL; try_acquire_for_recovery must succeed."""
        from nexus.orchestration.ownership import try_acquire_for_recovery

        run_id = self._make_run(status="RUNNING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)
        self._insert_approved_plan(run_id)

        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=False),
        ):
            reconcile_stale_ownership()

        # Verify ownership was released (CASE B)
        with get_connection() as conn:
            row = conn.execute("SELECT owner_host FROM runs WHERE id=?", (run_id,)).fetchone()
        self.assertIsNone(row["owner_host"])

        # Now try_acquire_for_recovery must succeed on the unowned run
        acquired = try_acquire_for_recovery(run_id)
        self.assertTrue(acquired, "try_acquire_for_recovery should accept an unowned non-terminal run")

        # Confirm ownership is now set to current process
        with get_connection() as conn:
            row = conn.execute(
                "SELECT owner_host, owner_pid FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        import socket
        self.assertEqual(row["owner_host"], socket.gethostname())
        self.assertEqual(int(row["owner_pid"]), os.getpid())


# ==============================================================================
# S: new GO startup does NOT auto-replay a CASE B (recoverable) run
# ==============================================================================
class TestS_GoStartupDoesNotAutoReplay(OrphanLifecycleTestCase):
    def _insert_approved_plan(self, run_id):
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO approved_plans (run_id, plan_json, created_at) VALUES (?, ?, ?)",
                (run_id, '{"steps":[]}', datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()

    def test_reconcile_stale_ownership_does_not_call_resume_run(self):
        """reconcile_stale_ownership() (called by GO) must NEVER call resume_run, even for CASE B."""
        from nexus.orchestration import recovery as rec_module

        run_id = self._make_run(status="RUNNING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)
        self._insert_approved_plan(run_id)

        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=False),
            patch.object(rec_module, "resume_run", side_effect=AssertionError("resume_run must not be called at reconcile")),
        ):
            # Must not raise even though approved plan exists
            reconcile_stale_ownership()

        # After reconciliation, run stays non-terminal (CASE B)
        from nexus.orchestration.recovery import RECOVERABLE_RUN_STATUSES
        self.assertIn(self._run_status(run_id), RECOVERABLE_RUN_STATUSES)


# ==============================================================================
# T: CASE B — completed workers remain COMPLETED after reconciliation
# ==============================================================================
class TestT_CaseB_CompletedWorkersUnchanged(OrphanLifecycleTestCase):
    def _insert_approved_plan(self, run_id):
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO approved_plans (run_id, plan_json, created_at) VALUES (?, ?, ?)",
                (run_id, '{"steps":[]}', datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()

    def test_completed_workers_stay_completed_in_case_b(self):
        """Under CASE B, terminal agents (COMPLETED) must not be changed."""
        run_id = self._make_run(status="RUNNING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)
        self._insert_approved_plan(run_id)

        done_id = create_agent(run_id, role="Worker", provider="p", model="m", status="COMPLETED")
        stale_id = create_agent(run_id, role="Worker", provider="p", model="m", status="RUNNING")

        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=False),
        ):
            reconcile_stale_ownership()

        statuses = self._agent_statuses(run_id)
        self.assertEqual(statuses[done_id], "COMPLETED",
                         "completed worker must not be changed by CASE B reconciliation")
        self.assertEqual(statuses[stale_id], "FAILED",
                         "stale active worker must be failed even in CASE B")


# ==============================================================================
# U: CASE B — stale WAITING/REVIEWING agent also becomes FAILED
# ==============================================================================
class TestU_CaseB_StaleWaitingReviewingAgentFailed(OrphanLifecycleTestCase):
    def _insert_approved_plan(self, run_id):
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO approved_plans (run_id, plan_json, created_at) VALUES (?, ?, ?)",
                (run_id, '{"steps":[]}', datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()

    def test_stale_waiting_reviewing_agents_failed_in_case_b(self):
        """WAITING and REVIEWING agents must be marked FAILED in CASE B (stale active)."""
        run_id = self._make_run(status="RUNNING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)
        self._insert_approved_plan(run_id)

        waiting_id  = create_agent(run_id, role="Worker", provider="p", model="m", status="WAITING")
        reviewing_id = create_agent(run_id, role="Reviewer", provider="p", model="m", status="REVIEWING")

        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=False),
        ):
            reconcile_stale_ownership()

        statuses = self._agent_statuses(run_id)
        self.assertEqual(statuses[waiting_id],   "FAILED", "stale WAITING agent must be FAILED in CASE B")
        self.assertEqual(statuses[reviewing_id], "FAILED", "stale REVIEWING agent must be FAILED in CASE B")

        # Run must stay non-terminal
        from nexus.orchestration.recovery import RECOVERABLE_RUN_STATUSES
        self.assertIn(self._run_status(run_id), RECOVERABLE_RUN_STATUSES)


# ==============================================================================
# V: try_acquire_for_recovery on unowned non-terminal run → True (new behaviour)
# ==============================================================================
class TestV_TryAcquireForRecoveryAcceptsUnownedRun(OrphanLifecycleTestCase):
    def test_try_acquire_for_recovery_accepts_unowned_run(self):
        """try_acquire_for_recovery must accept an unowned (NULL owner) non-terminal run."""
        from nexus.orchestration.ownership import try_acquire_for_recovery

        run_id = self._make_run(status="ROUTING")
        # Intentionally do NOT set an owner → owner_host/pid = NULL

        acquired = try_acquire_for_recovery(run_id)
        self.assertTrue(acquired, "try_acquire_for_recovery should accept an unowned non-terminal run")

        # Verify ownership is now held by current process
        with get_connection() as conn:
            row = conn.execute(
                "SELECT owner_host, owner_pid FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        import socket
        self.assertEqual(row["owner_host"], socket.gethostname())
        self.assertEqual(int(row["owner_pid"]), os.getpid())

    def test_try_acquire_for_recovery_rejects_live_same_host(self):
        """try_acquire_for_recovery must still reject a live-PID run."""
        from nexus.orchestration.ownership import try_acquire_for_recovery
        import os as _os

        run_id = self._make_run(status="ROUTING")
        live_pid = _os.getpid()  # current process is definitely alive
        self._set_owner(run_id, DEAD_HOST, live_pid)

        # Patch current_host to match DEAD_HOST so classification is live_same_host
        with patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST):
            acquired = try_acquire_for_recovery(run_id)
        self.assertFalse(acquired, "try_acquire_for_recovery must reject a live-PID run")


# ==============================================================================
# W: RUN-515B85EA-style — no PLAN_APPROVED → CASE A → terminal (verify case field)
# ==============================================================================
class TestW_Run515B85EAStyleCaseAVerified(OrphanLifecycleTestCase):
    def test_515b85ea_pattern_is_case_a_terminal(self):
        """Mirrors TestN_Run515B85EAStyle: orphan with only MISSION_TRIAGE → CASE A."""
        from nexus.registry.checkpoints import record_checkpoint

        run_id = self._make_run(status="ROUTING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)
        record_checkpoint(
            run_id=run_id,
            boundary="MISSION_TRIAGE",
            payload={"classification": "BOUNDED"},
        )
        agent_id = create_agent(run_id, role="Manager", provider="p", model="m", status="RUNNING")

        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=False),
        ):
            reconcile_stale_ownership()

        self.assertEqual(self._run_status(run_id), "FAILED", "CASE A: run must be terminal FAILED")
        self.assertEqual(self._agent_statuses(run_id)[agent_id], "FAILED")

        orphan_cp = next(
            c for c in self._checkpoints_for(run_id)
            if c["boundary"] == "LIFECYCLE_ORPHAN_DETECTED"
        )
        self.assertEqual(orphan_cp["payload"]["case"], "A_terminal_failed")


# ==============================================================================
# X: Phase 4.4 interrupted-run shape — RUNNING + PLAN_APPROVED + worker checkpoints → CASE B
# ==============================================================================
class TestX_Phase44InterruptedRunShape_CaseB(OrphanLifecycleTestCase):
    def _insert_approved_plan(self, run_id):
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO approved_plans (run_id, plan_json, created_at) VALUES (?, ?, ?)",
                (run_id, '{"steps":["worker-1","worker-2"]}',
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()

    def test_phase44_interrupted_run_is_case_b_recoverable(self):
        """
        Simulate a Phase 4.4-style run that was interrupted mid-integration:
        - RUNNING status, has PLAN_APPROVED, has PLAN_GENERATED + PLAN_APPROVED checkpoints,
          has at least one WORKER_COMPLETED checkpoint.
        - Stale RUNNING agent (the integration coordinator).
        Expected: CASE B — ownership released, run stays non-terminal, agents failed.
        """
        from nexus.registry.checkpoints import record_checkpoint

        run_id = self._make_run(status="RUNNING")
        self._set_owner(run_id, DEAD_HOST, DEAD_PID)
        self._insert_approved_plan(run_id)

        record_checkpoint(run_id=run_id, boundary="PLAN_GENERATED",
                          payload={"steps": 2})
        record_checkpoint(run_id=run_id, boundary="PLAN_APPROVED",
                          payload={"approver": "human"})
        record_checkpoint(run_id=run_id, boundary="WORKER_COMPLETED",
                          payload={"worker_ordinal": 0, "branch": "nexus/worker-1"})

        # Integration coordinator was still RUNNING when crash occurred
        coord_id = create_agent(run_id, role="Manager", provider="p", model="m", status="RUNNING")
        # Worker-1 completed
        w1_id = create_agent(run_id, role="Worker", provider="p", model="m", status="COMPLETED")

        with (
            patch("nexus.orchestration.ownership.current_host", return_value=DEAD_HOST),
            patch("nexus.orchestration.ownership.is_pid_alive", return_value=False),
        ):
            reconciled = reconcile_stale_ownership()

        self.assertIn(run_id, reconciled)

        # CASE B: run stays non-terminal
        from nexus.orchestration.recovery import RECOVERABLE_RUN_STATUSES
        status_after = self._run_status(run_id)
        self.assertIn(status_after, RECOVERABLE_RUN_STATUSES,
                      f"Phase 4.4-style run must remain recoverable (got {status_after})")

        # CASE B: ownership is NULL
        with get_connection() as conn:
            row = conn.execute(
                "SELECT owner_host, owner_pid FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        self.assertIsNone(row["owner_host"])
        self.assertIsNone(row["owner_pid"])

        statuses = self._agent_statuses(run_id)
        self.assertEqual(statuses[coord_id], "FAILED",
                         "stale RUNNING coordinator must be failed in CASE B")
        self.assertEqual(statuses[w1_id], "COMPLETED",
                         "completed worker must remain COMPLETED in CASE B")

        orphan_cp = next(
            c for c in self._checkpoints_for(run_id)
            if c["boundary"] == "LIFECYCLE_ORPHAN_DETECTED"
        )
        self.assertEqual(orphan_cp["payload"]["case"], "B_release_ownership")
        self.assertIn(coord_id, orphan_cp["payload"]["stale_agent_ids"])
        self.assertNotIn(w1_id, orphan_cp["payload"]["stale_agent_ids"])
