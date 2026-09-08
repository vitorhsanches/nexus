"""F.4 Mission/Task/Board persistence/recovery tests."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nexus.registry.database as database
import nexus.missions.service as mission_service
import nexus.tasks.registry as task_registry
import nexus.board.service as board_service
from nexus.board.models import BoardColumn


class F4BoardRecoveryTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp_dir.name)
        self._orig_db = database.DATABASE_PATH
        self._orig_storage = database.STORAGE_DIR
        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus-test.db"
        database.initialize_database()
        # ensure fresh memory
        mission_service._missions.clear()
        task_registry._tasks.clear()
        task_registry._attempts.clear()
        task_registry._next_attempt.clear()
        board_service._boards.clear()

    def tearDown(self):
        database.DATABASE_PATH = self._orig_db
        database.STORAGE_DIR = self._orig_storage
        mission_service._missions.clear()
        task_registry._tasks.clear()
        task_registry._attempts.clear()
        task_registry._next_attempt.clear()
        board_service._boards.clear()
        self._tmp_dir.cleanup()

    def _recover(self):
        from nexus.board.recovery import recover_all, clear_memory
        # simulate restart: clear memory then recover
        clear_memory()
        return recover_all()

    def test_all_mission_fields_survive(self):
        m = mission_service.create_mission(run_id="RUN-1", title="T", description="D", project_id="proj-1", execution_path="OMNIROUTE")
        mid = m.mission_id
        run_id = m.run_id
        created_at = m.created_at
        from nexus.board.recovery import clear_memory, rehydrate_missions
        clear_memory()
        rehydrate_missions()
        rm = mission_service.get_mission(mid)
        self.assertEqual(rm.run_id, run_id)
        self.assertEqual(rm.title, "T")
        self.assertEqual(rm.description, "D")
        self.assertEqual(rm.project_id, "proj-1")
        self.assertEqual(rm.execution_path, "OMNIROUTE")
        self.assertEqual(rm.created_at, created_at)
        self.assertEqual(rm.status, "CREATED")
        self.assertEqual(rm.mission_id, mid)

    def test_all_task_fields_survive(self):
        m = mission_service.create_mission(run_id="RUN-1", title="M")
        t = task_registry.create_task(mission_id=m.mission_id, title="TaskTitle", description="desc", priority="HIGH", dependencies=[], assigned_agent="agent-1", execution_policy={"provider":"openai","model":"gpt"}, acceptance_criteria=["ac1","ac2"])
        tid = t.task_id
        from nexus.board.recovery import clear_memory, rehydrate_missions, rehydrate_tasks_and_attempts
        clear_memory()
        rehydrate_missions()
        rehydrate_tasks_and_attempts()
        rt = task_registry.get_task(tid)
        self.assertEqual(rt.title, "TaskTitle")
        self.assertEqual(rt.description, "desc")
        self.assertEqual(rt.priority, "HIGH")
        self.assertEqual(rt.dependencies, [])
        self.assertEqual(rt.assigned_agent, "agent-1")
        self.assertEqual(rt.execution_policy, {"model":"gpt","provider":"openai"})
        self.assertEqual(rt.acceptance_criteria, ["ac1","ac2"])
        self.assertEqual(rt.mission_id, m.mission_id)
        self.assertEqual(rt.status, "CREATED")

    def test_assigned_agent_survives(self):
        m = mission_service.create_mission(run_id="RUN-1", title="M")
        t = task_registry.create_task(mission_id=m.mission_id, title="T", assigned_agent="orig")
        task_registry.assign_task_agent(t.task_id, "agent-X")
        from nexus.board.recovery import clear_memory, rehydrate_missions, rehydrate_tasks_and_attempts
        clear_memory()
        rehydrate_missions()
        rehydrate_tasks_and_attempts()
        self.assertEqual(task_registry.get_task(t.task_id).assigned_agent, "agent-X")

    def test_attempt_survives_and_sequence_continues(self):
        m = mission_service.create_mission(run_id="RUN-1", title="M")
        t = task_registry.create_task(mission_id=m.mission_id, title="T")
        a1 = task_registry.create_attempt(t.task_id, agent_id="AGT-1", model="gpt")
        a2 = task_registry.create_attempt(t.task_id, agent_id="AGT-2", model="gpt")
        self._recover()
        attempts = task_registry.list_attempts(task_id=t.task_id)
        ids = {a.attempt_id for a in attempts}
        self.assertIn(a1.attempt_id, ids)
        self.assertIn(a2.attempt_id, ids)
        # sequence continues without duplicates
        a3 = task_registry.create_attempt(t.task_id, agent_id="AGT-3", model="gpt")
        self.assertEqual(a3.attempt_id, f"ATT-{t.task_id}-3")
        self.assertEqual(len(task_registry.list_attempts(task_id=t.task_id)), 3)

    def test_board_reconstruction_all_columns(self):
        m = mission_service.create_mission(run_id="RUN-1", title="M")
        # Create tasks in different statuses via direct registry + transition chain
        t_todo = task_registry.create_task(mission_id=m.mission_id, title="todo")
        t_run = task_registry.create_task(mission_id=m.mission_id, title="run")
        t_review = task_registry.create_task(mission_id=m.mission_id, title="review")
        t_done = task_registry.create_task(mission_id=m.mission_id, title="done")
        # Drive statuses to desired board columns
        # RUNNING: CREATED->READY->CLAIMED->RUNNING
        for s in ["READY","CLAIMED","RUNNING"]:
            task_registry.update_task_status(t_run.task_id, s)
        # REVIEW: also through RUNNING->REVIEW
        for s in ["READY","CLAIMED","RUNNING","REVIEW"]:
            task_registry.update_task_status(t_review.task_id, s)
        # DONE: REVIEW->COMPLETED
        for s in ["READY","CLAIMED","RUNNING","REVIEW","COMPLETED"]:
            task_registry.update_task_status(t_done.task_id, s)
        from nexus.board.recovery import clear_memory, recover_all
        clear_memory()
        recover_all()
        board = board_service.get_board(m.mission_id)
        self.assertIn(t_todo.task_id, board.columns[BoardColumn.TODO])
        self.assertIn(t_run.task_id, board.columns[BoardColumn.IN_PROGRESS])
        self.assertIn(t_review.task_id, board.columns[BoardColumn.REVIEW])
        self.assertIn(t_done.task_id, board.columns[BoardColumn.DONE])
        # TODO count should be 1 (t_todo), FAILED maps to TODO tested separately

    def test_mission_tasks_relationship_reconstruction(self):
        m = mission_service.create_mission(run_id="RUN-1", title="M")
        t1 = task_registry.create_task(mission_id=m.mission_id, title="T1")
        t2 = task_registry.create_task(mission_id=m.mission_id, title="T2")
        t3 = task_registry.create_task(mission_id=m.mission_id, title="T3")
        self.assertEqual(len(m.tasks), 0)  # create_mission creates empty; tasks via registry not auto-appended
        # simulate mission.tasks should be rebuilt after recovery
        self._recover()
        rm = mission_service.get_mission(m.mission_id)
        self.assertEqual(len(rm.tasks), 3)
        tids = {t.task_id for t in rm.tasks}
        self.assertEqual(tids, {t1.task_id, t2.task_id, t3.task_id})
        # deterministic order by task_id
        sorted_ids = sorted([t1.task_id, t2.task_id, t3.task_id])
        self.assertEqual([t.task_id for t in rm.tasks], sorted_ids)
        self.assertTrue(len(rm.tasks) > 0)

    def test_idempotent_double_recovery_no_duplicates(self):
        m = mission_service.create_mission(run_id="RUN-1", title="M")
        t = task_registry.create_task(mission_id=m.mission_id, title="T")
        a = task_registry.create_attempt(t.task_id, agent_id="AGT-1", model="gpt")
        from nexus.board.recovery import recover_all, clear_memory
        clear_memory()
        recover_all()
        counts1 = (len(mission_service._missions), len(task_registry._tasks), len(task_registry.list_attempts(t.task_id)), len(board_service.get_board(m.mission_id).columns[BoardColumn.TODO]))
        recover_all()
        counts2 = (len(mission_service._missions), len(task_registry._tasks), len(task_registry.list_attempts(t.task_id)), len(board_service.get_board(m.mission_id).columns[BoardColumn.TODO]))
        self.assertEqual(counts1, counts2)
        # also mission.tasks no duplicates
        rm = mission_service.get_mission(m.mission_id)
        self.assertEqual(len(rm.tasks), 1)
        self.assertEqual(len(set(x.task_id for x in rm.tasks)), 1)
        # board cards no duplicates
        board = board_service.get_board(m.mission_id)
        all_cards = []
        for col in BoardColumn:
            all_cards.extend(board.columns[col])
        self.assertEqual(len(all_cards), len(set(all_cards)))

    def test_corrupt_json_fails_closed(self):
        m = mission_service.create_mission(run_id="RUN-1", title="M")
        t = task_registry.create_task(mission_id=m.mission_id, title="T", execution_policy={"a":1})
        # corrupt JSON directly
        with database.get_connection() as c:
            c.execute("UPDATE tasks SET execution_policy=? WHERE task_id=?", ("{not-json", t.task_id))
            c.commit()
        from nexus.board.recovery import clear_memory, rehydrate_missions, rehydrate_tasks_and_attempts
        clear_memory()
        rehydrate_missions()
        with self.assertRaises(ValueError):
            rehydrate_tasks_and_attempts()

    def test_corrupt_mission_status_fails_closed(self):
        m = mission_service.create_mission(run_id="RUN-1", title="M")
        with database.get_connection() as c:
            c.execute("UPDATE missions SET status=? WHERE mission_id=?", ("BOGUS", m.mission_id))
            c.commit()
        from nexus.board.recovery import clear_memory
        clear_memory()
        from nexus.missions.persistence import load_missions
        with self.assertRaises(ValueError):
            load_missions()

    def test_corrupt_task_status_fails_closed(self):
        m = mission_service.create_mission(run_id="RUN-1", title="M")
        t = task_registry.create_task(mission_id=m.mission_id, title="T")
        with database.get_connection() as c:
            c.execute("UPDATE tasks SET status=? WHERE task_id=?", ("BOGUS", t.task_id))
            c.commit()
        from nexus.board.recovery import clear_memory
        clear_memory()
        from nexus.missions.persistence import load_missions
        from nexus.tasks.persistence import load_tasks
        mission_service._missions[load_missions()[0].mission_id] = load_missions()[0]
        with self.assertRaises(ValueError):
            load_tasks()

    def test_corrupt_attempt_status_fails_closed(self):
        m = mission_service.create_mission(run_id="RUN-1", title="M")
        t = task_registry.create_task(mission_id=m.mission_id, title="T")
        a = task_registry.create_attempt(t.task_id, agent_id="AGT-1", model="gpt")
        with database.get_connection() as c:
            c.execute("UPDATE attempts SET status=? WHERE attempt_id=?", ("BOGUS", a.attempt_id))
            c.commit()
        from nexus.board.recovery import clear_memory
        clear_memory()
        from nexus.missions.persistence import load_missions
        from nexus.tasks.persistence import load_attempts
        for mm in load_missions():
            mission_service._missions[mm.mission_id]=mm
        from nexus.tasks.persistence import load_tasks
        for tt in load_tasks():
            task_registry._tasks[tt.task_id]=tt
        with self.assertRaises(ValueError):
            load_attempts()

    def test_orphan_task_fails_closed(self):
        m = mission_service.create_mission(run_id="RUN-1", title="M")
        from nexus.tasks.persistence import _ensure as _ensure_t
        _ensure_t()
        # create orphan by inserting task with non-existent mission
        with database.get_connection() as c:
            c.execute("INSERT INTO tasks(task_id,mission_id,title,description,status,priority,dependencies,assigned_agent,execution_policy,acceptance_criteria) VALUES (?,?,?,?,?,?,?,?,?,?)", ("TASK-ORPHAN","MISSION-NONEXIST","orphan","", "CREATED","MEDIUM",None,None,None,None))
            c.commit()
        from nexus.board.recovery import clear_memory, rehydrate_missions, rehydrate_tasks_and_attempts
        clear_memory()
        rehydrate_missions()
        with self.assertRaises(ValueError):
            rehydrate_tasks_and_attempts()

    def test_orphan_attempt_fails_closed(self):
        m = mission_service.create_mission(run_id="RUN-1", title="M")
        t = task_registry.create_task(mission_id=m.mission_id, title="T")
        with database.get_connection() as c:
            c.execute("INSERT INTO attempts(attempt_id,task_id,agent_id,model,status,result) VALUES (?,?,?,?,?,?)", ("ATT-ORPHAN-1","TASK-NONEXIST","AGT","gpt","PENDING",None))
            c.commit()
        from nexus.board.recovery import clear_memory, rehydrate_missions, rehydrate_tasks_and_attempts
        clear_memory()
        rehydrate_missions()
        # need tasks loaded first; rehydrate_tasks_and_attempts will fail on orphan attempt
        with self.assertRaises(ValueError):
            rehydrate_tasks_and_attempts()

    def test_legacy_empty_db_safe(self):
        from nexus.board.recovery import recover_all, clear_memory
        clear_memory()
        # empty DB should not raise
        result = recover_all()
        self.assertTrue(result)
        self.assertEqual(len(mission_service._missions), 0)
        self.assertEqual(len(task_registry._tasks), 0)

    def test_recovery_does_not_invoke_manager_worker(self):
        m = mission_service.create_mission(run_id="RUN-1", title="M")
        t = task_registry.create_task(mission_id=m.mission_id, title="T")
        with patch("nexus.missions.generator.generate_mission_from_plan") as mock_gen, patch("nexus.orchestration.resume.resume_run") as mock_resume:
            from nexus.board.recovery import clear_memory, recover_all
            clear_memory()
            recover_all()
            mock_gen.assert_not_called()
            mock_resume.assert_not_called()

    def test_move_task_not_durable_board_only(self):
        m = mission_service.create_mission(run_id="RUN-1", title="M")
        t = task_registry.create_task(mission_id=m.mission_id, title="T")
        # create board and move card arbitrarily
        board_service.create_board(m)
        board_service.move_task(t.task_id, BoardColumn.DONE)
        self.assertIn(t.task_id, board_service.get_board(m.mission_id).columns[BoardColumn.DONE])
        # after recovery, board follows Task.status (CREATED -> TODO) not DONE
        from nexus.board.recovery import clear_memory, recover_all
        clear_memory()
        recover_all()
        board2 = board_service.get_board(m.mission_id)
        self.assertIn(t.task_id, board2.columns[BoardColumn.TODO])
        self.assertNotIn(t.task_id, board2.columns[BoardColumn.DONE])
        # historical ledgers must be empty after recovery
        self.assertEqual(len(board2.events), 0)
        self.assertEqual(len(board2.assignments), 0)

    def test_board_unknown_status_fails_closed(self):
        m = mission_service.create_mission(run_id="RUN-1", title="M")
        t = task_registry.create_task(mission_id=m.mission_id, title="T")
        with database.get_connection() as c:
            c.execute("UPDATE tasks SET status=? WHERE task_id=?", ("UNKNOWN_STATUS", t.task_id))
            c.commit()
        from nexus.board.recovery import clear_memory
        clear_memory()
        # bypass task status validation by patching TASK_STATUSES temporarily
        import nexus.tasks.models as models
        orig = models.TASK_STATUSES
        models.TASK_STATUSES = orig | {"UNKNOWN_STATUS"}
        try:
            from nexus.board.recovery import rehydrate_missions, rehydrate_tasks_and_attempts, rebuild_boards
            rehydrate_missions()
            rehydrate_tasks_and_attempts()
            with self.assertRaises(ValueError):
                rebuild_boards()
        finally:
            models.TASK_STATUSES = orig

    def test_persistence_failure_rolls_back(self):
        m = mission_service.create_mission(run_id="RUN-1", title="M")
        with patch("nexus.tasks.persistence.persist_task", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                task_registry.create_task(mission_id=m.mission_id, title="fail")
        # no orphan in memory
        self.assertEqual(len([t for t in task_registry.list_tasks() if t.title=="fail"]), 0)
    def test_create_mission_from_plan_atomic_no_orphan_on_bundle_failure(self):
        plan = {"title": "Atomic", "workers": [{"scope": "T1"}, {"scope": "T2"}]}
        with patch("nexus.missions.persistence.persist_mission_bundle", side_effect=RuntimeError("bundle down")):
            with self.assertRaises(RuntimeError):
                mission_service.create_mission_from_plan(plan, run_id="RUN-BUNDLE-MMAI")
        import sqlite3
        con = sqlite3.connect(str(database.DATABASE_PATH))
        try:
            try:
                m_cnt = con.execute("SELECT COUNT(1) FROM missions").fetchone()[0]
            except Exception:
                m_cnt = 0
            try:
                t_cnt = con.execute("SELECT COUNT(1) FROM tasks").fetchone()[0]
            except Exception:
                t_cnt = 0
        finally:
            con.close()
        self.assertEqual(m_cnt, 0)
        self.assertEqual(t_cnt, 0)
        self.assertEqual(len(mission_service.list_missions()), 0)
        self.assertEqual(len(task_registry.list_tasks()), 0)

    def test_plan_materialization_does_not_individually_persist_tasks(self):
        plan = {"title": "NoIndiv", "workers": [{"scope": "T1"}, {"scope": "T2"}]}
        with patch("nexus.tasks.persistence.persist_task", side_effect=AssertionError("should not persist individually")):
            with patch("nexus.missions.persistence.persist_mission_bundle") as mock_bundle:
                mission_service.create_mission_from_plan(plan, run_id="RUN-NO-INDIV")
                mock_bundle.assert_called_once()
        self.assertEqual(len(mission_service.list_missions()), 1)
        self.assertEqual(len(task_registry.list_tasks()), 2)

    def test_assign_task_persistence_failure_restores_board_and_agent(self):
        m = mission_service.create_mission(run_id="RUN-A", title="M")
        t = task_registry.create_task(mission_id=m.mission_id, title="T")
        board = board_service.create_board(m)
        orig_cols = {col: list(board.columns[col]) for col in BoardColumn}
        self.assertIsNone(t.assigned_agent)
        with patch("nexus.tasks.persistence.persist_task", side_effect=RuntimeError("persist down")):
            with self.assertRaises(RuntimeError):
                board_service.assign_task(t.task_id, "agent-1")
        self.assertIsNone(t.assigned_agent)
        self.assertEqual(dict(board.columns), dict(orig_cols))
        self.assertEqual(len(board.assignments), 0)

    def test_create_attempt_persistence_failure_rolls_back(self):
        m = mission_service.create_mission(run_id="RUN-ATT", title="M")
        t = task_registry.create_task(mission_id=m.mission_id, title="T")
        before = list(task_registry.list_attempts(t.task_id))
        before_next = task_registry._next_attempt.get(t.task_id, 1)
        with patch("nexus.tasks.persistence.persist_attempt", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                task_registry.create_attempt(t.task_id, agent_id="AGT-1", model="gpt")
        self.assertEqual(task_registry.list_attempts(t.task_id), before)
        self.assertEqual(task_registry._next_attempt.get(t.task_id, 1), before_next)
