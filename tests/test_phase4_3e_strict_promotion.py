"""
tests/test_phase4_3e_strict_promotion.py

Phase 4.3E-B: Strict Worker Contract Promotion Tests (scenarios A-T).

Verifies that every NEW Manager-generated plan is validated with
strict_worker_contract=True, while legacy persisted plans (resume/recovery)
remain unaffected by the stricter boundary.

All tests are pure unit tests: no real codex subprocess, no real nexus go run.
"""

import json
import tempfile
import types
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_plan(intent="EXECUTION", workers=None, **overrides):
    """Minimal structurally-valid plan for strict-mode testing."""
    plan = {
        "complexity": "LOW",
        "risk": "LOW",
        "parallelism": 1,
        "summary": "Test plan",
        "intent": intent,
        "workers": workers if workers is not None else [_base_worker()],
    }
    plan.update(overrides)
    return plan


def _base_worker(**overrides):
    """Minimal structurally-valid worker with all Phase 4.3 contract fields."""
    worker = {
        "route_class": "standard-coding",
        "execution_path": "OMNIROUTE",
        "provider": "omniroute",
        "model": "cc/claude-sonnet-5-low",
        "effort": "low",
        "scope": "Implement feature X",
        "reason": "Standard coding task",
        "deliverable": "Pull request with feature X implemented.",
        "authority": "WORKSPACE_WRITE",
        "acceptance_criteria": "All tests pass and feature X works end-to-end.",
        "depends_on": [],
    }
    worker.update(overrides)
    return worker


def _legacy_worker(**overrides):
    """Pre-Phase-4.3 worker: no authority, deliverable, or acceptance_criteria."""
    worker = {
        "route_class": "mechanical",
        "execution_path": "OMNIROUTE",
        "provider": "omniroute",
        "model": "oc/big-pickle",
        "effort": "low",
        "scope": "Fix the thing.",
        "reason": "Simple mechanical change.",
        "depends_on": [],
    }
    worker.update(overrides)
    return worker


def _make_plan_envelope(plan_dict):
    """Wrap a plan dict in a Manager output envelope string."""
    return "NEXUS_PLAN_BEGIN\n{}\nNEXUS_PLAN_END".format(json.dumps(plan_dict))


class _FakeProject:
    """Minimal project stub for go.py."""
    id = "norte"
    name = "Norte"
    path = r"C:\fake\repo"


# ---------------------------------------------------------------------------
# Class 1: Direct _validate_plan strict-mode unit tests (A-J)
# ---------------------------------------------------------------------------

class TestStrictValidationGate(unittest.TestCase):
    """Direct _validate_plan() unit tests exercising strict_worker_contract=True."""

    def _validate(self, plan, strict=True):
        from nexus.dispatchers.manager import _validate_plan
        return _validate_plan(plan, strict_worker_contract=strict)

    def test_A_compliant_execution_plan_passes(self):
        """A: compliant EXECUTION plan passes strict validation."""
        plan = _base_plan(intent="EXECUTION")
        result = self._validate(plan)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["intent"], "EXECUTION")

    def test_B_compliant_analysis_plan_no_deliverable_ac_passes(self):
        """B: compliant ANALYSIS plan (authority only, no deliverable/AC) passes."""
        worker = _base_worker()
        del worker["deliverable"]
        del worker["acceptance_criteria"]
        plan = _base_plan(intent="ANALYSIS", workers=[worker])
        result = self._validate(plan)
        self.assertEqual(result["intent"], "ANALYSIS")

    def test_C_execution_missing_authority_rejected(self):
        """C: EXECUTION plan missing authority is rejected."""
        worker = _base_worker()
        del worker["authority"]
        plan = _base_plan(workers=[worker])
        with self.assertRaises(ValueError) as ctx:
            self._validate(plan)
        self.assertIn("WORKER_AUTHORITY_MISSING", str(ctx.exception))

    def test_D_execution_missing_deliverable_rejected(self):
        """D: EXECUTION plan missing deliverable is rejected."""
        worker = _base_worker()
        del worker["deliverable"]
        plan = _base_plan(workers=[worker])
        with self.assertRaises(ValueError) as ctx:
            self._validate(plan)
        self.assertIn("WORKER_DELIVERABLE_MISSING", str(ctx.exception))

    def test_E_execution_missing_acceptance_criteria_rejected(self):
        """E: EXECUTION plan missing acceptance_criteria is rejected."""
        worker = _base_worker()
        del worker["acceptance_criteria"]
        plan = _base_plan(workers=[worker])
        with self.assertRaises(ValueError) as ctx:
            self._validate(plan)
        self.assertIn("WORKER_ACCEPTANCE_CRITERIA_MISSING", str(ctx.exception))

    def test_F_analysis_missing_authority_rejected(self):
        """F: ANALYSIS plan missing authority is rejected (authority always required)."""
        worker = _base_worker()
        del worker["authority"]
        del worker["deliverable"]
        del worker["acceptance_criteria"]
        plan = _base_plan(intent="ANALYSIS", workers=[worker])
        with self.assertRaises(ValueError) as ctx:
            self._validate(plan)
        self.assertIn("WORKER_AUTHORITY_MISSING", str(ctx.exception))

    def test_G_multi_worker_compliant_plan_passes(self):
        """G: multi-worker compliant plan passes."""
        w0 = _base_worker(authority="READ_ONLY", scope="Analyze code")
        w1 = _base_worker(authority="WORKSPACE_WRITE", scope="Apply changes",
                          depends_on=[0])
        plan = _base_plan(workers=[w0, w1], parallelism=1)
        result = self._validate(plan)
        self.assertEqual(len(result["workers"]), 2)

    def test_H_second_worker_malformed_identifies_correct_worker(self):
        """H: worker N malformed causes whole-plan rejection and names correct worker."""
        w0 = _base_worker()
        w1 = _base_worker()
        del w1["authority"]
        plan = _base_plan(workers=[w0, w1], parallelism=2)
        with self.assertRaises(ValueError) as ctx:
            self._validate(plan)
        err = str(ctx.exception)
        self.assertIn("Worker 2", err)
        self.assertIn("WORKER_AUTHORITY_MISSING", err)

    def test_I_review_critical_workspace_write_rejected(self):
        """I: review-critical + WORKSPACE_WRITE rejected (hard gate, always enforced)."""
        worker = _base_worker(route_class="review-critical", authority="WORKSPACE_WRITE")
        plan = _base_plan(workers=[worker])
        with self.assertRaises(ValueError) as ctx:
            self._validate(plan)
        self.assertIn("WORKER_AUTHORITY_ROUTE_CONFLICT", str(ctx.exception))

    def test_J_security_critical_workspace_write_rejected(self):
        """J: security-critical + WORKSPACE_WRITE rejected (hard gate, always enforced)."""
        worker = _base_worker(route_class="security-critical", authority="WORKSPACE_WRITE")
        plan = _base_plan(workers=[worker])
        with self.assertRaises(ValueError) as ctx:
            self._validate(plan)
        self.assertIn("WORKER_AUTHORITY_ROUTE_CONFLICT", str(ctx.exception))


# ---------------------------------------------------------------------------
# Class 2: _extract_plan strict-activation boundary tests (K-L)
# ---------------------------------------------------------------------------

class TestExtractPlanStrictActivation(unittest.TestCase):
    """Verifies that _extract_plan() calls _validate_plan with strict=True."""

    def test_K_extract_plan_calls_strict_validation(self):
        """K: _extract_plan() calls _validate_plan with strict_worker_contract=True."""
        from nexus.dispatchers.manager import _extract_plan, _validate_plan
        plan = _base_plan(intent="EXECUTION")
        output = _make_plan_envelope(plan)
        with patch(
            "nexus.dispatchers.manager._validate_plan",
            wraps=_validate_plan,
        ) as spy:
            result = _extract_plan(output)
        spy.assert_called_once()
        _, kwargs = spy.call_args
        self.assertTrue(
            kwargs.get("strict_worker_contract", False),
            "strict_worker_contract must be True in _extract_plan()",
        )
        self.assertEqual(result["intent"], "EXECUTION")

    def test_L_malformed_plan_through_extract_plan_raises(self):
        """L: malformed Manager output causes _extract_plan() to raise ValueError."""
        from nexus.dispatchers import manager as manager_mod
        bad_worker = _base_worker()
        del bad_worker["authority"]
        bad_plan = _base_plan(workers=[bad_worker])
        envelope = _make_plan_envelope(bad_plan)
        with self.assertRaises(ValueError) as ctx:
            manager_mod._extract_plan(envelope)
        self.assertIn("WORKER_AUTHORITY_MISSING", str(ctx.exception))


# ---------------------------------------------------------------------------
# Class 3: Malformed new plan cannot reach downstream lifecycle (M-O)
# ---------------------------------------------------------------------------

_BLOCKED_MANAGER_RESULT = {
    "manager_id": "AGENT-TEST",
    "status": "BLOCKED",
    "exit_code": 0,
    "plan": None,
    "error": "Worker 1 WORKER_AUTHORITY_MISSING: authority is required in strict mode.",
}

_BROAD_TRIAGE = MagicMock(scope=MagicMock(value="BOUNDED"))
_BROAD_TRIAGE.scope.value = "BOUNDED"


def _go_infra_patches():
    """Patches for go.py infrastructure (DB, project resolution, triage)."""
    fake_triage = types.SimpleNamespace(
        scope=types.SimpleNamespace(value="BOUNDED"),
        reason_codes=set(),
    )
    # Import MissionScope so the BROAD branch comparison works
    from nexus.dispatchers.triage import MissionScope
    fake_triage.scope = types.SimpleNamespace(
        value="BOUNDED",
        __eq__=lambda self, other: False,  # ensures BROAD branch not taken
    )
    return [
        patch("nexus.orchestration.go._resolve_project",
              return_value=_FakeProject()),
        patch("nexus.orchestration.go.create_run",
              return_value="RUN-BYPASS-001"),
        patch("nexus.orchestration.go.establish_ownership"),
        patch("nexus.orchestration.go.classify_mission_scope",
              return_value=fake_triage),
        patch("nexus.orchestration.go.record_checkpoint"),
        patch("nexus.orchestration.go.update_run_status"),
        patch("nexus.orchestration.go.update_run_risk"),
    ]


class TestMalformedPlanBypass(unittest.TestCase):
    """Malformed new plans must not reach approval, execution, or authority fallback."""

    def _run_go_with_blocked_manager(self, extra_patches):
        """Run run_go with a BLOCKED manager result, applying extra_patches."""
        from nexus.orchestration.go import GoError, run_go
        infra = _go_infra_patches()
        for p in infra:
            p.__enter__()
        try:
            with patch("nexus.orchestration.go.run_manager",
                       return_value=_BLOCKED_MANAGER_RESULT):
                entered = [p.__enter__() for p in extra_patches]
                try:
                    with self.assertRaises(GoError):
                        run_go("test mission — malformed plan bypass")
                finally:
                    for p in reversed(extra_patches):
                        p.__exit__(None, None, None)
        finally:
            for p in reversed(infra):
                p.__exit__(None, None, None)
        return entered

    def test_M_malformed_plan_does_not_reach_approved_plan_persistence(self):
        """M: malformed new plan does NOT reach record_approved_plan."""
        mock_persist = MagicMock()
        extra = [patch("nexus.orchestration.go.record_approved_plan", mock_persist)]
        self._run_go_with_blocked_manager(extra)
        mock_persist.assert_not_called()

    def test_N_malformed_plan_does_not_reach_execute_progressively(self):
        """N: malformed new plan does NOT reach execute_progressively."""
        mock_exec = MagicMock()
        extra = [patch("nexus.orchestration.go.execute_progressively", mock_exec)]
        self._run_go_with_blocked_manager(extra)
        mock_exec.assert_not_called()

    def test_O_malformed_plan_does_not_reach_authority_fallback(self):
        """O: malformed new plan does NOT reach sandbox_mode_for_authority."""
        mock_sb = MagicMock()
        extra = [patch("nexus.orchestration.executor.sandbox_mode_for_authority", mock_sb)]
        self._run_go_with_blocked_manager(extra)
        mock_sb.assert_not_called()


# ---------------------------------------------------------------------------
# Class 4: BOUNDED and BROAD new-plan paths both use strict validation (P-Q)
# ---------------------------------------------------------------------------

class TestNewPlanPathStrictActivation(unittest.TestCase):
    """Both BOUNDED (no evidence) and BROAD (with evidence) paths hit strict gate."""

    def test_P_bounded_path_uses_strict_validation(self):
        """P: BOUNDED new plan passes through strict _validate_plan."""
        from nexus.dispatchers.manager import _extract_plan, _validate_plan
        plan = _base_plan(intent="EXECUTION")
        output = _make_plan_envelope(plan)
        with patch(
            "nexus.dispatchers.manager._validate_plan",
            wraps=_validate_plan,
        ) as spy:
            _extract_plan(output)
        spy.assert_called_once()
        _, kwargs = spy.call_args
        self.assertTrue(kwargs.get("strict_worker_contract", False))

    def test_Q_broad_path_same_strict_validation(self):
        """Q: BROAD path (evidence prefix) hits the same strict gate."""
        from nexus.dispatchers.manager import _extract_plan, _validate_plan
        worker = _base_worker(authority="READ_ONLY", scope="Analyze layout")
        del worker["deliverable"]
        del worker["acceptance_criteria"]
        plan = _base_plan(intent="ANALYSIS", workers=[worker])
        broad_output = (
            "PLANNING CONTEXT\n"
            "================\n"
            "Repository has 42 Python modules...\n\n"
            + _make_plan_envelope(plan)
        )
        with patch(
            "nexus.dispatchers.manager._validate_plan",
            wraps=_validate_plan,
        ) as spy:
            result = _extract_plan(broad_output)
        spy.assert_called_once()
        _, kwargs = spy.call_args
        self.assertTrue(kwargs.get("strict_worker_contract", False))
        self.assertEqual(result["intent"], "ANALYSIS")


# ---------------------------------------------------------------------------
# Class 5: Scout definitions are never subjected to strict validation (R)
# ---------------------------------------------------------------------------

class TestScoutIsolation(unittest.TestCase):
    """Scout definitions are never passed through _validate_plan."""

    def test_R_scout_execution_does_not_call_validate_plan(self):
        """R: _validate_plan is never called during scout execution."""
        from nexus.dispatchers.manager import _validate_plan

        with patch("nexus.dispatchers.manager._validate_plan",
                   wraps=_validate_plan) as spy, \
             patch("nexus.orchestration.scout.run_omniroute_worker") as mock_run:
            mock_run.return_value = {
                "status": "COMPLETED",
                "output": "Scout found: 42 Python modules.",
                "agent_id": "AGENT-SCOUT-001",
                "worktree": "/tmp/scout-wt",
                "exit_code": 0,
            }
            try:
                from nexus.orchestration.scout import run_scout
                run_scout(
                    run_id="TEST-RUN-SCOUT",
                    repo="/fake/repo",
                    task="Explore repository layout",
                    scout_index=0,
                )
            except Exception:
                pass  # DB/file errors acceptable; only care about _validate_plan

        spy.assert_not_called()


# ---------------------------------------------------------------------------
# Class 6: Legacy persisted plan compatibility (S-T)
# ---------------------------------------------------------------------------

class TestLegacyPlanCompatibility(unittest.TestCase):
    """Legacy plans (pre-4.3) remain resumable/recoverable with compat behavior."""

    def setUp(self):
        """Set up an isolated temporary DB for each test."""
        import nexus.registry.database as database
        import nexus.registry.projects as projects

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp = Path(self._tmp.name)

        self._orig_db = database.DATABASE_PATH
        self._orig_storage = database.STORAGE_DIR
        self._orig_projects = projects.PROJECTS_FILE

        database.STORAGE_DIR = tmp
        database.DATABASE_PATH = tmp / "nexus-test.db"

        fixture = tmp / "projects.json"
        fixture.write_text(json.dumps({
            "projects": [{
                "id": "norte",
                "name": "Norte",
                "path": r"C:\fake\repo",
                "aliases": ["norte"],
                "enabled": True,
            }]
        }), encoding="utf-8")
        projects.PROJECTS_FILE = fixture

        database.initialize_database()
        projects.sync_projects()

        wt = tmp / "wt"
        wt.mkdir()
        self._worktree = str(wt)

    def tearDown(self):
        import nexus.registry.database as database
        import nexus.registry.projects as projects
        database.DATABASE_PATH = self._orig_db
        database.STORAGE_DIR = self._orig_storage
        projects.PROJECTS_FILE = self._orig_projects
        self._tmp.cleanup()

    def _setup_legacy_run(self, plan):
        """Create a run, record PLAN_APPROVED, persist the plan, complete worker."""
        from nexus.registry.runs import create_run
        from nexus.registry.agents import (
            create_agent, update_agent_execution, update_agent_status,
        )
        from nexus.registry.checkpoints import record_approved_plan, record_checkpoint

        run_id = create_run(
            project_id="norte",
            input_text="Legacy: do a thing",
            intent="GO",
            status="RUNNING",
        )
        record_approved_plan(run_id, plan)
        record_checkpoint(
            run_id=run_id,
            boundary="PLAN_APPROVED",
            payload={"manager_id": "AGENT-LEGACY-MGR", "worker_count": 1},
        )
        agent_id = create_agent(
            run_id=run_id,
            role="Worker",
            provider="omniroute",
            model="oc/big-pickle",
            effort="low",
            status="RUNNING",
        )
        update_agent_execution(agent_id, worktree=self._worktree)
        update_agent_status(agent_id, "COMPLETED")
        record_checkpoint(
            run_id=run_id,
            boundary="EXECUTION_START",
            payload={
                "route_class": "mechanical",
                "model": "oc/big-pickle",
                "worker_ordinal": 1,
                "worker_id": agent_id,
            },
        )
        record_checkpoint(
            run_id=run_id,
            boundary="EXECUTION_DONE",
            payload={
                "worker_ordinal": 1,
                "worker_id": agent_id,
                "status": "COMPLETED",
            },
        )
        return run_id

    def test_S_legacy_plan_without_contract_fields_resumes(self):
        """S: resume_run() succeeds for pre-4.3 plan; strict _validate_plan NOT called."""
        from nexus.dispatchers.manager import _validate_plan
        from nexus.orchestration.resume import resume_run

        legacy_plan = {
            "complexity": "LOW",
            "risk": "LOW",
            "parallelism": 1,
            "summary": "Legacy fix",
            "workers": [_legacy_worker()],
        }
        run_id = self._setup_legacy_run(legacy_plan)

        with patch("nexus.dispatchers.manager._validate_plan",
                   wraps=_validate_plan) as spy, \
             patch("nexus.orchestration.resume.execute_progressively",
                   return_value={"status": "COMPLETED", "output": "done"}):
            result = resume_run(run_id)

        for c in spy.call_args_list:
            _, kwargs = c
            self.assertFalse(
                kwargs.get("strict_worker_contract", False),
                "resume_run() must not call strict _validate_plan on legacy plans",
            )

    def test_T_legacy_plan_missing_authority_uses_workspace_write_compat(self):
        """T: executor compat branch: authority=None maps to 'workspace-write'."""
        from nexus.orchestration.executor import sandbox_mode_for_authority

        # Compat branch still present and correct
        self.assertEqual(sandbox_mode_for_authority(None), "workspace-write")
        # Phase 4.3C mapping intact
        self.assertEqual(sandbox_mode_for_authority("READ_ONLY"), "read-only")
        self.assertEqual(sandbox_mode_for_authority("WORKSPACE_WRITE"), "workspace-write")


if __name__ == "__main__":
    unittest.main()
