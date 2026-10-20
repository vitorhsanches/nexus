"""Small preflight authority translation; existing run policy stays sovereign."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from nexus.orchestration.strategy import infer_pre_execution_risk


class AutonomyProfile(str, Enum):
    DEFAULT = "DEFAULT"
    LOCAL_AUTONOMOUS = "LOCAL_AUTONOMOUS"


class AuthorityResult(str, Enum):
    AUTHORIZED = "AUTHORIZED"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class AuthorityResolution:
    result: AuthorityResult
    autonomy_profile: AutonomyProfile
    requested_actions: tuple[str, ...]
    prohibited_actions: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict:
        return {
            "result": self.result.value,
            "autonomy_profile": self.autonomy_profile.value,
            "requested_actions": list(self.requested_actions),
            "prohibited_actions": list(self.prohibited_actions),
            "reason": self.reason,
        }


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in value if not unicodedata.combining(ch)).lower()


def autonomy_profile_for(text: str, explicit: str | None = None) -> AutonomyProfile:
    if explicit is not None:
        if explicit.lower() != "local":
            raise ValueError(f"Unsupported autonomy profile: {explicit!r}")
        return AutonomyProfile.LOCAL_AUTONOMOUS
    normalized = _normalize(text)
    markers = (
        "fully autonomous", "totally autonomous", "autonomous local",
        "de forma totalmente autonoma", "forma totalmente autonoma",
        "de forma autonoma", "autonomia local",
    )
    return (
        AutonomyProfile.LOCAL_AUTONOMOUS
        if any(marker in normalized for marker in markers)
        else AutonomyProfile.DEFAULT
    )


def _action_intent(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    normalized = _normalize(text)
    prohibited: list[str] = []
    requested: list[str] = []

    negative_push = (
        r"\b(?:do not|don't|no|without)\s+(?:commit\s*(?:and|or|/)\s*)?push\b",
        r"\bnao\s+(?:faca|fazer)\s+(?:commit\s*(?:e|ou|/)\s*)?push\b",
        r"\bnao\s+push\b",
    )
    negative_deploy = (
        r"\b(?:do not|don't|no|without)\s+(?:deploy|deployment)\b",
        r"\bnao\s+(?:faca\s+)?deploy\b",
    )
    for pattern in negative_push:
        if re.search(pattern, normalized):
            prohibited.append("REMOTE_PUSH")
            normalized = re.sub(pattern, "", normalized)
    for pattern in negative_deploy:
        if re.search(pattern, normalized):
            prohibited.append("EXTERNAL_DEPLOYMENT")
            normalized = re.sub(pattern, "", normalized)

    combined_restrictions = (
        "do not deploy or push", "do not deploy nor push",
        "nao faca deploy nem push", "nao faca deploy ou push",
    )
    if any(marker in _normalize(text) for marker in combined_restrictions):
        prohibited.extend(("EXTERNAL_DEPLOYMENT", "REMOTE_PUSH"))
        normalized = normalized.replace("push", "").replace("deploy", "")

    if "push" in normalized or "publish" in normalized:
        requested.append("REMOTE_PUSH")
    if "deploy" in normalized or "deployment" in normalized:
        requested.append("EXTERNAL_DEPLOYMENT")
    if any(marker in normalized for marker in ("credential", "secret", "permission")):
        requested.append("PROTECTED_SECURITY_CHANGE")
    if any(marker in normalized for marker in ("drop database", "delete database")):
        requested.append("DESTRUCTIVE_DATABASE_CHANGE")

    return tuple(dict.fromkeys(requested)), tuple(dict.fromkeys(prohibited))


def resolve_authority(
    task_text: str,
    *,
    explicit_autonomy: str | None = None,
) -> AuthorityResolution:
    profile = autonomy_profile_for(task_text, explicit_autonomy)
    requested, prohibited = _action_intent(task_text)

    if any(
        action in requested
        for action in ("PROTECTED_SECURITY_CHANGE", "DESTRUCTIVE_DATABASE_CHANGE")
    ):
        return AuthorityResolution(
            AuthorityResult.BLOCKED,
            profile,
            requested,
            prohibited,
            "Protected or destructive action is outside autonomous authority.",
        )
    if any(
        action in requested
        for action in ("REMOTE_PUSH", "EXTERNAL_DEPLOYMENT")
    ):
        return AuthorityResolution(
            AuthorityResult.NEEDS_APPROVAL,
            profile,
            requested,
            prohibited,
            "External publication or deployment requires approval.",
        )

    if prohibited:
        return AuthorityResolution(
            AuthorityResult.AUTHORIZED,
            profile,
            requested,
            prohibited,
            "Explicit external restrictions preserve local autonomous authority.",
        )

    # Reuse the existing pre-execution risk signal as a conservative backstop.
    if infer_pre_execution_risk(task_text) == "HIGH":
        return AuthorityResolution(
            AuthorityResult.NEEDS_APPROVAL,
            profile,
            requested,
            prohibited,
            "Existing risk signal requires policy-controlled execution.",
        )
    return AuthorityResolution(
        AuthorityResult.AUTHORIZED,
        profile,
        requested,
        prohibited,
        "Local read/write execution remains subject to existing run policy.",
    )
