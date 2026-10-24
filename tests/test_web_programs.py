"""Tests for the read-only Program observability projection
(nexus/web/programs.py) and its two API routes.

Uses hand-built Program state dicts (the same shape nexus.program.state
persists) rather than running real Programs, so these are fast and
deterministic while still exercising the real projection code -- no
fabricated fields ever appear in the output that Nexus itself did not
record.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from nexus.program import state as program_state
from nexus.web import programs


def _stage(title, status="PENDING", **overrides):
    stage = {
        "title": title,
        "status": status,
        "attempts": 0,
        "run_ids": [],
        "last_error": None,
        "last_failure_class": None,
        "excluded_models": [],
        "target_revision": None,
        "acceptance": [],
        "acceptance_summary": {
            "acceptance_total": 0, "acceptance_satisfied": 0,
            "acceptance_unsatisfied": 0, "acceptance_unverified": 0,
        },
    }
    stage.update(overrides)
    return stage


def _program(**overrides):
    program = {
        "program_id": "PROGRAM-TEST01",
        "name": "Test Program",
        "project_id": "test-program",
        "path": "/fake/repo",
        "status": "RUNNING",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:10:00+00:00",
        "stop_after_spec": False,
        "spec": {
            "summary": "A test app.", "stack": "Python",
            "setup_commands": [], "final_verify_commands": ["pytest"],
            "acceptance_criteria": [
                {"id": "AC-1", "description": "x", "verification_type": "file_exists",
                 "required": True, "path": "x.txt"},
            ],
            "phases": [{"title": "Phase 1"}, {"title": "Phase 2"}],
        },
        "phases": [_stage("Phase 1"), _stage("Phase 2")],
        "final": _stage("Final verification"),
        "events": [],
        "task_contract": None,
    }
    program.update(overrides)
    return program


class ProgramsFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(self.enterContext(_tmp_dir()))
        self._orig_dir = program_state.PROGRAMS_DIR
        program_state.PROGRAMS_DIR = self._tmp

    def tearDown(self):
        program_state.PROGRAMS_DIR = self._orig_dir

    def _save(self, program):
        program_state.save_program(program)
        return program


from contextlib import contextmanager
import tempfile


@contextmanager
def _tmp_dir():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        yield d


class ListProgramSummariesTestCase(ProgramsFixture):
    def test_lists_newest_first_with_core_fields(self):
        self._save(_program(program_id="PROGRAM-OLD", created_at="2026-01-01T00:00:00+00:00"))
        self._save(_program(program_id="PROGRAM-NEW", created_at="2026-02-01T00:00:00+00:00"))

        summaries = programs.list_program_summaries()

        self.assertEqual([s["program_id"] for s in summaries], ["PROGRAM-NEW", "PROGRAM-OLD"])
        summary = summaries[0]
        self.assertEqual(summary["status"], "RUNNING")
        self.assertEqual(summary["phase_count"], 2)
        self.assertIn("acceptance_summary", summary)

    def test_empty_store_returns_empty_list(self):
        self.assertEqual(programs.list_program_summaries(), [])

    def test_current_phase_prefers_running_over_pending(self):
        program = _program(phases=[
            _stage("Phase 1", status="DONE"),
            _stage("Phase 2", status="RUNNING"),
            _stage("Phase 3", status="PENDING"),
        ])
        self._save(program)

        summary = programs.summarize_program(program)

        self.assertEqual(summary["current_phase"], 2)
        self.assertEqual(summary["current_phase_label"], "Phase 2")

    def test_current_phase_falls_back_to_first_pending_when_nothing_running(self):
        program = _program(phases=[
            _stage("Phase 1", status="DONE"),
            _stage("Phase 2", status="PENDING"),
        ])
        self.assertEqual(programs.summarize_program(program)["current_phase"], 2)

    def test_current_phase_is_final_once_all_phases_done(self):
        program = _program(
            phases=[_stage("Phase 1", status="DONE"), _stage("Phase 2", status="DONE")],
            final=_stage("Final verification", status="RUNNING"),
        )
        summary = programs.summarize_program(program)
        self.assertEqual(summary["current_stage_kind"], "final")
        self.assertEqual(summary["current_phase"], 3)

    def test_completed_program_has_no_current_phase(self):
        program = _program(
            status="COMPLETED",
            phases=[_stage("Phase 1", status="DONE"), _stage("Phase 2", status="DONE")],
            final=_stage("Final verification", status="DONE"),
        )
        summary = programs.summarize_program(program)
        self.assertIsNone(summary["current_phase"])
        self.assertEqual(summary["current_stage_kind"], None)


class WaitingForUserTestCase(ProgramsFixture):
    def test_authority_required_is_waiting_for_user(self):
        program = _program(
            status="BLOCKED",
            blocked_reason="Phase 1 blocked [AUTHORITY_REQUIRED]: needs approval",
            phases=[_stage("Phase 1", status="BLOCKED", last_failure_class="AUTHORITY_REQUIRED")],
        )
        summary = programs.summarize_program(program)
        self.assertTrue(summary["waiting_for_user"])
        self.assertEqual(summary["waiting_reason"], "AUTHORITY_REQUIRED")

    def test_technical_block_is_not_waiting_for_user(self):
        program = _program(
            status="BLOCKED",
            phases=[_stage("Phase 1", status="BLOCKED", last_failure_class="VERIFICATION_FAILED")],
        )
        summary = programs.summarize_program(program)
        self.assertFalse(summary["waiting_for_user"])
        self.assertIsNone(summary["waiting_reason"])

    def test_running_program_is_never_waiting_for_user(self):
        program = _program(status="RUNNING")
        self.assertFalse(programs.summarize_program(program)["waiting_for_user"])

    def test_policy_blocked_and_unsupported_capability_also_need_a_human(self):
        for failure_class in ("POLICY_BLOCKED", "UNSUPPORTED_CAPABILITY"):
            program = _program(
                status="BLOCKED",
                phases=[_stage("Phase 1", status="BLOCKED", last_failure_class=failure_class)],
            )
            self.assertTrue(programs.summarize_program(program)["waiting_for_user"])


class ProgramViewTestCase(ProgramsFixture):
    def test_unknown_program_returns_none(self):
        self.assertIsNone(programs.get_program_view("PROGRAM-NOPE"))

    def test_phase_and_final_detail_shape(self):
        program = _program(
            phases=[_stage("Phase 1", status="DONE", attempts=1, run_ids=["RUN-1"])],
            final=_stage("Final verification", status="RUNNING", run_ids=["RUN-2"]),
        )
        self._save(program)

        view = programs.get_program_view("PROGRAM-TEST01")

        self.assertEqual(len(view["phases"]), 1)
        self.assertEqual(view["phases"][0]["run_ids"], ["RUN-1"])
        self.assertEqual(view["final"]["run_ids"], ["RUN-2"])
        self.assertEqual(view["current_stage_kind"], "final")
        self.assertIn("telemetry", view)
        self.assertIn("routing_timeline", view)
        self.assertIn("files_changed_by_stage", view)

    def test_active_agents_scoped_to_current_stage_run_ids_only(self):
        program = _program(
            phases=[
                _stage("Phase 1", status="DONE", run_ids=["RUN-DONE"]),
                _stage("Phase 2", status="RUNNING", run_ids=["RUN-ACTIVE"]),
            ],
        )
        self._save(program)

        with patch(
            "nexus.web.programs.operational.get_operational_run_detail",
            side_effect=lambda run_id: {
                "id": run_id, "status": "RUNNING", "display_status": "RUNNING",
                "agents": [{"id": f"AGT-{run_id}", "role": "Worker", "model": "m"}],
            },
        ), patch("nexus.web.programs.list_checkpoints", return_value=[]):
            view = programs.get_program_view("PROGRAM-TEST01")

        agent_ids = {a["id"] for a in view["active_agents"]}
        self.assertEqual(agent_ids, {"AGT-RUN-ACTIVE"})

    def test_missing_run_is_skipped_not_fabricated(self):
        program = _program(phases=[_stage("Phase 1", status="DONE", run_ids=["RUN-GHOST"])])
        self._save(program)

        with patch("nexus.web.programs.operational.get_operational_run_detail", return_value=None):
            view = programs.get_program_view("PROGRAM-TEST01")

        self.assertEqual(view["runs"], {})

    def test_telemetry_unknown_when_no_calls_recorded(self):
        # A run_id unique to this test: nexus.telemetry.runs is a
        # process-global in-memory store, so a generic id like "RUN-1"
        # could already carry calls recorded by an unrelated test in the
        # same pytest process.
        run_id = "RUN-WEB-PROGRAMS-TELEMETRY-EMPTY"
        program = _program(phases=[_stage("Phase 1", status="DONE", run_ids=[run_id])])
        self._save(program)

        with patch(
            "nexus.web.programs.operational.get_operational_run_detail",
            return_value={"id": run_id, "status": "COMPLETED", "agents": []},
        ), patch("nexus.web.programs.list_checkpoints", return_value=[]):
            view = programs.get_program_view("PROGRAM-TEST01")

        self.assertIsNone(view["telemetry"]["total_tokens"])
        self.assertEqual(view["telemetry"]["llm_calls"], 0)


class RoutingEventsTestCase(unittest.TestCase):
    def test_extracts_only_checkpoints_with_a_real_decision_or_verdict(self):
        checkpoints = [
            {"sequence": 1, "boundary": "EXECUTION_START", "created_at": "t1",
             "payload": {"model": "A", "provider": "p", "route_class": "standard-coding"}},
            {"sequence": 2, "boundary": "WORKER_ATTEMPT", "created_at": "t2",
             "payload": {"decision": "RETRY_SAME_TIER", "reason": "TRANSIENT_PROVIDER_ERROR", "model": "A"}},
            {"sequence": 3, "boundary": "WORKER_ATTEMPT", "created_at": "t3",
             "payload": {"decision": "FALLBACK_ROUTE_ATTEMPT", "model": "A"}},
            {"sequence": 4, "boundary": "LIFECYCLE", "created_at": "t4",
             "payload": {"verdict": "ESCALATE", "next_model": "B"}},
            {"sequence": 5, "boundary": "REVIEW", "created_at": "t5", "payload": {"verdict": "PASS"}},
        ]
        events = programs._routing_events(checkpoints)
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds, ["INITIAL_ROUTE", "RETRY_SAME_TIER", "FALLBACK_ROUTE_ATTEMPT", "ESCALATE"])
        self.assertEqual(events[3]["model"], "B")  # next_model fallback used for ESCALATE

    def test_no_decision_bearing_checkpoints_yields_no_events(self):
        checkpoints = [{"sequence": 1, "boundary": "PLAN_APPROVED", "created_at": "t1", "payload": {}}]
        self.assertEqual(programs._routing_events(checkpoints), [])


class FilesChangedTestCase(unittest.TestCase):
    def _repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        env_git = lambda *args: subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True,
            env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com",
                 "PATH": __import__("os").environ.get("PATH", "")},
        )
        env_git("init", "-b", "main")
        (repo / "a.txt").write_text("one")
        env_git("add", "-A")
        env_git("commit", "--no-verify", "-m", "init")
        return repo

    def test_real_diff_between_two_revisions(self):
        import tempfile
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            repo = self._repo(tmp_path)
            first = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
            ).stdout.strip()
            (repo / "b.txt").write_text("two")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "--no-verify", "-m", "second"], cwd=repo, check=True,
                capture_output=True,
                env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
                     "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com",
                     "PATH": __import__("os").environ.get("PATH", "")},
            )
            second = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
            ).stdout.strip()

            result = programs._files_changed(str(repo), first, second)

        self.assertTrue(result["available"])
        self.assertEqual(result["files"], ["b.txt"])

    def test_missing_repo_or_revision_is_unavailable_not_fabricated(self):
        self.assertEqual(programs._files_changed(None, "a", "b"), {"available": False})
        self.assertEqual(programs._files_changed("/fake", None, "b"), {"available": False})
        self.assertEqual(programs._files_changed("/fake", "a", "a"), {"available": False})

    def test_nonexistent_repo_path_fails_closed(self):
        result = programs._files_changed("/definitely/not/a/repo", "a", "b")
        self.assertEqual(result, {"available": False})


class ProgramApiRoutesTestCase(unittest.TestCase):
    def setUp(self):
        try:
            from starlette.testclient import TestClient  # noqa: PLC0415
        except ImportError:  # pragma: no cover
            self.skipTest("TestClient not installed")
        import tempfile

        self._tmp_ctx = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig_dir = program_state.PROGRAMS_DIR
        program_state.PROGRAMS_DIR = Path(self._tmp_ctx.name)

        from nexus.web.app import app  # noqa: PLC0415
        self.client = TestClient(app)

    def tearDown(self):
        program_state.PROGRAMS_DIR = self._orig_dir
        self._tmp_ctx.cleanup()

    def test_list_programs_endpoint(self):
        program_state.save_program(_program())
        response = self.client.get("/api/programs")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["programs"]), 1)
        self.assertEqual(body["programs"][0]["program_id"], "PROGRAM-TEST01")

    def test_program_detail_endpoint(self):
        program_state.save_program(_program())
        response = self.client.get("/api/programs/PROGRAM-TEST01")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["program"]["program_id"], "PROGRAM-TEST01")

    def test_program_detail_404_for_unknown_id(self):
        response = self.client.get("/api/programs/PROGRAM-NOPE")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
