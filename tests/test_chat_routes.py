"""HTTP contract tests for the /api/chat/* routes in nexus/web/routes.py.

Mirrors tests/test_web_go.py's TestClient pattern. The service layer
(nexus.chat.service) is mocked entirely here -- business logic (idempotency,
concurrency bounds, interrupt, SSE fan-out) is already covered by
tests/test_chat_service.py; this file only proves the HTTP envelope: status
codes, request/response shapes, and error mapping.
"""

import unittest
from unittest.mock import patch

from nexus.chat.service import ChatError


def _make_client():
    from starlette.testclient import TestClient
    from nexus.web.app import create_app

    return TestClient(create_app(), base_url="http://127.0.0.1:8000", raise_server_exceptions=True)


class CreateSessionEndpointTest(unittest.TestCase):
    def test_create_session_returns_the_service_payload(self):
        client = _make_client()
        fake_session = {
            "session_id": "CHAT-AAAA1111",
            "project_id": "demo",
            "workspace_path": r"C:\fake\demo",
            "codex_thread_id": None,
            "model": None,
            "provider": None,
            "status": "CREATED",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        with patch("nexus.chat.service.create_session", return_value=fake_session) as mock_create:
            response = client.post("/api/chat/sessions", json={"project_id": "demo"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), fake_session)
        mock_create.assert_called_once_with("demo")

    def test_create_session_forwards_exact_model_effort_and_cost_confirmation(self):
        client = _make_client()
        fake_session = {
            "session_id": "CHAT-NEW", "project_id": "demo", "requested_model": "cx/gpt-5.6-sol",
            "requested_effort": "low", "status": "CREATED",
        }
        with patch("nexus.chat.service.create_session", return_value=fake_session) as mock_create:
            response = client.post("/api/chat/sessions", json={
                "project_id": "demo", "model": "cx/gpt-5.6-sol", "effort": "low", "allow_unknown_cost": True,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), fake_session)
        mock_create.assert_called_once_with("demo", "cx/gpt-5.6-sol", "low", True)

    def test_unknown_project_maps_to_404(self):
        client = _make_client()
        with patch(
            "nexus.chat.service.create_session",
            side_effect=ChatError("PROJECT_NOT_FOUND", "Unknown project: 'nope'", http_status=404),
        ):
            response = client.post("/api/chat/sessions", json={"project_id": "nope"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "PROJECT_NOT_FOUND")

    def test_disabled_project_maps_to_400(self):
        client = _make_client()
        with patch(
            "nexus.chat.service.create_session",
            side_effect=ChatError("PROJECT_DISABLED", "disabled", http_status=400),
        ):
            response = client.post("/api/chat/sessions", json={"project_id": "demo"})
        self.assertEqual(response.status_code, 400)

    def test_missing_project_id_is_a_422_validation_error(self):
        client = _make_client()
        response = client.post("/api/chat/sessions", json={})
        self.assertEqual(response.status_code, 422)


class ListAndGetSessionEndpointTest(unittest.TestCase):
    def test_list_sessions(self):
        client = _make_client()
        with patch("nexus.chat.service.list_sessions", return_value=[{"session_id": "CHAT-1"}]) as mock_list:
            response = client.get("/api/chat/sessions?project_id=demo")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"sessions": [{"session_id": "CHAT-1"}]})
        mock_list.assert_called_once_with("demo")

    def test_chat_model_endpoint_returns_restricted_service_catalog(self):
        client = _make_client()
        expected = {"models": [{"id": "cx/gpt-5.6-sol", "availability": "unverified"}], "default_model": "cx/gpt-5.6-sol"}
        with patch("nexus.chat.service.list_models", return_value=expected):
            response = client.get("/api/chat/models")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)

    def test_get_session_not_found_maps_to_404(self):
        client = _make_client()
        with patch(
            "nexus.chat.service.get_session_detail",
            side_effect=ChatError("SESSION_NOT_FOUND", "nope", http_status=404),
        ):
            response = client.get("/api/chat/sessions/CHAT-NOPE")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "SESSION_NOT_FOUND")


class HistoryEndpointTest(unittest.TestCase):
    def test_history_passes_opaque_cursor_and_bounded_page_to_service(self):
        client = _make_client()
        expected = {"session_id": "CHAT-1", "items": [], "next_cursor": "next", "source": "codex_thread"}
        with patch("nexus.chat.service.get_history", return_value=expected) as history:
            response = client.get("/api/chat/sessions/CHAT-1/history?turn_id=CODEX-TURN&cursor=opaque%2F%2B&limit=17")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        history.assert_called_once_with("CHAT-1", turn_id="CODEX-TURN", cursor="opaque/+", limit=17)

    def test_history_rejects_unbounded_limit(self):
        response = _make_client().get("/api/chat/sessions/CHAT-1/history?limit=101")
        self.assertEqual(response.status_code, 422)

    def test_history_maps_session_scoped_turn_miss_to_404(self):
        with patch(
            "nexus.chat.service.get_history",
            side_effect=ChatError("TURN_NOT_FOUND", "turn not found", http_status=404),
        ):
            response = _make_client().get("/api/chat/sessions/CHAT-1/history?turn_id=foreign")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "TURN_NOT_FOUND")


class CreateTurnEndpointTest(unittest.TestCase):
    def test_empty_text_is_rejected_before_reaching_the_service(self):
        client = _make_client()
        with patch("nexus.chat.service.send_turn") as mock_send:
            response = client.post(
                "/api/chat/sessions/CHAT-1/turns", json={"text": "   "}
            )
        self.assertEqual(response.status_code, 422)
        mock_send.assert_not_called()

    def test_oversized_turn_or_idempotency_key_is_rejected(self):
        client = _make_client()
        with patch("nexus.chat.service.send_turn") as mock_send:
            long_text = client.post(
                "/api/chat/sessions/CHAT-1/turns", json={"text": "x" * 100_001}
            )
            long_key = client.post(
                "/api/chat/sessions/CHAT-1/turns",
                json={"text": "hello", "idempotency_key": "k" * 257},
            )
        self.assertEqual(long_text.status_code, 422)
        self.assertEqual(long_key.status_code, 422)
        mock_send.assert_not_called()

    def test_create_turn_returns_immediately_with_pending_status(self):
        client = _make_client()
        fake_turn = {
            "turn_id": "TURN-1",
            "session_id": "CHAT-1",
            "idempotency_key": "k1",
            "status": "PENDING",
            "response_text": None,
            "error_code": None,
            "error_message": None,
            "model": None,
            "provider": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "started_at": None,
            "finished_at": None,
        }
        with patch("nexus.chat.service.send_turn", return_value=fake_turn) as mock_send:
            response = client.post(
                "/api/chat/sessions/CHAT-1/turns",
                json={"text": "hello", "idempotency_key": "k1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "PENDING")
        mock_send.assert_called_once_with("CHAT-1", "hello", idempotency_key="k1")

    def test_session_busy_maps_to_409(self):
        client = _make_client()
        with patch(
            "nexus.chat.service.send_turn",
            side_effect=ChatError("SESSION_BUSY", "busy", http_status=409),
        ):
            response = client.post("/api/chat/sessions/CHAT-1/turns", json={"text": "hello"})
        self.assertEqual(response.status_code, 409)

    def test_too_many_concurrent_turns_maps_to_429(self):
        client = _make_client()
        with patch(
            "nexus.chat.service.send_turn",
            side_effect=ChatError("TOO_MANY_CONCURRENT_TURNS", "busy", http_status=429),
        ):
            response = client.post("/api/chat/sessions/CHAT-1/turns", json={"text": "hello"})
        self.assertEqual(response.status_code, 429)


class InterruptEndpointTest(unittest.TestCase):
    def test_interrupt_delegates_to_service(self):
        client = _make_client()
        with patch(
            "nexus.chat.service.interrupt_turn", return_value={"status": "INTERRUPT_REQUESTED"}
        ) as mock_interrupt:
            response = client.post("/api/chat/sessions/CHAT-1/interrupt?turn_id=TURN-1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "INTERRUPT_REQUESTED")
        mock_interrupt.assert_called_once_with("CHAT-1", "TURN-1")

    def test_interrupt_with_nothing_active_is_200_not_an_error(self):
        client = _make_client()
        with patch(
            "nexus.chat.service.interrupt_turn", return_value={"status": "NOT_ACTIVE", "turn": None}
        ):
            response = client.post("/api/chat/sessions/CHAT-1/interrupt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "NOT_ACTIVE")


class EventsEndpointTest(unittest.TestCase):
    def test_events_for_unknown_session_is_404_before_streaming(self):
        client = _make_client()
        with patch("nexus.registry.chat_sessions.get_session", return_value=None):
            response = client.get("/api/chat/sessions/CHAT-NOPE/events")
        self.assertEqual(response.status_code, 404)

    def test_events_for_a_finished_turn_emits_one_snapshot_and_closes(self):
        client = _make_client()
        fake_row = {"id": "CHAT-1"}
        snapshot = {"turn_id": "TURN-1", "status": "COMPLETED", "response_text": "hi"}
        with patch("nexus.registry.chat_sessions.get_session", return_value=fake_row), patch(
            "nexus.chat.service.latest_turn_id_for_session", return_value="TURN-1"
        ), patch(
            "nexus.chat.service.require_session_turn", return_value={"id": "TURN-1", "session_id": "CHAT-1"}
        ), patch("nexus.chat.service.subscribe_events", return_value=None), patch(
            "nexus.chat.service.turn_snapshot", return_value=snapshot
        ):
            response = client.get("/api/chat/sessions/CHAT-1/events")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        body = response.text
        self.assertIn("nexus/turnSnapshot", body)
        self.assertIn("TURN-1", body)

    def test_events_with_no_turns_yet_emits_no_turn_event(self):
        client = _make_client()
        with patch("nexus.registry.chat_sessions.get_session", return_value={"id": "CHAT-1"}), patch(
            "nexus.chat.service.latest_turn_id_for_session", return_value=None
        ):
            response = client.get("/api/chat/sessions/CHAT-1/events")
        self.assertEqual(response.status_code, 200)
        self.assertIn("nexus/noTurn", response.text)

    def test_foreign_turn_is_rejected_before_subscribing_or_snapshot(self):
        client = _make_client()
        with patch("nexus.registry.chat_sessions.get_session", return_value={"id": "CHAT-1"}), patch(
            "nexus.chat.service.require_session_turn",
            side_effect=ChatError("TURN_NOT_FOUND", "Turn not found in this session.", http_status=404),
        ), patch("nexus.chat.service.subscribe_events") as subscribe, patch(
            "nexus.chat.service.turn_snapshot"
        ) as snapshot:
            response = client.get("/api/chat/sessions/CHAT-1/events?turn_id=TURN-FOREIGN")
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
        snapshot = {"turn_id": "TURN-1", "status": "COMPLETED", "response_text": "ok"}
        with patch("nexus.registry.chat_sessions.get_session", return_value={"id": "CHAT-1"}), patch(
            "nexus.chat.service.require_session_turn", return_value={"id": "TURN-1", "session_id": "CHAT-1"}
        ), patch("nexus.chat.service.active_event_latest_seq", return_value=2), patch(
            "nexus.chat.service.subscribe_events",
            return_value=(subscriber, events, done, False, 1, 2),
        ), patch("nexus.chat.service.turn_snapshot", return_value=snapshot):
            response = _make_client().get(
                "/api/chat/sessions/CHAT-1/events?turn_id=TURN-1",
                headers={"Last-Event-ID": "CHAT-1:TURN-1:1"},
            )
        body = response.text
        self.assertIn("id: CHAT-1:TURN-1:2", body)
        self.assertIn('"id": "I2"', body)
        self.assertIn("nexus/turnEnded", body)

    def test_expired_event_cursor_emits_gap_snapshot_and_terminal(self):
        snapshot = {"turn_id": "TURN-1", "status": "INTERRUPTED", "response_text": None}
        with patch("nexus.registry.chat_sessions.get_session", return_value={"id": "CHAT-1"}), patch(
            "nexus.chat.service.require_session_turn", return_value={"id": "TURN-1", "session_id": "CHAT-1"}
        ), patch("nexus.chat.service.active_event_latest_seq", return_value=None), patch(
            "nexus.chat.service.subscribe_events", return_value=None
        ), patch("nexus.chat.service.turn_snapshot", return_value=snapshot):
            response = _make_client().get(
                "/api/chat/sessions/CHAT-1/events?turn_id=TURN-1",
                headers={"Last-Event-ID": "CHAT-1:TURN-1:8"},
            )
        self.assertIn("nexus/replayGap", response.text)
        self.assertIn("nexus/turnSnapshot", response.text)
        self.assertIn("nexus/turnEnded", response.text)

    def test_event_cursor_cannot_cross_turn_or_session(self):
        with patch("nexus.registry.chat_sessions.get_session", return_value={"id": "CHAT-1"}), patch(
            "nexus.chat.service.require_session_turn", return_value={"id": "TURN-2", "session_id": "CHAT-1"}
        ):
            response = _make_client().get(
                "/api/chat/sessions/CHAT-1/events?turn_id=TURN-2",
                headers={"Last-Event-ID": "CHAT-1:TURN-1:4"},
            )
        self.assertEqual(response.status_code, 400)

    def test_foreign_turn_is_authorized_before_active_cursor_inspection(self):
        from nexus.chat.service import ChatError

        with patch("nexus.registry.chat_sessions.get_session", return_value={"id": "CHAT-1"}), patch(
            "nexus.chat.service.require_session_turn", side_effect=ChatError("TURN_NOT_FOUND", "missing", http_status=404)
        ), patch("nexus.chat.service.active_event_latest_seq") as latest:
            response = _make_client().get(
                "/api/chat/sessions/CHAT-1/events?turn_id=TURN-FOREIGN",
                headers={"Last-Event-ID": "CHAT-1:TURN-FOREIGN:9"},
            )
        self.assertEqual(response.status_code, 404)
        latest.assert_not_called()

    def test_explicit_and_header_cursors_must_match(self):
        with patch("nexus.registry.chat_sessions.get_session", return_value={"id": "CHAT-1"}), patch(
            "nexus.chat.service.latest_turn_id_for_session", return_value="TURN-1"
        ):
            response = _make_client().get(
                "/api/chat/sessions/CHAT-1/events?turn_id=TURN-1&cursor=CHAT-1%3ATURN-1%3A1",
                headers={"Last-Event-ID": "CHAT-1:TURN-1:2"},
            )
        self.assertEqual(response.status_code, 400)


class LocalChatHttpProtectionTest(unittest.TestCase):
    def test_local_same_origin_requests_are_allowed(self):
        client = _make_client()
        with patch("nexus.chat.service.list_sessions", return_value=[]):
            response = client.get(
                "/api/chat/sessions", headers={"Origin": "http://127.0.0.1:8000"}
            )
        self.assertEqual(response.status_code, 200)

    def test_loopback_host_aliases_are_allowed(self):
        from starlette.testclient import TestClient
        from nexus.web.app import create_app

        for host in ("localhost", "127.0.0.1", "[::1]"):
            with self.subTest(host=host), patch("nexus.chat.service.list_sessions", return_value=[]):
                client = TestClient(create_app(), base_url=f"http://{host}:8000")
                response = client.get("/api/chat/sessions")
                self.assertEqual(response.status_code, 200)

    def test_different_and_malformed_hosts_are_rejected(self):
        client = _make_client()
        for host in ("evil.example", "localhost.evil.example", "127.0.0.1:0", "localhost@evil.example"):
            with self.subTest(host=host), patch("nexus.chat.service.list_sessions") as list_sessions:
                response = client.get("/api/chat/sessions", headers={"Host": host})
                self.assertEqual(response.status_code, 403)
                list_sessions.assert_not_called()

    def test_foreign_null_and_wrong_port_origins_are_rejected(self):
        client = _make_client()
        for origin in ("https://evil.example", "null", "http://127.0.0.1:9000", "http://localhost:8000"):
            with self.subTest(origin=origin), patch("nexus.chat.service.create_session") as create_session:
                response = client.post(
                    "/api/chat/sessions", json={"project_id": "demo"}, headers={"Origin": origin}
                )
                self.assertEqual(response.status_code, 403)
                create_session.assert_not_called()

    def test_chat_sse_and_interrupt_are_guarded_too(self):
        client = _make_client()
        for method, path in (("GET", "/api/chat/sessions/CHAT-1/events"),
                             ("POST", "/api/chat/sessions/CHAT-1/interrupt")):
            with self.subTest(path=path):
                response = client.request(method, path, headers={"Host": "attacker.example"})
                self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
