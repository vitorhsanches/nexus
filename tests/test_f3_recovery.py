import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.orchestration.ownership import classify_ownership, ensure_ownership_columns, try_acquire_for_recovery, try_acquire_for_resume
from nexus.orchestration.recovery import recover_one
from nexus.orchestration.resume import ResumeError
from nexus.registry.runs import create_run

FIXTURE_PROJECTS = {"projects": [{"id": "norte", "name": "Norte", "path": r"C:\fake\interface-life", "aliases": ["norte"], "enabled": True}]}

class F3RecoveryTestCase(unittest.TestCase):
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

    def _make_run(self, status="RUNNING"):
        return create_run(project_id="norte", input_text="Norte: recover a thing", intent="GO", status=status)

    def _set_owner(self, run_id, host, pid):
        with database.get_connection() as conn:
            conn.execute("UPDATE runs SET owner_host=?, owner_pid=?, owner_acquired_at=? WHERE id=?", (host, pid, "2026-09-07T00:00:00Z", run_id))
            conn.commit()

    def _set_status(self, run_id, status):
        with database.get_connection() as conn:
            conn.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
            conn.commit()

    def _owner(self, run_id):
        with database.get_connection() as conn:
            row = conn.execute("SELECT owner_host, owner_pid FROM runs WHERE id=?", (run_id,)).fetchone()
        return row["owner_host"], row["owner_pid"]

    def test_current_process_is_distinct_from_other_live_owner(self):
        with patch("nexus.orchestration.ownership.current_host", return_value="HOST"), patch("nexus.orchestration.ownership.current_pid", return_value=111):
            self.assertEqual(classify_ownership("HOST", 111), "current_process")

    def test_live_same_host_owner_is_not_recoverable(self):
        run_id = self._make_run(); self._set_owner(run_id, "HOST", 222)
        with patch("nexus.orchestration.ownership.current_host", return_value="HOST"), patch("nexus.orchestration.ownership.current_pid", return_value=111), patch("nexus.orchestration.ownership.is_pid_alive", return_value=True):
            self.assertFalse(try_acquire_for_recovery(run_id))

    def test_dead_same_host_owner_can_be_claimed(self):
        run_id = self._make_run(); self._set_owner(run_id, "HOST", 222)
        with patch("nexus.orchestration.ownership.current_host", return_value="HOST"), patch("nexus.orchestration.ownership.current_pid", return_value=111), patch("nexus.orchestration.ownership.is_pid_alive", return_value=False):
            self.assertTrue(try_acquire_for_recovery(run_id))
        self.assertEqual(self._owner(run_id), ("HOST", 111))

    def test_foreign_host_fails_closed(self):
        run_id = self._make_run(); self._set_owner(run_id, "OTHER", 222)
        with patch("nexus.orchestration.ownership.current_host", return_value="HOST"), patch("nexus.orchestration.ownership.current_pid", return_value=111):
            self.assertFalse(try_acquire_for_recovery(run_id))

    def test_legacy_unowned_run_is_not_auto_recovered(self):
        run_id = self._make_run(); self._set_owner(run_id, None, None)
        with patch("nexus.orchestration.ownership.current_host", return_value="HOST"), patch("nexus.orchestration.ownership.current_pid", return_value=111):
            self.assertFalse(try_acquire_for_recovery(run_id))
            with self.assertRaises(ResumeError) as ctx: recover_one(run_id)
        self.assertEqual(ctx.exception.code, "RECOVERY_NOT_ELIGIBLE")

    def test_unknown_status_fails_closed(self):
        run_id = self._make_run(); self._set_owner(run_id, "HOST", 222); self._set_status(run_id, "MYSTERY")
        with patch("nexus.orchestration.ownership.current_host", return_value="HOST"), patch("nexus.orchestration.ownership.current_pid", return_value=111), patch("nexus.orchestration.ownership.is_pid_alive", return_value=False):
            self.assertFalse(try_acquire_for_recovery(run_id))
            with self.assertRaises(ResumeError) as ctx: recover_one(run_id)
        self.assertEqual(ctx.exception.code, "RUN_TERMINAL")

    def test_awaiting_approval_is_not_recoverable(self):
        run_id = self._make_run("AWAITING_APPROVAL"); self._set_owner(run_id, "HOST", 222)
        with patch("nexus.orchestration.ownership.current_host", return_value="HOST"), patch("nexus.orchestration.ownership.current_pid", return_value=111), patch("nexus.orchestration.ownership.is_pid_alive", return_value=False):
            self.assertFalse(try_acquire_for_recovery(run_id))
            with self.assertRaises(ResumeError) as ctx: recover_one(run_id)
        self.assertEqual(ctx.exception.code, "RESUME_NOT_AVAILABLE")

    def test_terminal_run_is_not_recoverable(self):
        run_id = self._make_run("COMPLETED"); self._set_owner(run_id, "HOST", 222)
        with patch("nexus.orchestration.ownership.current_host", return_value="HOST"), patch("nexus.orchestration.ownership.current_pid", return_value=111), patch("nexus.orchestration.ownership.is_pid_alive", return_value=False):
            self.assertFalse(try_acquire_for_recovery(run_id))
            with self.assertRaises(ResumeError) as ctx: recover_one(run_id)
        self.assertEqual(ctx.exception.code, "RUN_TERMINAL")

    def test_recovery_claim_can_enter_resume_in_same_process(self):
        run_id = self._make_run(); self._set_owner(run_id, "HOST", 222)
        with patch("nexus.orchestration.ownership.current_host", return_value="HOST"), patch("nexus.orchestration.ownership.current_pid", return_value=111), patch("nexus.orchestration.ownership.is_pid_alive", return_value=False):
            self.assertTrue(try_acquire_for_recovery(run_id)); try_acquire_for_resume(run_id)
        self.assertEqual(self._owner(run_id), ("HOST", 111))

    def test_recover_one_does_not_self_block_after_claim(self):
        run_id = self._make_run(); self._set_owner(run_id, "HOST", 222)
        def fake_resume(rid):
            try_acquire_for_resume(rid)
            return {"run_id": rid, "resumed": True}
        with patch("nexus.orchestration.ownership.current_host", return_value="HOST"), patch("nexus.orchestration.ownership.current_pid", return_value=111), patch("nexus.orchestration.ownership.is_pid_alive", return_value=False), patch("nexus.orchestration.recovery.resume_run", side_effect=fake_resume):
            result = recover_one(run_id)
        self.assertTrue(result["resumed"])
        self.assertEqual(self._owner(run_id), ("HOST", 111))

    def test_two_recoverers_only_one_wins_atomic_claim(self):
        run_id = self._make_run(); self._set_owner(run_id, "HOST", 222)
        with patch("nexus.orchestration.ownership.current_host", return_value="HOST"), patch("nexus.orchestration.ownership.current_pid", return_value=111), patch("nexus.orchestration.ownership.is_pid_alive", return_value=False):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: try_acquire_for_recovery(run_id), range(2)))
        self.assertEqual(sorted(results), [False, True])

if __name__ == "__main__":
    unittest.main()
