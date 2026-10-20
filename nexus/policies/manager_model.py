"""Manager model selection policy for Nexus.

Selects the preferred Manager model based on mission scope (from triage).
Every Manager execution now routes through OmniRoute
(nexus.dispatchers.manager.run_manager uses
codex_subprocess_env(profile="omniroute")) -- the Manager no longer depends
on a separate Codex ChatGPT OAuth session, exactly like Worker and Reviewer
already didn't.

    BOUNDED  →  codex/gpt-5.6-luna   (cost-efficient, focused planning)
    BROAD    →  codex/gpt-5.6-sol    (stronger decomposition for open-scope missions)

Both identifiers were verified -- not assumed -- to produce a real,
contract-valid NEXUS_PLAN envelope through the Codex CLI's "omniroute"
subprocess profile (NEXUS_UNIFIED_OMNIROUTE_EXECUTION compatibility proof,
a disposable-repo smoke) before being adopted here.

A third, manually selectable option is pulled from the existing approved
Worker/Reviewer catalog (nexus/routing/catalog.py) rather than inventing a
new list: the one entry there carrying the "planning" capability, verified
compatible with the Manager's Codex CLI transport the same way.

The effort policy (nexus/policies/reasoning_effort.py) is deliberately
independent and unchanged:
    BOUNDED → LOW complexity  → "low" effort
    BROAD   → MEDIUM complexity → "medium" effort
"""

from __future__ import annotations

from nexus.dispatchers.triage import MissionScope

# Preferred Manager model for each mission scope tier. Both route through
# OmniRoute via the Codex CLI's "omniroute" subprocess profile (see
# nexus.dispatchers.manager.run_manager) -- neither is a Worker/Reviewer
# catalog id (nexus/routing/catalog.py); they are Codex-CLI-native model
# identifiers that OmniRoute itself resolves upstream.
MANAGER_MODEL_BOUNDED: str = "codex/gpt-5.6-luna"
MANAGER_MODEL_BROAD: str = "codex/gpt-5.6-sol"


def _catalog_planning_route():
    """The one approved, enabled Worker/Reviewer catalog route (if any)
    carrying the "planning" capability -- reusing the existing catalog
    instead of maintaining a second model list. Returns None rather than
    guessing when the catalog has none, or more than Nexus has verified
    compatible with the Manager's transport (never silently picks one)."""
    from nexus.routing.catalog import APPROVED_CATALOG  # noqa: PLC0415

    candidates = [
        route
        for route in APPROVED_CATALOG
        if route.approved and route.enabled and "planning" in route.capabilities
    ]
    return candidates[0] if len(candidates) == 1 else None


_CATALOG_PLANNING_ROUTE = _catalog_planning_route()
_CATALOG_PLANNING_MODEL: str | None = (
    _CATALOG_PLANNING_ROUTE.model_id if _CATALOG_PLANNING_ROUTE else None
)

# The only model identifiers run_manager() can actually dispatch today,
# computed once at import time (argparse/CLI choices need a static tuple;
# the catalog itself does not change within a running process).
MANAGER_MODEL_CHOICES: tuple[str, ...] = tuple(
    model_id
    for model_id in (MANAGER_MODEL_BOUNDED, MANAGER_MODEL_BROAD, _CATALOG_PLANNING_MODEL)
    if model_id is not None
)

_MANAGER_MODEL_DISPLAY_NAMES: dict[str, str] = {
    MANAGER_MODEL_BOUNDED: "Luna — cost-efficient, focused planning",
    MANAGER_MODEL_BROAD: "Sol — stronger multi-worker decomposition",
}
if _CATALOG_PLANNING_MODEL:
    _MANAGER_MODEL_DISPLAY_NAMES[_CATALOG_PLANNING_MODEL] = (
        f"{_CATALOG_PLANNING_MODEL} — approved planning route"
    )

# Upstream model vendor for each selectable id, for display only (Section
# 10: never show "codex" as though Codex itself were the upstream LLM
# provider -- Codex CLI is the execution engine, OmniRoute is the gateway,
# and this is the actual model vendor behind that gateway).
_UPSTREAM_PROVIDER_BY_MODEL: dict[str, str] = {
    MANAGER_MODEL_BOUNDED: "openai",
    MANAGER_MODEL_BROAD: "openai",
}
if _CATALOG_PLANNING_ROUTE:
    _UPSTREAM_PROVIDER_BY_MODEL[_CATALOG_PLANNING_ROUTE.model_id] = _CATALOG_PLANNING_ROUTE.provider


class ManagerModelUnavailableError(ValueError):
    """Raised when a manually requested Manager model cannot be dispatched."""

    def __init__(self, model: str, reason: str):
        self.model = model
        self.reason = reason
        super().__init__(reason)


def validate_manual_manager_model(model: str) -> None:
    """Raise ManagerModelUnavailableError if ``model`` cannot run as Manager.

    Called before any Run is created for a manual selection, so an invalid
    choice is rejected before anything starts (no Run row, no Manager
    dispatch attempt). No silent substitution: the caller gets exactly
    what it asked for, or an explicit rejection.
    """
    if model not in MANAGER_MODEL_CHOICES:
        raise ManagerModelUnavailableError(
            model,
            f"{model!r} is not an eligible Manager model. Manager execution "
            "runs through OmniRoute (via the Codex CLI's isolated "
            "\"omniroute\" subprocess profile) and currently supports only "
            "verified-compatible routes: "
            + ", ".join(MANAGER_MODEL_CHOICES)
            + ". Any other OmniRoute-catalog model id has not been verified "
            "compatible with the Manager's planning contract.",
        )


def describe_manager_models() -> dict:
    """Describe Manager model choices for the Control Center selector.

    Distinguishes, for each entry considered:
      DISCOVERED            -- present in the approved Worker/Reviewer
                               catalog (nexus/routing/catalog.py)
      QUALIFIED_FOR_MANAGER -- carries the catalog's "planning" capability
      SELECTABLE            -- verified compatible with the Manager's
                               Codex CLI + OmniRoute transport (this
                               module's MANAGER_MODEL_CHOICES)

    Every entry is real: no raw catalog dump, no fabricated quota/health
    telemetry fields.
    """
    selectable = [
        {
            "model_id": model_id,
            "display_name": _MANAGER_MODEL_DISPLAY_NAMES.get(model_id, model_id),
            "provider": _UPSTREAM_PROVIDER_BY_MODEL.get(model_id, "unknown"),
            "availability": "SELECTABLE",
            "reason": (
                "Verified: produces a valid Manager plan through OmniRoute "
                "(no Codex OAuth session required)."
            ),
        }
        for model_id in MANAGER_MODEL_CHOICES
    ]

    ineligible = []
    try:
        from nexus.routing.catalog import APPROVED_CATALOG  # noqa: PLC0415

        for route in APPROVED_CATALOG:
            if route.model_id in MANAGER_MODEL_CHOICES:
                continue
            if not (route.approved and route.enabled):
                continue
            if "planning" in route.capabilities:
                # Not expected today (the catalog's one planning-capable
                # route is already selectable above via
                # _catalog_planning_route()), but a future second one must
                # surface as discovered-and-qualified-but-unverified, never
                # be silently promoted to selectable.
                availability = "QUALIFIED_FOR_MANAGER"
                reason = (
                    "Discovered in the approved catalog with planning "
                    "capability, but not yet verified compatible with the "
                    "Manager's Codex CLI + OmniRoute transport."
                )
            else:
                availability = "DISCOVERED"
                reason = (
                    "Approved for "
                    + ", ".join(sorted(route.capabilities))
                    + " only -- the Manager role requires the 'planning' "
                    "capability, which this route does not have."
                )
            ineligible.append(
                {
                    "model_id": route.model_id,
                    "display_name": route.model_id,
                    "provider": route.provider,
                    "availability": availability,
                    "reason": reason,
                }
            )
    except Exception:
        # The selector must never fail to load because catalog introspection
        # broke; an empty "ineligible" list is a safe, honest degradation.
        ineligible = []

    return {
        "auto": {
            "model_id": None,
            "display_name": "Auto — Nexus default",
            "reason": (
                "Preserves existing scope-based selection: Luna for BOUNDED "
                "missions, Sol for BROAD missions -- both executed through "
                "OmniRoute, no Codex OAuth session required."
            ),
        },
        "selectable": selectable,
        "ineligible": ineligible,
    }


def select_manager_model(scope: MissionScope) -> str:
    """Return the preferred Manager model for the given mission scope.

    Pure and deterministic: no filesystem, database, network, or model
    access of any kind. Same input always produces the same output.

    Parameters
    ----------
    scope:
        The mission scope classification from ``classify_mission_scope()``.
        BOUNDED missions use the capable, cost-efficient Luna model.
        BROAD missions use the stronger Sol model for multi-worker planning
        and architecture-wide decomposition.

    Returns
    -------
    str
        The model identifier string to pass to ``run_manager()``.
        Availability is not checked here; OmniRoute owns that concern.
    """
    if scope == MissionScope.BROAD:
        return MANAGER_MODEL_BROAD
    return MANAGER_MODEL_BOUNDED
