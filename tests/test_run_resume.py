import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.orchestration.progressive import execute_progressively
from nexus.orchestration.resume import ResumeError, resume_run
from nexus.registry.agents import create_agent
from nexus.registry.checkpoints import (
    get_approved_plan,
    list_checkpoints,
    record_approved_plan,
    record_checkpoint,
)
from nexus.registry.runs import create_run, get_run, update_run_status


FIXTURE_PROJECTS = {
    "projects": [
        {
            "id": "norte",
            "name": "Norte",
            "path": r"C:\fake\interface-life",
            "aliases": ["norte"],
            "enabled": True,
        },
    ]
}


TWO_WORKER_PLAN = {
    "complexity": "LOW",
    "risk": "LOW",
    "parallelism": 1,
    "summary": "Do two small fixes.",
    "workers": [
        {
            "route_class": "mechanical",
            "execution_path": "OMNIROUTE",
            "provider": "omniroute",
            "model": "oc/big-pickle",
            "effort": "low",
            "scope": "Fix the first thing.",
            "reason": "Simple mechanical change.",
        },
        {
            "route_class": "mechanical",
            "execution_path": "OMNIROUTE",
            "provider": "omniroute",
            "model": "oc/big-pickle",
            "effort": "low",
            "scope": "Fix the second thing.",
            "reason": "Simple mechanical change.",
        },
    ],
}


ONE_WORKER_PLAN = {
    "complexity": "LOW",
    "risk": "LOW",
    "parallelism": 1,
    "summary": "Do a small fix.",
    "workers": [
        {
            "route_class": "mechanical",
            "execution_path": "OMNIROUTE",
            "provider": "omniroute",
            "model": "oc/big-pickle",
            "effort": "low",
            "scope": "Fix the thing.",
            "reason": "Simple mechanical change.",
        },
    ],
}


class RunResumeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp_dir.name)

        self._original_db_path = database.DATABASE_PATH
        self._original_storage_dir = database.STORAGE_DIR
        self._original_projects_file = projects.PROJECTS_FILE

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus-test.db"

        fixture_path = tmp_path / "projects.json"
        fixture_path.write_text(
            json.dumps(FIXTURE_PROJECTS), encoding="utf-8"
        )
        projects.PROJECTS_FILE = fixture_path

        database.initialize_database()
        projects.sync_projects()

        # A real, existing worktree directory. Tests that claim an
        # existing worktree must use this, never a fake nonexistent path.
        self._worktree_dir = tmp_path / "worker-worktree"
        self._worktree_dir.mkdir(parents=True, exist_ok=True)
        self.worktree = str(self._worktree_dir)

    def tearDown(self) -> None:
        database.DATABASE_PATH = self._original_db_path
        database.STORAGE_DIR = self._original_storage_dir
        projects.PROJECTS_FILE = self._original_projects_file
        self._tmp_dir.cleanup()

    def _make_run(self, status: str = "RUNNING") -> str:
        run_id = create_run(
            project_id="norte",
            input_text="Norte: do a thing",
            intent="GO",
            status=status,
        )
        return run_id

    def _record_plan_approved(self, run_id, manager_id="AGENT-MANAGER", worker_count=1) -> None:
        record_checkpoint(
            run_id=run_id,
            boundary="PLAN_APPROVED",
            payload={"manager_id": manager_id, "worker_count": worker_count},
        )

    def _register_worker_agent(
        self,
        run_id,
        role="Worker",
        status="COMPLETED",
        worktree=None,
        model="oc/big-pickle",
        provider="omniroute",
    ) -> str:
        worktree = worktree if worktree is not None else self.worktree
        agent_id = create_agent(
            run_id=run_id,
            role=role,
            provider=provider,
            model=model,
            effort="low",
            status="RUNNING",
        )
        from nexus.registry.agents import update_agent_execution, update_agent_status

        update_agent_execution(agent_id, worktree=worktree)
        update_agent_status(agent_id, status)
        return agent_id

    def _record_execution_start(self, run_id, manager_id="AGENT-MANAGER", worker_ordinal=1) -> None:
        record_checkpoint(
            run_id=run_id,
            boundary="EXECUTION_START",
            payload={
                "route_class": "mechanical",
                "model": "oc/big-pickle",
                "provider": "omniroute",
                "effort": "low",
                "execution_path": "OMNIROUTE",
                "manager_id": manager_id,
                "capability": "mechanical",
                "risk": "LOW",
                "degraded": False,
            },
            worker_ordinal=worker_ordinal,
        )

    def _record_worker_attempt(
        self,
        run_id,
        worker_id,
        worktree,
        attempt=1,
        status="COMPLETED",
        manager_id="AGENT-MANAGER",
        worker_ordinal=1,
        model="oc/big-pickle",
    ) -> None:
        record_checkpoint(
            run_id=run_id,
            boundary="WORKER_ATTEMPT",
            payload={
                "route_class": "mechanical",
                "model": model,
                "provider": "omniroute",
                "effort": "low",
                "execution_path": "OMNIROUTE",
                "manager_id": manager_id,
                "attempt": attempt,
                "status": status,
                "worker_id": worker_id,
                "worktree": worktree,
            },
            worker_ordinal=worker_ordinal,
        )

    # -- fail-closed guardrails -------------------------------------

    def test_resume_unknown_run_fails_closed(self):
        with self.assertRaises(ResumeError) as ctx:
            resume_run("RUN-DOESNOTEXIST")

        self.assertEqual(ctx.exception.code, "RUN_NOT_FOUND")

    def test_resume_terminal_run_is_rejected(self):
        run_id = self._make_run(status="COMPLETED")

        with self.assertRaises(ResumeError) as ctx:
            resume_run(run_id)

        self.assertEqual(ctx.exception.code, "RUN_TERMINAL")

    def test_resume_awaiting_approval_run_is_rejected(self):
        run_id = self._make_run(status="AWAITING_APPROVAL")

        with self.assertRaises(ResumeError) as ctx:
            resume_run(run_id)

        self.assertEqual(ctx.exception.code, "RESUME_NOT_AVAILABLE")

    def test_resume_without_approved_plan_fails_closed(self):
        run_id = self._make_run(status="RUNNING")

        with self.assertRaises(ResumeError) as ctx:
            resume_run(run_id)

        self.assertEqual(ctx.exception.code, "RESUME_PLAN_MISSING")

    def test_resume_missing_project_fails_closed(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)

        with database.get_connection() as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "UPDATE runs SET project_id = ? WHERE id = ?",
                ("ghost-project", run_id),
            )

        with self.assertRaises(ResumeError) as ctx:
            resume_run(run_id)

        self.assertEqual(ctx.exception.code, "RUN_NOT_FOUND")

    # -- 1. Durable PLAN_APPROVED checkpoint is mandatory -------------

    def test_resume_approved_plan_row_without_plan_approved_checkpoint_fails_closed(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)

        with self.assertRaises(ResumeError) as ctx:
            resume_run(run_id)

        self.assertEqual(ctx.exception.code, "RESUME_CHECKPOINT_MISSING")

    def test_resume_plan_approved_missing_manager_id_fails_closed(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        record_checkpoint(
            run_id=run_id,
            boundary="PLAN_APPROVED",
            payload={"worker_count": 1},
        )

        with self.assertRaises(ResumeError) as ctx:
            resume_run(run_id)

        self.assertEqual(ctx.exception.code, "RESUME_CHECKPOINT_MISSING")

    def test_resume_plan_approved_contradictory_manager_id_fails_closed(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        record_checkpoint(
            run_id=run_id,
            boundary="PLAN_APPROVED",
            payload={"manager_id": "AGENT-MANAGER-A", "worker_count": 1},
        )
        record_checkpoint(
            run_id=run_id,
            boundary="PLAN_APPROVED",
            payload={"manager_id": "AGENT-MANAGER-B", "worker_count": 1},
        )

        with self.assertRaises(ResumeError) as ctx:
            resume_run(run_id)

        self.assertEqual(ctx.exception.code, "RESUME_STATE_INCONSISTENT")

    def test_manager_is_never_invoked_during_resume(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        with patch("nexus.orchestration.resume.execute_progressively") as exec_mock, \
             patch("nexus.dispatchers.manager.run_manager") as manager_mock:

            exec_mock.return_value = {
                "status": "COMPLETED",
                "verdict": "PASS",
                "history": [],
                "worker": {"status": "COMPLETED"},
                "review": {"verdict": "PASS"},
            }

            resume_run(run_id)

            manager_mock.assert_not_called()

    def test_original_run_id_and_plan_are_preserved(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        with patch("nexus.orchestration.resume.execute_progressively") as exec_mock:
            exec_mock.return_value = {
                "status": "COMPLETED",
                "verdict": "PASS",
                "history": [],
                "worker": {"status": "COMPLETED"},
                "review": {"verdict": "PASS"},
            }

            result = resume_run(run_id)

        self.assertEqual(result["run_id"], run_id)
        self.assertEqual(get_approved_plan(run_id), ONE_WORKER_PLAN)

    def test_resume_start_checkpoint_is_recorded(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        with patch("nexus.orchestration.resume.execute_progressively") as exec_mock:
            exec_mock.return_value = {
                "status": "COMPLETED",
                "verdict": "PASS",
                "history": [],
                "worker": {"status": "COMPLETED"},
                "review": {"verdict": "PASS"},
            }

            resume_run(run_id)

        boundaries = [c["boundary"] for c in list_checkpoints(run_id)]
        self.assertIn("RESUME_START", boundaries)

    # -- branch B: skip already-PASSed workers -----------------------

    def test_resume_skips_already_passed_worker(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, TWO_WORKER_PLAN)
        self._record_plan_approved(run_id, worker_count=2)

        record_checkpoint(
            run_id=run_id,
            boundary="LIFECYCLE",
            payload={"attempt": 1, "verdict": "PASS"},
            worker_ordinal=1,
        )

        with patch("nexus.orchestration.resume.execute_progressively") as exec_mock:
            exec_mock.return_value = {
                "status": "COMPLETED",
                "verdict": "PASS",
                "history": [],
                "worker": {"status": "COMPLETED"},
                "review": {"verdict": "PASS"},
            }

            resume_run(run_id)

            called_ordinals = [
                call.kwargs["worker_ordinal"]
                for call in exec_mock.call_args_list
            ]

        self.assertEqual(called_ordinals, [2])

    # -- branch D: all workers already PASSed -> no-op completion ----

    def test_resume_with_all_workers_passed_completes_without_execution(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        record_checkpoint(
            run_id=run_id,
            boundary="LIFECYCLE",
            payload={"attempt": 1, "verdict": "PASS"},
            worker_ordinal=1,
        )

        with patch("nexus.orchestration.resume.execute_progressively") as exec_mock:
            result = resume_run(run_id)

            exec_mock.assert_not_called()

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(get_run(run_id)["status"], "COMPLETED")

    def test_repeated_resume_is_deterministic(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        record_checkpoint(
            run_id=run_id,
            boundary="LIFECYCLE",
            payload={"attempt": 1, "verdict": "PASS"},
            worker_ordinal=1,
        )

        first = resume_run(run_id)

        with self.assertRaises(ResumeError) as ctx:
            resume_run(run_id)

        self.assertEqual(ctx.exception.code, "RUN_TERMINAL")
        self.assertEqual(first["status"], "COMPLETED")

    def test_second_interruption_resume_remains_deterministic(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, TWO_WORKER_PLAN)
        self._record_plan_approved(run_id, worker_count=2)

        record_checkpoint(
            run_id=run_id,
            boundary="LIFECYCLE",
            payload={"attempt": 1, "verdict": "PASS"},
            worker_ordinal=1,
        )

        with patch("nexus.orchestration.resume.execute_progressively") as exec_mock:
            exec_mock.return_value = {
                "status": "BLOCKED",
                "reason": "REVIEW_FAILED",
                "history": [],
                "review": {"status": "FAILED"},
            }

            with self.assertRaises(ResumeError):
                resume_run(run_id)

        self.assertEqual(get_run(run_id)["status"], "BLOCKED")

        update_run_status(run_id, "RUNNING")

        with patch("nexus.orchestration.resume.execute_progressively") as exec_mock:
            exec_mock.return_value = {
                "status": "COMPLETED",
                "verdict": "PASS",
                "history": [],
                "worker": {"status": "COMPLETED"},
                "review": {"verdict": "PASS"},
            }

            result = resume_run(run_id)

            called_ordinals = [
                call.kwargs["worker_ordinal"]
                for call in exec_mock.call_args_list
            ]

        self.assertEqual(called_ordinals, [2])
        self.assertEqual(result["status"], "COMPLETED")

    # -- branch A: worker never started (only PLAN_APPROVED) ----------

    def test_resume_worker_never_started_executes_fresh(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        decision = unittest.mock.Mock(
            execution_path="OMNIROUTE",
            provider="omniroute",
            model="oc/big-pickle",
            effort="low",
            reason="test",
            degraded=False,
        )

        with patch(
            "nexus.orchestration.progressive.AdaptiveRoutingService"
        ) as routing_cls, patch(
            "nexus.orchestration.progressive.execute_worker"
        ) as execute_worker_mock, patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:

            routing_cls.return_value.select_route_for_capability.return_value = (
                decision
            )

            execute_worker_mock.return_value = {
                "status": "COMPLETED",
                "agent_id": "AGENT-WORKER",
                "worktree": self.worktree,
            }

            review_worker_mock.return_value = {
                "status": "COMPLETED",
                "reviewer_id": "AGENT-REVIEWER",
                "review": {
                    "verdict": "PASS",
                    "failure_class": None,
                    "summary": "Approved.",
                },
                "routing": {
                    "model": "cc/claude-sonnet-5-high",
                    "provider": "claude",
                    "effort": "high",
                    "execution_path": "OMNIROUTE",
                    "reason": "test",
                    "degraded": False,
                },
            }

            result = resume_run(run_id)

            execute_worker_mock.assert_called_once()

        self.assertEqual(result["status"], "COMPLETED")

    # -- branch C: WORKER_ATTEMPT COMPLETED but review absent ---------

    def test_resume_review_only_reuses_existing_worker(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        worker_id = self._register_worker_agent(run_id)
        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, worker_id, self.worktree)

        with patch(
            "nexus.orchestration.progressive.execute_worker"
        ) as execute_worker_mock, patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:

            review_worker_mock.return_value = {
                "status": "COMPLETED",
                "reviewer_id": "AGENT-REVIEWER",
                "review": {
                    "verdict": "PASS",
                    "failure_class": None,
                    "summary": "Approved.",
                },
                "routing": {
                    "model": "cc/claude-sonnet-5-high",
                    "provider": "claude",
                    "effort": "high",
                    "execution_path": "OMNIROUTE",
                    "reason": "test",
                    "degraded": False,
                },
            }

            result = resume_run(run_id)

            execute_worker_mock.assert_not_called()
            review_worker_mock.assert_called_once()

            _, review_kwargs = review_worker_mock.call_args
            self.assertEqual(review_kwargs["worker_id"], worker_id)
            self.assertEqual(review_kwargs["worktree"], self.worktree)

        self.assertEqual(result["status"], "COMPLETED")

    # -- 2. Review-only resume must verify the existing Worker Agent --

    def test_resume_review_only_missing_worker_agent_fails_closed(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, "AGENT-WORKER-GHOST", self.worktree)

        with patch(
            "nexus.orchestration.progressive.execute_worker"
        ) as execute_worker_mock, patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:

            with self.assertRaises(ResumeError) as ctx:
                resume_run(run_id)

            execute_worker_mock.assert_not_called()
            review_worker_mock.assert_not_called()

        self.assertEqual(ctx.exception.code, "RESUME_STATE_INCONSISTENT")

    def test_resume_review_only_wrong_run_worker_agent_fails_closed(self):
        run_id = self._make_run(status="RUNNING")
        other_run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        worker_id = self._register_worker_agent(other_run_id)

        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, worker_id, self.worktree)

        with patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:
            with self.assertRaises(ResumeError) as ctx:
                resume_run(run_id)

            review_worker_mock.assert_not_called()

        self.assertEqual(ctx.exception.code, "RESUME_STATE_INCONSISTENT")

    def test_resume_review_only_wrong_agent_role_fails_closed(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        worker_id = self._register_worker_agent(run_id, role="ManagerReview")

        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, worker_id, self.worktree)

        with patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:
            with self.assertRaises(ResumeError) as ctx:
                resume_run(run_id)

            review_worker_mock.assert_not_called()

        self.assertEqual(ctx.exception.code, "RESUME_STATE_INCONSISTENT")

    def test_resume_review_only_wrong_agent_status_fails_closed(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        worker_id = self._register_worker_agent(run_id, status="RUNNING")

        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, worker_id, self.worktree)

        with patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:
            with self.assertRaises(ResumeError) as ctx:
                resume_run(run_id)

            review_worker_mock.assert_not_called()

        self.assertEqual(ctx.exception.code, "RESUME_STATE_INCONSISTENT")

    def test_resume_review_only_agent_worktree_mismatch_fails_closed(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        other_dir = Path(self._tmp_dir.name) / "other-worktree"
        other_dir.mkdir(parents=True, exist_ok=True)

        worker_id = self._register_worker_agent(run_id, worktree=str(other_dir))

        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, worker_id, self.worktree)

        with patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:
            with self.assertRaises(ResumeError) as ctx:
                resume_run(run_id)

            review_worker_mock.assert_not_called()

        self.assertEqual(ctx.exception.code, "RESUME_STATE_INCONSISTENT")

    # -- 3. Physical worktree existence -------------------------------

    def test_resume_physical_worktree_missing_fails_closed(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        missing_dir = str(Path(self._tmp_dir.name) / "does-not-exist-worktree")

        worker_id = self._register_worker_agent(run_id, worktree=missing_dir)

        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, worker_id, missing_dir)

        with patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:
            with self.assertRaises(ResumeError) as ctx:
                resume_run(run_id)

            review_worker_mock.assert_not_called()

        self.assertEqual(ctx.exception.code, "RESUME_WORKTREE_MISSING")

    def test_resume_worker_failure_blocked_bubbles_up(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        with patch("nexus.orchestration.resume.execute_progressively") as exec_mock:
            exec_mock.return_value = {
                "status": "FAILED",
                "reason": "WORKER_EXECUTION_FAILED",
                "history": [],
                "worker": {"status": "FAILED"},
            }

            with self.assertRaises(ResumeError) as ctx:
                resume_run(run_id)

        self.assertEqual(ctx.exception.code, "WORKER_EXECUTION_FAILED")
        self.assertEqual(get_run(run_id)["status"], "FAILED")

    # -- fail-closed: missing worktree ---------------------------------

    def test_resume_missing_worktree_fails_closed(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        self._record_execution_start(run_id)
        record_checkpoint(
            run_id=run_id,
            boundary="WORKER_ATTEMPT",
            payload={
                "route_class": "mechanical",
                "model": "oc/big-pickle",
                "provider": "omniroute",
                "effort": "low",
                "execution_path": "OMNIROUTE",
                "manager_id": "AGENT-MANAGER",
                "attempt": 1,
                "status": "COMPLETED",
                "worker_id": "AGENT-WORKER-EXISTING",
                "worktree": None,
            },
            worker_ordinal=1,
        )

        with self.assertRaises(ResumeError) as ctx:
            resume_run(run_id)

        self.assertEqual(ctx.exception.code, "RESUME_WORKTREE_MISSING")
        self.assertEqual(get_run(run_id)["status"], "RUNNING")

    def test_resume_worker_attempt_not_completed_fails_closed(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        self._record_execution_start(run_id)
        self._record_worker_attempt(
            run_id, "AGENT-WORKER-EXISTING", self.worktree, status="FAILED"
        )

        with self.assertRaises(ResumeError) as ctx:
            resume_run(run_id)

        self.assertEqual(ctx.exception.code, "RESUME_WORKTREE_MISSING")

    # -- fail-closed: inconsistent state --------------------------------

    def test_resume_state_inconsistent_when_review_already_completed(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        worker_id = self._register_worker_agent(run_id)
        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, worker_id, self.worktree)
        record_checkpoint(
            run_id=run_id,
            boundary="REVIEW",
            payload={
                "attempt": 1,
                "status": "COMPLETED",
                "worker_id": worker_id,
                "verdict": "PASS",
                "summary": "Approved.",
            },
            worker_ordinal=1,
        )

        with patch(
            "nexus.orchestration.progressive.execute_worker"
        ) as execute_worker_mock, patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:
            result = resume_run(run_id)

            execute_worker_mock.assert_not_called()
            review_worker_mock.assert_not_called()

        self.assertEqual(result["status"], "COMPLETED")

        boundaries = [
            (c["boundary"], c["payload"].get("verdict"))
            for c in list_checkpoints(run_id)
            if c["boundary"] == "LIFECYCLE"
        ]
        self.assertIn(("LIFECYCLE", "PASS"), boundaries)

    def test_resume_checkpoint_missing_when_no_execution_start(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        record_checkpoint(
            run_id=run_id,
            boundary="WORKER_ATTEMPT",
            payload={
                "route_class": "mechanical",
                "model": "oc/big-pickle",
                "provider": "omniroute",
                "effort": "low",
                "execution_path": "OMNIROUTE",
                "manager_id": "AGENT-MANAGER",
                "attempt": 1,
                "status": "COMPLETED",
                "worker_id": "AGENT-WORKER-EXISTING",
                "worktree": self.worktree,
            },
            worker_ordinal=1,
        )

        with self.assertRaises(ResumeError) as ctx:
            resume_run(run_id)

        self.assertEqual(ctx.exception.code, "RESUME_CHECKPOINT_MISSING")

    # -- 4. persisted REVIEW/no LIFECYCLE never calls review_worker again

    def test_persisted_review_pass_recovery_never_calls_review_worker(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        worker_id = self._register_worker_agent(run_id)
        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, worker_id, self.worktree)
        record_checkpoint(
            run_id=run_id,
            boundary="REVIEW",
            payload={
                "attempt": 1,
                "status": "COMPLETED",
                "worker_id": worker_id,
                "verdict": "PASS",
                "summary": "Approved.",
            },
            worker_ordinal=1,
        )

        with patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:
            result = resume_run(run_id)
            review_worker_mock.assert_not_called()

        self.assertEqual(result["status"], "COMPLETED")

    def test_persisted_review_retry_recovery_never_calls_review_worker(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        worker_id = self._register_worker_agent(run_id)
        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, worker_id, self.worktree)
        record_checkpoint(
            run_id=run_id,
            boundary="REVIEW",
            payload={
                "attempt": 1,
                "status": "COMPLETED",
                "worker_id": worker_id,
                "verdict": "RETRY",
                "failure_class": "TESTS_FAILED",
                "summary": "Tests failed.",
            },
            worker_ordinal=1,
        )

        with patch(
            "nexus.orchestration.progressive.execute_worker"
        ) as execute_worker_mock, patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:
            execute_worker_mock.return_value = {
                "status": "COMPLETED",
                "agent_id": "AGENT-WORKER-ATTEMPT-2",
                "worktree": self.worktree,
            }
            review_worker_mock.return_value = {
                "status": "COMPLETED",
                "reviewer_id": "AGENT-REVIEWER",
                "review": {
                    "verdict": "PASS",
                    "failure_class": None,
                    "summary": "Approved.",
                },
                "routing": {
                    "model": "cc/claude-sonnet-5-high",
                    "provider": "claude",
                    "effort": "high",
                    "execution_path": "OMNIROUTE",
                    "reason": "test",
                    "degraded": False,
                },
            }

            result = resume_run(run_id)

            review_worker_mock.assert_called_once()
            execute_worker_mock.assert_called_once()

            _, execute_kwargs = execute_worker_mock.call_args
            self.assertEqual(execute_kwargs["worker_index"], 2)

            call_task = execute_kwargs.get("previous_failure")
            self.assertIsNotNone(call_task)
            self.assertIn("Tests failed.", call_task)

        self.assertEqual(result["status"], "COMPLETED")

    # -- duplicate-REVIEW safety (HIGH reviewer fix) -------------------

    def test_persisted_review_pass_no_duplicate_review_checkpoint(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        worker_id = self._register_worker_agent(run_id)
        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, worker_id, self.worktree)
        record_checkpoint(
            run_id=run_id,
            boundary="REVIEW",
            payload={
                "attempt": 1,
                "status": "COMPLETED",
                "worker_id": worker_id,
                "verdict": "PASS",
                "summary": "Approved.",
            },
            worker_ordinal=1,
        )

        with patch(
            "nexus.orchestration.progressive.execute_worker"
        ) as execute_worker_mock, patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:
            result = resume_run(run_id)

            execute_worker_mock.assert_not_called()
            review_worker_mock.assert_not_called()

        self.assertEqual(result["status"], "COMPLETED")

        checkpoints = list_checkpoints(run_id)
        review_checkpoints = [
            c for c in checkpoints if c["boundary"] == "REVIEW"
        ]
        lifecycle_checkpoints = [
            c for c in checkpoints if c["boundary"] == "LIFECYCLE"
        ]

        self.assertEqual(len(review_checkpoints), 1)
        self.assertEqual(len(lifecycle_checkpoints), 1)
        self.assertEqual(lifecycle_checkpoints[0]["payload"]["verdict"], "PASS")

    def test_persisted_review_retry_no_duplicate_review_checkpoint(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        worker_id = self._register_worker_agent(run_id)
        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, worker_id, self.worktree)
        record_checkpoint(
            run_id=run_id,
            boundary="REVIEW",
            payload={
                "attempt": 1,
                "status": "COMPLETED",
                "worker_id": worker_id,
                "verdict": "RETRY",
                "failure_class": "TESTS_FAILED",
                "summary": "Tests failed.",
            },
            worker_ordinal=1,
        )

        with patch(
            "nexus.orchestration.progressive.execute_worker"
        ) as execute_worker_mock, patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:
            execute_worker_mock.return_value = {
                "status": "COMPLETED",
                "agent_id": "AGENT-WORKER-ATTEMPT-2",
                "worktree": self.worktree,
            }
            review_worker_mock.return_value = {
                "status": "COMPLETED",
                "reviewer_id": "AGENT-REVIEWER",
                "review": {
                    "verdict": "PASS",
                    "failure_class": None,
                    "summary": "Approved.",
                },
                "routing": {
                    "model": "cc/claude-sonnet-5-high",
                    "provider": "claude",
                    "effort": "high",
                    "execution_path": "OMNIROUTE",
                    "reason": "test",
                    "degraded": False,
                },
            }

            result = resume_run(run_id)

            execute_worker_mock.assert_called_once()
            review_worker_mock.assert_called_once()

        self.assertEqual(result["status"], "COMPLETED")

        checkpoints = list_checkpoints(run_id)
        attempt_1_reviews = [
            c
            for c in checkpoints
            if c["boundary"] == "REVIEW" and c["payload"]["attempt"] == 1
        ]
        attempt_2_reviews = [
            c
            for c in checkpoints
            if c["boundary"] == "REVIEW" and c["payload"]["attempt"] == 2
        ]

        self.assertEqual(len(attempt_1_reviews), 1)
        self.assertEqual(len(attempt_2_reviews), 1)
        self.assertEqual(attempt_1_reviews[0]["payload"]["verdict"], "RETRY")

    def test_persisted_review_escalate_no_duplicate_review_checkpoint(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        worker_id = self._register_worker_agent(run_id)
        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, worker_id, self.worktree)
        record_checkpoint(
            run_id=run_id,
            boundary="REVIEW",
            payload={
                "attempt": 1,
                "status": "COMPLETED",
                "worker_id": worker_id,
                "verdict": "ESCALATE",
                "failure_class": "REPEATED_FAILURE",
                "summary": "Retry budget exhausted.",
            },
            worker_ordinal=1,
        )

        with patch(
            "nexus.orchestration.progressive.execute_worker"
        ) as execute_worker_mock, patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:
            execute_worker_mock.return_value = {
                "status": "COMPLETED",
                "agent_id": "AGENT-WORKER-ATTEMPT-2",
                "worktree": self.worktree,
            }
            review_worker_mock.return_value = {
                "status": "COMPLETED",
                "reviewer_id": "AGENT-REVIEWER",
                "review": {
                    "verdict": "PASS",
                    "failure_class": None,
                    "summary": "Approved.",
                },
                "routing": {
                    "model": "cc/claude-sonnet-5-low",
                    "provider": "omniroute",
                    "effort": "low",
                    "execution_path": "OMNIROUTE",
                    "reason": "test",
                    "degraded": False,
                },
            }

            result = resume_run(run_id)

            # Only the NEW attempt (2) is executed/reviewed live; the
            # persisted attempt-1 REVIEW must never be re-invoked.
            execute_worker_mock.assert_called_once()
            review_worker_mock.assert_called_once()

        self.assertEqual(result["status"], "COMPLETED")

        checkpoints = list_checkpoints(run_id)
        attempt_1_reviews = [
            c
            for c in checkpoints
            if c["boundary"] == "REVIEW" and c["payload"]["attempt"] == 1
        ]
        attempt_2_reviews = [
            c
            for c in checkpoints
            if c["boundary"] == "REVIEW" and c["payload"]["attempt"] == 2
        ]

        self.assertEqual(len(attempt_1_reviews), 1)
        self.assertEqual(len(attempt_2_reviews), 1)
        self.assertEqual(attempt_1_reviews[0]["payload"]["verdict"], "ESCALATE")

    def test_second_interruption_after_persisted_review_never_duplicates(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        worker_id = self._register_worker_agent(run_id)
        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, worker_id, self.worktree)
        record_checkpoint(
            run_id=run_id,
            boundary="REVIEW",
            payload={
                "attempt": 1,
                "status": "COMPLETED",
                "worker_id": worker_id,
                "verdict": "PASS",
                "summary": "Approved.",
            },
            worker_ordinal=1,
        )

        original_record_checkpoint = record_checkpoint

        def crash_after_review_reused(*args, **kwargs):
            if kwargs.get("boundary") == "LIFECYCLE":
                raise RuntimeError("simulated crash before LIFECYCLE persists")
            return original_record_checkpoint(*args, **kwargs)

        with patch(
            "nexus.orchestration.progressive.execute_worker"
        ) as execute_worker_mock, patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock, patch(
            "nexus.orchestration.progressive.record_checkpoint",
            side_effect=crash_after_review_reused,
        ):
            with self.assertRaises(RuntimeError):
                resume_run(run_id)

            execute_worker_mock.assert_not_called()
            review_worker_mock.assert_not_called()

        checkpoints = list_checkpoints(run_id)
        review_checkpoints = [
            c for c in checkpoints if c["boundary"] == "REVIEW"
        ]
        lifecycle_checkpoints = [
            c for c in checkpoints if c["boundary"] == "LIFECYCLE"
        ]

        self.assertEqual(len(review_checkpoints), 1)
        self.assertEqual(len(lifecycle_checkpoints), 0)

        update_run_status(run_id, "RUNNING")

        with patch(
            "nexus.orchestration.progressive.execute_worker"
        ) as execute_worker_mock, patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:
            result = resume_run(run_id)

            execute_worker_mock.assert_not_called()
            review_worker_mock.assert_not_called()

        self.assertEqual(result["status"], "COMPLETED")

        checkpoints = list_checkpoints(run_id)
        review_checkpoints = [
            c for c in checkpoints if c["boundary"] == "REVIEW"
        ]
        lifecycle_checkpoints = [
            c for c in checkpoints if c["boundary"] == "LIFECYCLE"
        ]

        self.assertEqual(len(review_checkpoints), 1)
        self.assertEqual(len(lifecycle_checkpoints), 1)

    # -- 5. durable RETRY lifecycle starts attempt 2 only --------------

    def test_durable_retry_starts_attempt_2_only(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        worker_id = self._register_worker_agent(run_id)
        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, worker_id, self.worktree, attempt=1)
        record_checkpoint(
            run_id=run_id,
            boundary="REVIEW",
            payload={
                "attempt": 1,
                "status": "COMPLETED",
                "worker_id": worker_id,
                "verdict": "RETRY",
                "failure_class": "TESTS_FAILED",
                "summary": "Something broke.",
            },
            worker_ordinal=1,
        )
        record_checkpoint(
            run_id=run_id,
            boundary="LIFECYCLE",
            payload={
                "attempt": 1,
                "verdict": "RETRY",
                "worker_id": worker_id,
                "next_attempt": 2,
            },
            worker_ordinal=1,
        )

        with patch(
            "nexus.orchestration.progressive.execute_worker"
        ) as execute_worker_mock, patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:
            execute_worker_mock.return_value = {
                "status": "COMPLETED",
                "agent_id": "AGENT-WORKER-ATTEMPT-2",
                "worktree": self.worktree,
            }
            review_worker_mock.return_value = {
                "status": "COMPLETED",
                "reviewer_id": "AGENT-REVIEWER",
                "review": {
                    "verdict": "PASS",
                    "failure_class": None,
                    "summary": "Approved.",
                },
                "routing": {
                    "model": "cc/claude-sonnet-5-high",
                    "provider": "claude",
                    "effort": "high",
                    "execution_path": "OMNIROUTE",
                    "reason": "test",
                    "degraded": False,
                },
            }

            result = resume_run(run_id)

            execute_worker_mock.assert_called_once()
            review_worker_mock.assert_called_once()

            _, execute_kwargs = execute_worker_mock.call_args
            self.assertEqual(execute_kwargs["worker_index"], 2)

        self.assertEqual(result["status"], "COMPLETED")

    def test_previous_failure_comes_from_durable_review(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        worker_id = self._register_worker_agent(run_id)
        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, worker_id, self.worktree, attempt=1)
        record_checkpoint(
            run_id=run_id,
            boundary="REVIEW",
            payload={
                "attempt": 1,
                "status": "COMPLETED",
                "worker_id": worker_id,
                "verdict": "RETRY",
                "failure_class": "LINT_FAILED",
                "summary": "Lint failed on file X.",
            },
            worker_ordinal=1,
        )
        record_checkpoint(
            run_id=run_id,
            boundary="LIFECYCLE",
            payload={
                "attempt": 1,
                "verdict": "RETRY",
                "worker_id": worker_id,
                "next_attempt": 2,
            },
            worker_ordinal=1,
        )

        with patch(
            "nexus.orchestration.progressive.execute_worker"
        ) as execute_worker_mock, patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:
            execute_worker_mock.return_value = {
                "status": "COMPLETED",
                "agent_id": "AGENT-WORKER-ATTEMPT-2",
                "worktree": self.worktree,
            }
            review_worker_mock.return_value = {
                "status": "COMPLETED",
                "reviewer_id": "AGENT-REVIEWER",
                "review": {
                    "verdict": "PASS",
                    "failure_class": None,
                    "summary": "Approved.",
                },
                "routing": {
                    "model": "cc/claude-sonnet-5-high",
                    "provider": "claude",
                    "effort": "high",
                    "execution_path": "OMNIROUTE",
                    "reason": "test",
                    "degraded": False,
                },
            }

            resume_run(run_id)

            _, execute_kwargs = execute_worker_mock.call_args
            previous_failure = execute_kwargs["previous_failure"]

        self.assertIn("LINT_FAILED", previous_failure)
        self.assertIn("Lint failed on file X.", previous_failure)

    # -- 6. durable ESCALATE -------------------------------------------

    def test_durable_escalate_deterministic_recovery(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        worker_id = self._register_worker_agent(run_id)
        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, worker_id, self.worktree, attempt=1)
        record_checkpoint(
            run_id=run_id,
            boundary="REVIEW",
            payload={
                "attempt": 1,
                "status": "COMPLETED",
                "worker_id": worker_id,
                "verdict": "ESCALATE",
                "failure_class": "REPEATED_FAILURE",
                "summary": "Retry budget exhausted.",
            },
            worker_ordinal=1,
        )
        record_checkpoint(
            run_id=run_id,
            boundary="LIFECYCLE",
            payload={
                "attempt": 1,
                "verdict": "ESCALATE",
                "worker_id": worker_id,
                "next_attempt": 2,
                "next_model": "cc/claude-sonnet-5-low",
                "next_provider": "omniroute",
                "next_effort": "low",
                "next_execution_path": "OMNIROUTE",
            },
            worker_ordinal=1,
        )

        with patch(
            "nexus.orchestration.progressive.execute_worker"
        ) as execute_worker_mock, patch(
            "nexus.orchestration.progressive.review_worker"
        ) as review_worker_mock:
            execute_worker_mock.return_value = {
                "status": "COMPLETED",
                "agent_id": "AGENT-WORKER-ATTEMPT-2",
                "worktree": self.worktree,
            }
            review_worker_mock.return_value = {
                "status": "COMPLETED",
                "reviewer_id": "AGENT-REVIEWER",
                "review": {
                    "verdict": "PASS",
                    "failure_class": None,
                    "summary": "Approved.",
                },
                "routing": {
                    "model": "cc/claude-sonnet-5-high",
                    "provider": "claude",
                    "effort": "high",
                    "execution_path": "OMNIROUTE",
                    "reason": "test",
                    "degraded": False,
                },
            }

            result = resume_run(run_id)

            execute_worker_mock.assert_called_once()
            _, execute_kwargs = execute_worker_mock.call_args
            self.assertEqual(execute_kwargs["worker_index"], 2)
            self.assertEqual(
                execute_kwargs["worker"]["model"], "cc/claude-sonnet-5-low"
            )

        self.assertEqual(result["status"], "COMPLETED")

    def test_durable_escalate_missing_route_fields_is_unsafe_boundary(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        worker_id = self._register_worker_agent(run_id)
        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, worker_id, self.worktree, attempt=1)
        record_checkpoint(
            run_id=run_id,
            boundary="REVIEW",
            payload={
                "attempt": 1,
                "status": "COMPLETED",
                "worker_id": worker_id,
                "verdict": "ESCALATE",
                "failure_class": "REPEATED_FAILURE",
                "summary": "Retry budget exhausted.",
            },
            worker_ordinal=1,
        )
        record_checkpoint(
            run_id=run_id,
            boundary="LIFECYCLE",
            payload={
                "attempt": 1,
                "verdict": "ESCALATE",
                "worker_id": worker_id,
                "next_attempt": 2,
            },
            worker_ordinal=1,
        )

        with patch(
            "nexus.orchestration.progressive.execute_worker"
        ) as execute_worker_mock:
            with self.assertRaises(ResumeError) as ctx:
                resume_run(run_id)

            execute_worker_mock.assert_not_called()

        self.assertEqual(ctx.exception.code, "RESUME_UNSAFE_BOUNDARY")

    # -- 7. contiguous PASS prefix / ordinal range ----------------------

    def test_non_contiguous_pass_fails_closed(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, TWO_WORKER_PLAN)
        self._record_plan_approved(run_id, worker_count=2)

        record_checkpoint(
            run_id=run_id,
            boundary="LIFECYCLE",
            payload={"attempt": 1, "verdict": "PASS"},
            worker_ordinal=2,
        )

        with self.assertRaises(ResumeError) as ctx:
            resume_run(run_id)

        self.assertEqual(ctx.exception.code, "RESUME_STATE_INCONSISTENT")

    def test_out_of_range_worker_ordinal_fails_closed(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        record_checkpoint(
            run_id=run_id,
            boundary="LIFECYCLE",
            payload={"attempt": 1, "verdict": "PASS"},
            worker_ordinal=5,
        )

        with self.assertRaises(ResumeError) as ctx:
            resume_run(run_id)

        self.assertEqual(ctx.exception.code, "RESUME_STATE_INCONSISTENT")

    def test_zero_worker_ordinal_fails_closed(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        record_checkpoint(
            run_id=run_id,
            boundary="EXECUTION_START",
            payload={
                "route_class": "mechanical",
                "model": "oc/big-pickle",
                "provider": "omniroute",
                "effort": "low",
                "execution_path": "OMNIROUTE",
                "manager_id": "AGENT-MANAGER",
                "capability": "mechanical",
                "risk": "LOW",
                "degraded": False,
            },
            worker_ordinal=0,
        )

        with self.assertRaises(ResumeError) as ctx:
            resume_run(run_id)

        self.assertEqual(ctx.exception.code, "RESUME_STATE_INCONSISTENT")

    # -- 8. checkpoint attempt consistency ------------------------------

    def test_review_worker_id_mismatch_fails_closed(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        worker_id = self._register_worker_agent(run_id)
        self._record_execution_start(run_id)
        self._record_worker_attempt(run_id, worker_id, self.worktree)
        record_checkpoint(
            run_id=run_id,
            boundary="REVIEW",
            payload={
                "attempt": 1,
                "status": "COMPLETED",
                "worker_id": "AGENT-WORKER-DIFFERENT",
                "verdict": "PASS",
                "summary": "Approved.",
            },
            worker_ordinal=1,
        )

        with self.assertRaises(ResumeError) as ctx:
            resume_run(run_id)

        self.assertEqual(ctx.exception.code, "RESUME_STATE_INCONSISTENT")

    def test_impossible_attempt_ordering_fails_closed(self):
        run_id = self._make_run(status="RUNNING")
        record_approved_plan(run_id, ONE_WORKER_PLAN)
        self._record_plan_approved(run_id)

        worker_id = self._register_worker_agent(run_id)
        self._record_execution_start(run_id)
        self._record_worker_attempt(
            run_id, worker_id, self.worktree, attempt=3
        )

        with self.assertRaises(ResumeError) as ctx:
            resume_run(run_id)

        self.assertEqual(ctx.exception.code, "RESUME_STATE_INCONSISTENT")


if __name__ == "__main__":
    unittest.main()
