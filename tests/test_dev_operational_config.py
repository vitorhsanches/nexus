from __future__ import annotations

import json
import sys

import pytest

from nexus.dev import models, test_profiles


def test_dev_models_are_live_approved_coding_routes_and_prefer_free(monkeypatch):
    monkeypatch.setattr(
        "nexus.chat.service.list_models",
        lambda: {
            "models": [
                {"id": "cc/claude-sonnet-5-low", "name": "Claude", "selectable": True},
                {
                    "id": "openrouter/cohere/north-mini-code:free",
                    "name": "North",
                    "selectable": True,
                },
                {"id": "unapproved/model", "name": "Unknown", "selectable": True},
            ],
            "default_model": "cc/claude-sonnet-5-low",
            "catalog_error": None,
        },
    )
    payload = models.list_dev_models()
    assert [item["id"] for item in payload["models"]] == [
        "openrouter/cohere/north-mini-code:free",
        "cc/claude-sonnet-5-low",
    ]
    assert payload["default_model"] == "openrouter/cohere/north-mini-code:free"
    assert payload["models"][0]["cost_status"] == "free"


def test_local_profiles_are_project_bound_and_expand_python(tmp_path, monkeypatch):
    local_file = tmp_path / "profiles.json"
    local_file.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "id": "unit",
                        "name": "Unit tests",
                        "project_ids": ["demo"],
                        "argv": ["{python}", "-m", "unittest", "discover", "-v"],
                        "timeout_seconds": 45,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(test_profiles, "LOCAL_PROFILES_FILE", local_file)

    assert test_profiles.list_test_profiles("other") == []
    assert test_profiles.list_test_profiles("demo")[0]["id"] == "unit"
    argv, timeout = test_profiles.resolve_test_profile("unit", "demo")
    assert argv[0] == sys.executable
    assert timeout == 45
    assert test_profiles.resolve_test_profile("unit", "other") is None


def test_local_profiles_reject_shell_launchers(tmp_path, monkeypatch):
    local_file = tmp_path / "profiles.json"
    local_file.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "id": "unsafe",
                        "name": "Unsafe",
                        "project_ids": ["demo"],
                        "argv": ["powershell", "-Command", "do-anything"],
                        "timeout_seconds": 30,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(test_profiles, "LOCAL_PROFILES_FILE", local_file)

    with pytest.raises(
        test_profiles.VerificationProfileConfigurationError, match="command shell"
    ):
        test_profiles.list_test_profiles("demo")
