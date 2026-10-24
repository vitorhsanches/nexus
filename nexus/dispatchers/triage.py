"""Deterministic, side-effect-free mission-scope triage for Nexus Phase 4.1.

This module answers exactly one narrow question about a raw mission request
string, before any planning happens: is the mission concrete/delimited
enough to be decomposed into a Worker DAG right now (BOUNDED), or does it
first require discovering evidence about the repository before a sensible
decomposition is even possible (BROAD)?

This is deliberately NOT the same question nexus/manager/planner.py's
classifier answers. That classifier sorts a Mission into an *intent*
(EXECUTION / ANALYSIS / QUESTION / PLANNING) so Stack B can pick a fixed,
hardcoded task template. This module sorts a mission into a *scope*
(BOUNDED / BROAD) describing how well the request itself delimits its own
target -- a mission can be EXECUTION intent and still be BROAD ("fix the
main problems you find"), or ANALYSIS intent and still be BOUNDED ("review
nexus/routing/service.py for the mechanical LOW fallback"). Reusing the
intent classifier's keyword lists would answer the wrong question, so this
module defines its own signal vocabulary instead; it only borrows the
proven text-normalization technique (NFKD accent stripping + whitespace
collapse) that both nexus/manager/planner.py and nexus/router/intent.py
already use for the same reason: deterministic EN/PT matching.

Design constraints (Phase 4.1 -- see Claude outputs/NEXUS_PHASE4_ARCHITECTURE
for the approved roadmap this implements):

* Pure function: no filesystem, no database, no network, no LLM/subprocess
  calls, no logging side effects. Same input always yields the same output.
* Conservative: this phase does not yet act differently on BROAD missions
  (Phase 4.2 will), so an ambiguous or empty mission must default to
  BOUNDED -- a false "BOUNDED" costs nothing today, while a false "BROAD"
  would be a warning sign for work that doesn't actually exist yet.
* BROAD requires two independent signals to agree: an undelimited-scope
  anchor (the mission talks about "this project" / "this repository" /
  "the architecture" as a whole, not a named part of it) AND an
  open-ended investigative target (the mission asks to find/identify
  "the main problems", "opportunities", "priorities" -- plural, unnamed
  in advance). Neither signal alone is sufficient; this is what keeps two
  incidental keyword hits from tipping an otherwise ordinary mission into
  BROAD.
* A concrete target (an explicit file path or a well-known source
  extension) always overrides to BOUNDED, even if broad-sounding language
  is also present, because a mission that names its own target has
  already done the delimiting work a BROAD classification exists to flag.
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum
from typing import NamedTuple


class MissionScope(str, Enum):
    """The two mission-scope classes Phase 4.1 distinguishes.

    BOUNDED: the mission names or clearly implies a specific target
    (a file, function, component, subsystem, or well-defined operation)
    tight enough to plan directly, without first discovering evidence
    about the repository.

    BROAD: the mission asks Nexus to survey an undelimited scope (a
    whole project/repository/architecture) and itself decide what the
    interesting problems/opportunities/priorities even are, which a
    single-shot plan cannot responsibly commit to without evidence.
    """

    BOUNDED = "BOUNDED"
    BROAD = "BROAD"


# ---------------------------------------------------------------------------
# Reason codes -- deterministic, structural labels only. Never free text and
# never anything derived from a model (there is no model in this module);
# these exist purely so a checkpoint/test can assert *why* a classification
# was reached without persisting any prose.
# ---------------------------------------------------------------------------
REASON_EMPTY_INPUT = "EMPTY_INPUT"
REASON_CONCRETE_TARGET_OVERRIDE = "CONCRETE_TARGET_OVERRIDE"
REASON_BROAD_SCOPE_ANCHOR_AND_OPEN_TARGET = "BROAD_SCOPE_ANCHOR_AND_OPEN_TARGET"
REASON_NO_BROAD_SIGNAL = "NO_BROAD_SIGNAL"


def _normalize(text: str) -> str:
    """Lowercase, strip accents, and collapse whitespace.

    Same technique already used by nexus/manager/planner.py and
    nexus/router/intent.py, reused here as a shared, proven approach to
    deterministic EN/PT text matching -- not a reuse of their keyword
    vocabularies, which answer a different question (see module docstring).
    """

    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _contains(normalized: str, phrase: str) -> bool:
    return re.search(
        r"(?<![\w])" + re.escape(phrase) + r"(?![\w])", normalized
    ) is not None


# Undelimited-scope anchors: the mission refers to "the whole thing" rather
# than a named part of it. EN + PT, accent-stripped forms (normalization
# strips accents before matching, so entries here are already accent-free).
_BROAD_SCOPE_ANCHORS = (
    "this project",
    "este projeto",
    "o projeto",
    "this repository",
    "this repo",
    "neste repositorio",
    "no repositorio",
    "nesta base de codigo",
    "this codebase",
    "the codebase",
    "a base de codigo",
    "arquitetura do nexus",
    "arquitetura geral",
    "the architecture of",
    "a arquitetura do",
    "o sistema como um todo",
    "toda a base de codigo",
    "the whole system",
    "o sistema inteiro",
)

# Open-ended investigative targets: the mission asks to discover unnamed,
# plural problems/opportunities/priorities rather than act on a stated one.
_OPEN_ENDED_TARGETS = (
    "principais problemas",
    "main problems",
    "most important issues",
    "important issues",
    "principais riscos",
    "main risks",
    "oportunidades de melhoria",
    "opportunities for improvement",
    "melhorias prioritarias",
    "priority improvements",
    "propose priorities",
    "proponha as prioridades",
    "o que precisa melhorar",
    "what needs improvement",
    "reliability issues",
    "architectural issues",
    "principais riscos independentes",
    "riscos independentes",
)

# A concrete target always overrides broad-sounding language back to
# BOUNDED: a path-like token (has a slash) or a filename with a common
# source/text extension, or a backtick-quoted identifier.
_PATH_LIKE = re.compile(r"[\w\-]+(?:[/\\][\w\-.]+)+")
_FILE_EXTENSION = re.compile(
    r"\b[\w\-]+\.(?:py|js|jsx|ts|tsx|go|rb|java|c|cc|cpp|h|hpp|"
    r"md|rst|json|ya?ml|toml|ini|cfg|conf|txt|sql|ps1|sh|bat)\b"
)
_BACKTICK_IDENTIFIER = re.compile(r"`[^`\s][^`]*`")


def _has_concrete_target(raw_text: str) -> bool:
    """Detect an explicit file/path/identifier target in the ORIGINAL text.

    Deliberately runs on the un-normalized text: normalization lowercases
    and strips characters in a way that is irrelevant to path/extension
    detection and would only risk corrupting path separators or casing
    that matters for a real filename.
    """

    return bool(
        _PATH_LIKE.search(raw_text)
        or _FILE_EXTENSION.search(raw_text)
        or _BACKTICK_IDENTIFIER.search(raw_text)
    )


def _has_broad_scope_anchor(normalized: str) -> bool:
    return any(_contains(normalized, phrase) for phrase in _BROAD_SCOPE_ANCHORS)


def _has_open_ended_target(normalized: str) -> bool:
    return any(_contains(normalized, phrase) for phrase in _OPEN_ENDED_TARGETS)


class TriageResult(NamedTuple):
    """The outcome of classify_mission_scope().

    A plain (scope, reason_codes) 2-tuple would work too (and still
    unpacks the same way), but naming the fields makes call sites and
    checkpoint-payload construction self-documenting -- this is the one
    deliberate deviation from the conceptual
    ``classify_mission_scope(task_text: str) -> MissionScope`` signature
    sketched in the Phase 4.1 request. A bare ``MissionScope`` return
    would have forced either a second, redundant pass over the text to
    recover *why* a classification was reached (needed for the
    MISSION_TRIAGE checkpoint's reason_codes and for the test suite's
    proof-of-determinism assertions), or smuggling the reason onto the
    enum member itself, which would break its use as a plain, stable
    string value elsewhere (e.g. JSON serialization into the checkpoint
    payload). Returning both together in one pure call avoids both.
    """

    scope: MissionScope
    reason_codes: tuple[str, ...]


def classify_mission_scope(task_text: str) -> TriageResult:
    """Classify a raw mission request as BOUNDED or BROAD.

    Pure and deterministic: no filesystem, database, network, or model
    access of any kind. Safe to call on untrusted, empty, or whitespace
    input. Returns the classification plus an ordered tuple of
    deterministic, structural reason codes (never free text, never model
    reasoning -- there is no model here) explaining which rule fired.

    Ambiguous or empty input conservatively classifies BOUNDED: this
    phase does not yet change execution behavior based on the result, so
    a false BROAD would be more misleading than a false BOUNDED.
    """

    if not isinstance(task_text, str) or not task_text.strip():
        return TriageResult(MissionScope.BOUNDED, (REASON_EMPTY_INPUT,))

    if _has_concrete_target(task_text):
        return TriageResult(
            MissionScope.BOUNDED, (REASON_CONCRETE_TARGET_OVERRIDE,)
        )

    normalized = _normalize(task_text)

    if _has_broad_scope_anchor(normalized) and _has_open_ended_target(normalized):
        return TriageResult(
            MissionScope.BROAD, (REASON_BROAD_SCOPE_ANCHOR_AND_OPEN_TARGET,)
        )

    return TriageResult(MissionScope.BOUNDED, (REASON_NO_BROAD_SIGNAL,))
