"""Tests for the Nexus Mission Lifecycle V1 (nexus/missions/lifecycle.py)."""

import unittest

from nexus.missions.lifecycle import InvalidMissionTransitionError, transition


class MissionLifecycleTestCase(unittest.TestCase):
    def test_created_to_planning(self):
        self.assertEqual(transition("CREATED", "PLANNING"), "PLANNING")

    def test_created_to_ready(self):
        self.assertEqual(transition("CREATED", "READY"), "READY")

    def test_created_to_failed(self):
        self.assertEqual(transition("CREATED", "FAILED"), "FAILED")

    def test_planning_to_ready(self):
        self.assertEqual(transition("PLANNING", "READY"), "READY")

    def test_planning_to_failed(self):
        self.assertEqual(transition("PLANNING", "FAILED"), "FAILED")

    def test_ready_to_running(self):
        self.assertEqual(transition("READY", "RUNNING"), "RUNNING")

    def test_ready_to_failed(self):
        self.assertEqual(transition("READY", "FAILED"), "FAILED")

    def test_running_to_completed(self):
        self.assertEqual(transition("RUNNING", "COMPLETED"), "COMPLETED")

    def test_running_to_failed(self):
        self.assertEqual(transition("RUNNING", "FAILED"), "FAILED")

    def test_completed_is_terminal(self):
        with self.assertRaises(InvalidMissionTransitionError):
            transition("COMPLETED", "RUNNING")

    def test_failed_is_terminal(self):
        with self.assertRaises(InvalidMissionTransitionError):
            transition("FAILED", "READY")

    def test_illegal_created_to_running(self):
        with self.assertRaises(InvalidMissionTransitionError):
            transition("CREATED", "RUNNING")

    def test_illegal_ready_to_completed(self):
        with self.assertRaises(InvalidMissionTransitionError):
            transition("READY", "COMPLETED")

    def test_same_state_is_idempotent_noop(self):
        self.assertEqual(transition("READY", "READY"), "READY")
        self.assertEqual(transition("COMPLETED", "COMPLETED"), "COMPLETED")
        self.assertEqual(transition("FAILED", "FAILED"), "FAILED")

    def test_unknown_status_rejected(self):
        with self.assertRaises(InvalidMissionTransitionError):
            transition("CREATED", "BOGUS")


if __name__ == "__main__":
    unittest.main()
