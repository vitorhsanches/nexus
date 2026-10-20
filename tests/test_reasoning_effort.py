"""Tests for the NEXUS adaptive reasoning effort policy.

Tests A-V (22 tests) as specified for Phase B (Adaptive Reasoning Effort).

All tests are pure unit tests of ``select_reasoning_effort()``.  No
filesystem, database, network, or model access of any kind is made.

Tests cover the full effort matrix:
    A-C  : MANAGER role
    D-F  : SCOUT role
    G-L  : WORKER role (complexity × risk × has_dependencies)
    M-Q  : REVIEWER role (risk × route_class)
    R-V  : Retry escalation (reasoning vs. non-reasoning failures)
"""

import unittest

from nexus.policies.reasoning_effort import (
    REASONING_FAILURE_CLASSES,
    NON_REASONING_FAILURE_CLASSES,
    EffortDecision,
    select_reasoning_effort,
)


class TestReasoningEffortManager(unittest.TestCase):
    """A-C: MANAGER role effort selection."""

    def test_a_manager_bounded_scope_is_low(self):
        """Test A: MANAGER + LOW complexity (BOUNDED scope) → effort=low."""
        decision = select_reasoning_effort(
            role="MANAGER",
            complexity="LOW",
            risk=None,
        )
        self.assertIsInstance(decision, EffortDecision)
        self.assertEqual(decision.effort, "low")
        self.assertIn("LOW", decision.reason)

    def test_b_manager_broad_scope_is_medium(self):
        """Test B: MANAGER + MEDIUM complexity (BROAD scope) → effort=medium."""
        decision = select_reasoning_effort(
            role="MANAGER",
            complexity="MEDIUM",
            risk=None,
        )
        self.assertEqual(decision.effort, "medium")
        self.assertIn("MEDIUM", decision.reason)

    def test_c_manager_high_complexity_is_medium(self):
        """Test C: MANAGER + HIGH complexity → effort=medium (not high)."""
        decision = select_reasoning_effort(
            role="MANAGER",
            complexity="HIGH",
            risk=None,
        )
        self.assertEqual(decision.effort, "medium")
        self.assertIn("HIGH", decision.reason)


class TestReasoningEffortScout(unittest.TestCase):
    """D-F: SCOUT role effort selection."""

    def test_d_scout_default_low_complexity_is_low(self):
        """Test D: SCOUT + LOW complexity (default) → effort=low."""
        decision = select_reasoning_effort(
            role="SCOUT",
            complexity="LOW",
            risk=None,
        )
        self.assertEqual(decision.effort, "low")

    def test_e_scout_medium_complexity_is_medium(self):
        """Test E: SCOUT + MEDIUM complexity → effort=medium."""
        decision = select_reasoning_effort(
            role="SCOUT",
            complexity="MEDIUM",
            risk=None,
        )
        self.assertEqual(decision.effort, "medium")

    def test_f_scout_high_complexity_is_medium(self):
        """Test F: SCOUT + HIGH complexity → effort=medium."""
        decision = select_reasoning_effort(
            role="SCOUT",
            complexity="HIGH",
            risk=None,
        )
        self.assertEqual(decision.effort, "medium")


class TestReasoningEffortWorker(unittest.TestCase):
    """G-L: WORKER role effort selection (complexity × risk matrix)."""

    def test_g_worker_low_complexity_any_risk_is_low(self):
        """Test G: WORKER + LOW complexity + HIGH risk → effort=low."""
        decision = select_reasoning_effort(
            role="WORKER",
            complexity="LOW",
            risk="HIGH",
        )
        self.assertEqual(decision.effort, "low")

    def test_h_worker_medium_complexity_low_risk_is_low(self):
        """Test H: WORKER + MEDIUM complexity + LOW risk → effort=low."""
        decision = select_reasoning_effort(
            role="WORKER",
            complexity="MEDIUM",
            risk="LOW",
        )
        self.assertEqual(decision.effort, "low")

    def test_i_worker_medium_complexity_medium_risk_is_medium(self):
        """Test I: WORKER + MEDIUM complexity + MEDIUM risk → effort=medium."""
        decision = select_reasoning_effort(
            role="WORKER",
            complexity="MEDIUM",
            risk="MEDIUM",
        )
        self.assertEqual(decision.effort, "medium")

    def test_j_worker_medium_complexity_high_risk_is_medium(self):
        """Test J: WORKER + MEDIUM complexity + HIGH risk → effort=medium."""
        decision = select_reasoning_effort(
            role="WORKER",
            complexity="MEDIUM",
            risk="HIGH",
        )
        self.assertEqual(decision.effort, "medium")

    def test_k_worker_high_complexity_low_risk_is_medium(self):
        """Test K: WORKER + HIGH complexity + LOW risk → effort=medium."""
        decision = select_reasoning_effort(
            role="WORKER",
            complexity="HIGH",
            risk="LOW",
        )
        self.assertEqual(decision.effort, "medium")

    def test_l_worker_high_complexity_with_dependencies_is_medium(self):
        """Test L: WORKER + HIGH complexity + has_dependencies → effort=medium."""
        decision = select_reasoning_effort(
            role="WORKER",
            complexity="HIGH",
            risk="LOW",
            has_dependencies=True,
        )
        self.assertEqual(decision.effort, "medium")
        self.assertIn("dependent", decision.reason)


class TestReasoningEffortReviewer(unittest.TestCase):
    """M-Q: REVIEWER role effort selection (risk × route_class)."""

    def test_m_reviewer_low_risk_is_low(self):
        """Test M: REVIEWER + LOW risk → effort=low."""
        decision = select_reasoning_effort(
            role="REVIEWER",
            risk="LOW",
        )
        self.assertEqual(decision.effort, "low")

    def test_n_reviewer_medium_risk_is_medium(self):
        """Test N: REVIEWER + MEDIUM risk → effort=medium."""
        decision = select_reasoning_effort(
            role="REVIEWER",
            risk="MEDIUM",
        )
        self.assertEqual(decision.effort, "medium")

    def test_o_reviewer_high_risk_is_medium(self):
        """Test O: REVIEWER + HIGH risk → effort=medium."""
        decision = select_reasoning_effort(
            role="REVIEWER",
            risk="HIGH",
        )
        self.assertEqual(decision.effort, "medium")

    def test_p_reviewer_critical_risk_is_high(self):
        """Test P: REVIEWER + CRITICAL risk → effort=high."""
        decision = select_reasoning_effort(
            role="REVIEWER",
            risk="CRITICAL",
        )
        self.assertEqual(decision.effort, "high")

    def test_q_reviewer_security_critical_route_is_high_even_at_low_risk(self):
        """Test Q: REVIEWER + security-critical route_class + LOW risk → effort=high."""
        decision = select_reasoning_effort(
            role="REVIEWER",
            risk="LOW",
            route_class="security-critical",
        )
        self.assertEqual(decision.effort, "high")
        self.assertIn("security-critical", decision.reason)


class TestReasoningEffortEscalation(unittest.TestCase):
    """R-V: Retry effort escalation (reasoning vs. non-reasoning failures)."""

    def test_r_capability_failure_escalates_low_to_medium(self):
        """Test R: CAPABILITY_FAILURE (reasoning) + low → medium."""
        decision = select_reasoning_effort(
            role="WORKER",
            complexity="MEDIUM",
            risk="LOW",
            previous_effort="low",
            failure_class="CAPABILITY_FAILURE",
        )
        self.assertEqual(decision.effort, "medium")
        self.assertIn("CAPABILITY_FAILURE", decision.reason)

    def test_s_requirement_failure_escalates_medium_to_high(self):
        """Test S: REQUIREMENT_FAILURE (reasoning) + medium → high."""
        decision = select_reasoning_effort(
            role="WORKER",
            complexity="HIGH",
            risk="MEDIUM",
            previous_effort="medium",
            failure_class="REQUIREMENT_FAILURE",
        )
        self.assertEqual(decision.effort, "high")
        self.assertIn("REQUIREMENT_FAILURE", decision.reason)

    def test_t_validation_failure_at_high_stays_high(self):
        """Test T: VALIDATION_FAILURE (reasoning) + high → high (already at max)."""
        decision = select_reasoning_effort(
            role="WORKER",
            complexity="HIGH",
            risk="HIGH",
            previous_effort="high",
            failure_class="VALIDATION_FAILURE",
        )
        self.assertEqual(decision.effort, "high")

    def test_u_provider_failure_does_not_escalate_effort(self):
        """Test U: PROVIDER_FAILURE (non-reasoning) + low → low (unchanged)."""
        decision = select_reasoning_effort(
            role="WORKER",
            complexity="MEDIUM",
            risk="MEDIUM",
            previous_effort="low",
            failure_class="PROVIDER_FAILURE",
        )
        self.assertEqual(decision.effort, "low")
        self.assertIn("non-reasoning", decision.reason)

    def test_v_transient_failure_does_not_escalate_effort(self):
        """Test V: TRANSIENT (non-reasoning) + medium → medium (unchanged)."""
        decision = select_reasoning_effort(
            role="WORKER",
            complexity="HIGH",
            risk="HIGH",
            previous_effort="medium",
            failure_class="TRANSIENT",
        )
        self.assertEqual(decision.effort, "medium")
        self.assertIn("non-reasoning", decision.reason)


class TestReasoningEffortConstants(unittest.TestCase):
    """Additional structural invariants -- not counted in A-V."""

    def test_reasoning_failure_classes_are_expected_set(self):
        """REASONING_FAILURE_CLASSES contains exactly the expected entries."""
        self.assertIn("CAPABILITY_FAILURE", REASONING_FAILURE_CLASSES)
        self.assertIn("REQUIREMENT_FAILURE", REASONING_FAILURE_CLASSES)
        self.assertIn("VALIDATION_FAILURE", REASONING_FAILURE_CLASSES)
        # Non-reasoning classes must NOT be present
        self.assertNotIn("PROVIDER_FAILURE", REASONING_FAILURE_CLASSES)
        self.assertNotIn("TRANSIENT", REASONING_FAILURE_CLASSES)
        self.assertNotIn("TOOL_FAILURE", REASONING_FAILURE_CLASSES)
        self.assertNotIn("SCOPE_VIOLATION", REASONING_FAILURE_CLASSES)

    def test_non_reasoning_failure_classes_never_escalate(self):
        """All NON_REASONING_FAILURE_CLASSES leave effort unchanged."""
        for fc in NON_REASONING_FAILURE_CLASSES:
            for base_effort in ("low", "medium", "high"):
                decision = select_reasoning_effort(
                    role="WORKER",
                    complexity="HIGH",
                    risk="HIGH",
                    previous_effort=base_effort,
                    failure_class=fc,
                )
                self.assertEqual(
                    decision.effort,
                    base_effort,
                    msg=f"{fc} should not escalate effort from {base_effort!r}",
                )

    def test_effort_decision_is_immutable(self):
        """EffortDecision is frozen (immutable)."""
        decision = select_reasoning_effort(role="MANAGER", complexity="LOW")
        with self.assertRaises((AttributeError, TypeError)):
            decision.effort = "high"  # type: ignore[misc]

    def test_select_reasoning_effort_is_pure(self):
        """Same inputs always produce the same output (determinism check)."""
        kwargs = dict(
            role="WORKER",
            complexity="MEDIUM",
            risk="HIGH",
            route_class="standard-coding",
            has_dependencies=True,
        )
        result1 = select_reasoning_effort(**kwargs)
        result2 = select_reasoning_effort(**kwargs)
        self.assertEqual(result1, result2)

    def test_unknown_role_defaults_to_low_effort(self):
        """Unknown role never raises; defaults conservatively to low."""
        decision = select_reasoning_effort(role="UNKNOWN_FUTURE_ROLE")
        self.assertEqual(decision.effort, "low")


if __name__ == "__main__":
    unittest.main()
