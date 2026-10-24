"""HTTP contract tests for the /api/dev/* routes in nexus/web/routes.py
(F2.2 Phase 2/3). Mirrors tests/test_chat_routes.py's TestClient pattern:
the service/executor layer is mocked entirely here -- business logic is
already covered by tests/test_dev_service.py, tests/test_dev_executor.py
and tests/test_dev_feature_gate.py. This file only proves the HTTP
envelope: status codes, request/response shapes, error mapping, and the
shared localhost/same-origin protection.
"""

import unittest
from unittest.mock import patch

from nexus.dev.service import DevServiceError


def _make_client():
    from starlette.testclient import TestClient
    from nexus.web.app import create_app

    return TestClient(create_app(), base_url="http://127.0.0.1:8000", raise_server_exceptions=True)


class CreateDevSessionEndpointTest(unittest.TestCase):
    def test_create_session_returns_the_service_payload(self):
        client = _make_client()
        fake_session = {
            "session_id": "DEV-AAAA1111", "project_id": "demo", "worktree_path": r"C:\fake\wt",
            "branch": "nexus/dev/DEV-AAAA1111", "base_sha": "abc123", "requested_model": None,
            "status": "ACTIVE", "error_code": None, "error_message": None,
            "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
        }
        with patch("nexus.dev.service.create_dev_session", return_value=fake_session) as mock_create:
            response = client.post("/api/dev/sessions", json={"project_id": "demo"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), fake_session)
        mock_create.assert_called_once_with("demo", requested_model=None)

    def test_unauthorized_project_maps_to_403(self):
        client = _make_client()
        with patch(
            "nexus.dev.service.create_dev_session",
            side_effect=DevServiceError("DEV_MODE_DISABLED", "not authorized", http_status=403),
        ):
            response = client.post("/api/dev/sessions", json={"project_id": "demo"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "DEV_MODE_DISABLED")

    def test_unknown_project_maps_to_404(self):
        client = _make_client()
        with patch(
            "nexus.dev.service.create_dev_session",
            side_effect=DevServiceError("PROJECT_NOT_FOUND", "nope", http_status=404),
        ):
            response = client.post("/api/dev/sessions", json={"project_id": "nope"})
        self.assertEqual(response.status_code, 404)

    def test_missing_project_id_is_a_422_validation_error(self):
        client = _make_client()
        response = client.post("/api/dev/sessions", json={})
        self.assertEqual(response.status_code, 422)

    def test_caller_cannot_supply_a_filesystem_path(self):
        """The request schema has no path-like field at all -- a client
        cannot steer the worktree location even if it tries to smuggle one
        through an unexpected field (FastAPI/Pydantic ignores it)."""
        client = _make_client()
        fake_session = {"session_id": "DEV-1", "project_id": "demo", "status": "ACTIVE"}
        with patch("nexus.dev.service.create_dev_session", return_value=fake_session) as mock_create:
            client.post(
                "/api/dev/sessions",
                json={"project_id": "demo", "worktree_path": r"C:\attacker\controlled"},
            )
        mock_create.assert_called_once_with("demo", requested_model=None)


class ListAndGetDevSessionEndpointTest(unittest.TestCase):
    def test_list_sessions(self):
        client = _make_client()
        with patch("nexus.dev.service.list_sessions", return_value=[{"session_id": "DEV-1"}]) as mock_list:
            response = client.get("/api/dev/sessions?project_id=demo")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"sessions": [{"session_id": "DEV-1"}]})
        mock_list.assert_called_once_with("demo")

    def test_get_session_not_found_maps_to_404(self):
        client = _make_client()
        with patch(
            "nexus.dev.service.get_session_detail",
            side_effect=DevServiceError("SESSION_NOT_FOUND", "nope", http_status=404),
        ):
            response = client.get("/api/dev/sessions/DEV-NOPE")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "SESSION_NOT_FOUND")

    def test_get_session_returns_service_payload(self):
        client = _make_client()
        fake = {"session_id": "DEV-1", "status": "ACTIVE"}
        with patch("nexus.dev.service.get_session_detail", return_value=fake):
            response = client.get("/api/dev/sessions/DEV-1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), fake)


class ExecutionStatusEndpointTest(unittest.TestCase):
    def test_reports_disabled_by_default(self):
        client = _make_client()
        with patch("nexus.dev.feature_gate.execution_enabled", return_value=False):
            response = client.get("/api/dev/execution-status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"execution_enabled": False})

    def test_reports_enabled_when_the_gate_is_on(self):
        client = _make_client()
        with patch("nexus.dev.feature_gate.execution_enabled", return_value=True):
            response = client.get("/api/dev/execution-status")
        self.assertEqual(response.json(), {"execution_enabled": True})


class ChangedFilesEndpointTest(unittest.TestCase):
    def test_returns_the_executor_s_changed_file_summary(self):
        client = _make_client()
        files = [{"path": "src/calc.py", "status": "modified"}, {"path": "tests/test_calc.py", "status": "added"}]
        with patch("nexus.dev.executor.changed_files", return_value=files) as mock_changed:
            response = client.get("/api/dev/sessions/DEV-1/changed-files")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"files": files})
        mock_changed.assert_called_once_with("DEV-1")

    def test_unknown_session_maps_to_404(self):
        client = _make_client()
        with patch(
            "nexus.dev.executor.changed_files",
            side_effect=DevServiceError("SESSION_NOT_FOUND", "nope", http_status=404),
        ):
            response = client.get("/api/dev/sessions/DEV-NOPE/changed-files")
        self.assertEqual(response.status_code, 404)

    def test_revoked_authorization_maps_to_403(self):
        client = _make_client()
        with patch(
            "nexus.dev.executor.changed_files",
            side_effect=DevServiceError("DEV_MODE_DISABLED", "revoked", http_status=403),
        ):
            response = client.get("/api/dev/sessions/DEV-1/changed-files")
        self.assertEqual(response.status_code, 403)


class DiffReviewEndpointTest(unittest.TestCase):
    def test_returns_read_only_diff_payload(self):
        client = _make_client()
        fake = {"base_sha": "a" * 40, "head_sha": "a" * 40, "files": [], "review_id": "b" * 64}
        with patch("nexus.dev.executor.review_diff", return_value=fake) as review:
            response = client.get("/api/dev/sessions/DEV-1/diff")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), fake)
        review.assert_called_once_with("DEV-1")

    def test_diff_authorization_errors_are_preserved(self):
        client = _make_client()
        with patch("nexus.dev.executor.review_diff", side_effect=DevServiceError("DIFF_UNAVAILABLE", "unavailable", http_status=409)):
            response = client.get("/api/dev/sessions/DEV-1/diff")
        self.assertEqual(response.status_code, 409)

    def test_available_even_when_the_execution_gate_is_disabled(self):
        """Read-only inspection must never depend on the write-execution
        feature gate -- the route itself never checks it."""
        client = _make_client()
        with patch("nexus.dev.executor.changed_files", return_value=[]), patch(
            "nexus.dev.feature_gate.execution_enabled", return_value=False
        ) as gate_check:
            response = client.get("/api/dev/sessions/DEV-1/changed-files")
        self.assertEqual(response.status_code, 200)
        gate_check.assert_not_called()


class DevTurnEndpointTest(unittest.TestCase):
    def test_empty_prompt_is_rejected_before_reaching_the_executor(self):
        client = _make_client()
        with patch("nexus.dev.executor.run_dev_turn") as mock_run:
            response = client.post("/api/dev/sessions/DEV-1/turns", json={"prompt": "   "})
        self.assertEqual(response.status_code, 422)
        mock_run.assert_not_called()

    def test_create_turn_returns_immediately_with_pending_status(self):
        client = _make_client()
        fake_turn = {
            "turn_id": "DEVTURN-1", "dev_session_id": "DEV-1", "prompt": "do x",
            "requested_model": None, "status": "PENDING", "response_text": None,
            "error_code": None, "error_message": None,
            "created_at": "2026-01-01T00:00:00+00:00", "started_at": None, "finished_at": None,
        }
        with patch("nexus.dev.executor.run_dev_turn", return_value=fake_turn) as mock_run:
            response = client.post("/api/dev/sessions/DEV-1/turns", json={"prompt": "do x"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "PENDING")
        mock_run.assert_called_once_with("DEV-1", "do x", model=None, effort=None)

    def test_execution_disabled_maps_to_403(self):
        client = _make_client()
        with patch(
            "nexus.dev.executor.run_dev_turn",
            side_effect=DevServiceError("DEV_EXECUTION_DISABLED", "disabled", http_status=403),
        ):
            response = client.post("/api/dev/sessions/DEV-1/turns", json={"prompt": "do x"})
        self.assertEqual(response.status_code, 403)

    def test_session_busy_maps_to_409_duplicate_click_protection(self):
        client = _make_client()
        with patch("nexus.dev.executor.run_dev_turn", side_effect=DevServiceError("SESSION_BUSY", "busy", http_status=409)):
            response = client.post("/api/dev/sessions/DEV-1/turns", json={"prompt": "do x"})
        self.assertEqual(response.status_code, 409)

    def test_too_many_concurrent_turns_maps_to_429(self):
        client = _make_client()
        with patch("nexus.dev.executor.run_dev_turn", side_effect=DevServiceError("TOO_MANY_CONCURRENT_DEV_TURNS", "busy", http_status=429)):
            response = client.post("/api/dev/sessions/DEV-1/turns", json={"prompt": "do x"})
        self.assertEqual(response.status_code, 429)


class HumanApprovedIntegrationEndpointTest(unittest.TestCase):
    def test_integration_gate_status_is_reported_separately(self):
        client = _make_client()
        with patch("nexus.dev.integration_gate.integration_enabled", return_value=False):
            response = client.get("/api/dev/integration-status")
        self.assertEqual(response.json(), {"integration_enabled": False})

    def test_create_review_and_preflight_use_session_review_ids(self):
        client = _make_client()
        review = {"review_id": "a" * 64, "status": "REVIEWED"}
        preflight = {"review_id": "a" * 64, "ready": True, "issues": []}
        with patch("nexus.dev.integration.create_review", return_value=review) as create, patch(
            "nexus.dev.integration.integration_preflight", return_value=preflight
        ) as check:
            created = client.post("/api/dev/sessions/DEV-1/reviews", json={})
            checked = client.get(f"/api/dev/sessions/DEV-1/preflight?review_id={'a' * 64}")
        self.assertEqual(created.json(), review)
        self.assertEqual(checked.json(), preflight)
        create.assert_called_once_with("DEV-1")
        check.assert_called_once_with("DEV-1", "a" * 64)

    def test_apply_requires_strict_confirmation_and_delegates_review_id(self):
        client = _make_client()
        review_id = "b" * 64
        fake = {"status": "APPLIED", "commit_sha": "c" * 40}
        with patch("nexus.dev.integration.apply_review", return_value=fake) as apply:
            bad = client.post("/api/dev/sessions/DEV-1/apply", json={"review_id": review_id, "confirmed": "true"})
            good = client.post(f"/api/dev/sessions/DEV-1/apply", json={"review_id": review_id, "confirmed": True})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(good.json(), fake)
        apply.assert_called_once_with("DEV-1", review_id, confirmed=True)

    def test_reject_and_history_are_separate_operations(self):
        client = _make_client()
        review_id = "d" * 64
        with patch("nexus.dev.integration.reject_review", return_value={"status": "REJECTED"}) as reject, patch(
            "nexus.dev.integration.list_history", return_value=[{"review_id": review_id, "status": "REJECTED"}]
        ) as history:
            rejected = client.post("/api/dev/sessions/DEV-1/reject", json={"review_id": review_id})
            rows = client.get("/api/dev/sessions/DEV-1/integrations")
        self.assertEqual(rejected.json()["status"], "REJECTED")
        self.assertEqual(rows.json()["integrations"][0]["status"], "REJECTED")
        reject.assert_called_once_with("DEV-1", review_id)
        history.assert_called_once_with("DEV-1")

    def test_list_turns_delegates_after_authorizing_the_session(self):
        client = _make_client()
        with patch("nexus.dev.service.get_session_detail", return_value={"session_id": "DEV-1"}), patch(
            "nexus.dev.executor.list_turns", return_value=[{"turn_id": "T1"}]
        ) as mock_list:
            response = client.get("/api/dev/sessions/DEV-1/turns")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"turns": [{"turn_id": "T1"}]})
        mock_list.assert_called_once_with("DEV-1")

    def test_list_turns_for_unknown_session_is_404_before_listing(self):
        client = _make_client()
        with patch(
            "nexus.dev.service.get_session_detail",
            side_effect=DevServiceError("SESSION_NOT_FOUND", "nope", http_status=404),
        ), patch("nexus.dev.executor.list_turns") as mock_list:
            response = client.get("/api/dev/sessions/DEV-NOPE/turns")
        self.assertEqual(response.status_code, 404)
        mock_list.assert_not_called()

    def test_get_turn_not_found_maps_to_404(self):
        client = _make_client()
        with patch(
            "nexus.dev.executor.get_turn_detail",
            side_effect=DevServiceError("TURN_NOT_FOUND", "nope", http_status=404),
        ):
            response = client.get("/api/dev/sessions/DEV-1/turns/DEVTURN-NOPE")
        self.assertEqual(response.status_code, 404)


class InterruptDevTurnEndpointTest(unittest.TestCase):
    def test_interrupt_delegates_to_executor(self):
        client = _make_client()
        with patch(
            "nexus.dev.executor.interrupt_dev_turn", return_value={"status": "INTERRUPT_REQUESTED", "turn_id": "T1"}
        ) as mock_interrupt:
            response = client.post("/api/dev/sessions/DEV-1/interrupt?turn_id=T1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "INTERRUPT_REQUESTED")
        mock_interrupt.assert_called_once_with("DEV-1", "T1")

    def test_interrupt_with_nothing_active_is_200_not_an_error(self):
        client = _make_client()
        with patch(
            "nexus.dev.executor.interrupt_dev_turn", return_value={"status": "NOT_ACTIVE", "turn": None}
        ):
            response = client.post("/api/dev/sessions/DEV-1/interrupt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "NOT_ACTIVE")


class DevEventsEndpointTest(unittest.TestCase):
    def test_events_for_unknown_session_is_404_before_streaming(self):
        client = _make_client()
        with patch(
            "nexus.dev.service.get_session_detail",
            side_effect=DevServiceError("SESSION_NOT_FOUND", "nope", http_status=404),
        ):
            response = client.get("/api/dev/sessions/DEV-NOPE/events")
        self.assertEqual(response.status_code, 404)

    def test_events_for_a_finished_turn_emits_one_snapshot_and_closes(self):
        client = _make_client()
        snapshot = {"turn_id": "DEVTURN-1", "status": "COMPLETED", "response_text": "hi"}
        with patch("nexus.dev.service.get_session_detail", return_value={"session_id": "DEV-1"}), patch(
            "nexus.dev.executor.latest_turn_id_for_session", return_value="DEVTURN-1"
        ), patch(
            "nexus.dev.executor.require_dev_session_turn", return_value={"id": "DEVTURN-1", "dev_session_id": "DEV-1"}
        ), patch("nexus.dev.executor.subscribe_events", return_value=None), patch(
            "nexus.dev.executor.turn_snapshot", return_value=snapshot
        ):
            response = client.get("/api/dev/sessions/DEV-1/events")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        body = response.text
        self.assertIn("nexus/turnSnapshot", body)
        self.assertIn("DEVTURN-1", body)

    def test_events_with_no_turns_yet_emits_no_turn_event(self):
        client = _make_client()
        with patch("nexus.dev.service.get_session_detail", return_value={"session_id": "DEV-1"}), patch(
            "nexus.dev.executor.latest_turn_id_for_session", return_value=None
        ):
            response = client.get("/api/dev/sessions/DEV-1/events")
        self.assertEqual(response.status_code, 200)
        self.assertIn("nexus/noTurn", response.text)

    def test_foreign_turn_is_rejected_before_subscribing_or_snapshot(self):
        client = _make_client()
        with patch("nexus.dev.service.get_session_detail", return_value={"session_id": "DEV-1"}), patch(
            "nexus.dev.executor.require_dev_session_turn",
            side_effect=DevServiceError("TURN_NOT_FOUND", "Turn not found in this development session.", http_status=404),
        ), patch("nexus.dev.executor.subscribe_events") as subscribe, patch(
            "nexus.dev.executor.turn_snapshot"
        ) as snapshot:
            response = client.get("/api/dev/sessions/DEV-1/events?turn_id=DEVTURN-FOREIGN")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "TURN_NOT_FOUND")
        subscribe.assert_not_called()
        snapshot.assert_not_called()

    def test_last_event_id_replays_only_newer_events_and_emits_turn_ended(self):
        import queue
        import threading

        subscriber = queue.Queue()
        done = threading.Event()
        done.set()
        events = [(2, {"method": "item/completed", "params": {"item": {"id": "I2"}}})]
        snapshot = {"turn_id": "DEVTURN-1", "status": "COMPLETED", "response_text": "ok"}
        with patch("nexus.dev.service.get_session_detail", return_value={"session_id": "DEV-1"}), patch(
            "nexus.dev.executor.require_dev_session_turn", return_value={"id": "DEVTURN-1", "dev_session_id": "DEV-1"}
        ), patch("nexus.dev.executor.active_event_latest_seq", return_value=2), patch(
            "nexus.dev.executor.subscribe_events",
            return_value=(subscriber, events, done, False, 1, 2),
        ), patch("nexus.dev.executor.turn_snapshot", return_value=snapshot):
            response = _make_client().get(
                "/api/dev/sessions/DEV-1/events?turn_id=DEVTURN-1",
                headers={"Last-Event-ID": "DEV-1:DEVTURN-1:1"},
            )
        body = response.text
        self.assertIn("id: DEV-1:DEVTURN-1:2", body)
        self.assertIn('"id": "I2"', body)
        self.assertIn("nexus/turnEnded", body)

    def test_event_cursor_cannot_cross_turn_or_session(self):
        with patch("nexus.dev.service.get_session_detail", return_value={"session_id": "DEV-1"}), patch(
            "nexus.dev.executor.require_dev_session_turn", return_value={"id": "DEVTURN-2", "dev_session_id": "DEV-1"}
        ):
            response = _make_client().get(
                "/api/dev/sessions/DEV-1/events?turn_id=DEVTURN-2",
                headers={"Last-Event-ID": "DEV-1:DEVTURN-1:4"},
            )
        self.assertEqual(response.status_code, 400)


class LocalDevHttpProtectionTest(unittest.TestCase):
    """The same localhost/same-origin middleware guarding /api/chat/ also
    guards /api/dev/ -- it is extended, not re-implemented, in
    nexus/web/app.py's protect_chat_localhost."""

    def test_local_same_origin_requests_are_allowed(self):
        client = _make_client()
        with patch("nexus.dev.service.list_sessions", return_value=[]):
            response = client.get(
                "/api/dev/sessions", headers={"Origin": "http://127.0.0.1:8000"}
            )
        self.assertEqual(response.status_code, 200)

    def test_different_and_malformed_hosts_are_rejected(self):
        client = _make_client()
        for host in ("evil.example", "localhost.evil.example", "127.0.0.1:0"):
            with self.subTest(host=host), patch("nexus.dev.service.list_sessions") as list_sessions:
                response = client.get("/api/dev/sessions", headers={"Host": host})
                self.assertEqual(response.status_code, 403)
                list_sessions.assert_not_called()

    def test_foreign_origin_on_turn_creation_is_rejected(self):
        client = _make_client()
        with patch("nexus.dev.executor.run_dev_turn") as mock_run:
            response = client.post(
                "/api/dev/sessions/DEV-1/turns",
                json={"prompt": "do x"},
                headers={"Origin": "https://evil.example"},
            )
        self.assertEqual(response.status_code, 403)
        mock_run.assert_not_called()

    def test_dev_sse_and_interrupt_are_guarded_too(self):
        client = _make_client()
        for method, path in (("GET", "/api/dev/sessions/DEV-1/events"),
                              ("POST", "/api/dev/sessions/DEV-1/interrupt"),
                              ("GET", "/api/dev/sessions/DEV-1/changed-files")):
            with self.subTest(path=path):
                response = client.request(method, path, headers={"Host": "attacker.example"})
                self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
