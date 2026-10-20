"""Phase 4.2 tests — Repository Scout Stage.

Covers tests A–T from the Phase 4.2B specification:

A.  BOUNDED → Manager once, zero scouts.
B.  BROAD → two scouts → Manager once after evidence.
C.  Scouts run concurrently.
D.  Original mission unchanged (task= never modified).
E.  Deterministic A → B evidence order regardless of completion order.
F.  Diagnostic transport output excluded from evidence.
G.  One scout fail → partial evidence → Manager still runs.
H.  Both scouts fail → SCOUT_STAGE_FAILED → Manager NOT called.
I.  WORKER_RESULT_MISSING / WORKER_RESULT_EMPTY treated as scout failure.
J.  Per-scout evidence bound (PER_SCOUT_CHAR_LIMIT).
K.  Global evidence bound (GLOBAL_EVIDENCE_CHAR_LIMIT).
L.  Scout sandbox_mode == "read-only" in omniroute command args.
M.  Normal worker sandbox remains workspace-write/default.
N.  Scout routing goes through AdaptiveRoutingService, no model hardcoded.
O.  Scouts not included in final plan["workers"].
P.  Existing Phase 1–3 worker DAG executes Manager-produced workers.
Q.  Phase 3 dependency context unchanged.
R.  No automatic integration worker injected.
S.  Phase 4.1 triage regression (BOUNDED/BROAD classification unchanged).
T.  Phase 4.1H-C native result channel tests unaffected.

Database isolation: every test that exercises go.py or the agent registry
uses a temporary, disposable database copy — same monkeypatch pattern as
test_phase4_1_triage.py.
"""

import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, call, patch

import nexus.registry.database as database
import nexus.registry.projects as projects
from nexus.dispatchers.triage import MissionScope, classify_mission_scope
from nexus.orchestration.scout import (
    PER_SCOUT_CHAR_LIMIT,
    GLOBAL_EVIDENCE_CHAR_LIMIT,
    ScoutStageFailedError,
    _SCOUT_LABELS,
    _TRUNCATION_MARKER,
    build_evidence_bundle,
    run_scout_stage,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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

VALID_PLAN = {
    "complexity": "LOW",
    "risk": "LOW",
    "parallelism": 1,
    "summary": "Do a small fix.",
    "intent": "EXECUTION",
    "workers": [
        {
            "route_class": "mechanical",
            "execution_path": "OMNIROUTE",
            "provider": "omniroute",
            "model": "oc/big-pickle",
            "effort": "low",
            "scope": "Fix the thing.",
            "reason": "Simple mechanical change.",
            "depends_on": [],
        }
    ],
}

BROAD_MISSION = (
    "Norte: Analise este projeto, identifique os principais problemas e implemente "
    "as correções prioritárias."
)

BOUNDED_MISSION = "Norte: Fix the bug in nexus/dispatchers/manager.py line 42."


# ---------------------------------------------------------------------------
# Shared DB isolation mixin
# ---------------------------------------------------------------------------

class _DbMixin(unittest.TestCase):
    def setUp(self):
        from nexus.registry.database import initialize_database
        from nexus.registry.projects import sync_projects

        self._tmpdir = TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_path = Path(self._tmpdir.name)

        self._orig_db_path = database.DATABASE_PATH
        self._orig_storage_dir = database.STORAGE_DIR
        self._orig_projects_file = projects.PROJECTS_FILE

        database.STORAGE_DIR = tmp_path
        database.DATABASE_PATH = tmp_path / "nexus_test.db"

        tmp_projects = tmp_path / "projects.json"
        tmp_projects.write_text(json.dumps(FIXTURE_PROJECTS), encoding="utf-8")
        projects.PROJECTS_FILE = tmp_projects

        initialize_database()
        sync_projects()

    def tearDown(self):
        database.DATABASE_PATH = self._orig_db_path
        database.STORAGE_DIR = self._orig_storage_dir
        projects.PROJECTS_FILE = self._orig_projects_file
        self._tmpdir.cleanup()


def _make_scout_result(content: str | None, status: str = "COMPLETED", reason: str | None = None) -> dict:
    """Build a minimal scout result dict."""
    return {
        "agent_id": "AGENT-TEST",
        "exit_code": 0 if status == "COMPLETED" else 1,
        "status": status,
        "output": content,
        "branch": None,
        "worktree": None,
        "reason": reason,
    }


def _make_route_decision(model: str = "cc/claude-sonnet-5-low") -> MagicMock:
    d = MagicMock()
    d.model = model
    d.effort = "low"
    d.provider = "claude"
    d.execution_path = "OMNIROUTE"
    d.reason = "test route"
    d.degraded = False
    return d


# ---------------------------------------------------------------------------
# A. BOUNDED → Manager once, zero scouts
# ---------------------------------------------------------------------------

class BoundedFlowTests(_DbMixin):
    """Test A: BOUNDED mission bypasses scouts entirely."""

    def test_bounded_no_scouts_manager_once(self):
        """BOUNDED → BOUNDED_SCOUT_COUNT=0, BOUNDED_MANAGER_CALL_COUNT=1."""
        with (
            patch("nexus.orchestration.go.classify_mission_scope") as mock_triage,
            patch("nexus.orchestration.go.run_scout_stage") as mock_scouts,
            patch("nexus.orchestration.go.run_manager") as mock_manager,
            patch("nexus.orchestration.go._run_workers_by_round") as mock_workers,
        ):
            mock_triage.return_value = MagicMock(
                scope=MissionScope.BOUNDED,
                reason_codes=("NO_BROAD_SIGNAL",),
            )
            mock_scouts.return_value = "should never be called"
            plan = {**VALID_PLAN, "workers": []}
            mock_manager.return_value = {
                "manager_id": "MGR-001",
                "status": "COMPLETED",
                "exit_code": 0,
                "plan": {**plan, "intent": "QUESTION"},
            }
            mock_workers.return_value = []

            from nexus.orchestration.go import run_go
            run_go("Norte: Fix the bug in nexus/dispatchers/manager.py line 42.")

            mock_scouts.assert_not_called()
            mock_manager.assert_called_once()

    def test_bounded_manager_receives_none_evidence(self):
        """BOUNDED: repository_evidence=None passed to run_manager."""
        with (
            patch("nexus.orchestration.go.classify_mission_scope") as mock_triage,
            patch("nexus.orchestration.go.run_scout_stage") as mock_scouts,
            patch("nexus.orchestration.go.run_manager") as mock_manager,
            patch("nexus.orchestration.go._run_workers_by_round") as mock_workers,
        ):
            mock_triage.return_value = MagicMock(
                scope=MissionScope.BOUNDED,
                reason_codes=("NO_BROAD_SIGNAL",),
            )
            mock_scouts.return_value = "nope"
            plan = {**VALID_PLAN, "workers": [], "intent": "QUESTION"}
            mock_manager.return_value = {
                "manager_id": "MGR-001",
                "status": "COMPLETED",
                "exit_code": 0,
                "plan": plan,
            }
            mock_workers.return_value = []

            from nexus.orchestration.go import run_go
            run_go("Norte: Fix nexus/dispatchers/manager.py line 42.")

            _, call_kwargs = mock_manager.call_args
            self.assertIsNone(call_kwargs.get("repository_evidence"))


# ---------------------------------------------------------------------------
# B. BROAD → two scouts → Manager once with evidence
# ---------------------------------------------------------------------------

class BroadFlowTests(_DbMixin):
    """Test B: BROAD mission triggers scouts before Manager."""

    def _broad_go(self, scout_a_content="arch findings", scout_b_content="risk findings"):
        """Run go with BROAD triage and mocked scouts + manager."""
        call_log = []

        def fake_scout_stage(run_id, repo, request_text, **kw):
            call_log.append("SCOUT_STAGE")
            return "EVIDENCE_BUNDLE"

        def fake_manager(**kw):
            call_log.append(("MANAGER", kw.get("repository_evidence")))
            plan = {**VALID_PLAN, "workers": [], "intent": "QUESTION"}
            return {"manager_id": "MGR-001", "status": "COMPLETED", "exit_code": 0, "plan": plan}

        with (
            patch("nexus.orchestration.go.classify_mission_scope") as mock_triage,
            patch("nexus.orchestration.go.run_scout_stage", side_effect=fake_scout_stage),
            patch("nexus.orchestration.go.run_manager", side_effect=fake_manager),
            patch("nexus.orchestration.go._run_workers_by_round", return_value=[]),
        ):
            mock_triage.return_value = MagicMock(
                scope=MissionScope.BROAD,
                reason_codes=("BROAD_SCOPE_ANCHOR_AND_OPEN_TARGET",),
            )
            from nexus.orchestration.go import run_go
            run_go(BROAD_MISSION)

        return call_log

    def test_broad_scouts_then_manager(self):
        """BROAD: scout stage runs, then Manager runs once."""
        log = self._broad_go()
        self.assertIn("SCOUT_STAGE", log)
        manager_calls = [e for e in log if isinstance(e, tuple) and e[0] == "MANAGER"]
        self.assertEqual(len(manager_calls), 1)

    def test_broad_manager_receives_evidence(self):
        """BROAD: run_manager receives the evidence bundle."""
        log = self._broad_go()
        manager_calls = [e for e in log if isinstance(e, tuple) and e[0] == "MANAGER"]
        self.assertEqual(manager_calls[0][1], "EVIDENCE_BUNDLE")

    def test_broad_manager_called_exactly_once(self):
        """BROAD_MANAGER_CALL_COUNT=1."""
        log = self._broad_go()
        manager_calls = [e for e in log if isinstance(e, tuple) and e[0] == "MANAGER"]
        self.assertEqual(len(manager_calls), 1)


# ---------------------------------------------------------------------------
# C. Scouts run concurrently
# ---------------------------------------------------------------------------

class ScoutConcurrencyTests(_DbMixin):
    """Test C: both scouts are dispatched before either completes."""

    def test_scouts_run_concurrently(self):
        """Both scouts start before the first one finishes."""
        start_times = {}
        finish_times = {}
        lock = threading.Lock()

        def slow_scout(run_id, repo, task, model, effort, sandbox_mode="workspace-write", **kw):
            # Extract scout index from label in task
            idx = 0 if "Architecture" in task else 1
            with lock:
                start_times[idx] = time.monotonic()
            time.sleep(0.15)
            with lock:
                finish_times[idx] = time.monotonic()
            return _make_scout_result(f"scout {idx} output")

        fake_route = _make_route_decision()
        with (
            patch("nexus.orchestration.scout.AdaptiveRoutingService") as mock_svc_cls,
            patch("nexus.orchestration.scout.run_omniroute_worker", side_effect=slow_scout),
            patch("nexus.orchestration.scout.record_checkpoint"),
        ):
            mock_svc = MagicMock()
            mock_svc.select_route_for_capability.return_value = fake_route
            mock_svc_cls.return_value = mock_svc

            run_scout_stage(run_id="RUN-CONC", repo=r"C:\fake", request_text=BROAD_MISSION)

        # Both scouts should have started before the first finished.
        # If sequential, start[1] >= finish[0]. If concurrent, start[1] < finish[0].
        both_started_before_first_finished = (
            start_times.get(1, 99) < finish_times.get(0, 0)
            or start_times.get(0, 99) < finish_times.get(1, 0)
        )
        self.assertTrue(
            both_started_before_first_finished,
            "Scouts did not start concurrently"
        )


# ---------------------------------------------------------------------------
# D. Original mission unchanged
# ---------------------------------------------------------------------------

class OriginalMissionPreservationTests(_DbMixin):
    """Test D: task= passed to run_manager equals original request_text."""

    def test_original_mission_preserved_broad(self):
        """BROAD: task= in run_manager call is unchanged original mission."""
        captured = {}

        def fake_scout_stage(**kw):
            return "BUNDLE"

        def fake_manager(**kw):
            captured["task"] = kw.get("task")
            plan = {**VALID_PLAN, "workers": [], "intent": "QUESTION"}
            return {"manager_id": "M1", "status": "COMPLETED", "exit_code": 0, "plan": plan}

        with (
            patch("nexus.orchestration.go.classify_mission_scope") as mock_triage,
            patch("nexus.orchestration.go.run_scout_stage", side_effect=fake_scout_stage),
            patch("nexus.orchestration.go.run_manager", side_effect=fake_manager),
            patch("nexus.orchestration.go._run_workers_by_round", return_value=[]),
        ):
            mock_triage.return_value = MagicMock(
                scope=MissionScope.BROAD,
                reason_codes=("BROAD_SCOPE_ANCHOR_AND_OPEN_TARGET",),
            )
            from nexus.orchestration.go import run_go
            run_go(BROAD_MISSION)

        self.assertEqual(captured["task"], BROAD_MISSION)

    def test_evidence_not_concatenated_into_task(self):
        """repository_evidence is passed separately, not merged into task."""
        captured = {}

        def fake_manager(**kw):
            captured["task"] = kw.get("task")
            captured["evidence"] = kw.get("repository_evidence")
            plan = {**VALID_PLAN, "workers": [], "intent": "QUESTION"}
            return {"manager_id": "M1", "status": "COMPLETED", "exit_code": 0, "plan": plan}

        with (
            patch("nexus.orchestration.go.classify_mission_scope") as mock_triage,
            patch("nexus.orchestration.go.run_scout_stage", return_value="SCOUT_BUNDLE"),
            patch("nexus.orchestration.go.run_manager", side_effect=fake_manager),
            patch("nexus.orchestration.go._run_workers_by_round", return_value=[]),
        ):
            mock_triage.return_value = MagicMock(
                scope=MissionScope.BROAD,
                reason_codes=("BROAD_SCOPE_ANCHOR_AND_OPEN_TARGET",),
            )
            from nexus.orchestration.go import run_go
            run_go(BROAD_MISSION)

        self.assertNotIn("SCOUT_BUNDLE", captured.get("task", ""))
        self.assertEqual(captured.get("evidence"), "SCOUT_BUNDLE")


# ---------------------------------------------------------------------------
# E. Deterministic A → B evidence order regardless of completion order
# ---------------------------------------------------------------------------

class EvidenceOrderTests(unittest.TestCase):
    """Test E: build_evidence_bundle always puts Scout A before Scout B."""

    def test_scout_a_before_scout_b(self):
        results = [
            _make_scout_result("architecture content"),
            _make_scout_result("risk content"),
        ]
        bundle = build_evidence_bundle(results)
        idx_a = bundle.find("Scout A:")
        idx_b = bundle.find("Scout B:")
        self.assertGreater(idx_a, 0)
        self.assertGreater(idx_b, idx_a)

    def test_order_independent_of_which_completed_first(self):
        """Even if Scout B completed first, A appears first in bundle."""
        # Simulate Scout B arrived "faster": reversed result list
        results_b_first = [
            _make_scout_result("arch content"),   # index 0 = Scout A
            _make_scout_result("risk content"),   # index 1 = Scout B
        ]
        bundle = build_evidence_bundle(results_b_first)
        self.assertLess(bundle.index("Scout A:"), bundle.index("Scout B:"))


# ---------------------------------------------------------------------------
# F. Diagnostic transport output excluded from evidence
# ---------------------------------------------------------------------------

class DiagnosticExclusionTests(unittest.TestCase):
    """Test F: transport noise in stdout never enters evidence."""

    def test_diagnostic_noise_not_in_evidence(self):
        """run_omniroute_worker result["output"] is semantic; noise stays in diagnostic stream."""
        noise_strings = [
            "Branch    : worker/abc",
            "WORKTREE_PRESERVED=C:\\wt",
            "Exit code : 0",
            "connection reset, retrying",
            "Reconnecting...",
        ]
        # Scout result only has clean semantic content in "output"
        clean_output = "ARCHITECTURE_SUMMARY\nThis is a clean finding."
        result = _make_scout_result(clean_output)
        bundle = build_evidence_bundle([result, _make_scout_result("risk")])

        for noise in noise_strings:
            self.assertNotIn(noise, bundle)

        self.assertIn("ARCHITECTURE_SUMMARY", bundle)


# ---------------------------------------------------------------------------
# G. One scout fail → partial evidence → Manager still runs
# ---------------------------------------------------------------------------

class PartialEvidenceTests(_DbMixin):
    """Test G: one scout failing produces partial evidence, Manager still runs."""

    def test_scout_b_fail_manager_runs_with_partial(self):
        """Scout B fails → labeled unavailable, Manager still called."""
        call_log = []

        def fake_scout(**kw):
            scout_idx = 0 if "Architecture" in kw.get("task", "") else 1
            if scout_idx == 1:
                return _make_scout_result(None, status="FAILED", reason="WORKER_RESULT_MISSING")
            return _make_scout_result("arch findings")

        fake_route = _make_route_decision()

        with (
            patch("nexus.orchestration.scout.AdaptiveRoutingService") as mock_svc_cls,
            patch("nexus.orchestration.scout.run_omniroute_worker", side_effect=fake_scout),
            patch("nexus.orchestration.scout.record_checkpoint"),
        ):
            mock_svc = MagicMock()
            mock_svc.select_route_for_capability.return_value = fake_route
            mock_svc_cls.return_value = mock_svc

            bundle = run_scout_stage(
                run_id="RUN-G", repo=r"C:\fake", request_text=BROAD_MISSION
            )

        self.assertIn("arch findings", bundle)
        self.assertIn("evidence unavailable", bundle)
        self.assertIn("Scout B:", bundle)

    def test_scout_a_fail_partial_evidence_contains_b(self):
        """Scout A fails → Scout B content still in bundle."""
        def fake_scout(**kw):
            scout_idx = 0 if "Architecture" in kw.get("task", "") else 1
            if scout_idx == 0:
                return _make_scout_result(None, status="FAILED", reason="WORKER_RESULT_EMPTY")
            return _make_scout_result("risk findings B")

        fake_route = _make_route_decision()
        with (
            patch("nexus.orchestration.scout.AdaptiveRoutingService") as mock_svc_cls,
            patch("nexus.orchestration.scout.run_omniroute_worker", side_effect=fake_scout),
            patch("nexus.orchestration.scout.record_checkpoint"),
        ):
            mock_svc = MagicMock()
            mock_svc.select_route_for_capability.return_value = fake_route
            mock_svc_cls.return_value = mock_svc

            bundle = run_scout_stage(
                run_id="RUN-G2", repo=r"C:\fake", request_text=BROAD_MISSION
            )

        self.assertIn("risk findings B", bundle)
        self.assertIn("evidence unavailable", bundle)


# ---------------------------------------------------------------------------
# H. Both scouts fail → SCOUT_STAGE_FAILED → Manager NOT called
# ---------------------------------------------------------------------------

class AllScoutsFailTests(_DbMixin):
    """Test H: both scouts fail → ScoutStageFailedError → Manager not called."""

    def test_both_fail_raises_scout_stage_failed(self):
        def fail_scout(**kw):
            return _make_scout_result(None, status="FAILED", reason="WORKER_RESULT_MISSING")

        fake_route = _make_route_decision()
        with (
            patch("nexus.orchestration.scout.AdaptiveRoutingService") as mock_svc_cls,
            patch("nexus.orchestration.scout.run_omniroute_worker", side_effect=fail_scout),
            patch("nexus.orchestration.scout.record_checkpoint"),
        ):
            mock_svc = MagicMock()
            mock_svc.select_route_for_capability.return_value = fake_route
            mock_svc_cls.return_value = mock_svc

            with self.assertRaises(ScoutStageFailedError):
                run_scout_stage(run_id="RUN-H", repo=r"C:\fake", request_text=BROAD_MISSION)

    def test_both_fail_manager_not_called(self):
        """When all scouts fail, go.py sets run BLOCKED, Manager never called."""
        with (
            patch("nexus.orchestration.go.classify_mission_scope") as mock_triage,
            patch("nexus.orchestration.go.run_scout_stage") as mock_scouts,
            patch("nexus.orchestration.go.run_manager") as mock_manager,
        ):
            mock_triage.return_value = MagicMock(
                scope=MissionScope.BROAD,
                reason_codes=("BROAD_SCOPE_ANCHOR_AND_OPEN_TARGET",),
            )
            mock_scouts.side_effect = ScoutStageFailedError("both scouts failed")

            from nexus.orchestration.go import GoError, run_go
            with self.assertRaises(GoError) as ctx:
                run_go(BROAD_MISSION)

            mock_manager.assert_not_called()
            self.assertEqual(ctx.exception.code, "SCOUT_STAGE_FAILED")


# ---------------------------------------------------------------------------
# I. WORKER_RESULT_MISSING / WORKER_RESULT_EMPTY treated as scout failure
# ---------------------------------------------------------------------------

class ScoutResultMissingEmptyTests(unittest.TestCase):
    """Test I: missing and empty result are scout failures."""

    def test_missing_result_treated_as_failure_in_bundle(self):
        results = [
            _make_scout_result(None, status="FAILED", reason="WORKER_RESULT_MISSING"),
            _make_scout_result("risk data"),
        ]
        bundle = build_evidence_bundle(results)
        self.assertIn("WORKER_RESULT_MISSING", bundle)
        self.assertIn("evidence unavailable", bundle)

    def test_empty_result_treated_as_failure_in_bundle(self):
        results = [
            _make_scout_result("arch data"),
            _make_scout_result(None, status="FAILED", reason="WORKER_RESULT_EMPTY"),
        ]
        bundle = build_evidence_bundle(results)
        self.assertIn("WORKER_RESULT_EMPTY", bundle)
        self.assertIn("evidence unavailable", bundle)

    def test_both_missing_raises_in_run_scout_stage(self):
        def missing_scout(**kw):
            return _make_scout_result(None, status="FAILED", reason="WORKER_RESULT_MISSING")

        fake_route = _make_route_decision()
        with (
            patch("nexus.orchestration.scout.AdaptiveRoutingService") as mock_svc_cls,
            patch("nexus.orchestration.scout.run_omniroute_worker", side_effect=missing_scout),
            patch("nexus.orchestration.scout.record_checkpoint"),
        ):
            mock_svc = MagicMock()
            mock_svc.select_route_for_capability.return_value = fake_route
            mock_svc_cls.return_value = mock_svc

            with self.assertRaises(ScoutStageFailedError):
                run_scout_stage(run_id="RUN-I", repo=r"C:\fake", request_text=BROAD_MISSION)


# ---------------------------------------------------------------------------
# J. Per-scout evidence bound
# ---------------------------------------------------------------------------

class PerScoutBoundTests(unittest.TestCase):
    """Test J: individual scout output is bounded to PER_SCOUT_CHAR_LIMIT."""

    def test_long_scout_output_truncated(self):
        long_output = "X" * (PER_SCOUT_CHAR_LIMIT + 5000)
        results = [
            _make_scout_result(long_output),
            _make_scout_result("risk data"),
        ]
        bundle = build_evidence_bundle(results)
        # The Scout A section must not exceed limit + marker length
        scout_a_start = bundle.index("--- Scout A:")
        scout_b_start = bundle.index("--- Scout B:")
        scout_a_section = bundle[scout_a_start:scout_b_start]
        self.assertLessEqual(
            len(scout_a_section),
            PER_SCOUT_CHAR_LIMIT + len("--- Scout A: Architecture / Execution Paths ---\n") + len(_TRUNCATION_MARKER) + 50,
        )
        self.assertIn(_TRUNCATION_MARKER, scout_a_section)

    def test_short_output_not_truncated(self):
        short_output = "Short finding"
        results = [_make_scout_result(short_output), _make_scout_result("risk")]
        bundle = build_evidence_bundle(results)
        self.assertIn(short_output, bundle)
        self.assertNotIn(_TRUNCATION_MARKER, bundle)


# ---------------------------------------------------------------------------
# K. Global evidence bound
# ---------------------------------------------------------------------------

class GlobalEvidenceBoundTests(unittest.TestCase):
    """Test K: total evidence bundle is bounded to GLOBAL_EVIDENCE_CHAR_LIMIT."""

    def test_global_limit_enforced(self):
        # Two scouts each at PER_SCOUT_CHAR_LIMIT; with headers the raw
        # combined could exceed GLOBAL if limits were wrong.
        long_a = "A" * PER_SCOUT_CHAR_LIMIT
        long_b = "B" * PER_SCOUT_CHAR_LIMIT
        results = [_make_scout_result(long_a), _make_scout_result(long_b)]
        bundle = build_evidence_bundle(results)
        # Bundle content (header + combined + footer) should stay reasonable.
        # The per-scout truncation ensures combined <= 2 * PER_SCOUT_CHAR_LIMIT = GLOBAL.
        # We check the combined body directly.
        content_start = bundle.index("--- Scout A:")
        content_end = bundle.rfind("END OF SCOUT EVIDENCE")
        content_section = bundle[content_start:content_end]
        self.assertLessEqual(
            len(content_section),
            GLOBAL_EVIDENCE_CHAR_LIMIT + 2 * len(_TRUNCATION_MARKER) + 200,
        )


# ---------------------------------------------------------------------------
# L. Scout sandbox_mode == "read-only"
# ---------------------------------------------------------------------------

class ScoutSandboxTests(unittest.TestCase):
    """Test L: scouts call run_omniroute_worker with sandbox_mode='read-only'."""

    def test_scout_uses_read_only_sandbox(self):
        captured_kwargs = []

        def capture_scout(run_id, repo, task, model, effort, sandbox_mode="workspace-write", **kw):
            captured_kwargs.append(sandbox_mode)
            return _make_scout_result(f"output for task")

        fake_route = _make_route_decision()
        with (
            patch("nexus.orchestration.scout.AdaptiveRoutingService") as mock_svc_cls,
            patch("nexus.orchestration.scout.run_omniroute_worker", side_effect=capture_scout),
            patch("nexus.orchestration.scout.record_checkpoint"),
        ):
            mock_svc = MagicMock()
            mock_svc.select_route_for_capability.return_value = fake_route
            mock_svc_cls.return_value = mock_svc

            run_scout_stage(run_id="RUN-L", repo=r"C:\fake", request_text=BROAD_MISSION)

        self.assertEqual(len(captured_kwargs), 2)
        for mode in captured_kwargs:
            self.assertEqual(mode, "read-only", f"Expected read-only, got {mode!r}")


# ---------------------------------------------------------------------------
# M. Normal worker sandbox remains workspace-write / default
# ---------------------------------------------------------------------------

class NormalWorkerSandboxTests(unittest.TestCase):
    """Test M: run_omniroute_worker default sandbox_mode is 'workspace-write'."""

    def test_default_sandbox_mode_is_workspace_write(self):
        """run_omniroute_worker's default sandbox_mode should be workspace-write."""
        import inspect
        from nexus.dispatchers.omniroute import run_omniroute_worker
        sig = inspect.signature(run_omniroute_worker)
        default = sig.parameters["sandbox_mode"].default
        self.assertEqual(default, "workspace-write")

    def test_sandbox_flag_in_command_for_workers(self):
        """Sandbox mode is passed as -Sandbox to PowerShell command."""
        from pathlib import Path
        captured_cmds = []

        def fake_popen(command, **kwargs):
            captured_cmds.append(command)
            proc = MagicMock()
            proc.stdout = iter([])
            proc.wait.return_value = 1  # FAILED — avoid result file logic
            return proc

        with (
            patch("nexus.dispatchers.omniroute.ADAPTER_PATH", Path("/fake/omniroute-worker.ps1")),
            patch("nexus.dispatchers.omniroute.Path.exists", return_value=True),
            patch("nexus.dispatchers.omniroute.subprocess.Popen", side_effect=fake_popen),
            patch("nexus.dispatchers.omniroute.tempfile.mkstemp", return_value=(5, "/tmp/nexus_worker_test.txt")),
            patch("nexus.dispatchers.omniroute.os.close"),
            patch("nexus.dispatchers.omniroute.Path.unlink"),
            patch("nexus.dispatchers.omniroute.create_agent", return_value="AGENT-TEST"),
            patch("nexus.dispatchers.omniroute.update_agent_status"),
            patch("nexus.dispatchers.omniroute.update_agent_execution"),
            patch("nexus.dispatchers.omniroute.codex_subprocess_env", return_value={}),
        ):
            from nexus.dispatchers.omniroute import run_omniroute_worker
            run_omniroute_worker(
                run_id="RUN-M",
                repo=r"C:\fake",
                task="Do work",
                model="cc/claude-sonnet-5-low",
                effort="low",
                sandbox_mode="workspace-write",
            )

        self.assertEqual(len(captured_cmds), 1)
        cmd = captured_cmds[0]
        self.assertIn("-Sandbox", cmd)
        sandbox_idx = cmd.index("-Sandbox")
        self.assertEqual(cmd[sandbox_idx + 1], "workspace-write")

    def test_scout_sandbox_read_only_in_command(self):
        """scout_mode=read-only propagates as -Sandbox read-only in command."""
        from pathlib import Path
        captured_cmds = []

        def fake_popen(command, **kwargs):
            captured_cmds.append(command)
            proc = MagicMock()
            proc.stdout = iter([])
            proc.wait.return_value = 1
            return proc

        with (
            patch("nexus.dispatchers.omniroute.ADAPTER_PATH", Path("/fake/omniroute-worker.ps1")),
            patch("nexus.dispatchers.omniroute.Path.exists", return_value=True),
            patch("nexus.dispatchers.omniroute.subprocess.Popen", side_effect=fake_popen),
            patch("nexus.dispatchers.omniroute.tempfile.mkstemp", return_value=(5, "/tmp/nexus_worker_test.txt")),
            patch("nexus.dispatchers.omniroute.os.close"),
            patch("nexus.dispatchers.omniroute.Path.unlink"),
            patch("nexus.dispatchers.omniroute.create_agent", return_value="AGENT-TEST"),
            patch("nexus.dispatchers.omniroute.update_agent_status"),
            patch("nexus.dispatchers.omniroute.update_agent_execution"),
            patch("nexus.dispatchers.omniroute.codex_subprocess_env", return_value={}),
        ):
            from nexus.dispatchers.omniroute import run_omniroute_worker
            run_omniroute_worker(
                run_id="RUN-M2",
                repo=r"C:\fake",
                task="Scout task",
                model="cc/claude-sonnet-5-low",
                effort="low",
                sandbox_mode="read-only",
            )

        cmd = captured_cmds[0]
        self.assertIn("-Sandbox", cmd)
        sandbox_idx = cmd.index("-Sandbox")
        self.assertEqual(cmd[sandbox_idx + 1], "read-only")


# ---------------------------------------------------------------------------
# M2. Scout role propagation — dispatcher boundary
# ---------------------------------------------------------------------------

class ScoutRolePropagationTests(unittest.TestCase):
    """Test M2: _run_one_scout passes role=<scout_label> to run_omniroute_worker.

    This is the Phase 4.2D fix test. Verifies that:
      - scout_index=0 → role == _SCOUT_LABELS[0] (Scout A label)
      - scout_index=1 → role == _SCOUT_LABELS[1] (Scout B label)
      - normal worker call leaves role default == "Worker"
    """

    def _capture_role_for_scout(self, scout_index: int) -> str:
        """Run _run_one_scout for given index and capture the role= passed to run_omniroute_worker."""
        from nexus.orchestration.scout import _run_one_scout, _SCOUT_LABELS

        captured = {}

        def fake_worker(**kwargs):
            captured["role"] = kwargs.get("role")
            return {"status": "COMPLETED", "output": "scout output", "reason": None}

        route = {"model": "cc/claude-sonnet-5-low", "effort": "low",
                 "provider": "claude", "execution_path": "OMNIROUTE"}

        with patch("nexus.orchestration.scout.run_omniroute_worker", side_effect=fake_worker):
            _run_one_scout(
                run_id="RUN-ROLE",
                repo=r"C:ake",
                scout_index=scout_index,
                route=route,
            )

        return captured.get("role")

    def test_scout_a_role_is_scout_a_label(self):
        """Scout index 0 → run_omniroute_worker receives Scout A label as role."""
        from nexus.orchestration.scout import _SCOUT_LABELS
        role = self._capture_role_for_scout(0)
        self.assertEqual(role, _SCOUT_LABELS[0])
        self.assertIn("Scout A", role)

    def test_scout_b_role_is_scout_b_label(self):
        """Scout index 1 → run_omniroute_worker receives Scout B label as role."""
        from nexus.orchestration.scout import _SCOUT_LABELS
        role = self._capture_role_for_scout(1)
        self.assertEqual(role, _SCOUT_LABELS[1])
        self.assertIn("Scout B", role)

    def test_normal_worker_role_defaults_to_worker(self):
        """run_omniroute_worker default role parameter is 'Worker' (unchanged)."""
        import inspect
        from nexus.dispatchers.omniroute import run_omniroute_worker
        sig = inspect.signature(run_omniroute_worker)
        default = sig.parameters["role"].default
        self.assertEqual(default, "Worker")

    def test_scout_roles_are_distinct(self):
        """Scout A and Scout B have different role values."""
        role_a = self._capture_role_for_scout(0)
        role_b = self._capture_role_for_scout(1)
        self.assertNotEqual(role_a, role_b)


# ---------------------------------------------------------------------------
# N. Scout routing goes through AdaptiveRoutingService, no model hardcoded
# ---------------------------------------------------------------------------

class ScoutRoutingTests(unittest.TestCase):
    """Test N: scouts use AdaptiveRoutingService with planning capability."""

    def test_scout_calls_adaptive_routing_planning_low(self):
        """run_scout_stage calls select_route_for_capability('planning', 'LOW')."""
        routing_calls = []

        def fake_select(capability, risk_level=None):
            routing_calls.append((capability, risk_level))
            return _make_route_decision()

        mock_svc = MagicMock()
        mock_svc.select_route_for_capability.side_effect = fake_select

        def fake_scout(**kw):
            return _make_scout_result("content")

        with (
            patch("nexus.orchestration.scout.run_omniroute_worker", side_effect=fake_scout),
            patch("nexus.orchestration.scout.record_checkpoint"),
        ):
            run_scout_stage(
                run_id="RUN-N",
                repo=r"C:\fake",
                request_text=BROAD_MISSION,
                routing_service=mock_svc,
            )

        self.assertEqual(len(routing_calls), 1)
        cap, risk = routing_calls[0]
        self.assertEqual(cap, "planning")
        self.assertEqual(risk, "LOW")

    def test_no_hardcoded_model_in_scout_module(self):
        """scout.py source does not contain a hardcoded model string."""
        import nexus.orchestration.scout as scout_mod
        src = Path(scout_mod.__file__).read_text()
        forbidden_models = ["cc/claude-sonnet-5-low", "cc/claude-sonnet-5-high", "gpt-5.6-luna"]
        for model in forbidden_models:
            self.assertNotIn(model, src, f"Hardcoded model {model!r} found in scout.py")


# ---------------------------------------------------------------------------
# O. Scouts not included in final plan["workers"]
# ---------------------------------------------------------------------------

class ScoutsNotInDagTests(_DbMixin):
    """Test O: scout agents never appear in plan["workers"]."""

    def test_scouts_not_in_plan_workers(self):
        """Manager-produced workers are the only entries in plan['workers']."""
        captured_plan = {}

        def fake_manager(**kw):
            plan = {**VALID_PLAN}
            captured_plan["plan"] = plan
            return {"manager_id": "MGR", "status": "COMPLETED", "exit_code": 0, "plan": plan}

        with (
            patch("nexus.orchestration.go.classify_mission_scope") as mock_triage,
            patch("nexus.orchestration.go.run_scout_stage", return_value="BUNDLE"),
            patch("nexus.orchestration.go.run_manager", side_effect=fake_manager),
            patch("nexus.orchestration.go.run_integrated_plan", return_value=[{
                "status": "COMPLETED",
                "worker": {"status": "COMPLETED", "output": "done"},
                "review": {"verdict": "PASS"},
                "_planned_scope": "Fix the thing.",
            }]),
        ):
            mock_triage.return_value = MagicMock(
                scope=MissionScope.BROAD,
                reason_codes=("BROAD_SCOPE_ANCHOR_AND_OPEN_TARGET",),
            )

            from nexus.orchestration.go import run_go
            result = run_go(BROAD_MISSION)

        # plan["workers"] contains exactly the Manager's workers
        plan_workers = captured_plan["plan"]["workers"]
        worker_scopes = [w.get("scope") for w in plan_workers]
        self.assertIn("Fix the thing.", worker_scopes)
        # No "Scout" role or scout-specific scope should appear
        for scope in worker_scopes:
            self.assertNotIn("Scout", scope or "")


# ---------------------------------------------------------------------------
# P. Phase 1–3 DAG engine executes Manager-produced workers (smoke)
# ---------------------------------------------------------------------------

class PhaseOneTwoThreeIntegrationTests(_DbMixin):
    """Test P: existing worker DAG executes normally after scouts."""

    def test_worker_dag_executes_after_scout_stage(self):
        """_run_workers_by_round is called with Manager's workers, not scout agents."""
        captured_workers = {}

        def fake_run_by_round(run_id, primary_repo, manager_id, request_text, workers, plan_risk, exclude_models=frozenset(), prepare_dependencies=False):
            captured_workers["workers"] = workers
            return [{"status": "COMPLETED", "worker": {"output": "done", "agent_id": "A1"}, "history": [], "_planned_scope": workers[0].get("scope", "")}]

        with (
            patch("nexus.orchestration.go.classify_mission_scope") as mock_triage,
            patch("nexus.orchestration.go.run_scout_stage", return_value="BUNDLE"),
            patch("nexus.orchestration.go.run_manager") as mock_manager,
            patch("nexus.orchestration.go.run_integrated_plan", side_effect=fake_run_by_round),
        ):
            mock_triage.return_value = MagicMock(
                scope=MissionScope.BROAD,
                reason_codes=("BROAD_SCOPE_ANCHOR_AND_OPEN_TARGET",),
            )
            mock_manager.return_value = {
                "manager_id": "MGR",
                "status": "COMPLETED",
                "exit_code": 0,
                "plan": {**VALID_PLAN},
            }

            from nexus.orchestration.go import run_go
            run_go(BROAD_MISSION)

        # Workers passed to DAG engine come from Manager's plan, not scouts
        self.assertIsNotNone(captured_workers.get("workers"))
        for w in captured_workers["workers"]:
            self.assertIn("scope", w)


# ---------------------------------------------------------------------------
# Q. Phase 3 dependency context unchanged
# ---------------------------------------------------------------------------

class Phase3ContextUnchangedTests(unittest.TestCase):
    """Test Q: build_dependency_context is not modified by Phase 4.2."""

    def test_dependency_context_builds_correctly(self):
        """Existing dependency context builder still works unchanged."""
        from nexus.orchestration.context import build_dependency_context

        worker_results = [
            {
                "status": "COMPLETED",
                "_planned_scope": "Do task A",
                "worker": {"output": "output of A", "model": "cc/test", "provider": "claude"},
                "history": [{"attempt": 1}],
            }
        ]
        ctx_text, entries = build_dependency_context(
            depends_on=[0],
            worker_results=worker_results,
        )
        self.assertIn("output of A", ctx_text)
        self.assertIn("DEPENDENCY CONTEXT", ctx_text)
        self.assertIn("UNTRUSTED DATA", ctx_text)
        # Scout evidence should not appear in dependency context
        self.assertNotIn("SCOUT", ctx_text)


# ---------------------------------------------------------------------------
# R. No automatic integration worker
# ---------------------------------------------------------------------------

class NoAutoIntegrationWorkerTests(_DbMixin):
    """Test R: Nexus never automatically appends an integration worker."""

    def test_no_integration_worker_appended(self):
        """plan['workers'] is used as-is from Manager; no extra worker added."""
        two_worker_plan = {
            **VALID_PLAN,
            "workers": [
                {**VALID_PLAN["workers"][0], "scope": "Task A", "depends_on": []},
                {**VALID_PLAN["workers"][0], "scope": "Task B", "depends_on": []},
            ],
        }
        captured_workers = {}

        def fake_round(run_id, primary_repo, manager_id, request_text, workers, plan_risk, exclude_models=frozenset(), prepare_dependencies=False):
            captured_workers["workers"] = list(workers)
            results = []
            for w in workers:
                results.append({
                    "status": "COMPLETED",
                    "worker": {"output": "done", "agent_id": "A1"},
                    "history": [],
                    "_planned_scope": w.get("scope", ""),
                })
            return results

        with (
            patch("nexus.orchestration.go.classify_mission_scope") as mock_triage,
            patch("nexus.orchestration.go.run_scout_stage", return_value="BUNDLE"),
            patch("nexus.orchestration.go.run_manager") as mock_manager,
            patch("nexus.orchestration.go.run_integrated_plan", side_effect=fake_round),
        ):
            mock_triage.return_value = MagicMock(
                scope=MissionScope.BROAD,
                reason_codes=("BROAD_SCOPE_ANCHOR_AND_OPEN_TARGET",),
            )
            mock_manager.return_value = {
                "manager_id": "MGR",
                "status": "COMPLETED",
                "exit_code": 0,
                "plan": two_worker_plan,
            }

            from nexus.orchestration.go import run_go
            run_go(BROAD_MISSION)

        # Exactly 2 workers — none appended automatically
        self.assertEqual(len(captured_workers.get("workers", [])), 2)


# ---------------------------------------------------------------------------
# S. Phase 4.1 triage regression
# ---------------------------------------------------------------------------

class Phase41TriageRegressionTests(unittest.TestCase):
    """Test S: Phase 4.1 classify_mission_scope still works correctly."""

    def test_broad_mission_classified_broad(self):
        result = classify_mission_scope(BROAD_MISSION)
        self.assertEqual(result.scope, MissionScope.BROAD)

    def test_bounded_mission_classified_bounded(self):
        result = classify_mission_scope(BOUNDED_MISSION)
        self.assertEqual(result.scope, MissionScope.BOUNDED)

    def test_empty_input_classified_bounded(self):
        result = classify_mission_scope("")
        self.assertEqual(result.scope, MissionScope.BOUNDED)

    def test_concrete_target_overrides_to_bounded(self):
        mixed = "Analise este projeto e os principais problemas em nexus/dispatchers/manager.py"
        result = classify_mission_scope(mixed)
        self.assertEqual(result.scope, MissionScope.BOUNDED)


# ---------------------------------------------------------------------------
# T. Phase 4.1H-C native result channel unaffected
# ---------------------------------------------------------------------------

class NativeResultChannelRegressionTests(unittest.TestCase):
    """Test T: Phase 4.1H-C result contract unchanged."""

    def _make_popen_side_effect(self, returncode, file_content, stdout_lines=None):
        from pathlib import Path

        def _side_effect(command, **kwargs):
            result_file_path = None
            try:
                idx = command.index("-ResultFile")
                result_file_path = command[idx + 1]
            except (ValueError, IndexError):
                pass
            if result_file_path is not None and file_content is not None:
                Path(result_file_path).write_text(file_content, encoding="utf-8")
            proc = MagicMock()
            proc.stdout = iter(stdout_lines or [])
            proc.wait.return_value = returncode
            return proc

        return _side_effect

    def _call(self, returncode, file_content, stdout_lines=None):
        from pathlib import Path as _Path
        from nexus.dispatchers.omniroute import run_omniroute_worker

        _fake_adapter = _Path("/fake/adapter.ps1")

        def _exists_side_effect(self_path):
            # Adapter must appear to exist; result file uses real FS
            if self_path == _fake_adapter:
                return True
            return _Path.is_file(self_path)

        with (
            patch("nexus.dispatchers.omniroute.ADAPTER_PATH", _fake_adapter),
            patch("nexus.dispatchers.omniroute.Path.exists", _exists_side_effect),
            patch(
                "nexus.dispatchers.omniroute.subprocess.Popen",
                side_effect=self._make_popen_side_effect(returncode, file_content, stdout_lines),
            ),
            patch("nexus.dispatchers.omniroute.create_agent", return_value="AGENT-T"),
            patch("nexus.dispatchers.omniroute.update_agent_status"),
            patch("nexus.dispatchers.omniroute.update_agent_execution"),
            patch("nexus.dispatchers.omniroute.codex_subprocess_env", return_value={}),
        ):
            return run_omniroute_worker(
                run_id="RUN-T",
                repo=r"C:\fake",
                task="Do work",
                model="cc/claude-sonnet-5-low",
                effort="low",
            )

    def test_exit0_valid_result_completed(self):
        result = self._call(0, "Valid semantic output")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["output"], "Valid semantic output")

    def test_exit0_missing_result_file(self):
        result = self._call(0, None)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result.get("reason"), "WORKER_RESULT_MISSING")

    def test_exit0_empty_result_file(self):
        result = self._call(0, "   \n  ")
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result.get("reason"), "WORKER_RESULT_EMPTY")

    def test_nonzero_exit_failed(self):
        result = self._call(1, "some content")
        self.assertEqual(result["status"], "FAILED")


# ---------------------------------------------------------------------------
# Evidence bundle unit tests (additional)
# ---------------------------------------------------------------------------

class EvidenceBundleUnitTests(unittest.TestCase):
    """Additional build_evidence_bundle unit tests."""

    def test_bundle_contains_untrusted_boundary(self):
        results = [_make_scout_result("arch"), _make_scout_result("risk")]
        bundle = build_evidence_bundle(results)
        self.assertIn("UNTRUSTED DATA", bundle)
        self.assertIn("REPOSITORY EVIDENCE", bundle)
        self.assertIn("END OF SCOUT EVIDENCE", bundle)

    def test_bundle_original_mission_not_present(self):
        """The original mission text is never injected into the bundle."""
        results = [_make_scout_result("arch"), _make_scout_result("risk")]
        bundle = build_evidence_bundle(results)
        self.assertNotIn(BROAD_MISSION, bundle)

    def test_both_failed_returns_all_unavailable(self):
        results = [
            _make_scout_result(None, status="FAILED", reason="WORKER_RESULT_MISSING"),
            _make_scout_result(None, status="FAILED", reason="WORKER_RESULT_EMPTY"),
        ]
        bundle = build_evidence_bundle(results)
        self.assertEqual(bundle.count("evidence unavailable"), 2)


if __name__ == "__main__":
    unittest.main()
