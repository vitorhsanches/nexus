"""Server-owned verification profiles for autonomous Development missions.

Profiles are code-owned argv arrays. Mission text and model output can select
a profile by name, but can never provide or extend the command itself.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VerificationProfile:
    argv: tuple[str, ...]
    timeout_seconds: int


TRUSTED_TEST_PROFILES: dict[str, VerificationProfile] = {
    "nexus-backend": VerificationProfile(("{python}", "-m", "pytest", "-q"), 1800),
    "nexus-f36b": VerificationProfile(
        ("{python}", "-m", "pytest", "-q", "tests/test_autonomous_mission_loop.py", "tests/test_dev_mission_routes.py"),
        300,
    ),
}


def resolve_test_profile(name: str) -> tuple[list[str], int] | None:
    profile = TRUSTED_TEST_PROFILES.get(name)
    if profile is None:
        return None
    argv = [sys.executable if item == "{python}" else item for item in profile.argv]
    return argv, profile.timeout_seconds
