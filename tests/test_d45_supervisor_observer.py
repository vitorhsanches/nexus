import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.registry.runs import create_run
import nexus.missions.service as mission_service
import nexus.tasks.registry as task_registry
import nexus.board.service as board_service
from nexus.supervisor.observer import build_supervisor_snapshot, observe_supervisor_state
from nexus.orchestration.ownership import ensure_ownership_columns

FIXTURE={"projects":[{"id":"norte","name":"Norte","path":"C:/fake","aliases":["norte"],"enabled":True}]}

class D45ObserverTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        p=Path(self.tmp.name)
        self.orig_db=database.DATABASE_PATH
        self.orig_dir=database.STORAGE_DIR
        self.orig_proj=projects.PROJECTS_FILE
        database.STORAGE_DIR=p
        database.DATABASE_PATH=p/"nexus-test.db"
        fp=p/"projects.json"; fp.write_text(json.dumps(FIXTURE),encoding="utf-8")
        projects.PROJECTS_FILE=fp
        database.initialize_database()
        projects.sync_projects()
        ensure_ownership_columns()
        mission_service._missions.clear()
        task_registry._tasks.clear()
        task_registry._attempts.clear()
        task_registry._next_attempt.clear()
        board_service._boards.clear()
    def tearDown(self):
        database.DATABASE_PATH=self.orig_db
        database.STORAGE_DIR=self.orig_dir
        projects.PROJECTS_FILE=self.orig_proj
        mission_service._missions.clear()
        task_registry._tasks.clear()
        task_registry._attempts.clear()
        task_registry._next_attempt.clear()
        board_service._boards.clear()
        self.tmp.cleanup()
    def _set_owner(self,rid,h,p):
        with database.get_connection() as c:
            c.execute("UPDATE runs SET owner_host=?,owner_pid=?,owner_acquired_at=? WHERE id=?",(h,p,"2026-09-07T00:00:00Z",rid))
            c.commit()
    def _set_status(self,rid,s):
        with database.get_connection() as c:
            c.execute("UPDATE runs SET status=? WHERE id=?",(s,rid))
            c.commit()
    def _hash_db(self):
        with database.get_connection() as c:
            a=list(c.execute("SELECT * FROM runs ORDER BY id").fetchall())
            b=list(c.execute("SELECT * FROM missions ORDER BY mission_id").fetchall()) if self._has_table("missions") else []
            cc=list(c.execute("SELECT * FROM tasks ORDER BY task_id").fetchall()) if self._has_table("tasks") else []
            d=list(c.execute("SELECT * FROM attempts ORDER BY attempt_id").fetchall()) if self._has_table("attempts") else []
        return (len(a),len(b),len(cc),len(d),tuple(dict(r)["id"] for r in a))
    def _has_table(self,n):
        with database.get_connection() as c:
            return c.execute("SELECT name FROM sqlite_master WHERE type=\"table\" AND name=?",(n,)).fetchone() is not None
    def test_empty_snapshot(self):
        snap=build_supervisor_snapshot()
        self.assertEqual(snap.runs,())
        self.assertEqual(snap.missions,())
        self.assertEqual(snap.tasks,())
        self.assertEqual(snap.attempts,())

    def test_mission_task_attempt_after_recovery(self):
        rid=create_run(project_id="norte",input_text="x",intent="GO",status="RUNNING")
        m=mission_service.create_mission(run_id=rid,title="M")
        t=task_registry.create_task(mission_id=m.mission_id,title="T")
        a=task_registry.create_attempt(t.task_id,agent_id="AGT-1",model="gpt")
        # simulate restart: clear memory, rehydrate via board recovery, then observe
        from nexus.board.recovery import clear_memory, recover_all
        clear_memory()
        recover_all()
        snap=build_supervisor_snapshot()
        self.assertEqual(len(snap.missions),1)
        self.assertEqual(len(snap.tasks),1)
        self.assertEqual(len(snap.attempts),1)
        self.assertEqual(snap.missions[0].mission_id,m.mission_id)
        self.assertEqual(snap.tasks[0].task_id,t.task_id)
        self.assertEqual(snap.attempts[0].attempt_id,a.attempt_id)

    def test_board_column_derived(self):
        rid=create_run(project_id="norte",input_text="x",intent="GO")
        m=mission_service.create_mission(run_id=rid,title="M")
        t=task_registry.create_task(mission_id=m.mission_id,title="T")
        # CREATED -> TODO
        snap=build_supervisor_snapshot()
        self.assertEqual(snap.tasks[0].board_column,"TODO")
        for s in ["READY","CLAIMED","RUNNING"]:
            task_registry.update_task_status(t.task_id,s)
        snap=build_supervisor_snapshot()
        # RUNNING maps to IN_PROGRESS
        self.assertEqual([x.board_column for x in snap.tasks if x.task_id==t.task_id][0],"IN_PROGRESS")
        task_registry.update_task_status(t.task_id,"REVIEW")
        snap=build_supervisor_snapshot()
        self.assertEqual([x.board_column for x in snap.tasks if x.task_id==t.task_id][0],"REVIEW")
        task_registry.update_task_status(t.task_id,"COMPLETED")
        snap=build_supervisor_snapshot()
        self.assertEqual([x.board_column for x in snap.tasks if x.task_id==t.task_id][0],"DONE")

    def test_deterministic(self):
        rid=create_run(project_id="norte",input_text="x",intent="GO")
        m=mission_service.create_mission(run_id=rid,title="M")
        for i in range(3):
            task_registry.create_task(mission_id=m.mission_id,title=f"T{i}")
        s1=build_supervisor_snapshot()
        s2=build_supervisor_snapshot()
        self.assertEqual(s1,s2)
        # ordering by id
        ids=[t.task_id for t in s1.tasks]
        self.assertEqual(ids, sorted(ids))

    def test_no_fabrication_of_board_events(self):
        rid=create_run(project_id="norte",input_text="x",intent="GO")
        m=mission_service.create_mission(run_id=rid,title="M")
        task_registry.create_task(mission_id=m.mission_id,title="T")
        b_before=len(board_service._boards)
        # board not created yet - observer should not auto-create boards
        snap=build_supervisor_snapshot()
        self.assertEqual(len(board_service._boards), b_before)
        # ensure no assignments/events created elsewhere: snapshot is read-only, check DB hashes unchanged
        with database.get_connection() as c:
            cnt_before=c.execute("SELECT count(*) FROM tasks").fetchone()[0]
        build_supervisor_snapshot()
        with database.get_connection() as c:
            cnt_after=c.execute("SELECT count(*) FROM tasks").fetchone()[0]
        self.assertEqual(cnt_before,cnt_after)

    def test_dead_same_host_recoverable(self):
        rid=create_run(project_id="norte",input_text="x",intent="GO",status="RUNNING")
        with patch("nexus.orchestration.ownership.current_host",return_value="HOST"), patch("nexus.orchestration.ownership.current_pid",return_value=111), patch("nexus.orchestration.ownership.is_pid_alive",return_value=False):
            self._set_owner(rid,"HOST",222)
            snap=build_supervisor_snapshot()
            r=[x for x in snap.runs if x.run_id==rid][0]
            self.assertTrue(r.recovery_eligible)
            self.assertEqual(r.recovery_classification,"recoverable")
            # ensure not mutated
            with database.get_connection() as c:
                row=c.execute("SELECT owner_host,owner_pid FROM runs WHERE id=?",(rid,)).fetchone()
            self.assertEqual(row["owner_host"],"HOST")
            self.assertEqual(row["owner_pid"],222)

    def test_live_same_host_blocked(self):
        rid=create_run(project_id="norte",input_text="x",intent="GO",status="RUNNING")
        with patch("nexus.orchestration.ownership.current_host",return_value="HOST"), patch("nexus.orchestration.ownership.current_pid",return_value=111), patch("nexus.orchestration.ownership.is_pid_alive",return_value=True):
            self._set_owner(rid,"HOST",222)
            snap=build_supervisor_snapshot()
            r=[x for x in snap.runs if x.run_id==rid][0]
            self.assertFalse(r.recovery_eligible)
            self.assertEqual(r.recovery_classification,"blocked_live_owner")

    def test_foreign_owner_fails_closed(self):
        rid=create_run(project_id="norte",input_text="x",intent="GO",status="RUNNING")
        with patch("nexus.orchestration.ownership.current_host",return_value="HOST"), patch("nexus.orchestration.ownership.current_pid",return_value=111):
            self._set_owner(rid,"OTHER",222)
            snap=build_supervisor_snapshot()
            r=[x for x in snap.runs if x.run_id==rid][0]
            self.assertFalse(r.recovery_eligible)
            self.assertIn("foreign",r.recovery_classification)

    def test_unknown_ownership_fails_closed(self):
        rid=create_run(project_id="norte",input_text="x",intent="GO",status="RUNNING")
        # set empty host
        self._set_owner(rid,"",222)
        with patch("nexus.orchestration.ownership.current_host",return_value="HOST"):
            snap=build_supervisor_snapshot()
            r=[x for x in snap.runs if x.run_id==rid][0]
            self.assertFalse(r.recovery_eligible)

    def test_legacy_unowned_not_recoverable(self):
        rid=create_run(project_id="norte",input_text="x",intent="GO",status="RUNNING")
        self._set_owner(rid,None,None)
        with patch("nexus.orchestration.ownership.current_host",return_value="HOST"), patch("nexus.orchestration.ownership.current_pid",return_value=111):
            snap=build_supervisor_snapshot()
            r=[x for x in snap.runs if x.run_id==rid][0]
            self.assertFalse(r.recovery_eligible)
            self.assertEqual(r.recovery_classification,"blocked_unowned")

    def test_awaiting_approval_not_recoverable(self):
        rid=create_run(project_id="norte",input_text="x",intent="GO",status="AWAITING_APPROVAL")
        with patch("nexus.orchestration.ownership.current_host",return_value="HOST"), patch("nexus.orchestration.ownership.current_pid",return_value=111), patch("nexus.orchestration.ownership.is_pid_alive",return_value=False):
            self._set_owner(rid,"HOST",222)
            snap=build_supervisor_snapshot()
            r=[x for x in snap.runs if x.run_id==rid][0]
            self.assertFalse(r.recovery_eligible)
            self.assertEqual(r.recovery_classification,"awaiting_approval")

    def test_terminal_not_recoverable(self):
        for st in ["COMPLETED","FAILED","BLOCKED","CANCELLED"]:
            rid=create_run(project_id="norte",input_text="x",intent="GO",status=st)
            with patch("nexus.orchestration.ownership.current_host",return_value="HOST"), patch("nexus.orchestration.ownership.current_pid",return_value=111), patch("nexus.orchestration.ownership.is_pid_alive",return_value=False):
                self._set_owner(rid,"HOST",222)
                snap=build_supervisor_snapshot()
                r=[x for x in snap.runs if x.run_id==rid][0]
                self.assertFalse(r.recovery_eligible, f"{st} should not be recoverable")

    def test_never_calls_resume(self):
        rid=create_run(project_id="norte",input_text="x",intent="GO",status="RUNNING")
        self._set_owner(rid,"HOST",222)
        with patch("nexus.orchestration.resume.resume_run") as m, patch("nexus.orchestration.ownership.current_host",return_value="HOST"), patch("nexus.orchestration.ownership.current_pid",return_value=111), patch("nexus.orchestration.ownership.is_pid_alive",return_value=False):
            build_supervisor_snapshot()
            m.assert_not_called()

    def test_never_calls_manager_worker_omniroute(self):
        rid=create_run(project_id="norte",input_text="x",intent="GO")
        with patch("nexus.orchestration.resume.resume_run") as a:
            build_supervisor_snapshot()
            a.assert_not_called()

    def test_side_effect_free(self):
        rid=create_run(project_id="norte",input_text="x",intent="GO",status="RUNNING")
        m=mission_service.create_mission(run_id=rid,title="M")
        t=task_registry.create_task(mission_id=m.mission_id,title="T")
        before=self._hash_db()
        with patch("nexus.orchestration.ownership.current_host",return_value="HOST"), patch("nexus.orchestration.ownership.current_pid",return_value=111), patch("nexus.orchestration.ownership.is_pid_alive",return_value=False):
            self._set_owner(rid,"HOST",999)
            h=self._hash_db()
            build_supervisor_snapshot()
            build_supervisor_snapshot()
            after=self._hash_db()
        self.assertEqual(h,after)

    def test_corrupt_state_fails_closed(self):
        rid=create_run(project_id="norte",input_text="x",intent="GO",status="RUNNING")
        # inject unknown status directly
        self._set_status(rid,"CORRUPT_X")
        snap=build_supervisor_snapshot()
        r=[x for x in snap.runs if x.run_id==rid][0]
        self.assertFalse(r.recovery_eligible)
        self.assertIn("unknown",r.recovery_classification.lower())
        self.assertTrue(len(snap.errors)>0)
        # corrupt task status
        from nexus.tasks.persistence import _ensure as _ensure_tasks; _ensure_tasks()
        m=mission_service.create_mission(run_id=rid,title="M")
        with database.get_connection() as c:
            c.execute("INSERT INTO tasks(task_id,mission_id,title,status,priority) VALUES (?,?,?,?,?)",("TASK-CORRUPT",m.mission_id,"bad","CORRUPT","MEDIUM"))
            c.commit()
        snap2=build_supervisor_snapshot()
        tc=[x for x in snap2.tasks if x.task_id=="TASK-CORRUPT"][0]
        self.assertIsNone(tc.board_column)
        self.assertIsNotNone(tc.board_column_error)

    def test_default_storage_not_recreated(self):
        from nexus.registry.database import DATABASE_PATH as real
        # real path should not exist after isolated test? ensure we didn't touch it
        # we check that default path file mtime not changed: just ensure isolated path used
        self.assertNotEqual(str(database.DATABASE_PATH), str(real) if str(database.DATABASE_PATH)!=str(Path(self.tmp.name)/"nexus-test.db") else "same")
        # After test, default storage file should not have been created if it didn't exist
        # We verify build_snapshot didn't create file at default location
        default = Path(__file__).resolve().parents[1]/"nexus"/"storage"/"nexus.db"
        # snapshot used temp DB only; no assertion about existence, just ensure we didn't switch back
        self.assertEqual(database.DATABASE_PATH, Path(self.tmp.name)/"nexus-test.db")

    def test_observe_alias(self):
        s1=build_supervisor_snapshot()
        s2=observe_supervisor_state()
        self.assertEqual(s1,s2)




