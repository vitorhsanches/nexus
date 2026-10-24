"""
tests/test_full_result_persistence.py

Proves that a Worker/synthesis deliverable's complete text survives
persistence, independent of the bounded `agents.result` preview column.

Root cause under test: nexus.dispatchers.omniroute.run_omniroute_worker()
used to persist only `worker_output[-8000:]` to the database -- the full
semantic last-message content (a synthesis Worker's real report, which can
be tens of thousands of characters) was silently reduced to its last 8000
characters, permanently losing the beginning of the report. A real
FinaSync review hit exactly this.

Fix: a separate, uncapped-in-practice `full_result` column
(nexus.registry.agents.update_agent_execution(full_result=...)) stores the
complete deliverable. `result` keeps its existing bounded-preview
behaviour and role unchanged -- no existing reader of `result` is
affected. The API only returns `full_result` through one explicit,
per-agent endpoint (operational.get_operational_agent_result /
GET /api/operational/agents/{id}/result); every bulk/listing read exposes
only a boolean `has_full_result` flag plus the bounded preview.

Cases (per the task's CASE A-H):
  A. Worker result < preview limit            -> preview == full
  B. Worker result > 8000 chars                -> preview bounded, full intact
  C. ~25k char synthesis result                -> begin/middle/end all present
  D. Simulated Nexus restart (fresh connection) -> full result still available
  E. Default/bulk API projection                -> never returns full text
  F. Explicit full-result accessor               -> returns complete content
  G. Historical row (pre-migration, no full_result) -> honest unavailable state
  H. Execution semantics unchanged                -> result/status/output contract intact
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.registry.agents import (
    FULL_RESULT_SAFETY_LIMIT,
    create_agent,
    get_agent_result,
    update_agent_execution,
)
from nexus.web import operational


_FIXTURE_PROJECTS = {
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


class _DbMixin(unittest.TestCase):
    """Fresh in-process SQLite database per test, with one Run to hang Agents off."""

    RUN_ID = "RUN-FULLRESULT-TEST"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmp.name)

        self._orig_db = database.DATABASE_PATH
        self._orig_storage = database.STORAGE_DIR
        self._orig_projects = projects.PROJECTS_FILE

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "test.db"

        fixture = tmp_path / "projects.json"
        fixture.write_text(json.dumps(_FIXTURE_PROJECTS), encoding="utf-8")
        projects.PROJECTS_FILE = fixture

        database.initialize_database()
        projects.sync_projects()

        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO runs (id,project_id,input,intent,status,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (self.RUN_ID, "norte", "do work", "GO", "RUNNING", "now"),
            )

    def tearDown(self):
        database.DATABASE_PATH = self._orig_db
        database.STORAGE_DIR = self._orig_storage
        projects.PROJECTS_FILE = self._orig_projects
        self._tmp.cleanup()

    def _new_agent(self, role="Worker"):
        return create_agent(
            run_id=self.RUN_ID,
            role=role,
            provider="omniroute",
            model="oc/big-pickle",
            status="RUNNING",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Case A — small result: preview == full, full retrievable
# ─────────────────────────────────────────────────────────────────────────────

class CaseASmallResultTests(_DbMixin):
    def test_small_result_preview_equals_full(self):
        agent_id = self._new_agent()
        short_text = "Worker completed the task successfully."

        update_agent_execution(agent_id, result=short_text[-8000:], full_result=short_text)

        stored = get_agent_result(agent_id)
        self.assertTrue(stored["has_full_result"])
        self.assertEqual(stored["preview"], short_text)
        self.assertEqual(stored["full_result"], short_text)
        self.assertFalse(stored["full_result_truncated"])


# ─────────────────────────────────────────────────────────────────────────────
# Case B — result > 8000 chars: preview bounded, full result intact
# ─────────────────────────────────────────────────────────────────────────────

class CaseBOverPreviewLimitTests(_DbMixin):
    def test_result_over_8000_chars_preview_bounded_full_intact(self):
        agent_id = self._new_agent()
        long_text = "A" * 500 + "B" * 8600 + "C" * 500  # 9600 chars total

        update_agent_execution(
            agent_id,
            result=long_text[-8000:],
            full_result=long_text,
        )

        with database.get_connection() as conn:
            row = conn.execute(
                "SELECT result FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
        self.assertEqual(len(row["result"]), 8000)
        # The bounded preview lost the leading "A"s -- exactly the historical bug.
        self.assertNotIn("A", row["result"])

        stored = get_agent_result(agent_id)
        self.assertTrue(stored["has_full_result"])
        self.assertEqual(len(stored["full_result"]), 9600)
        self.assertTrue(stored["full_result"].startswith("A" * 500))
        self.assertTrue(stored["full_result"].endswith("C" * 500))
        self.assertFalse(stored["full_result_truncated"])


# ─────────────────────────────────────────────────────────────────────────────
# Case C — ~25k char synthesis report: begin/middle/end all retrievable
# ─────────────────────────────────────────────────────────────────────────────

class CaseCSynthesisReportTests(_DbMixin):
    def _build_report(self) -> str:
        return (
            "BEGIN_REPORT\n"
            + ("x" * 10000)
            + "\nMIDDLE_REPORT\n"
            + ("y" * 10000)
            + "\nEND_REPORT\n"
        )

    def test_25k_synthesis_report_begin_middle_end_all_present(self):
        agent_id = self._new_agent(role="Worker")  # synthesis Worker: same code path
        report = self._build_report()
        self.assertGreater(len(report), 20000)

        update_agent_execution(agent_id, result=report[-8000:], full_result=report)

        stored = get_agent_result(agent_id)
        self.assertTrue(stored["has_full_result"])
        full = stored["full_result"]
        self.assertIn("BEGIN_REPORT", full)
        self.assertIn("MIDDLE_REPORT", full)
        self.assertIn("END_REPORT", full)
        self.assertEqual(len(full), len(report))

        # The bounded preview, by contrast, lost BEGIN_REPORT -- the exact
        # failure mode the bug report described.
        preview = stored["preview"]
        self.assertNotIn("BEGIN_REPORT", preview)
        self.assertIn("END_REPORT", preview)


# ─────────────────────────────────────────────────────────────────────────────
# Case D — simulated restart: fresh connection still sees the full result
# ─────────────────────────────────────────────────────────────────────────────

class CaseDRestartTests(_DbMixin):
    def test_full_result_survives_simulated_restart(self):
        agent_id = self._new_agent()
        report = "BEGIN_REPORT\n" + ("z" * 20000) + "\nEND_REPORT\n"
        update_agent_execution(agent_id, result=report[-8000:], full_result=report)

        # Simulate a restart: drop any in-process state and open a brand new
        # connection against the same on-disk database file.
        with database.get_connection() as conn:
            row = conn.execute(
                "SELECT full_result, full_result_truncated FROM agents WHERE id = ?",
                (agent_id,),
            ).fetchone()

        self.assertIsNotNone(row["full_result"])
        self.assertIn("BEGIN_REPORT", row["full_result"])
        self.assertIn("END_REPORT", row["full_result"])

        stored = get_agent_result(agent_id)
        self.assertTrue(stored["has_full_result"])
        self.assertEqual(len(stored["full_result"]), len(report))


# ─────────────────────────────────────────────────────────────────────────────
# Case E — default/bulk API projection never returns the full text
# ─────────────────────────────────────────────────────────────────────────────

class CaseEBoundedApiDefaultTests(_DbMixin):
    def test_operational_agents_listing_omits_full_result_text(self):
        agent_id = self._new_agent()
        report = "R" * 50000
        update_agent_execution(agent_id, result=report[-8000:], full_result=report)

        agents = operational.get_operational_agents(run_id=self.RUN_ID)
        self.assertEqual(len(agents), 1)
        item = agents[0]

        self.assertTrue(item["has_full_result"])
        self.assertIs(type(item["has_full_result"]), bool)
        self.assertNotIn("full_result", item)
        # The bounded preview that IS present must stay small.
        self.assertLessEqual(len(item.get("result") or ""), 8000)

    def test_operational_run_detail_omits_full_result_text(self):
        agent_id = self._new_agent()
        report = "S" * 50000
        update_agent_execution(agent_id, result=report[-8000:], full_result=report)

        detail = operational.get_operational_run_detail(self.RUN_ID)
        self.assertIsNotNone(detail)
        self.assertEqual(len(detail["agents"]), 1)
        self.assertNotIn("full_result", detail["agents"][0])
        self.assertTrue(detail["agents"][0]["has_full_result"])


# ─────────────────────────────────────────────────────────────────────────────
# Case F — explicit full-result accessor returns complete content
# ─────────────────────────────────────────────────────────────────────────────

class CaseFExplicitFullResultTests(_DbMixin):
    def test_explicit_accessor_returns_complete_text(self):
        agent_id = self._new_agent()
        report = "BEGIN_REPORT\n" + ("m" * 30000) + "\nEND_REPORT\n"
        update_agent_execution(agent_id, result=report[-8000:], full_result=report)

        result = operational.get_operational_agent_result(agent_id)
        self.assertIsNotNone(result)
        self.assertTrue(result["has_full_result"])
        self.assertEqual(result["full_result"], report)
        self.assertFalse(result["full_result_truncated"])

    def test_explicit_accessor_unknown_agent_returns_none(self):
        self.assertIsNone(operational.get_operational_agent_result("AGENT-DOES-NOT-EXIST"))

    def test_safety_limit_truncation_is_explicit_not_silent(self):
        agent_id = self._new_agent()
        oversized = "Q" * (FULL_RESULT_SAFETY_LIMIT + 1000)
        update_agent_execution(agent_id, full_result=oversized)

        result = operational.get_operational_agent_result(agent_id)
        self.assertTrue(result["has_full_result"])
        self.assertTrue(result["full_result_truncated"])
        self.assertEqual(len(result["full_result"]), FULL_RESULT_SAFETY_LIMIT)


# ─────────────────────────────────────────────────────────────────────────────
# Case G — historical row predating full-result support degrades honestly
# ─────────────────────────────────────────────────────────────────────────────

class CaseGHistoricalRowTests(_DbMixin):
    def test_historical_row_without_full_result_reports_unavailable(self):
        agent_id = self._new_agent()
        # Only ever wrote the bounded preview -- exactly what every pre-fix
        # agent row in a real, already-running Nexus database looks like.
        update_agent_execution(agent_id, result="legacy bounded preview only")

        result = operational.get_operational_agent_result(agent_id)
        self.assertFalse(result["has_full_result"])
        self.assertIsNone(result["full_result"])
        self.assertEqual(result["preview"], "legacy bounded preview only")

        agents = operational.get_operational_agents(run_id=self.RUN_ID)
        self.assertFalse(agents[0]["has_full_result"])


# ─────────────────────────────────────────────────────────────────────────────
# Case H — execution semantics (status/output contract) unchanged
# ─────────────────────────────────────────────────────────────────────────────

class CaseHExecutionSemanticsUnchangedTests(_DbMixin):
    """Exercises the real dispatcher end to end to prove the fix is wiring,
    not a behaviour change: COMPLETED/FAILED mapping, exit codes, and the
    in-memory `output` field are identical to the pre-fix contract."""

    def _call_dispatcher(self, file_content, stdout_lines=None):
        from nexus.dispatchers.omniroute import run_omniroute_worker

        def _popen_side_effect(command, **kwargs):
            idx = command.index("-ResultFile")
            Path(command[idx + 1]).write_text(file_content, encoding="utf-8")
            proc = MagicMock()
            proc.stdout = iter(stdout_lines or [])
            proc.wait.return_value = 0
            return proc

        with (
            patch("nexus.dispatchers.omniroute.ADAPTER_PATH") as mock_path,
            patch("nexus.dispatchers.omniroute.codex_subprocess_env", return_value={}),
            patch(
                "nexus.dispatchers.omniroute.subprocess.Popen",
                side_effect=_popen_side_effect,
            ),
        ):
            mock_path.exists.return_value = True
            return run_omniroute_worker(
                run_id=self.RUN_ID,
                repo=r"C:\fake\interface-life",
                task="do work",
                model="oc/big-pickle",
            )

    def test_disposable_smoke_over_20k_chars_preserved_end_to_end(self):
        """Section 10's disposable smoke test: a >20k char synthetic report
        with BEGIN/MIDDLE/END markers survives the real dispatcher path,
        the default API stays bounded, and the explicit endpoint returns
        everything -- with zero change to status/exit_code/output."""
        report = (
            "BEGIN_REPORT\n"
            + ("alpha " * 4000)
            + "\nMIDDLE_REPORT\n"
            + ("beta " * 4000)
            + "\nEND_REPORT\n"
        )
        self.assertGreater(len(report), 20000)

        result = self._call_dispatcher(file_content=report)

        # Execution semantics: unchanged.
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["exit_code"], 0)
        # run_omniroute_worker() strips the result file content; compare stripped.
        self.assertEqual(result["output"], report.strip())
        self.assertNotIn("reason", result)

        # Default API: bounded.
        agents = operational.get_operational_agents(run_id=self.RUN_ID)
        self.assertEqual(len(agents), 1)
        self.assertTrue(agents[0]["has_full_result"])
        self.assertLessEqual(len(agents[0]["result"]), 8000)
        self.assertNotIn("full_result", agents[0])

        # Explicit full-result request: complete.
        full = operational.get_operational_agent_result(result["agent_id"])
        self.assertTrue(full["has_full_result"])
        self.assertIn("BEGIN_REPORT", full["full_result"])
        self.assertIn("MIDDLE_REPORT", full["full_result"])
        self.assertIn("END_REPORT", full["full_result"])
        self.assertEqual(full["full_result"], report.strip())
        self.assertFalse(full["full_result_truncated"])

    def test_failed_run_never_fabricates_a_full_result(self):
        """A FAILED Worker (no usable semantic output) must never end up
        with has_full_result=True -- full preservation is for genuine
        deliverables only, never diagnostic noise."""
        from nexus.dispatchers.omniroute import run_omniroute_worker

        def _popen_side_effect(command, **kwargs):
            proc = MagicMock()
            proc.stdout = iter(["diagnostic noise only\n"])
            proc.wait.return_value = 1
            return proc

        with (
            patch("nexus.dispatchers.omniroute.ADAPTER_PATH") as mock_path,
            patch("nexus.dispatchers.omniroute.codex_subprocess_env", return_value={}),
            patch(
                "nexus.dispatchers.omniroute.subprocess.Popen",
                side_effect=_popen_side_effect,
            ),
        ):
            mock_path.exists.return_value = True
            result = run_omniroute_worker(
                run_id=self.RUN_ID,
                repo=r"C:\fake\interface-life",
                task="do work",
                model="oc/big-pickle",
            )

        self.assertEqual(result["status"], "FAILED")
        full = operational.get_operational_agent_result(result["agent_id"])
        self.assertFalse(full["has_full_result"])
        self.assertIsNone(full["full_result"])


if __name__ == "__main__":
    unittest.main()
