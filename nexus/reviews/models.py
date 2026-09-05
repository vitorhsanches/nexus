"""Review domain model for Nexus Real Review Gate V1.

Reuses the exact verdict/failure-class vocabulary already proven by the
legacy Manager review contract (nexus.dispatchers.review), so this module
is a strict, reusable domain object instead of an unvalidated dict passed
around orchestration."""

from dataclasses import dataclass, field
from typing import Optional


VERDICTS = {
    "PASS",
    "RETRY",
    "ESCALATE",
    "BLOCKED",
}

FAILURE_CLASSES = {
    "TRANSIENT",
    "TOOL_FAILURE",
    "PROVIDER_FAILURE",
    "VALIDATION_FAILURE",
    "SCOPE_VIOLATION",
    "CAPABILITY_FAILURE",
    "REQUIREMENT_FAILURE",
    "UNKNOWN",
}


class InvalidReviewDecisionError(ValueError):
    """Raised when a ReviewDecision fails strict validation."""


@dataclass(slots=True, frozen=True)
class ReviewDecision:
    """A validated review verdict over a single Task Attempt.

    Malformed reviewer output must fail closed: construction always
    validates the verdict, failure_class, summary, and evidence, raising
    ``InvalidReviewDecisionError`` on any violation. Mirrors the legacy
    ``nexus.dispatchers.review._validate_review`` contract.
    """

    verdict: str
    failure_class: Optional[str] = None
    summary: str = ""
    evidence: list = field(default_factory=list)

    def __post_init__(self):
        if self.verdict not in VERDICTS:
            raise InvalidReviewDecisionError(
                f"Invalid review verdict: {self.verdict!r}"
            )

        if self.failure_class is not None and self.failure_class not in FAILURE_CLASSES:
            raise InvalidReviewDecisionError(
                f"Invalid failure_class: {self.failure_class!r}"
            )

        if not isinstance(self.summary, str) or not self.summary.strip():
            raise InvalidReviewDecisionError("Review summary is missing.")

        if not isinstance(self.evidence, list) or not self.evidence:
            raise InvalidReviewDecisionError("Review must contain evidence.")

        for item in self.evidence:
            if not isinstance(item, str) or not item.strip():
                raise InvalidReviewDecisionError(
                    "Review evidence contains an invalid item."
                )

        if self.verdict == "PASS" and self.failure_class is not None:
            raise InvalidReviewDecisionError(
                "PASS review cannot contain a failure_class."
            )


def review_decision_from_dict(data):
    """Build a validated ReviewDecision from a plain dict.

    Used at the boundary where a Reviewer implementation returns raw
    (potentially untrusted) data; fails closed via
    ``InvalidReviewDecisionError`` on any malformed field.
    """
    if not isinstance(data, dict):
        raise InvalidReviewDecisionError("Review payload must be a dict.")
    return ReviewDecision(
        verdict=data.get("verdict"),
        failure_class=data.get("failure_class"),
        summary=data.get("summary"),
        evidence=list(data.get("evidence") or []),
    )
