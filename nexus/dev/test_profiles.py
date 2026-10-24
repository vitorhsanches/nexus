"""Operator-owned verification profiles for autonomous Development missions.

Mission requests select a profile by its identifier. The executable and
arguments always come from Nexus code or the ignored machine-local profile
file; model output and mission text never become subprocess arguments.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
LOCAL_PROFILES_FILE = ROOT_DIR / "config" / "dev-test-profiles.local.json"
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_EXECUTABLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHELL_EXECUTABLES = frozenset(
    {
        "bash",
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "sh",
        "wsl",
        "wsl.exe",
    }
)


class VerificationProfileConfigurationError(ValueError):
    """Raised when the local operator profile file is unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class VerificationProfile:
    profile_id: str
    name: str
    project_ids: tuple[str, ...]
    argv: tuple[str, ...]
    timeout_seconds: int
    source: str


BUILTIN_PROFILES: tuple[VerificationProfile, ...] = (
    VerificationProfile(
        "nexus-backend",
        "Nexus backend suite",
        ("nexus",),
        ("{python}", "-m", "pytest", "-q"),
        1800,
        "builtin",
    ),
    VerificationProfile(
        "nexus-f36b",
        "Nexus autonomous mission tests",
        ("nexus",),
        (
            "{python}",
            "-m",
            "pytest",
            "-q",
            "tests/test_autonomous_mission_loop.py",
            "tests/test_dev_mission_routes.py",
        ),
        300,
        "builtin",
    ),
)


def _validate_profile(raw: object, *, index: int) -> VerificationProfile:
    if not isinstance(raw, dict):
        raise VerificationProfileConfigurationError(f"Profile #{index} must be an object.")
    profile_id = raw.get("id")
    name = raw.get("name")
    project_ids = raw.get("project_ids")
    argv = raw.get("argv")
    timeout = raw.get("timeout_seconds", 300)
    if not isinstance(profile_id, str) or not _PROFILE_ID_RE.fullmatch(profile_id):
        raise VerificationProfileConfigurationError(f"Profile #{index} has an invalid id.")
    if not isinstance(name, str) or not name.strip() or len(name) > 128:
        raise VerificationProfileConfigurationError(
            f"Profile {profile_id!r} has an invalid name."
        )
    if (
        not isinstance(project_ids, list)
        or not project_ids
        or len(project_ids) > 50
        or any(
            not isinstance(value, str) or not _PROFILE_ID_RE.fullmatch(value)
            for value in project_ids
        )
    ):
        raise VerificationProfileConfigurationError(
            f"Profile {profile_id!r} must name one or more valid project_ids."
        )
    if (
        not isinstance(argv, list)
        or not argv
        or len(argv) > 64
        or any(
            not isinstance(value, str)
            or not value
            or len(value) > 512
            or "\x00" in value
            or "\r" in value
            or "\n" in value
            for value in argv
        )
    ):
        raise VerificationProfileConfigurationError(f"Profile {profile_id!r} has invalid argv.")
    executable = argv[0]
    if executable != "{python}" and not _EXECUTABLE_RE.fullmatch(executable):
        raise VerificationProfileConfigurationError(
            f"Profile {profile_id!r} executable must be {{python}} or a bare PATH command."
        )
    if executable.casefold() in _SHELL_EXECUTABLES:
        raise VerificationProfileConfigurationError(
            f"Profile {profile_id!r} cannot launch a command shell."
        )
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 5 <= timeout <= 3600
    ):
        raise VerificationProfileConfigurationError(
            f"Profile {profile_id!r} timeout_seconds must be between 5 and 3600."
        )
    return VerificationProfile(
        profile_id,
        name.strip(),
        tuple(dict.fromkeys(project_ids)),
        tuple(argv),
        timeout,
        "local",
    )


def _load_local_profiles() -> tuple[VerificationProfile, ...]:
    if not LOCAL_PROFILES_FILE.exists():
        return ()
    try:
        with LOCAL_PROFILES_FILE.open("r", encoding="utf-8-sig") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise VerificationProfileConfigurationError(
            f"Could not read {LOCAL_PROFILES_FILE.name}: {error}"
        ) from error
    if not isinstance(payload, dict) or not isinstance(payload.get("profiles"), list):
        raise VerificationProfileConfigurationError(
            f"{LOCAL_PROFILES_FILE.name} must contain a profiles array."
        )
    profiles = tuple(
        _validate_profile(raw, index=index)
        for index, raw in enumerate(payload["profiles"], 1)
    )
    ids = [profile.profile_id for profile in profiles]
    if len(ids) != len(set(ids)):
        raise VerificationProfileConfigurationError(
            "Local verification profile ids must be unique."
        )
    return profiles


def list_test_profiles(project_id: str) -> list[dict]:
    profiles = [*BUILTIN_PROFILES, *_load_local_profiles()]
    return [
        {
            "id": profile.profile_id,
            "name": profile.name,
            "timeout_seconds": profile.timeout_seconds,
            "source": profile.source,
        }
        for profile in profiles
        if project_id in profile.project_ids
    ]


def resolve_test_profile(name: str, project_id: str) -> tuple[list[str], int] | None:
    profiles = [*BUILTIN_PROFILES, *_load_local_profiles()]
    profile = next(
        (
            item
            for item in profiles
            if item.profile_id == name and project_id in item.project_ids
        ),
        None,
    )
    if profile is None:
        return None
    argv = [sys.executable if item == "{python}" else item for item in profile.argv]
    return argv, profile.timeout_seconds
