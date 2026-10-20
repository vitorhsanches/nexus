"""Phase 4.7 quota-aware routing policy, with no live provider calls."""

import pytest

from nexus.dispatchers.omniroute import _looks_quota_exhausted, _looks_transient
from nexus.routing.models import ModelRoute, QuotaState, ResourceSnapshot, RoutingRequest
from nexus.routing.resources import capacity_sufficient, estimated_demand_pct
from nexus.routing.router import NoEligibleRouteError, select_best_route


def route(model, provider, cost, *, capability="standard-coding", risk="medium"):
    return ModelRoute(
        model_id=model, provider=provider, execution_path="OMNIROUTE",
        capabilities=frozenset({capability}), max_risk_level=risk,
        approved=True, cost_class=cost,
    )


FREE = route("free/coding", "free", "free")
PAID = route("paid/coding", "paid", "premium")
CLAUDE = route("cc/claude-sonnet-5-low", "claude", "standard")


def known(provider, pct):
    return ResourceSnapshot(provider, QuotaState.KNOWN, pct)


def choose(catalog, resources, risk="medium"):
    return select_best_route(
        RoutingRequest("standard-coding", risk_level=risk),
        catalog=catalog, resources=resources,
    )


def test_demand_is_pure_deterministic_policy():
    assert [estimated_demand_pct("mechanical", "low"),
            estimated_demand_pct("standard-coding", "medium"),
            estimated_demand_pct("advanced-coding", "medium")] == [5, 15, 30]
    assert estimated_demand_pct("standard-coding", "high") == 30
    assert estimated_demand_pct("standard-coding", "medium") == 15


def test_safety_reserve_rejects_boundary_below_20():
    assert capacity_sufficient(known("free", 19.9), "standard-coding", "medium") is False
    assert capacity_sufficient(known("free", 20), "standard-coding", "medium") is True


def test_sufficient_free_beats_premium_even_when_premium_has_more_headroom():
    selected = choose((PAID, FREE), {"paid": known("paid", 90), "free": known("free", 25)})
    assert selected.route is FREE


def test_insufficient_free_does_not_beat_sufficient_paid():
    selected = choose((FREE, PAID), {"free": known("free", 19), "paid": known("paid", 50)})
    assert selected.route is PAID


def test_claude_low_is_skipped_for_other_capable_provider():
    selected = choose((CLAUDE, FREE), {"claude": known("claude", 8), "free": known("free", 30)})
    assert selected.route is FREE


def test_incapable_free_leaves_premium_as_fallback():
    incapable = route("free/mechanical", "free", "free", capability="mechanical")
    selected = choose((incapable, PAID), {"free": known("free", 80), "paid": known("paid", 30)})
    assert selected.route is PAID


def test_unknown_is_conservative_but_still_usable():
    unknown = ResourceSnapshot("free", QuotaState.UNKNOWN)
    selected = choose((FREE, PAID), {"free": unknown, "paid": known("paid", 30)})
    assert selected.route is PAID
    selected = choose((FREE,), {"free": unknown})
    assert selected.route is FREE


def test_capacity_rejection_is_structured():
    with pytest.raises(NoEligibleRouteError) as exc:
        choose((FREE,), {"free": known("free", 19)})
    assert exc.value.rejections[0]["reason"] == "capacity_insufficient"


def test_all_exhausted_capable_routes_unavailable():
    exhausted = {
        "free": ResourceSnapshot("free", QuotaState.EXHAUSTED, 0),
        "paid": ResourceSnapshot("paid", QuotaState.EXHAUSTED, 0),
    }
    with pytest.raises(NoEligibleRouteError) as exc:
        choose((FREE, PAID), exhausted)
    assert {r["reason"] for r in exc.value.rejections} == {"exhausted"}


def test_no_capability_and_no_capacity_are_distinct():
    incapable = route("free/mechanical", "free", "free", capability="mechanical")
    with pytest.raises(NoEligibleRouteError) as exc:
        choose((incapable, PAID), {"free": known("free", 80), "paid": known("paid", 5)})
    assert {r["reason"] for r in exc.value.rejections} == {"capability", "capacity_insufficient"}


def test_risk_gate_precedes_capacity_and_cost():
    low_risk = route("free/low", "free", "free", risk="low")
    selected = choose((low_risk, PAID), {"free": known("free", 100), "paid": known("paid", 50)})
    assert selected.route is PAID


def test_quota_signal_requires_explicit_evidence():
    assert _looks_quota_exhausted("429 usage limit exhausted") is True
    assert _looks_quota_exhausted("429 Too Many Requests") is False
    assert _looks_transient("429 Too Many Requests") is True
    assert _looks_quota_exhausted("503 Service Unavailable") is False


def test_provider_exclusion_flows_through_service():
    from nexus.routing.service import AdaptiveRoutingService
    from nexus.routing.telemetry import OmniRouteTelemetrySnapshot
    service = AdaptiveRoutingService(
        telemetry_client=OmniRouteTelemetrySnapshot(),
        approved_catalog=(CLAUDE, FREE),
    )
    decision = service.select_route_for_capability(
        "standard-coding", risk_level="medium",
        blocked_providers=frozenset({"claude"}),
    )
    assert decision.model == FREE.model_id


def test_exhausted_provider_stays_excluded_during_total_telemetry_outage():
    from nexus.routing.service import AdaptiveRoutingService
    from nexus.routing.telemetry import OmniRouteTelemetrySnapshot
    snapshot = OmniRouteTelemetrySnapshot(errors=("/v1/models unavailable",))
    service = AdaptiveRoutingService(
        telemetry_client=snapshot,
        approved_catalog=(CLAUDE,),
        legacy_fallback={"standard-coding": (CLAUDE.model_id, "claude", "low", "OMNIROUTE")},
    )
    with pytest.raises(NoEligibleRouteError) as exc:
        service.select_route_for_capability(
            "standard-coding", risk_level="medium",
            blocked_providers=frozenset({"claude"}),
        )
    assert exc.value.rejections[0]["reason"] == "blocked_provider"


def test_dispatcher_marks_only_explicit_quota_exhaustion():
    from unittest.mock import patch
    from nexus.dispatchers.omniroute import run_omniroute_worker
    from tests.test_omniroute_exit_code_propagation import _DbMixin, _make_popen_side_effect

    class Harness(_DbMixin):
        pass

    harness = Harness()
    harness.setUp()
    try:
        for line, expected in [
            ("429 quota exhausted\n", "QUOTA_EXHAUSTED"),
            ("429 Too Many Requests\n", "TRANSIENT_PROVIDER_ERROR"),
        ]:
            with patch("nexus.dispatchers.omniroute.ADAPTER_PATH") as path, patch(
                "nexus.dispatchers.omniroute.subprocess.Popen",
                side_effect=_make_popen_side_effect(
                    returncode=1, file_content=None, stdout_lines=[line],
                ),
            ):
                path.exists.return_value = True
                result = run_omniroute_worker(
                    run_id="RUN-PROP-TEST", repo="C:/fake/repo",
                    task="test", model="free/model",
                )
            assert result["reason"] == expected
    finally:
        harness.tearDown()
