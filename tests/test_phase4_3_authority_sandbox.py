"""
tests/test_phase4_3_authority_sandbox.py

Phase 4.3C: Worker Authority → Sandbox Mode enforcement tests.

Validates that the authority field on Manager worker plans is deterministically
translated to the correct sandbox_mode passed to run_omniroute_worker(), and
that the mapping is preserved across retries, escalations, and dependency
context injection.

No real Manager, Worker, Codex, OmniRoute, subprocess, or network calls are
made; everything is mocked/injected.
"""
import unittest
from copy import deepcopy
from unittest.mock import MagicMock, call, patch

from nexus.orchestration.executor import (
    sandbox_mode_for_authority,
    execute_worker,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_worker(authority=None, route_class="standard-coding", **overrides):
    """Minimal fully-routed worker dict as it arrives at execute_worker."""
    w = {
        "route_class": route_class,
        "execution_path": "OMNIROUTE",
        "provider": "omniroute",
        "model": "cc/claude-sonnet-5-low",
        "effort": "low",
        "scope": "Analyze the module",
        "reason": "Standard analysis",
        "depends_on": [],
    }
    if authority is not None:
        w["authority"] = authority
    w.update(overrides)
    return w


def _successful_omniroute_result():
    return {
        "status": "COMPLETED",
        "exit_code": 0,
        "agent_id": "AGENT-TEST",
        "worktree": None,
        "output": "done",
    }


# ---------------------------------------------------------------------------
# A: READ_ONLY → "read-only"
# ---------------------------------------------------------------------------
class TestReadOnlyMapsToReadOnly(unittest.TestCase):
    def test_read_only_direct_mapping(self):
        """Test A: READ_ONLY authority maps to 'read-only' sandbox mode."""
        self.assertEqual(sandbox_mode_for_authority("READ_ONLY"), "read-only")

    def test_read_only_reaches_omniroute(self):
        """Test A+N: READ_ONLY worker causes run_omniroute_worker to receive read-only."""
        worker = _make_worker(authority="READ_ONLY")
        with patch(
            "nexus.orchestration.executor.run_omniroute_worker",
            return_value=_successful_omniroute_result(),
        ) as mock_omniroute:
            execute_worker(
                run_id="RUN-A",
                repo="C:/fake/repo",
                manager_id="MGR-A",
                worker=worker,
            )
        mock_omniroute.assert_called_once()
        _, kwargs = mock_omniroute.call_args
        self.assertEqual(kwargs["sandbox_mode"], "read-only")


# ---------------------------------------------------------------------------
# B: WORKSPACE_WRITE → "workspace-write"
# ---------------------------------------------------------------------------
class TestWorkspaceWriteMapsToWorkspaceWrite(unittest.TestCase):
    def test_workspace_write_direct_mapping(self):
        """Test B: WORKSPACE_WRITE maps to 'workspace-write' sandbox mode."""
        self.assertEqual(
            sandbox_mode_for_authority("WORKSPACE_WRITE"), "workspace-write"
        )

    def test_workspace_write_reaches_omniroute(self):
        """Test B+N: WORKSPACE_WRITE worker causes run_omniroute_worker to receive workspace-write."""
        worker = _make_worker(authority="WORKSPACE_WRITE")
        with patch(
            "nexus.orchestration.executor.run_omniroute_worker",
            return_value=_successful_omniroute_result(),
        ) as mock_omniroute:
            execute_worker(
                run_id="RUN-B",
                repo="C:/fake/repo",
                manager_id="MGR-B",
                worker=worker,
            )
        _, kwargs = mock_omniroute.call_args
        self.assertEqual(kwargs["sandbox_mode"], "workspace-write")


# ---------------------------------------------------------------------------
# C: authority absent → "workspace-write" (backward-compat fallback)
# ---------------------------------------------------------------------------
class TestAbsentAuthorityFallsBackToWorkspaceWrite(unittest.TestCase):
    def test_none_authority_maps_to_workspace_write(self):
        """Test C: None authority → 'workspace-write' compat fallback."""
        self.assertEqual(sandbox_mode_for_authority(None), "workspace-write")

    def test_absent_authority_key_reaches_omniroute_with_workspace_write(self):
        """Test C+M: worker without authority key gets workspace-write."""
        worker = _make_worker()  # no authority key
        self.assertNotIn("authority", worker)
        with patch(
            "nexus.orchestration.executor.run_omniroute_worker",
            return_value=_successful_omniroute_result(),
        ) as mock_omniroute:
            execute_worker(
                run_id="RUN-C",
                repo="C:/fake/repo",
                manager_id="MGR-C",
                worker=worker,
            )
        _, kwargs = mock_omniroute.call_args
        self.assertEqual(kwargs["sandbox_mode"], "workspace-write")


# ---------------------------------------------------------------------------
# D: unknown explicit authority → fail closed
# ---------------------------------------------------------------------------
class TestUnknownAuthorityFailsClosed(unittest.TestCase):
    def test_unknown_string_fails_closed(self):
        """Test D: unrecognized explicit authority raises, not silently workspace-write."""
        with self.assertRaises(ValueError) as ctx:
            sandbox_mode_for_authority("ADMIN")
        self.assertIn("WORKER_AUTHORITY_INVALID", str(ctx.exception))
        self.assertIn("ADMIN", str(ctx.exception))

    def test_read_write_fails_closed(self):
        with self.assertRaises(ValueError) as ctx:
            sandbox_mode_for_authority("READ_WRITE")
        self.assertIn("WORKER_AUTHORITY_INVALID", str(ctx.exception))

    def test_empty_string_fails_closed(self):
        """Empty string is an explicit invalid authority, not the same as None."""
        with self.assertRaises(ValueError) as ctx:
            sandbox_mode_for_authority("")
        self.assertIn("WORKER_AUTHORITY_INVALID", str(ctx.exception))

    def test_unknown_authority_reaching_execute_worker_fails_closed(self):
        """Test D: invalid authority reaching execute_worker propagates ValueError."""
        worker = _make_worker(authority="SUPERUSER")
        with self.assertRaises(ValueError) as ctx:
            execute_worker(
                run_id="RUN-D",
                repo="C:/fake/repo",
                manager_id="MGR-D",
                worker=worker,
            )
        self.assertIn("WORKER_AUTHORITY_INVALID", str(ctx.exception))


# ---------------------------------------------------------------------------
# E: non-string explicit authority → fail closed
# ---------------------------------------------------------------------------
class TestNonStringAuthorityFailsClosed(unittest.TestCase):
    def test_int_fails_closed(self):
        """Test E: integer authority is not a valid explicit value."""
        with self.assertRaises((ValueError, AttributeError, TypeError)):
            sandbox_mode_for_authority(1)

    def test_bool_fails_closed(self):
        """Test E: bool authority is not a valid explicit value."""
        with self.assertRaises((ValueError, AttributeError, TypeError)):
            sandbox_mode_for_authority(True)

    def test_list_fails_closed(self):
        with self.assertRaises((ValueError, AttributeError, TypeError)):
            sandbox_mode_for_authority(["READ_ONLY"])


# ---------------------------------------------------------------------------
# F: READ_ONLY worker retry → every attempt remains read-only
# ---------------------------------------------------------------------------
class TestReadOnlyRetryPreservesAuthority(unittest.TestCase):
    def test_three_retries_all_read_only(self):
        """Test F: sandbox_mode stays 'read-only' across multiple execute_worker calls
        for the same READ_ONLY worker (simulating retries with same worker dict)."""
        worker = _make_worker(authority="READ_ONLY")
        captured_sandbox_modes = []

        def fake_omniroute(**kwargs):
            captured_sandbox_modes.append(kwargs.get("sandbox_mode"))
            return _successful_omniroute_result()

        with patch(
            "nexus.orchestration.executor.run_omniroute_worker",
            side_effect=fake_omniroute,
        ):
            for attempt in range(1, 4):
                execute_worker(
                    run_id=f"RUN-F-{attempt}",
                    repo="C:/fake/repo",
                    manager_id="MGR-F",
                    worker=worker,
                    worker_index=attempt,
                )

        self.assertEqual(len(captured_sandbox_modes), 3)
        for mode in captured_sandbox_modes:
            self.assertEqual(mode, "read-only",
                             f"Retry changed sandbox mode to {mode!r}")


# ---------------------------------------------------------------------------
# G: WORKSPACE_WRITE worker retry → every attempt remains workspace-write
# ---------------------------------------------------------------------------
class TestWorkspaceWriteRetryPreservesAuthority(unittest.TestCase):
    def test_three_retries_all_workspace_write(self):
        """Test G: sandbox_mode stays 'workspace-write' across multiple retries."""
        worker = _make_worker(authority="WORKSPACE_WRITE")
        captured = []

        def fake_omniroute(**kwargs):
            captured.append(kwargs.get("sandbox_mode"))
            return _successful_omniroute_result()

        with patch(
            "nexus.orchestration.executor.run_omniroute_worker",
            side_effect=fake_omniroute,
        ):
            for attempt in range(1, 4):
                execute_worker(
                    run_id=f"RUN-G-{attempt}",
                    repo="C:/fake/repo",
                    manager_id="MGR-G",
                    worker=worker,
                    worker_index=attempt,
                )

        self.assertEqual(len(captured), 3)
        for mode in captured:
            self.assertEqual(mode, "workspace-write")


# ---------------------------------------------------------------------------
# H: route/model escalation does not alter sandbox
# ---------------------------------------------------------------------------
class TestEscalationPreservesAuthority(unittest.TestCase):
    def test_escalated_worker_retains_read_only(self):
        """Test H: after escalation (model change), authority field is preserved
        and sandbox mode is still 'read-only'."""
        # Simulate what next_route does: deepcopy + update with routing fields only
        original = _make_worker(authority="READ_ONLY", model="cc/claude-sonnet-5-low")
        escalated = deepcopy(original)
        # next_route only updates routing fields, never authority
        escalated.update({
            "model": "cc/claude-sonnet-5",
            "effort": "medium",
        })

        self.assertEqual(escalated.get("authority"), "READ_ONLY")

        captured = []

        def fake_omniroute(**kwargs):
            captured.append(kwargs.get("sandbox_mode"))
            return _successful_omniroute_result()

        with patch(
            "nexus.orchestration.executor.run_omniroute_worker",
            side_effect=fake_omniroute,
        ):
            execute_worker(
                run_id="RUN-H",
                repo="C:/fake/repo",
                manager_id="MGR-H",
                worker=escalated,
                worker_index=2,
            )

        self.assertEqual(captured[0], "read-only",
                         "Escalation changed sandbox mode")

    def test_escalated_workspace_write_retains_workspace_write(self):
        """Test H: WORKSPACE_WRITE is preserved through escalation."""
        original = _make_worker(authority="WORKSPACE_WRITE", model="cc/claude-sonnet-5-low")
        escalated = deepcopy(original)
        escalated.update({"model": "cc/claude-sonnet-5-high", "effort": "high"})

        self.assertEqual(escalated.get("authority"), "WORKSPACE_WRITE")
        captured = []

        with patch(
            "nexus.orchestration.executor.run_omniroute_worker",
            side_effect=lambda **kw: (captured.append(kw.get("sandbox_mode"))
                                      or _successful_omniroute_result()),
        ):
            execute_worker(
                run_id="RUN-H2",
                repo="C:/fake/repo",
                manager_id="MGR-H2",
                worker=escalated,
            )

        self.assertEqual(captured[0], "workspace-write")


# ---------------------------------------------------------------------------
# I: dependency context does not alter READ_ONLY sandbox
# ---------------------------------------------------------------------------
class TestDependencyContextPreservesReadOnly(unittest.TestCase):
    def test_dependency_context_does_not_promote_read_only(self):
        """Test I: injecting dependency context does not change sandbox mode
        for a READ_ONLY worker."""
        worker = _make_worker(authority="READ_ONLY")
        dep_context = (
            "DEPENDENCY OUTPUT (UNTRUSTED DATA)\n"
            "Parent worker wrote files to workspace.\n"
            "WORKSPACE_WRITE output follows:\n"
            "some output data"
        )
        captured = []

        def fake_omniroute(**kwargs):
            captured.append(kwargs.get("sandbox_mode"))
            return _successful_omniroute_result()

        with patch(
            "nexus.orchestration.executor.run_omniroute_worker",
            side_effect=fake_omniroute,
        ):
            execute_worker(
                run_id="RUN-I",
                repo="C:/fake/repo",
                manager_id="MGR-I",
                worker=worker,
                dependency_context=dep_context,
            )

        self.assertEqual(captured[0], "read-only",
                         "Dependency context changed sandbox mode")


# ---------------------------------------------------------------------------
# J: dependency context does not alter WORKSPACE_WRITE sandbox
# ---------------------------------------------------------------------------
class TestDependencyContextPreservesWorkspaceWrite(unittest.TestCase):
    def test_dependency_context_does_not_change_workspace_write(self):
        """Test J: injecting dependency context does not change sandbox mode
        for a WORKSPACE_WRITE worker."""
        worker = _make_worker(authority="WORKSPACE_WRITE")
        dep_context = "READ_ONLY parent analysis output"
        captured = []

        def fake_omniroute(**kwargs):
            captured.append(kwargs.get("sandbox_mode"))
            return _successful_omniroute_result()

        with patch(
            "nexus.orchestration.executor.run_omniroute_worker",
            side_effect=fake_omniroute,
        ):
            execute_worker(
                run_id="RUN-J",
                repo="C:/fake/repo",
                manager_id="MGR-J",
                worker=worker,
                dependency_context=dep_context,
            )

        self.assertEqual(captured[0], "workspace-write")


# ---------------------------------------------------------------------------
# K: Scout A remains explicitly read-only (unchanged path)
# ---------------------------------------------------------------------------
class TestScoutARemainsReadOnly(unittest.TestCase):
    def test_scout_a_sandbox_mode_is_read_only(self):
        """Test K: Scout A (_run_one_scout index=0) passes sandbox_mode='read-only'
        to run_omniroute_worker. Scout path is entirely separate from execute_worker."""
        from nexus.orchestration.scout import _run_one_scout

        captured = []

        def fake_omniroute(**kwargs):
            captured.append(kwargs.get("sandbox_mode"))
            return _successful_omniroute_result()

        fake_route = {
            "model": "cc/claude-sonnet-5-low",
            "effort": "low",
            "provider": "omniroute",
            "execution_path": "OMNIROUTE",
        }

        with patch(
            "nexus.orchestration.scout.run_omniroute_worker",
            side_effect=fake_omniroute,
        ):
            _run_one_scout(
                run_id="RUN-K",
                repo="C:/fake/repo",
                scout_index=0,
                route=fake_route,
            )

        self.assertGreater(len(captured), 0, "Scout did not call run_omniroute_worker")
        for mode in captured:
            self.assertEqual(mode, "read-only",
                             f"Scout A used sandbox_mode={mode!r}, expected 'read-only'")


# ---------------------------------------------------------------------------
# L: Scout B remains explicitly read-only (unchanged path)
# ---------------------------------------------------------------------------
class TestScoutBRemainsReadOnly(unittest.TestCase):
    def test_scout_b_sandbox_mode_is_read_only(self):
        """Test L: Scout B (_run_one_scout index=1) passes sandbox_mode='read-only'."""
        from nexus.orchestration.scout import _run_one_scout

        captured = []

        def fake_omniroute(**kwargs):
            captured.append(kwargs.get("sandbox_mode"))
            return _successful_omniroute_result()

        fake_route = {
            "model": "cc/claude-sonnet-5-low",
            "effort": "low",
            "provider": "omniroute",
            "execution_path": "OMNIROUTE",
        }

        with patch(
            "nexus.orchestration.scout.run_omniroute_worker",
            side_effect=fake_omniroute,
        ):
            _run_one_scout(
                run_id="RUN-L",
                repo="C:/fake/repo",
                scout_index=1,
                route=fake_route,
            )

        self.assertGreater(len(captured), 0)
        for mode in captured:
            self.assertEqual(mode, "read-only")


# ---------------------------------------------------------------------------
# M: Normal legacy worker behavior preserved
# ---------------------------------------------------------------------------
class TestLegacyWorkerBehaviorPreserved(unittest.TestCase):
    def test_legacy_worker_no_authority_gets_workspace_write(self):
        """Test M: pre-4.3B worker without authority still executes workspace-write."""
        worker = _make_worker()  # no authority
        captured = []

        def fake_omniroute(**kwargs):
            captured.append(kwargs.get("sandbox_mode"))
            return _successful_omniroute_result()

        with patch(
            "nexus.orchestration.executor.run_omniroute_worker",
            side_effect=fake_omniroute,
        ):
            result = execute_worker(
                run_id="RUN-M",
                repo="C:/fake/repo",
                manager_id="MGR-M",
                worker=worker,
            )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(captured[0], "workspace-write")


# ---------------------------------------------------------------------------
# N: run_omniroute_worker receives the expected sandbox_mode (explicit param)
# ---------------------------------------------------------------------------
class TestOmnirouteReceivesSandboxMode(unittest.TestCase):
    def test_sandbox_mode_kwarg_is_explicitly_passed(self):
        """Test N: sandbox_mode is an explicit kwarg, not relying on default."""
        worker = _make_worker(authority="READ_ONLY")

        with patch(
            "nexus.orchestration.executor.run_omniroute_worker",
            return_value=_successful_omniroute_result(),
        ) as mock_omniroute:
            execute_worker(
                run_id="RUN-N",
                repo="C:/fake/repo",
                manager_id="MGR-N",
                worker=worker,
            )

        # Verify sandbox_mode is passed as an explicit keyword argument
        _, kwargs = mock_omniroute.call_args
        self.assertIn("sandbox_mode", kwargs,
                      "sandbox_mode was not passed as an explicit kwarg")
        self.assertEqual(kwargs["sandbox_mode"], "read-only")


# ---------------------------------------------------------------------------
# O: No keyword inference from scope/task
# ---------------------------------------------------------------------------
class TestNoKeywordInferenceFromScope(unittest.TestCase):
    def test_delete_in_scope_does_not_affect_sandbox(self):
        """Test O: task scope mentioning 'delete' does not auto-set workspace-write
        for a READ_ONLY worker."""
        worker = _make_worker(
            authority="READ_ONLY",
            scope="delete all temporary files and clean up the workspace",
        )
        captured = []

        def fake_omniroute(**kwargs):
            captured.append(kwargs.get("sandbox_mode"))
            return _successful_omniroute_result()

        with patch(
            "nexus.orchestration.executor.run_omniroute_worker",
            side_effect=fake_omniroute,
        ):
            execute_worker(
                run_id="RUN-O",
                repo="C:/fake/repo",
                manager_id="MGR-O",
                worker=worker,
            )

        self.assertEqual(captured[0], "read-only",
                         "Scope keyword inference changed sandbox mode")

    def test_read_only_in_scope_does_not_set_read_only_for_workspace_write(self):
        """Test O: scope mentioning 'read-only' does not override explicit WORKSPACE_WRITE."""
        worker = _make_worker(
            authority="WORKSPACE_WRITE",
            scope="read-only analysis then apply fixes",
        )
        captured = []

        with patch(
            "nexus.orchestration.executor.run_omniroute_worker",
            side_effect=lambda **kw: (captured.append(kw.get("sandbox_mode"))
                                       or _successful_omniroute_result()),
        ):
            execute_worker(
                run_id="RUN-O2",
                repo="C:/fake/repo",
                manager_id="MGR-O2",
                worker=worker,
            )

        self.assertEqual(captured[0], "workspace-write")


# ---------------------------------------------------------------------------
# P: Reviewer behavior unchanged (review_worker uses separate code path)
# ---------------------------------------------------------------------------
class TestReviewerBehaviorUnchanged(unittest.TestCase):
    def test_review_worker_uses_separate_code_path_not_execute_worker(self):
        """Test P: review_worker (nexus.dispatchers.review) does NOT call
        execute_worker. It dispatches via codex CLI directly with a hardcoded
        --sandbox read-only flag, entirely bypassing the worker authority→sandbox
        bridge. Phase 4.3C must not change this.

        We verify the separation by confirming that importing execute_worker
        and mocking it does not affect review_worker calls at all.
        """
        from nexus.dispatchers.review import review_worker
        from nexus.orchestration.executor import execute_worker as real_execute_worker

        execute_worker_was_called = []

        with patch(
            "nexus.orchestration.executor.execute_worker",
            side_effect=lambda *a, **kw: execute_worker_was_called.append(True),
        ), patch("nexus.dispatchers.review._resolve_reviewer_route") as mock_route,            patch("nexus.dispatchers.review.subprocess") as mock_subprocess:
            decision = MagicMock()
            decision.model = "cc/claude-sonnet-5-high"
            decision.effort = "high"
            decision.provider = "omniroute"
            decision.execution_path = "OMNIROUTE"
            decision.reason = "review-qualified"
            decision.degraded = False
            mock_route.return_value = decision

            # subprocess.Popen simulation
            mock_proc = MagicMock()
            mock_proc.stdout.readline.side_effect = [
                b"NEXUS_REVIEW_BEGIN\n",
                b'{"verdict":"PASS","summary":"ok","failure_class":null}\n',
                b"NEXUS_REVIEW_END\n",
                b"",
            ]
            mock_proc.wait.return_value = 0
            mock_proc.returncode = 0
            mock_subprocess.Popen.return_value = mock_proc

            try:
                with patch("nexus.registry.agents.create_agent", return_value="REVIEWER-1"),                      patch("nexus.registry.agents.update_agent_status"):
                    review_worker(
                        run_id="RUN-P",
                        worker_id="WORKER-1",
                        worktree="C:/fake/wt",
                        original_task="Fix the bug",
                        worker_scope="Fix module X",
                    )
            except Exception:
                pass

        # The key invariant: review_worker never went through execute_worker
        self.assertEqual(
            execute_worker_was_called, [],
            "review_worker unexpectedly called execute_worker — "
            "Phase 4.3C must not route review through the worker authority bridge",
        )

    def test_review_sandbox_policy_is_read_only_in_source(self):
        """Test P: verify review.py source hardcodes read-only sandbox
        via --sandbox read-only CLI argument (not through authority mapping)."""
        import inspect
        from nexus.dispatchers import review as review_module
        source = inspect.getsource(review_module)
        # review.py calls codex with --sandbox read-only directly
        self.assertIn('read-only', source)
        # review.py must NOT call sandbox_mode_for_authority
        self.assertNotIn("sandbox_mode_for_authority", source)


# ---------------------------------------------------------------------------
# Q: Phase 4.3B validation remains intact
# ---------------------------------------------------------------------------
class TestPhase43BValidationIntact(unittest.TestCase):
    def test_validate_plan_still_rejects_review_critical_workspace_write(self):
        """Test Q: 4.3B hard gate still works after 4.3C changes."""
        from nexus.dispatchers.manager import _validate_plan

        plan = {
            "complexity": "LOW",
            "risk": "LOW",
            "parallelism": 1,
            "summary": "Test",
            "intent": "EXECUTION",
            "workers": [{
                "route_class": "review-critical",
                "execution_path": "OMNIROUTE",
                "provider": "omniroute",
                "model": "cc/claude-sonnet-5-low",
                "effort": "low",
                "scope": "Review the code",
                "reason": "Review task",
                "authority": "WORKSPACE_WRITE",
                "deliverable": "Review report",
                "acceptance_criteria": "Report covers all concerns.",
                "depends_on": [],
            }],
        }
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan)
        self.assertIn("WORKER_AUTHORITY_ROUTE_CONFLICT", str(ctx.exception))

    def test_validate_plan_strict_mode_requires_authority(self):
        """Test Q: 4.3B strict mode still requires authority in strict mode."""
        from nexus.dispatchers.manager import _validate_plan

        plan = {
            "complexity": "LOW",
            "risk": "LOW",
            "parallelism": 1,
            "summary": "Legacy plan",
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
        with self.assertRaises(ValueError) as ctx:
            _validate_plan(plan, strict_worker_contract=True)
        self.assertIn("WORKER_AUTHORITY_MISSING", str(ctx.exception))


# ---------------------------------------------------------------------------
# Determinism: mapping function is pure
# ---------------------------------------------------------------------------
class TestMappingFunctionIsPure(unittest.TestCase):
    def test_same_input_always_same_output(self):
        """Mapping is deterministic: repeated calls produce identical results."""
        for _ in range(100):
            self.assertEqual(sandbox_mode_for_authority("READ_ONLY"), "read-only")
            self.assertEqual(sandbox_mode_for_authority("WORKSPACE_WRITE"), "workspace-write")
            self.assertEqual(sandbox_mode_for_authority(None), "workspace-write")

    def test_no_state_mutation_between_calls(self):
        """Calling with different values in sequence produces correct results."""
        results = [
            sandbox_mode_for_authority("READ_ONLY"),
            sandbox_mode_for_authority("WORKSPACE_WRITE"),
            sandbox_mode_for_authority(None),
            sandbox_mode_for_authority("READ_ONLY"),
        ]
        self.assertEqual(results, ["read-only", "workspace-write", "workspace-write", "read-only"])


if __name__ == "__main__":
    unittest.main()
