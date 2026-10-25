"""Focused tests for the Phase 2 Control Center read-only projection."""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nexus.routing.models import QuotaState, ResourceSnapshot
from nexus.routing.telemetry import (
    DiscoveredModel,
    OmniRouteTelemetrySnapshot,
    ProviderTelemetry,
    ServerHealth,
)
from nexus.web.app import app
from nexus.web.native_runtime import get_native_runtime_status
from nexus.web.routing_status import get_routing_status


class SnapshotClient:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def collect(self):
        return self.snapshot


def test_no_authenticated_providers_is_truthful_degraded_state():
    snapshot = OmniRouteTelemetrySnapshot(
        discovered_models=(
            DiscoveredModel("cc/claude-sonnet-5-low", "claude"),
        ),
        server_health=ServerHealth(
            status="healthy",
            configured_count=0,
            credential_total=0,
            credential_healthy=0,
            credential_failed=0,
            cryptography_status="healthy",
            cryptography_provider="aes-256-gcm",
        ),
    )

    result = get_routing_status(SnapshotClient(snapshot))

    assert result["runtime"]["status"] == "ONLINE"
    assert result["runtime"]["credential_store"] == "HEALTHY"
    assert result["runtime"]["providers"] == "AUTH REQUIRED"
    assert result["summary"]["callable_routes"] == 0
    assert result["summary"]["actions_available"] is False
    claude = next(
        route for route in result["routes"]
        if route["model"] == "cc/claude-sonnet-5-low"
    )
    assert claude["health"] == "AUTH REQUIRED"
    assert claude["callable"] is False


def test_explicit_usable_provider_makes_discovered_approved_route_callable():
    snapshot = OmniRouteTelemetrySnapshot(
        discovered_models=(
            DiscoveredModel("cc/claude-sonnet-5-low", "claude"),
        ),
        provider_telemetry={
            "claude": ProviderTelemetry(
                provider="claude",
                resource=ResourceSnapshot(
                    provider="claude",
                    state=QuotaState.UNKNOWN,
                    headroom_pct=None,
                    healthy=True,
                ),
                token_status="valid",
                credential_usable=True,
                healthy=True,
            )
        },
        server_health=ServerHealth(
            status="healthy",
            configured_count=1,
            credential_total=1,
            credential_healthy=1,
            credential_failed=0,
            cryptography_status="healthy",
        ),
    )

    result = get_routing_status(SnapshotClient(snapshot))

    assert result["summary"]["callable_routes"] >= 1
    assert result["summary"]["actions_available"] is True


def test_routing_status_endpoint_is_read_only_projection():
    expected = {
        "runtime": {"status": "ONLINE"},
        "summary": {"callable_routes": 0, "actions_available": False},
        "routes": [],
        "policy": [],
        "errors": [],
        "warnings": [],
    }
    with patch("nexus.web.routing_status.get_routing_status", return_value=expected):
        response = TestClient(app).get("/api/routing/status")
    assert response.status_code == 200
    assert response.json() == expected


def test_native_runtime_status_is_local_and_does_not_query_omniroute():
    result = get_native_runtime_status(
        codex_probe=lambda: {"id": "codex_native", "status": "READY"},
        claude_probe=lambda: {"id": "claude_native", "status": "AUTH_REQUIRED"},
    )
    assert result["native_first"] is True
    assert result["native_ready"] is True
    assert result["omniroute"] == {
        "status": "NOT_QUERIED",
        "reason": "OmniRoute telemetry is only collected after opening Models & Routing.",
    }


def test_native_runtime_endpoint_uses_only_the_native_probe():
    expected = {
        "default_runtime": "native",
        "native_first": True,
        "native_ready": True,
        "runtimes": {"codex_native": {"status": "READY"}},
        "omniroute": {"status": "NOT_QUERIED"},
    }
    with patch("nexus.web.native_runtime.get_native_runtime_status", return_value=expected) as probe, \
            patch("nexus.web.routing_status.OmniRouteTelemetryClient", side_effect=AssertionError("telemetry queried")):
        response = TestClient(app).get("/api/runtime/status")
    assert response.status_code == 200
    assert response.json() == expected
    probe.assert_called_once_with()


def test_control_center_assets_and_required_panels_are_present():
    web_dir = Path(__file__).resolve().parents[1] / "nexus" / "web"
    html = (web_dir / "templates" / "nexus.html").read_text(encoding="utf-8")
    script = (web_dir / "static" / "js" / "control-center.js").read_text(encoding="utf-8")
    css = (web_dir / "static" / "css" / "control-center.css").read_text(encoding="utf-8")

    for panel_id in (
        "overview-dashboard",
        "overview-program",
        "overview-terminal",
        "overview-routing",
        "overview-recovery",
        "overview-activity",
        "command-palette",
    ):
        assert panel_id in html
    assert "/api/routing/status" in script
    assert "/api/runtime/status" in script
    assert "NOT QUERIED" in html
    assert "runtimeDisabled" in script
    assert "state.availability.programs ? activePrograms" in script
    assert "state.availability.agents ? runningTasks" in script
    assert "SAFE MODE" not in html
    assert "WORKSPACE TRUSTED" not in html
    assert "Active Workers" in html
    assert "grid-template-areas" in css


def test_agent_output_modal_assets_are_present():
    """Cockpit Task 4: EXPAND OUTPUT lazy-loads full agent results.

    Regression guard for the pieces a Python test can see without a JS
    runtime: the expand action, the on-demand fetch of the existing
    per-agent result endpoint, keyboard close, and the modal's styling.
    """
    web_dir = Path(__file__).resolve().parents[1] / "nexus" / "web"
    script = (web_dir / "static" / "js" / "nexus.js").read_text(encoding="utf-8")
    css = (web_dir / "static" / "css" / "nexus.css").read_text(encoding="utf-8")

    assert "EXPAND OUTPUT" in script
    assert "has_full_result" in script
    assert "/api/operational/agents/" in script and "/result" in script
    assert "agentResultCache" in script  # dedupes repeated opens / in-flight fetches
    assert '"Escape"' in script
    assert ".agent-output-modal" in css


def test_mission_progress_assets_and_invariants_are_present():
    """Cockpit Task 3: compact Mission Progress rows next to the Agent Panes.

    There is no JS test runner in this repo (by design -- see Task 4's
    equivalent guard), so this is a source-level regression check: it
    confirms the new pieces exist and, via simple textual-order assertions,
    that the two correctness rules the task spec calls out are still wired
    the way they were implemented -- not a replacement for exercising the
    real DOM in a browser.
    """
    web_dir = Path(__file__).resolve().parents[1] / "nexus" / "web"
    html = (web_dir / "templates" / "nexus.html").read_text(encoding="utf-8")
    script = (web_dir / "static" / "js" / "nexus.js").read_text(encoding="utf-8")
    css = (web_dir / "static" / "css" / "nexus.css").read_text(encoding="utf-8")

    assert 'id="mission-progress"' in html
    assert "function renderMissionProgress" in script
    assert "function resolveReviewVerdict" in script
    assert "renderMissionProgress(agents, checkpoints)" in script  # wired into the existing 3s poll
    assert ".mission-progress" in css and ".mp-row" in css

    # No fictional agents/roles: only known Nexus roles are listed, and an
    # empty/missing agent list renders nothing rather than placeholder rows.
    assert 'MISSION_PROGRESS_ROLE_ORDER = ["Manager", "Scout", "Worker", "ManagerReview"]' in script
    assert "if (!list.length) return;" in script

    # A reviewer's lifecycle status must never stand in for its verdict:
    # resolveReviewVerdict's only source of truth is the REVIEW checkpoint
    # it is handed (see test_review_verdict_resolver_never_fabricates_pass
    # below for the executable proof) -- no "COMPLETED implies PASS"
    # fallback should exist in the source at all.
    assert 'rev.status === "COMPLETED") verdict = "PASS"' not in script

    # Review rows in Mission Progress reuse the exact same resolver as the
    # Review panel -- never a second, possibly-diverging verdict calculation.
    assert "resolveReviewVerdict(a, agents, reviewCpByReviewerId)" in script


def _extract_js_function(source, name):
    """Pull one ``function NAME(...) { ... }`` out of nexus.js by brace
    balance, so the regression test below executes the real, shipped
    implementation (sliced verbatim from the file) instead of a hand-copied
    mirror that could silently drift from it."""
    match = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", source)
    assert match, f"{name} not found in nexus.js"
    depth = 0
    for i in range(match.start(), len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start():i + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


def test_review_verdict_resolver_never_fabricates_pass():
    """Regression test for the RUN-0FCCAD6C false-PASS bug.

    Root cause: resolveReviewVerdict used to key its checkpoint lookup on
    agent.worker_ordinal, a field /api/operational/agents never populates
    (it only exists on checkpoint rows) -- so the lookup always missed and
    every reviewer fell through to "status COMPLETED implies PASS", however
    the REVIEW checkpoint's real, authoritative verdict read (RETRY,
    BLOCKED, ...). The fix keys the lookup on reviewer_id instead, which
    the orchestrator (nexus/orchestration/progressive.py) stamps on every
    REVIEW checkpoint alongside the real verdict.

    This test extracts buildReviewCpByReviewerId/resolveReviewVerdict/
    findAgent verbatim from nexus.js and executes them in Node against
    fixtures mirroring the real persisted records for RUN-0FCCAD6C and
    RUN-D5CF3C61 (dumped from nexus/storage/nexus.db for this investigation),
    so it exercises the actual shipped logic, not a reimplementation of it.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available in this environment")

    web_dir = Path(__file__).resolve().parents[1] / "nexus" / "web"
    script = (web_dir / "static" / "js" / "nexus.js").read_text(encoding="utf-8")

    functions = "\n".join(
        _extract_js_function(script, name)
        for name in ("findAgent", "buildReviewCpByReviewerId", "resolveReviewVerdict")
    )

    harness = """
'use strict';
const assert = require('assert');

%(functions)s

function agent(id, role, status, parent) {
  return { id, role, status, parent_agent_id: parent || null };
}

function reviewCp(reviewerId, workerId, verdict) {
  return { boundary: 'REVIEW', payload: { reviewer_id: reviewerId, worker_id: workerId, verdict } };
}

// 1) Explicit PASS: the common, legitimate case must still read PASS.
{
  const agents = [agent('AGENT-W1', 'Worker', 'COMPLETED'), agent('AGENT-R1', 'ManagerReview', 'COMPLETED', 'AGENT-W1')];
  const cps = buildReviewCpByReviewerId([reviewCp('AGENT-R1', 'AGENT-W1', 'PASS')]);
  const r = resolveReviewVerdict(agents[1], agents, cps);
  assert.strictEqual(r.verdict, 'PASS');
  assert.strictEqual(r.cls, 'pass');
  assert.strictEqual(r.text, 'PASS');
}

// 2) Explicit rejection (BLOCKED is the real vocabulary for "review did not
//    pass"; Nexus's VERDICTS set is {PASS, RETRY, ESCALATE, BLOCKED}, no
//    literal "FAIL" -- see nexus/reviews/models.py).
{
  const agents = [agent('AGENT-W1', 'Worker', 'COMPLETED'), agent('AGENT-R1', 'ManagerReview', 'COMPLETED', 'AGENT-W1')];
  const cps = buildReviewCpByReviewerId([reviewCp('AGENT-R1', 'AGENT-W1', 'BLOCKED')]);
  const r = resolveReviewVerdict(agents[1], agents, cps);
  assert.strictEqual(r.verdict, 'BLOCKED');
  assert.strictEqual(r.cls, 'fail');
}

// 3) The THE core regression: reviewer agent reached lifecycle COMPLETED
//    but no REVIEW checkpoint was ever recorded for it. Must be an honest
//    "not recorded", never a fabricated PASS. This is exactly
//    RUN-0FCCAD6C's shape before the fix.
{
  const agents = [agent('AGENT-W1', 'Worker', 'COMPLETED'), agent('AGENT-R1', 'ManagerReview', 'COMPLETED', 'AGENT-W1')];
  const cps = buildReviewCpByReviewerId([]);
  const r = resolveReviewVerdict(agents[1], agents, cps);
  assert.strictEqual(r.verdict, null);
  assert.strictEqual(r.cls, 'pending');
  assert.strictEqual(r.text, 'NOT RECORDED');
}

// 4) Reviewer agent itself BLOCKED (e.g. TOOL_FAILURE in the dispatcher
//    before it could produce a verdict): still "not recorded", never a
//    fabricated FAIL either -- the resolver must not infer anything from
//    agent lifecycle status in either direction.
{
  const agents = [agent('AGENT-W1', 'Worker', 'COMPLETED'), agent('AGENT-R1', 'ManagerReview', 'BLOCKED', 'AGENT-W1')];
  const cps = buildReviewCpByReviewerId([]);
  const r = resolveReviewVerdict(agents[1], agents, cps);
  assert.strictEqual(r.verdict, null);
  assert.strictEqual(r.text, 'NOT RECORDED');
}

// 5) Multiple attempts, real RUN-0FCCAD6C data: attempt 1's reviewer
//    (AGENT-128EA02F) verdict RETRY, attempt 2's reviewer (AGENT-6E96339B,
//    a DIFFERENT agent, same worker_ordinal slot) verdict BLOCKED. Each
//    must resolve to its own attempt's verdict -- never PASS, never
//    cross-contaminated with the other attempt.
{
  const agents = [
    agent('AGENT-6AF4AD51', 'Worker', 'COMPLETED', 'AGENT-MGR'),
    agent('AGENT-128EA02F', 'ManagerReview', 'COMPLETED', 'AGENT-6AF4AD51'),
    agent('AGENT-641BCD9F', 'Worker', 'COMPLETED', 'AGENT-MGR'),
    agent('AGENT-6E96339B', 'ManagerReview', 'COMPLETED', 'AGENT-641BCD9F'),
  ];
  const cps = buildReviewCpByReviewerId([
    reviewCp('AGENT-128EA02F', 'AGENT-6AF4AD51', 'RETRY'),
    reviewCp('AGENT-6E96339B', 'AGENT-641BCD9F', 'BLOCKED'),
  ]);
  const r1 = resolveReviewVerdict(agents[1], agents, cps);
  const r2 = resolveReviewVerdict(agents[3], agents, cps);
  assert.strictEqual(r1.verdict, 'RETRY');
  assert.strictEqual(r1.cls, 'fail');
  assert.strictEqual(r2.verdict, 'BLOCKED');
  assert.strictEqual(r2.cls, 'fail');
}

// 6) One verdict engine: a LIFECYCLE-boundary checkpoint for an unrelated
//    reviewer must never be read as review evidence (only REVIEW-boundary
//    checkpoints feed the map), so there is no second, divergent source.
{
  const cps = buildReviewCpByReviewerId([
    { boundary: 'LIFECYCLE', payload: { reviewer_id: 'AGENT-DECOY', verdict: 'PASS' } },
  ]);
  assert.strictEqual(cps['AGENT-DECOY'], undefined);
}

// 7) Historical run with incomplete checkpoints: a REVIEW checkpoint
//    missing reviewer_id entirely must not crash and must not match.
{
  const agents = [agent('AGENT-W1', 'Worker', 'COMPLETED'), agent('AGENT-R1', 'ManagerReview', 'COMPLETED', 'AGENT-W1')];
  const cps = buildReviewCpByReviewerId([{ boundary: 'REVIEW', payload: { verdict: 'PASS' } }]);
  const r = resolveReviewVerdict(agents[1], agents, cps);
  assert.strictEqual(r.verdict, null);
  assert.strictEqual(r.text, 'NOT RECORDED');
}

// 8) Successful integration at scale: RUN-D5CF3C61's real shape, 4 workers
//    each with their own reviewer, all authoritatively PASS -- no
//    regression for the common, legitimate, fully-passing case.
{
  const workerIds = ['AGENT-0AD83777', 'AGENT-04DA9F58', 'AGENT-A3EBCCD7', 'AGENT-03BD3D12'];
  const reviewerIds = ['AGENT-95D99DC4', 'AGENT-16661835', 'AGENT-E6155A4A', 'AGENT-3379E810'];
  const agents = [];
  const cpList = [];
  for (let i = 0; i < 4; i++) {
    agents.push(agent(workerIds[i], 'Worker', 'COMPLETED', 'AGENT-MGR'));
    agents.push(agent(reviewerIds[i], 'ManagerReview', 'COMPLETED', workerIds[i]));
    cpList.push(reviewCp(reviewerIds[i], workerIds[i], 'PASS'));
  }
  const cps = buildReviewCpByReviewerId(cpList);
  for (let i = 0; i < 4; i++) {
    const reviewer = agents.find(a => a.id === reviewerIds[i]);
    const r = resolveReviewVerdict(reviewer, agents, cps);
    assert.strictEqual(r.verdict, 'PASS');
    assert.strictEqual(r.cls, 'pass');
  }
}

console.log('OK');
""" % {"functions": functions}

    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(harness)
        harness_path = f.name

    try:
        result = subprocess.run(
            [node, harness_path], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, (
            f"verdict resolver regression harness failed:\n{result.stdout}\n{result.stderr}"
        )
    finally:
        Path(harness_path).unlink(missing_ok=True)


def _run_node_harness(functions_source, body_source):
    """Write functions_source + body_source to a temp .js file, run it under
    Node, and assert it exits 0 (the harness itself raises via Node's
    `assert` module on any failed expectation)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available in this environment")

    harness = "'use strict';\nconst assert = require('assert');\n\n%s\n\n%s\n" % (
        functions_source,
        body_source,
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(harness)
        harness_path = f.name

    try:
        result = subprocess.run([node, harness_path], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, f"harness failed:\n{result.stdout}\n{result.stderr}"
    finally:
        Path(harness_path).unlink(missing_ok=True)


def test_agent_activity_attribution_and_history_bound():
    """Cockpit Phase 2A: per-agent execution events in the Agent Pane.

    Extracts buildAgentActivity verbatim from nexus.js (same approach as
    test_review_verdict_resolver_never_fabricates_pass) and executes it
    against fixtures mirroring the real persisted records for RUN-0FCCAD6C
    (two attempts, two distinct Worker/Reviewer agent pairs sharing one
    worker_ordinal slot) and RUN-D5CF3C61 (multiple concurrent Workers).
    """
    web_dir = Path(__file__).resolve().parents[1] / "nexus" / "web"
    script = (web_dir / "static" / "js" / "nexus.js").read_text(encoding="utf-8")
    limit_match = re.search(r"var AGENT_ACTIVITY_LIMIT = (\d+);", script)
    assert limit_match, "AGENT_ACTIVITY_LIMIT not found in nexus.js"
    functions = limit_match.group(0) + "\n" + _extract_js_function(script, "buildAgentActivity")

    body = """
function agent(id, role, status, startedAt, finishedAt) {
  return { id, role, status, started_at: startedAt || null, finished_at: finishedAt || null };
}

function cp(boundary, payload, createdAt) {
  return { boundary, payload, created_at: createdAt || '2026-01-01T00:00:00Z' };
}

// 1) Correct event-to-agent attribution: a WORKER_ATTEMPT checkpoint for a
//    DIFFERENT worker_id must never appear on this agent's timeline.
{
  const w1 = agent('AGENT-W1', 'Worker', 'COMPLETED', '2026-01-01T00:00:00Z', '2026-01-01T00:05:00Z');
  const checkpoints = [
    cp('WORKER_ATTEMPT', { attempt: 1, status: 'COMPLETED', worker_id: 'AGENT-W1' }, '2026-01-01T00:02:00Z'),
    cp('WORKER_ATTEMPT', { attempt: 1, status: 'COMPLETED', worker_id: 'AGENT-OTHER' }, '2026-01-01T00:03:00Z'),
  ];
  const events = buildAgentActivity(w1, checkpoints, null);
  const texts = events.map(e => e.text);
  assert.ok(texts.some(t => t.indexOf('Attempt 1 completed') !== -1));
  assert.strictEqual(events.filter(e => e.text.indexOf('Attempt') !== -1).length, 1);
}

// 2) Reviewer correlation by reviewer_id, not worker_id: the Reviewer's own
//    pane must read its 'Review completed' line from reviewer_id matching,
//    and must never fabricate PASS when the real verdict is not PASS.
{
  const reviewer = agent('AGENT-R1', 'ManagerReview', 'COMPLETED', '2026-01-01T00:02:00Z', '2026-01-01T00:03:00Z');
  const checkpoints = [
    cp('REVIEW', { attempt: 1, reviewer_id: 'AGENT-R1', worker_id: 'AGENT-W1', verdict: 'BLOCKED' }, '2026-01-01T00:03:00Z'),
  ];
  const events = buildAgentActivity(reviewer, checkpoints, null);
  const reviewEvent = events.find(e => e.text.indexOf('Review completed') !== -1);
  assert.ok(reviewEvent, 'expected a Review completed entry');
  assert.strictEqual(reviewEvent.text, 'Review completed — BLOCKED');
  assert.strictEqual(reviewEvent.cls, 'bad');
  assert.ok(!events.some(e => e.text.indexOf('PASS') !== -1), 'must never show PASS for a BLOCKED verdict');
}

// 3) Multiple attempts (real RUN-0FCCAD6C shape): attempt 1's Worker/Reviewer
//    pair must only ever see attempt 1's events, never attempt 2's.
{
  const w1 = agent('AGENT-6AF4AD51', 'Worker', 'COMPLETED');
  const r1 = agent('AGENT-128EA02F', 'ManagerReview', 'COMPLETED');
  const checkpoints = [
    cp('WORKER_ATTEMPT', { attempt: 1, status: 'COMPLETED', worker_id: 'AGENT-6AF4AD51' }, '2026-01-01T00:01:00Z'),
    cp('REVIEW', { attempt: 1, reviewer_id: 'AGENT-128EA02F', worker_id: 'AGENT-6AF4AD51', verdict: 'RETRY' }, '2026-01-01T00:02:00Z'),
    cp('WORKER_ATTEMPT', { attempt: 2, status: 'COMPLETED', worker_id: 'AGENT-641BCD9F' }, '2026-01-01T00:03:00Z'),
    cp('REVIEW', { attempt: 2, reviewer_id: 'AGENT-6E96339B', worker_id: 'AGENT-641BCD9F', verdict: 'BLOCKED' }, '2026-01-01T00:04:00Z'),
  ];
  const w1Events = buildAgentActivity(w1, checkpoints, null);
  const r1Events = buildAgentActivity(r1, checkpoints, null);
  assert.strictEqual(w1Events.filter(e => e.text.indexOf('Attempt') !== -1).length, 1);
  assert.ok(w1Events.some(e => e.text.indexOf('Reviewed — RETRY') !== -1));
  assert.ok(!w1Events.some(e => e.text.indexOf('BLOCKED') !== -1), 'attempt 1 worker must not see attempt 2 verdict');
  assert.ok(r1Events.some(e => e.text === 'Review completed — RETRY'));
}

// 4) Event dedup / idempotence: the same inputs always produce the same
//    output (full rebuild every poll, never accumulated state).
{
  const w1 = agent('AGENT-W1', 'Worker', 'COMPLETED', '2026-01-01T00:00:00Z', '2026-01-01T00:05:00Z');
  const checkpoints = [cp('WORKER_ATTEMPT', { attempt: 1, status: 'COMPLETED', worker_id: 'AGENT-W1' }, '2026-01-01T00:02:00Z')];
  const first = buildAgentActivity(w1, checkpoints, null);
  const second = buildAgentActivity(w1, checkpoints, null);
  assert.deepStrictEqual(first, second);
}

// 5) Timestamp ordering: Started first, checkpoint events in their given
//    (already-ascending, per the checkpoints API) order, Finished last.
{
  const w1 = agent('AGENT-W1', 'Worker', 'COMPLETED', '2026-01-01T00:00:00Z', '2026-01-01T00:05:00Z');
  const checkpoints = [cp('WORKER_ATTEMPT', { attempt: 1, status: 'COMPLETED', worker_id: 'AGENT-W1' }, '2026-01-01T00:02:00Z')];
  const events = buildAgentActivity(w1, checkpoints, null);
  assert.strictEqual(events[0].text, 'Started');
  assert.strictEqual(events[events.length - 1].text, 'Finished — COMPLETED');
}

// 6) Missing event fields: an attempt number absent from the payload must
//    not crash -- renders an honest '?' rather than fabricating a number.
{
  const w1 = agent('AGENT-W1', 'Worker', 'RUNNING');
  const checkpoints = [cp('WORKER_ATTEMPT', { status: 'COMPLETED', worker_id: 'AGENT-W1' }, '2026-01-01T00:02:00Z')];
  const events = buildAgentActivity(w1, checkpoints, null);
  assert.ok(events.some(e => e.text === 'Attempt ? completed'));
}

// 7) No false activity entries: an agent with no started_at/finished_at and
//    no matching checkpoints gets an empty list (caller renders nothing),
//    never a fabricated placeholder row.
{
  const w1 = agent('AGENT-W1', 'Worker', 'CREATED');
  const events = buildAgentActivity(w1, [], null);
  assert.deepStrictEqual(events, []);
}

// 8) Failed run: the run's structured failure (already sanitized/bounded
//    server-side) appears on the exact agent it is attributed to, and only
//    that agent.
{
  const w1 = agent('AGENT-W1', 'Worker', 'FAILED', '2026-01-01T00:00:00Z', '2026-01-01T00:05:00Z');
  const w2 = agent('AGENT-W2', 'Worker', 'COMPLETED', '2026-01-01T00:00:00Z', '2026-01-01T00:05:00Z');
  const failure = { agent_id: 'AGENT-W1', message: 'npm ci failed: ENOENT' };
  const w1Events = buildAgentActivity(w1, [], failure);
  const w2Events = buildAgentActivity(w2, [], failure);
  assert.ok(w1Events.some(e => e.text === 'Failure: npm ci failed: ENOENT' && e.cls === 'bad'));
  assert.ok(!w2Events.some(e => e.text.indexOf('Failure') !== -1));
}

// 9) Historical compatibility: checkpoints entirely absent (undefined, as
//    an older/partial payload might pass) must not crash.
{
  const w1 = agent('AGENT-W1', 'Worker', 'COMPLETED', '2026-01-01T00:00:00Z', '2026-01-01T00:05:00Z');
  const events = buildAgentActivity(w1, undefined, undefined);
  assert.strictEqual(events.length, 2); // Started, Finished only
}

// 10) Bounded history per pane: more than the cap collapses to the most
//     recent entries (never thousands of rows for a long-running agent).
{
  const w1 = agent('AGENT-W1', 'Worker', 'RUNNING', '2026-01-01T00:00:00Z');
  const checkpoints = [];
  for (let i = 1; i <= 20; i++) {
    checkpoints.push(cp('WORKER_ATTEMPT', { attempt: i, status: 'COMPLETED', worker_id: 'AGENT-W1' }, '2026-01-01T00:0' + (i % 9) + ':00Z'));
  }
  const events = buildAgentActivity(w1, checkpoints, null);
  assert.ok(events.length <= 8, 'expected activity history to be bounded');
  assert.ok(events[events.length - 1].text.indexOf('Attempt 20') !== -1, 'bound must keep the most recent events');
}

console.log('OK');
"""

    _run_node_harness(functions, body)
