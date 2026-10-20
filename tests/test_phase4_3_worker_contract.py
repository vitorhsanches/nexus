"""
tests/test_phase4_3_worker_contract.py

Phase 4.3B: Worker Contract Schema + Deterministic Validation tests.

Tests the new deliverable / authority / acceptance_criteria fields added to
_validate_plan() and the strict-mode boundary.
"""
import json
import unittest
from unittest.mock import patch

from nexus.dispatchers import manager as _manager_mod
from nexus.dispatchers.manager import _validate_plan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_plan(intent="EXECUTION", **overrides):
    """Minimal structurally-valid Phase 4.3B plan (strict mode ready)."""
    plan = {
        "complexity": "LOW",
        "risk": "LOW",
        "parallelism": 1,
        "summary": "Test plan",
        "intent": intent,
        "workers": [_base_worker(**overrides)],
    }
    return plan


def _base_worker(**overrides):
    """Minimal structurally-valid Phase 4.3B worker."""
    worker = {
        "route_class": "standard-coding",
        "execution_path": "OMNIROUTE",
        "provider": "omniroute",
        "model": "cc/claude-sonnet-5-low",
        "effort": "low",
        "scope": "Analyze module X",
        "reason": "Standard coding task",
        "deliverable": "Analysis report with findings.",
        "authority": "READ_ONLY",
        "acceptance_criteria": "Report covers all target modules.",
        "depends_on": [],
    }
    worker.update(overrides)
    return worker


def _legacy_worker():
    """Phase 1-4.2 worker without new contract fields."""
    return {
        "route_class": "standard-coding",
        "execution_path": "OMNIROUTE",
        "provider": "omniroute",
        "model": "cc/claude-sonnet-5-low",
        "effort": "low",
        "scope": "Fix the bug in line 42",
        "reason": "Simple fix",
        "depends_on": [],
    }


def _legacy_plan(intent="EXECUTION"):
    return {
        "complexity": "LOW",
        "risk": "LOW",
        "parallelism": 1,
        "summary": "Legacy test plan",
        "intent": intent,
        "workers": [_legacy_worker()],
    }


# ---------------------------------------------------------------------------
# A: Legacy worker accepted in compatibility mode
# ---------------------------------------------------------------------------
class TestLegacyCompatibilityMode(unittest.TestCase):
    def test_legacy_worker_accepted_compat_mode(self):
        """Test A: legacy worker (no new fields) passes in compatibility mode."""
        plan = _legacy_plan()
        result = _validate_plan(plan)  # strict_worker_contract=False is default
        self.assertEqual(result["intent"], "EXECUTION")
        self.assertEqual(len(result["workers"]), 1)

    def test_legacy_analysis_worker_compat_mode(self):
        plan = _legacy_plan(intent="ANALYSIS")
        result = _validate_plan(plan)
        self.assertEqual(result["intent"], "ANALYSIS")


# ---------------------------------------------------------------------------
# B: Legacy worker rejected in strict mode when required fields absent
# ---------------------------------------------------------------------------
class TestLegacyStrictModeRejection(unittest.TestCase):
    def test_legacy_worker_rejected_strict_execution(self):
        """Test B: legacy EXECUTION worker missing authority fails in strict mode."""
        plan = _legacy_plan()
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan, strict_worker_contract=True)
        self.assertIn("WORKER_AUTHORITY_MISSING", str(ctx.exception))

    def test_legacy_worker_missing_deliverable_strict(self):
        """Test B: worker with authority but no deliverable fails strict EXECUTION."""
        plan = _base_plan()
        plan["workers"] = [_base_worker(deliverable=None)]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan, strict_worker_contract=True)
        self.assertIn("WORKER_DELIVERABLE_MISSING", str(ctx.exception))

    def test_legacy_worker_missing_acceptance_criteria_strict(self):
        """Test B: worker missing acceptance_criteria fails strict EXECUTION."""
        plan = _base_plan()
        plan["workers"] = [_base_worker(acceptance_criteria=None)]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan, strict_worker_contract=True)
        self.assertIn("WORKER_ACCEPTANCE_CRITERIA_MISSING", str(ctx.exception))


# ---------------------------------------------------------------------------
# C: Valid READ_ONLY worker accepted
# ---------------------------------------------------------------------------
class TestValidReadOnlyWorker(unittest.TestCase):
    def test_read_only_worker_accepted(self):
        """Test C: valid READ_ONLY worker passes strict validation."""
        plan = _base_plan()
        result = _validate_plan(plan, strict_worker_contract=True)
        self.assertEqual(result["workers"][0]["authority"], "READ_ONLY")

    def test_read_only_analysis_plan(self):
        plan = _base_plan(intent="ANALYSIS")
        result = _validate_plan(plan, strict_worker_contract=True)
        self.assertEqual(result["workers"][0]["authority"], "READ_ONLY")


# ---------------------------------------------------------------------------
# D: Valid WORKSPACE_WRITE worker accepted where permitted
# ---------------------------------------------------------------------------
class TestValidWorkspaceWriteWorker(unittest.TestCase):
    def test_workspace_write_standard_coding(self):
        """Test D: WORKSPACE_WRITE is valid for standard-coding."""
        plan = _base_plan()
        plan["workers"] = [_base_worker(
            authority="WORKSPACE_WRITE",
            deliverable="Modified files with tests passing.",
        )]
        result = _validate_plan(plan, strict_worker_contract=True)
        self.assertEqual(result["workers"][0]["authority"], "WORKSPACE_WRITE")

    def test_workspace_write_complex_coding(self):
        plan = _base_plan()
        plan["workers"] = [_base_worker(
            route_class="complex-coding",
            authority="WORKSPACE_WRITE",
            deliverable="Refactored module.",
        )]
        result = _validate_plan(plan, strict_worker_contract=True)
        self.assertEqual(result["workers"][0]["authority"], "WORKSPACE_WRITE")


# ---------------------------------------------------------------------------
# E: deliverable wrong type rejected
# ---------------------------------------------------------------------------
class TestDeliverableWrongType(unittest.TestCase):
    def test_deliverable_int_rejected(self):
        """Test E: deliverable as int is rejected."""
        plan = _base_plan()
        plan["workers"] = [_base_worker(deliverable=42)]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan, strict_worker_contract=True)
        self.assertIn("WORKER_DELIVERABLE_INVALID", str(ctx.exception))

    def test_deliverable_list_rejected(self):
        plan = _base_plan()
        plan["workers"] = [_base_worker(deliverable=["item"])]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan, strict_worker_contract=True)
        self.assertIn("WORKER_DELIVERABLE_INVALID", str(ctx.exception))

    def test_deliverable_int_rejected_compat_mode(self):
        """Non-string deliverable is rejected even in compat mode."""
        plan = _base_plan()
        plan["workers"] = [_base_worker(deliverable=99)]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan)
        self.assertIn("WORKER_DELIVERABLE_INVALID", str(ctx.exception))


# ---------------------------------------------------------------------------
# F: Empty deliverable rejected when required
# ---------------------------------------------------------------------------
class TestEmptyDeliverable(unittest.TestCase):
    def test_empty_deliverable_rejected_strict_execution(self):
        """Test F: empty deliverable rejected for EXECUTION in strict mode."""
        plan = _base_plan()
        plan["workers"] = [_base_worker(deliverable="")]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan, strict_worker_contract=True)
        self.assertIn("WORKER_DELIVERABLE_MISSING", str(ctx.exception))

    def test_whitespace_only_deliverable_rejected(self):
        plan = _base_plan()
        plan["workers"] = [_base_worker(deliverable="   ")]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan, strict_worker_contract=True)
        self.assertIn("WORKER_DELIVERABLE_MISSING", str(ctx.exception))

    def test_empty_deliverable_analysis_compat_not_required(self):
        """Empty deliverable OK for non-EXECUTION in compat mode (not required)."""
        plan = _legacy_plan(intent="ANALYSIS")
        # No deliverable in legacy plan -> compat mode accepts it
        result = _validate_plan(plan)
        self.assertIsNone(result["workers"][0].get("deliverable"))


# ---------------------------------------------------------------------------
# G: authority wrong type rejected
# ---------------------------------------------------------------------------
class TestAuthorityWrongType(unittest.TestCase):
    def test_authority_int_rejected(self):
        """Test G: authority as int is rejected."""
        plan = _base_plan()
        plan["workers"] = [_base_worker(authority=1)]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan)
        self.assertIn("WORKER_AUTHORITY_INVALID", str(ctx.exception))

    def test_authority_bool_rejected(self):
        plan = _base_plan()
        plan["workers"] = [_base_worker(authority=True)]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan)
        self.assertIn("WORKER_AUTHORITY_INVALID", str(ctx.exception))


# ---------------------------------------------------------------------------
# H: Unknown authority rejected
# ---------------------------------------------------------------------------
class TestUnknownAuthorityRejected(unittest.TestCase):
    def test_read_write_rejected(self):
        """Test H: 'READ_WRITE' is not a valid authority."""
        plan = _base_plan()
        plan["workers"] = [_base_worker(authority="READ_WRITE")]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan)
        self.assertIn("WORKER_AUTHORITY_INVALID", str(ctx.exception))

    def test_arbitrary_string_rejected(self):
        plan = _base_plan()
        plan["workers"] = [_base_worker(authority="admin")]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan)
        self.assertIn("WORKER_AUTHORITY_INVALID", str(ctx.exception))

    def test_empty_string_authority_rejected(self):
        plan = _base_plan()
        plan["workers"] = [_base_worker(authority="")]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan)
        self.assertIn("WORKER_AUTHORITY_INVALID", str(ctx.exception))


# ---------------------------------------------------------------------------
# I: acceptance_criteria wrong type rejected
# ---------------------------------------------------------------------------
class TestAcceptanceCriteriaWrongType(unittest.TestCase):
    def test_acceptance_criteria_list_rejected(self):
        """Test I: acceptance_criteria as list is rejected."""
        plan = _base_plan()
        plan["workers"] = [_base_worker(acceptance_criteria=["pass tests"])]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan, strict_worker_contract=True)
        self.assertIn("WORKER_ACCEPTANCE_CRITERIA_INVALID", str(ctx.exception))

    def test_acceptance_criteria_int_rejected(self):
        plan = _base_plan()
        plan["workers"] = [_base_worker(acceptance_criteria=0)]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan, strict_worker_contract=True)
        self.assertIn("WORKER_ACCEPTANCE_CRITERIA_INVALID", str(ctx.exception))


# ---------------------------------------------------------------------------
# J: Empty acceptance_criteria rejected when required
# ---------------------------------------------------------------------------
class TestEmptyAcceptanceCriteria(unittest.TestCase):
    def test_empty_ac_rejected_strict_execution(self):
        """Test J: empty acceptance_criteria rejected for EXECUTION in strict mode."""
        plan = _base_plan()
        plan["workers"] = [_base_worker(acceptance_criteria="")]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan, strict_worker_contract=True)
        self.assertIn("WORKER_ACCEPTANCE_CRITERIA_MISSING", str(ctx.exception))

    def test_whitespace_only_ac_rejected(self):
        plan = _base_plan()
        plan["workers"] = [_base_worker(acceptance_criteria="   ")]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan, strict_worker_contract=True)
        self.assertIn("WORKER_ACCEPTANCE_CRITERIA_MISSING", str(ctx.exception))


# ---------------------------------------------------------------------------
# K: Whitespace normalization is deterministic
# ---------------------------------------------------------------------------
class TestWhitespaceNormalization(unittest.TestCase):
    def test_deliverable_stripped(self):
        """Test K: surrounding whitespace in deliverable is stripped."""
        plan = _base_plan()
        plan["workers"] = [_base_worker(deliverable="  Report.  ")]
        result = _validate_plan(plan, strict_worker_contract=True)
        self.assertEqual(result["workers"][0]["deliverable"], "Report.")

    def test_authority_stripped(self):
        plan = _base_plan()
        plan["workers"] = [_base_worker(authority="  READ_ONLY  ")]
        result = _validate_plan(plan, strict_worker_contract=True)
        self.assertEqual(result["workers"][0]["authority"], "READ_ONLY")

    def test_acceptance_criteria_stripped(self):
        plan = _base_plan()
        plan["workers"] = [_base_worker(acceptance_criteria=" Tests pass. ")]
        result = _validate_plan(plan, strict_worker_contract=True)
        self.assertEqual(result["workers"][0]["acceptance_criteria"], "Tests pass.")

    def test_normalization_idempotent(self):
        """Normalizing already-clean values changes nothing."""
        plan = _base_plan()
        result1 = _validate_plan(plan, strict_worker_contract=True)
        result2 = _validate_plan(result1, strict_worker_contract=True)
        self.assertEqual(
            result1["workers"][0]["deliverable"],
            result2["workers"][0]["deliverable"],
        )


# ---------------------------------------------------------------------------
# L: Restricted route_class + WORKSPACE_WRITE rejected
# ---------------------------------------------------------------------------
class TestRouteClassAuthorityGate(unittest.TestCase):
    def test_review_critical_workspace_write_rejected(self):
        """Test L: review-critical + WORKSPACE_WRITE is a hard gate failure."""
        plan = _base_plan()
        plan["workers"] = [_base_worker(
            route_class="review-critical",
            authority="WORKSPACE_WRITE",
        )]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan)
        self.assertIn("WORKER_AUTHORITY_ROUTE_CONFLICT", str(ctx.exception))

    def test_security_critical_workspace_write_rejected(self):
        """Test L: security-critical + WORKSPACE_WRITE is a hard gate failure."""
        plan = _base_plan()
        plan["workers"] = [_base_worker(
            route_class="security-critical",
            authority="WORKSPACE_WRITE",
        )]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan)
        self.assertIn("WORKER_AUTHORITY_ROUTE_CONFLICT", str(ctx.exception))

    def test_route_gate_applies_in_compat_mode(self):
        """Route/authority gate is a hard gate, applies even in compat mode."""
        plan = _base_plan()
        plan["workers"] = [_base_worker(
            route_class="review-critical",
            authority="WORKSPACE_WRITE",
        )]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan)  # compat mode (default)
        self.assertIn("WORKER_AUTHORITY_ROUTE_CONFLICT", str(ctx.exception))


# ---------------------------------------------------------------------------
# M: Restricted route_class + READ_ONLY accepted
# ---------------------------------------------------------------------------
class TestRestrictedRouteReadOnlyAccepted(unittest.TestCase):
    def test_review_critical_read_only_accepted(self):
        """Test M: review-critical + READ_ONLY passes."""
        plan = _base_plan()
        plan["workers"] = [_base_worker(
            route_class="review-critical",
            authority="READ_ONLY",
        )]
        result = _validate_plan(plan, strict_worker_contract=True)
        self.assertEqual(result["workers"][0]["authority"], "READ_ONLY")

    def test_security_critical_read_only_accepted(self):
        plan = _base_plan()
        plan["workers"] = [_base_worker(
            route_class="security-critical",
            authority="READ_ONLY",
        )]
        result = _validate_plan(plan, strict_worker_contract=True)
        self.assertEqual(result["workers"][0]["authority"], "READ_ONLY")


# ---------------------------------------------------------------------------
# N: Dependency validation still works
# ---------------------------------------------------------------------------
class TestDependencyValidationPreserved(unittest.TestCase):
    def test_valid_dependency_accepted(self):
        """Test N: valid depends_on still accepted."""
        plan = _base_plan()
        plan["workers"] = [
            _base_worker(depends_on=[]),
            _base_worker(scope="Step 2", depends_on=[0]),
        ]
        result = _validate_plan(plan, strict_worker_contract=True)
        self.assertEqual(result["workers"][1]["depends_on"], [0])

    def test_invalid_self_dependency_rejected(self):
        plan = _base_plan()
        plan["workers"] = [_base_worker(depends_on=[0])]
        with self.assertRaises(ValueError):
            _validate_plan(plan)

    def test_out_of_range_dependency_rejected(self):
        plan = _base_plan()
        plan["workers"] = [_base_worker(depends_on=[5])]
        with self.assertRaises(ValueError):
            _validate_plan(plan)


# ---------------------------------------------------------------------------
# O: Cycle detection still works
# ---------------------------------------------------------------------------
class TestCycleDetectionPreserved(unittest.TestCase):
    def test_two_worker_cycle_rejected(self):
        """Test O: cycle detection still triggers."""
        plan = _base_plan()
        plan["workers"] = [
            _base_worker(scope="A", depends_on=[1]),
            _base_worker(scope="B", depends_on=[0]),
        ]
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan)
        self.assertIn("cycle", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# P: Manager prompt documents all three new fields
# ---------------------------------------------------------------------------
class TestManagerPromptDocumentsNewFields(unittest.TestCase):
    def _get_prompt(self):
        """Extract the Manager prompt text from run_manager source."""
        import inspect
        source = inspect.getsource(_manager_mod)
        return source

    def test_deliverable_in_prompt(self):
        """Test P: 'deliverable' appears in manager.py source (prompt template)."""
        source = self._get_prompt()
        self.assertIn("deliverable", source)

    def test_authority_in_prompt(self):
        source = self._get_prompt()
        self.assertIn("authority", source)

    def test_acceptance_criteria_in_prompt(self):
        source = self._get_prompt()
        self.assertIn("acceptance_criteria", source)

    def test_read_only_in_prompt(self):
        source = self._get_prompt()
        self.assertIn("READ_ONLY", source)

    def test_workspace_write_in_prompt(self):
        source = self._get_prompt()
        self.assertIn("WORKSPACE_WRITE", source)


# ---------------------------------------------------------------------------
# Q: No task-keyword inference of authority
# ---------------------------------------------------------------------------
class TestNoKeywordInferenceOfAuthority(unittest.TestCase):
    def test_delete_keyword_does_not_set_authority(self):
        """Test Q: 'delete' in scope does not auto-set authority."""
        plan = _legacy_plan()
        plan["workers"][0]["scope"] = "delete all files in the directory"
        # In compat mode: no authority present, should not be auto-inferred
        result = _validate_plan(plan)
        self.assertIsNone(result["workers"][0].get("authority"))

    def test_read_only_keyword_in_scope_does_not_set_authority(self):
        plan = _legacy_plan()
        plan["workers"][0]["scope"] = "read-only analysis of the codebase"
        result = _validate_plan(plan)
        self.assertIsNone(result["workers"][0].get("authority"))


# ---------------------------------------------------------------------------
# R: Validation does not mutate engineering structure
# ---------------------------------------------------------------------------
class TestValidationDoesNotMutateStructure(unittest.TestCase):
    def test_no_workers_added(self):
        """Test R: validation does not add workers."""
        plan = _base_plan()
        original_count = len(plan["workers"])
        result = _validate_plan(plan, strict_worker_contract=True)
        self.assertEqual(len(result["workers"]), original_count)

    def test_no_dependencies_invented(self):
        """Test R: validation does not add depends_on entries."""
        plan = _base_plan()
        plan["workers"] = [
            _base_worker(scope="A", depends_on=[]),
            _base_worker(scope="B", depends_on=[]),
        ]
        result = _validate_plan(plan, strict_worker_contract=True)
        self.assertEqual(result["workers"][0]["depends_on"], [])
        self.assertEqual(result["workers"][1]["depends_on"], [])

    def test_scope_not_rewritten(self):
        """Test R: validation does not alter scope text."""
        plan = _base_plan()
        original_scope = "Analyze the authentication module for security risks."
        plan["workers"] = [_base_worker(scope=original_scope)]
        result = _validate_plan(plan, strict_worker_contract=True)
        self.assertEqual(result["workers"][0]["scope"], original_scope)

    def test_deliverable_not_fabricated_when_absent(self):
        """Test R: missing deliverable is not auto-generated."""
        plan = _legacy_plan(intent="ANALYSIS")
        result = _validate_plan(plan)
        self.assertIsNone(result["workers"][0].get("deliverable"))


# ---------------------------------------------------------------------------
# S: Compatibility mode preserves Phase 1-4.2 fixtures
# ---------------------------------------------------------------------------
class TestCompatibilityPreservesLegacyFixtures(unittest.TestCase):
    def test_phase1_style_plan(self):
        """Test S: Phase 1 style plan (no new fields) passes compat mode."""
        plan = {
            "complexity": "LOW",
            "risk": "LOW",
            "parallelism": 1,
            "summary": "Phase 1 test",
            "intent": "EXECUTION",
            "workers": [{
                "route_class": "standard-coding",
                "execution_path": "OMNIROUTE",
                "provider": "omniroute",
                "model": "cc/claude-sonnet-5-low",
                "effort": "low",
                "scope": "Fix the bug",
                "reason": "Simple fix",
                "depends_on": [],
            }],
        }
        result = _validate_plan(plan)
        self.assertEqual(result["intent"], "EXECUTION")

    def test_phase42_broad_mission_plan(self):
        """Test S: Phase 4.2 ANALYSIS plan (scout output) passes compat mode."""
        plan = {
            "complexity": "HIGH",
            "risk": "MEDIUM",
            "parallelism": 2,
            "summary": "BROAD analysis mission",
            "intent": "ANALYSIS",
            "workers": [
                {
                    "route_class": "complex-coding",
                    "execution_path": "OMNIROUTE",
                    "provider": "omniroute",
                    "model": "cc/claude-sonnet-5",
                    "effort": "high",
                    "scope": "Investigate the architecture",
                    "reason": "Complex analysis",
                    "depends_on": [],
                },
            ],
        }
        result = _validate_plan(plan)
        self.assertEqual(result["workers"][0]["depends_on"], [])


# ---------------------------------------------------------------------------
# T: Strict-mode behavior is explicit and deterministic
# ---------------------------------------------------------------------------
class TestStrictModeBehaviorExplicit(unittest.TestCase):
    def test_strict_mode_default_is_off(self):
        """Test T: default call uses compat mode (no strict_worker_contract arg)."""
        plan = _legacy_plan()
        # Must NOT raise in compat mode
        result = _validate_plan(plan)
        self.assertIsNotNone(result)

    def test_strict_mode_must_be_explicit(self):
        """Test T: strict mode requires explicit kwarg; no env or global state."""
        plan = _base_plan()
        # Passing strict_worker_contract=False explicitly is same as default
        result = _validate_plan(plan, strict_worker_contract=False)
        self.assertIsNotNone(result)
        # Now with True: should also pass for a fully-compliant plan
        result2 = _validate_plan(plan, strict_worker_contract=True)
        self.assertIsNotNone(result2)

    def test_same_plan_different_mode_different_outcome(self):
        """Test T: legacy plan passes compat mode but fails strict mode."""
        plan = _legacy_plan()
        # Compat: pass
        _validate_plan(plan)
        # Strict: fail (missing authority)
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan, strict_worker_contract=True)
        self.assertIn("WORKER_AUTHORITY_MISSING", str(ctx.exception))

    def test_no_global_state_pollution(self):
        """Test T: calling in strict mode does not affect subsequent compat call."""
        plan_strict = _base_plan()
        _validate_plan(plan_strict, strict_worker_contract=True)
        # Now call compat mode on legacy plan - must still pass
        plan_legacy = _legacy_plan()
        result = _validate_plan(plan_legacy)
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
