from nexus.routing.models import ModelRoute, QuotaState, ResourceSnapshot, RoutingRequest
from nexus.routing.router import select_best_route


def _route(model, cost, capability="advanced-coding", quality="standard"):
    return ModelRoute(
        model_id=model,
        provider=model.split("/", 1)[0],
        execution_path="OMNIROUTE",
        capabilities=frozenset({capability}),
        max_risk_level="medium",
        approved=True,
        cost_class=cost,
        quality_tier=quality,
    )


def _available(*providers):
    return {
        provider: ResourceSnapshot(provider=provider, state=QuotaState.UNKNOWN)
        for provider in providers
    }


def test_cheapest_capable_prefers_free_over_premium():
    free = _route("free/model", "free")
    premium = _route("premium/model", "premium", quality="high")
    selected = select_best_route(
        RoutingRequest(capability="advanced-coding", risk_level="medium"),
        catalog=(premium, free), resources=_available("free", "premium"),
    )
    assert selected.route is free
    assert selected.reason.startswith("CHEAPEST_CAPABLE:")


def test_free_capability_mismatch_does_not_beat_capable_premium():
    free = _route("free/model", "free", capability="mechanical")
    premium = _route("premium/model", "premium", quality="high")
    selected = select_best_route(
        RoutingRequest(capability="advanced-coding", risk_level="medium"),
        catalog=(free, premium), resources=_available("free", "premium"),
    )
    assert selected.route is premium


def test_unknown_cost_does_not_outrank_known_free():
    free = _route("free/model", "free")
    unknown = _route("unknown/model", "unknown", quality="high")
    selected = select_best_route(
        RoutingRequest(capability="advanced-coding", risk_level="medium"),
        catalog=(unknown, free), resources=_available("unknown", "free"),
    )
    assert selected.route is free


def test_exhausted_free_route_is_excluded_before_cost_preference():
    free = _route("free/model", "free")
    premium = _route("premium/model", "premium", quality="high")
    resources = _available("free", "premium")
    resources["free"] = ResourceSnapshot(
        provider="free", state=QuotaState.EXHAUSTED, headroom_pct=0.0,
    )
    selected = select_best_route(
        RoutingRequest(capability="advanced-coding", risk_level="medium"),
        catalog=(free, premium), resources=resources,
    )
    assert selected.route is premium


def test_cheap_mechanical_route_beats_premium_mechanical_route():
    cheap = _route("cheap/model", "free", capability="mechanical")
    premium = _route("premium/model", "premium", capability="mechanical", quality="high")
    selected = select_best_route(
        RoutingRequest(capability="mechanical", risk_level="low"),
        catalog=(premium, cheap), resources=_available("cheap", "premium"),
    )
    assert selected.route is cheap
