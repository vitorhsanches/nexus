"""Review requirement policy for Nexus missions."""

from nexus.routing.models import RiskLevel


def should_review(risk_level: str | None) -> bool:
    """
    Decide if a mission execution requires reviewer validation.

    HIGH and CRITICAL risk always require review.
    LOW and MEDIUM can skip review.
    """

    if risk_level is None:
        return True

    return risk_level in {
        RiskLevel.HIGH.value,
        RiskLevel.CRITICAL.value,
    }