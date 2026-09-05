"""Injectable reviewer abstraction for Nexus Real Review Gate V1.

A Reviewer takes the evidence assembled for a completed (REVIEW-state)
Attempt and returns a validated ReviewDecision. Production code paths are
never invoked automatically just by importing this module: callers must
explicitly construct and pass a Reviewer. No implementation here calls a
real provider/model -- the real Manager/Luna reviewer is out of scope for
this milestone and will be wired in a future one."""

from abc import ABC, abstractmethod

from nexus.reviews.models import ReviewDecision, review_decision_from_dict


class Reviewer(ABC):
    """Pluggable review backend invoked by the reviewed execution service."""

    @abstractmethod
    def review(self, evidence):
        """Return a ReviewDecision (or a dict coercible into one) for ``evidence``."""
        raise NotImplementedError


class AlwaysPassReviewer(Reviewer):
    """Deterministic test reviewer that always returns PASS."""

    def review(self, evidence):
        return ReviewDecision(
            verdict="PASS",
            failure_class=None,
            summary="Simulated automatic pass.",
            evidence=["simulated-pass"],
        )


class SequenceReviewer(Reviewer):
    """Deterministic test reviewer that returns a fixed verdict sequence.

    Each call to ``review`` consumes the next verdict in ``verdicts``. The
    last verdict is reused for any call beyond the sequence's length so a
    trailing BLOCKED/PASS keeps behaving deterministically instead of
    raising once exhausted."""

    _DEFAULT_SUMMARIES = {
        "PASS": "Simulated pass.",
        "RETRY": "Simulated retry: transient failure.",
        "ESCALATE": "Simulated escalation: capability tier insufficient.",
        "BLOCKED": "Simulated block: cannot safely proceed.",
    }

    _DEFAULT_FAILURE_CLASSES = {
        "RETRY": "TRANSIENT",
        "ESCALATE": "CAPABILITY_FAILURE",
        "BLOCKED": "REQUIREMENT_FAILURE",
    }

    def __init__(self, verdicts, summaries=None, evidence=None):
        if not verdicts:
            raise ValueError("SequenceReviewer requires at least one verdict.")
        self._verdicts = list(verdicts)
        self._summaries = summaries or {}
        self._evidence = evidence or ["simulated-evidence"]
        self._index = 0

    def review(self, evidence):
        index = min(self._index, len(self._verdicts) - 1)
        verdict = self._verdicts[index]
        self._index += 1

        summary = self._summaries.get(verdict) or self._DEFAULT_SUMMARIES[verdict]
        failure_class = self._DEFAULT_FAILURE_CLASSES.get(verdict)

        return ReviewDecision(
            verdict=verdict,
            failure_class=failure_class,
            summary=summary,
            evidence=list(self._evidence),
        )


def coerce_decision(result):
    """Normalize a Reviewer's return value into a validated ReviewDecision."""
    if isinstance(result, ReviewDecision):
        return result
    return review_decision_from_dict(result)
