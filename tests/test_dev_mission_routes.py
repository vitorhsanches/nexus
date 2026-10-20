from unittest.mock import patch

from fastapi.testclient import TestClient

from nexus.web.app import app


def test_mission_start_route_contract():
    payload = {"mission_id": "M1", "status": "PENDING"}
    with patch("nexus.dev.mission_runner.start_mission", return_value=payload) as start:
        response = TestClient(app, base_url="http://127.0.0.1").post(
            "/api/dev/sessions/DEV-1/missions",
            json={"description": "do work", "model": "model-a", "allowed_models": ["model-a"], "test_profile": "nexus-f36b"},
        )
    assert response.status_code == 200 and response.json() == payload
    start.assert_called_once()


def test_mission_read_progress_tests_result_and_interrupt_routes():
    mission = {
        "mission_id": "M1", "status": "READY_FOR_REVIEW", "attempt_count": 1,
        "current_turn_id": "T1", "updated_at": "now", "error_code": None,
        "error_message": None, "tests": [{"status": "PASSED"}], "result": {"ok": True},
    }
    with patch("nexus.dev.mission_runner.list_missions", return_value=[mission]), patch(
        "nexus.dev.mission_runner.get_mission", return_value=mission
    ), patch("nexus.dev.mission_runner.interrupt_mission", return_value={"status": "NOT_ACTIVE"}):
        client = TestClient(app, base_url="http://127.0.0.1")
        assert client.get("/api/dev/sessions/DEV-1/missions").json()["missions"] == [mission]
        assert client.get("/api/dev/sessions/DEV-1/missions/M1").json() == mission
        assert client.get("/api/dev/sessions/DEV-1/missions/M1/progress").json()["status"] == "READY_FOR_REVIEW"
        assert client.get("/api/dev/sessions/DEV-1/missions/M1/tests").json()["tests"][0]["status"] == "PASSED"
        assert client.get("/api/dev/sessions/DEV-1/missions/M1/result").json()["result"] == {"ok": True}
        assert client.post("/api/dev/sessions/DEV-1/missions/M1/interrupt").json()["status"] == "NOT_ACTIVE"
