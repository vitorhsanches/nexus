"""Nexus v2.0-D.4.2 additive fleet/inventory normalization layer.

This module builds a deterministic, read-only view over OmniRoute runtime
discovery (DiscoveredModel) for fleet/inventory reporting purposes only.

CRITICAL INVARIANTS
--------------------
- This module is purely additive. It does not modify, wrap, or replace
  ModelRoute, APPROVED_CATALOG, the deterministic routing policy
  (nexus.routing.router), persistence, the Control Board, or production
  route selection behavior in any way.
- Discovery is never approval. Nothing in this module ever sets or implies
  production eligibility.
- Centralized Terra blocking is reused as-is via
  nexus.routing.catalog.is_blocked_model; this module never re-implements
  or diverges from that policy.
- All normalization here is deterministic and side-effect free: no network
  calls, no randomness, no wall-clock reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from nexus.routing.catalog import is_blocked_model
from nexus.routing.telemetry import DiscoveredModel


class Provenance(str, Enum):
    """Where a fleet route entry originated from."""

    CATALOG = "catalog"
    DISCOVERED = "discovered"
    UNRESOLVED = "unresolved"


class NormalizationStatus(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class RouteKind(str, Enum):
    """Deterministic route topology classification.

    - FIXED: a single, specific model identity.
    - POOL: a multi-model routing alias/pool (for example an auto/* or
      known dynamic selector structures).
    - ALIAS: an identity known to be one of several namespace aliases that
      resolve to the same underlying model family.
    - UNKNOWN: the identity could not be classified deterministically.
    """

    FIXED = "fixed"
    POOL = "pool"
    ALIAS = "alias"
    UNKNOWN = "unknown"


class Modality(str, Enum):
    """Deterministic modality classification for a discovered model."""

    TEXT = "text"
    VISION = "vision"
    IMAGE = "image"
    AUDIO = "audio"
    EMBEDDING = "embedding"
    RERANK = "rerank"
    SAFETY = "safety"
    UNRESOLVED = "unresolved"


class Effort(str, Enum):
    """Deterministic effort normalization."""

    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    NONE = "none"
    AUTO = "auto"
    UNRESOLVED = "unresolved"


_PROVIDER_NAMESPACE_ALIASES: dict[str, str] = {
    "cc": "claude",
    "oc": "opencode",
}


def canonical_provider_namespace(namespace: str) -> str:
    """Map a raw namespace prefix to its canonical provider identity."""

    key = (namespace or "").strip().lower()
    return _PROVIDER_NAMESPACE_ALIASES.get(key, key)


_EFFORT_SYNONYMS: dict[str, str] = {
    "minimal": Effort.MINIMAL.value,
    "min": Effort.MINIMAL.value,
    "low": Effort.LOW.value,
    "medium": Effort.MEDIUM.value,
    "med": Effort.MEDIUM.value,
    "high": Effort.HIGH.value,
    "xhigh": Effort.XHIGH.value,
    "x-high": Effort.XHIGH.value,
    "extra-high": Effort.XHIGH.value,
    "extrahigh": Effort.XHIGH.value,
    "ultra": Effort.XHIGH.value,
    "none": Effort.NONE.value,
    "no-think": Effort.NONE.value,
    "nothink": Effort.NONE.value,
    "no_think": Effort.NONE.value,
    "auto": Effort.AUTO.value,
}

_EFFORT_TOKEN_PATTERN = re.compile(
    r"(?:^|[-_/:.])("
    r"x-?high|extra-?high|no-?think|minimal|medium|xhigh|"
    r"none|auto|high|low|min|med|ultra"
    r")(?:$|[-_/:.])",
    re.IGNORECASE,
)


def normalize_effort(raw: str | None) -> str:
    """Normalize a raw effort/no-think token to a deterministic label.

    Returns Effort.UNRESOLVED.value when the input cannot be classified.
    """

    if not raw or not raw.strip():
        return Effort.UNRESOLVED.value

    key = raw.strip().lower()

    if key in _EFFORT_SYNONYMS:
        return _EFFORT_SYNONYMS[key]

    match = _EFFORT_TOKEN_PATTERN.search(f"-{key}-")

    if match:
        token = match.group(1).lower()
        return _EFFORT_SYNONYMS.get(token, Effort.UNRESOLVED.value)

    return Effort.UNRESOLVED.value


def has_no_think_marker(model_id: str) -> bool:
    """Return True when the raw identity explicitly signals no-think mode."""

    normalized = (model_id or "").lower()
    return bool(re.search(r"(?:^|[-_/:.])no-?think(?:$|[-_/:.])", normalized))


def infer_effort_from_model_id(model_id: str) -> str:
    """Infer an effort label from a raw model id embedded/trailing tokens."""

    if has_no_think_marker(model_id):
        return Effort.NONE.value

    return normalize_effort(model_id)


_MODALITY_LITERALS: dict[str, str] = {
    "text": Modality.TEXT.value,
    "chat": Modality.TEXT.value,
    "code": Modality.TEXT.value,
    "vision": Modality.VISION.value,
    "image": Modality.IMAGE.value,
    "audio": Modality.AUDIO.value,
    "speech": Modality.AUDIO.value,
    "embedding": Modality.EMBEDDING.value,
    "rerank": Modality.RERANK.value,
    "safety": Modality.SAFETY.value,
}

_MODEL_ID_MODALITY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("rerank", Modality.RERANK.value),
    ("embed", Modality.EMBEDDING.value),
    ("whisper", Modality.AUDIO.value),
    ("tts", Modality.AUDIO.value),
    ("speech", Modality.AUDIO.value),
    ("audio", Modality.AUDIO.value),
    ("flux", Modality.IMAGE.value),
    ("dall-e", Modality.IMAGE.value),
    ("dalle", Modality.IMAGE.value),
    ("stable-diffusion", Modality.IMAGE.value),
    ("image", Modality.IMAGE.value),
    ("guard", Modality.SAFETY.value),
    ("safety", Modality.SAFETY.value),
    ("moderation", Modality.SAFETY.value),
    ("vision", Modality.VISION.value),
)


def normalize_modality(raw: str | None) -> str:
    """Normalize an explicit modality label to a deterministic value."""

    if not raw or not raw.strip():
        return Modality.UNRESOLVED.value

    return _MODALITY_LITERALS.get(
        raw.strip().lower(),
        Modality.UNRESOLVED.value,
    )


def infer_modality_from_model_id(model_id: str) -> str:
    """Infer modality from a raw model id using deterministic keyword rules.

    Falls back to Modality.UNRESOLVED.value when no keyword matches.
    """

    normalized = (model_id or "").lower()

    for keyword, modality in _MODEL_ID_MODALITY_KEYWORDS:
        if keyword in normalized:
            return modality

    return Modality.UNRESOLVED.value


_POOL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^auto/[^/]+(?::free)?$", re.IGNORECASE),
    re.compile(r"^openrouter/openrouter/free$", re.IGNORECASE),
    re.compile(r"^oc/opencode-free$", re.IGNORECASE),
)


def _is_pool_selector(model_id: str) -> bool:
    """Recognize known dynamic selector structures, not generic free tokens."""

    return any(pattern.fullmatch(model_id.strip()) for pattern in _POOL_PATTERNS)


def infer_route_kind(model_id: str, is_alias: bool = False) -> str:
    """Classify the routing topology of a raw model identity.

    - POOL for multi-model pool selectors such as auto/best-free,
      auto/coding:free, or openrouter/openrouter/free.
    - ALIAS when the caller has already determined this identity is one of
      several namespace aliases resolving to the same family.
    - FIXED for a single specific model identity.
    - UNKNOWN when the identity is empty/malformed.
    """

    if not model_id or not model_id.strip():
        return RouteKind.UNKNOWN.value

    if _is_pool_selector(model_id):
        return RouteKind.POOL.value

    if is_alias:
        return RouteKind.ALIAS.value

    return RouteKind.FIXED.value


_TRAILING_TOKEN_PATTERN = re.compile(
    r"[-_/:.](?:x-?high|extra-?high|no-?think|minimal|medium|xhigh|"
    r"none|auto|high|low|min|med|ultra|free)$",
    re.IGNORECASE,
)


def _strip_trailing_tokens(base: str) -> str:
    """Repeatedly strip trailing effort/no-think/pool tokens from a base id."""

    stripped = base

    while True:
        match = _TRAILING_TOKEN_PATTERN.search(stripped)

        if not match:
            break

        stripped = stripped[: match.start()]

    return stripped


def parse_namespace(model_id: str) -> tuple[str, str]:
    """Split a raw model id into (namespace, remainder).

    When there is no / separator, the namespace is empty and the full
    identity is returned as the remainder.
    """

    if "/" in model_id:
        namespace, _, remainder = model_id.partition("/")
        return namespace, remainder

    return "", model_id


def family_key_for(model_id: str) -> str:
    """Compute a deterministic, namespace/alias/effort-insensitive family key.

    Provider namespace aliases (for example cc and claude) and trailing
    effort/no-think/pool tokens are normalized away so that distinct
    discovered aliases of the same underlying model collapse to a single
    family key.
    """

    if not model_id or not model_id.strip():
        return ""

    namespace, remainder = parse_namespace(model_id)
    canonical_namespace = canonical_provider_namespace(namespace)
    base = _strip_trailing_tokens(remainder)

    if canonical_namespace:
        return f"{canonical_namespace}:{base}"

    return base


def _recognized_family_key(model_id: str) -> str | None:
    """Return a family key only for deliberately supported identity rules."""
    if not model_id or not model_id.strip():
        return None
    normalized = model_id.strip().lower()
    namespace, remainder = parse_namespace(normalized)
    canonical_namespace = canonical_provider_namespace(namespace)
    base = _strip_trailing_tokens(remainder)
    identity = f"{canonical_namespace}/{base}" if canonical_namespace else base
    if _is_pool_selector(model_id):
        return family_key_for(model_id)
    supported_tokens = ("claude/", "cc/", "gemini", "glm", "nemotron", "opencode", "openrouter")
    if any(token in identity or token in base for token in supported_tokens):
        return family_key_for(model_id)
    return None


@dataclass(frozen=True, slots=True)
class NormalizedFleetRoute:
    """A deterministic, additive, read-only fleet inventory entry.

    This is intentionally separate from ModelRoute: it carries no approval
    semantics and is never consumed by the routing policy.
    """

    model_id: str
    alias: str
    namespace: str
    provider: str
    execution_path: str
    family: str
    effort: str
    route_kind: str
    modality: str
    provenance: str
    normalization_status: str = NormalizationStatus.RESOLVED.value
    policy_blocked: bool = False
    approved: bool = False
    enabled: bool = True
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self):
        if not self.model_id or not self.model_id.strip():
            raise ValueError("model_id must be non-empty.")
        if not self.provider or not self.provider.strip():
            raise ValueError("provider must be non-empty.")
        if not self.execution_path or not self.execution_path.strip():
            raise ValueError("execution_path must be non-empty.")


@dataclass(frozen=True, slots=True)
class ModelFamily:
    """A deduplicated family of discovered aliases for one underlying model."""

    family_key: str
    canonical_provider: str
    base_model: str
    aliases: frozenset[str] = field(default_factory=frozenset)
    route_kinds: frozenset[str] = field(default_factory=frozenset)
    modalities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self):
        if not self.family_key or not self.family_key.strip():
            raise ValueError("family_key must be non-empty.")


DEFAULT_EXECUTION_PATH = "OMNIROUTE"


def normalize_discovered_model(
    discovered: DiscoveredModel,
    execution_path: str = DEFAULT_EXECUTION_PATH,
    is_alias: bool = False,
) -> NormalizedFleetRoute:
    """Normalize a single DiscoveredModel into a NormalizedFleetRoute.

    Nothing here grants approval or eligibility; approved is always False
    for discovery-sourced entries.
    """

    model_id = discovered.model_id
    namespace, _ = parse_namespace(model_id)
    canonical_provider = (
        canonical_provider_namespace(namespace)
        if namespace
        else discovered.provider
    )

    family = _recognized_family_key(model_id)
    resolved = family is not None
    effort = infer_effort_from_model_id(model_id)
    modality = infer_modality_from_model_id(model_id)
    route_kind = infer_route_kind(model_id, is_alias=is_alias) if resolved else RouteKind.UNKNOWN.value
    blocked = is_blocked_model(model_id)

    return NormalizedFleetRoute(
        model_id=model_id,
        alias=model_id,
        namespace=namespace,
        provider=discovered.provider,
        execution_path=execution_path,
        family=family or UNRESOLVED_FAMILY,
        effort=effort,
        route_kind=route_kind,
        modality=modality,
        provenance=Provenance.DISCOVERED.value,
        normalization_status=(NormalizationStatus.RESOLVED.value if resolved else NormalizationStatus.UNRESOLVED.value),
        policy_blocked=blocked,
        approved=False,
        enabled=resolved and not blocked,
        capabilities=frozenset(),
    )


def normalize_discovered_fleet(
    discovered: tuple[DiscoveredModel, ...],
    execution_path: str = DEFAULT_EXECUTION_PATH,
) -> tuple[NormalizedFleetRoute, ...]:
    """Normalize a full discovery batch, marking cross-namespace aliases.

    A route is marked RouteKind.ALIAS when at least one other discovered
    entry shares the same family key (pool-classified entries retain
    RouteKind.POOL regardless of family collisions).
    """

    family_counts: dict[str, int] = {}

    for entry in discovered:
        key = _recognized_family_key(entry.model_id)
        if key is not None:
            family_counts[key] = family_counts.get(key, 0) + 1

    normalized: list[NormalizedFleetRoute] = []

    for entry in discovered:
        key = _recognized_family_key(entry.model_id)
        is_alias = family_counts.get(key, 0) > 1

        route = normalize_discovered_model(
            entry,
            execution_path=execution_path,
            is_alias=is_alias,
        )

        normalized.append(route)

    return tuple(normalized)


UNRESOLVED_FAMILY = "unresolved"
UNRESOLVED_MODEL_ID = "unresolved"


def unresolved_route(
    provider: str = "unknown",
    execution_path: str = DEFAULT_EXECUTION_PATH,
) -> NormalizedFleetRoute:
    """A deterministic placeholder for an identity that failed normalization."""

    return NormalizedFleetRoute(
        model_id=UNRESOLVED_MODEL_ID,
        alias=UNRESOLVED_MODEL_ID,
        namespace="",
        provider=provider or "unknown",
        execution_path=execution_path,
        family=UNRESOLVED_FAMILY,
        effort=Effort.UNRESOLVED.value,
        route_kind=RouteKind.UNKNOWN.value,
        modality=Modality.UNRESOLVED.value,
        provenance=Provenance.UNRESOLVED.value,
        normalization_status=NormalizationStatus.UNRESOLVED.value,
        policy_blocked=False,
        approved=False,
        enabled=False,
        capabilities=frozenset(),
    )


def build_families(
    routes: tuple[NormalizedFleetRoute, ...],
) -> tuple[ModelFamily, ...]:
    """Deduplicate normalized routes into one ModelFamily per family key."""

    grouped: dict[str, dict] = {}
    order: list[str] = []

    for route in routes:
        if route.normalization_status != NormalizationStatus.RESOLVED.value:
            continue
        key = route.family

        if key not in grouped:
            order.append(key)
            grouped[key] = {
                "canonical_provider": canonical_provider_namespace(
                    route.namespace
                )
                or route.provider,
                "base_model": _strip_trailing_tokens(
                    parse_namespace(route.model_id)[1]
                    or route.model_id
                ),
                "aliases": set(),
                "route_kinds": set(),
                "modalities": set(),
            }

        bucket = grouped[key]
        bucket["aliases"].add(route.alias)
        bucket["route_kinds"].add(route.route_kind)
        bucket["modalities"].add(route.modality)

    families = []

    for key in order:
        bucket = grouped[key]
        families.append(
            ModelFamily(
                family_key=key,
                canonical_provider=bucket["canonical_provider"],
                base_model=bucket["base_model"],
                aliases=frozenset(bucket["aliases"]),
                route_kinds=frozenset(bucket["route_kinds"]),
                modalities=frozenset(bucket["modalities"]),
            )
        )

    return tuple(families)


@dataclass(frozen=True, slots=True)
class FleetSummary:
    """A deterministic, read-only inventory summary."""

    total_routes: int
    total_families: int
    policy_blocked_count: int
    by_route_kind: dict[str, int] = field(default_factory=dict)
    by_modality: dict[str, int] = field(default_factory=dict)
    by_provenance: dict[str, int] = field(default_factory=dict)


def _count_by(
    routes: tuple[NormalizedFleetRoute, ...],
    attribute: str,
) -> dict[str, int]:
    counts: dict[str, int] = {}

    for route in routes:
        value = getattr(route, attribute)
        counts[value] = counts.get(value, 0) + 1

    return dict(sorted(counts.items()))


def summarize_fleet(
    routes: tuple[NormalizedFleetRoute, ...],
) -> FleetSummary:
    """Build a deterministic inventory summary over normalized fleet routes."""

    families = build_families(routes)
    blocked_count = sum(1 for route in routes if route.policy_blocked)

    return FleetSummary(
        total_routes=len(routes),
        total_families=len(families),
        policy_blocked_count=blocked_count,
        by_route_kind=_count_by(routes, "route_kind"),
        by_modality=_count_by(routes, "modality"),
        by_provenance=_count_by(routes, "provenance"),
    )
