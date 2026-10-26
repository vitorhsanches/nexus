const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const devSource = fs.readFileSync(path.join(root, "nexus/web/static/js/dev.js"), "utf8");
const nexusTemplateSource = fs.readFileSync(path.join(root, "nexus/web/templates/nexus.html"), "utf8");

class Element {
  constructor(id) {
    this.id = id || ""; this.children = []; this.parentNode = null; this.attributes = {};
    this.listeners = {}; this.hidden = false; this.disabled = false; this.value = "";
    this.checked = false;
    this.textContent = ""; this.dataset = {}; this.className = ""; this.title = "";
    this.classList = {
      values: new Set(),
      add: (name) => this.classList.values.add(name),
      remove: (name) => this.classList.values.delete(name),
      contains: (name) => this.classList.values.has(name),
      toggle: (name, enabled) => enabled ? this.classList.values.add(name) : this.classList.values.delete(name),
    };
  }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key] || null; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(key, callback) { this.listeners[key] = callback; }
  dispatch(key, event = {}) { if (this.listeners[key]) this.listeners[key](event); }
  focus() { this.focused = true; }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  append(...children) { children.forEach((child) => this.appendChild(child)); }
  replaceChildren(...children) { this.children.forEach((child) => { child.parentNode = null; }); this.children = []; this.append(...children); }
  get innerText() { return this.textContent; }
}

function setup(options = {}) {
  const ids = [
    "dev-return", "dev-project-select", "dev-create-session", "dev-gate-banner",
    "dev-backend-status", "dev-status-text", "dev-project-note",
    "dev-session-list", "dev-session-detail", "dev-turn-result", "dev-changed-files", "dev-connection-status",
    "dev-review-refresh", "dev-review-summary", "dev-review-changed", "dev-review-files", "dev-review-patch",
    "dev-integration-gate", "dev-create-review", "dev-integration-preflight", "dev-reject-review", "dev-apply-confirm", "dev-apply-review", "dev-integration-summary", "dev-integration-message", "dev-integration-history-list",
    "dev-model-search", "dev-model-result-count", "dev-model-select", "dev-effort-select",
    "dev-prompt-input", "dev-run-button", "dev-stop-button",
    "dev-mission-description", "dev-mission-test-profile", "dev-mission-start", "dev-mission-stop", "dev-mission-progress",
    "dev-activity-status", "dev-activity-elapsed", "dev-activity-events",
    "dev-backend-select", "dev-approvals",
    "view-mode-badge",
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, new Element(id)]));
  const nav = [new Element("nav-overview"), new Element("nav-dev")]; nav[0].classList.add("active");
  const body = { dataset: { view: "overview" }, setAttribute(name, value) { if (name === "data-view") this.dataset.view = value; } };
  const document = {
    body, activeElement: new Element("prior-focus"),
    getElementById(id) { return elements[id] || null; },
    createElement() { return new Element(""); },
    createTextNode(text) { const node = new Element(""); node.textContent = text; node.isTextNode = true; return node; },
    querySelectorAll(selector) { return selector === ".nav-item" ? nav : []; },
    querySelector(selector) { return selector === ".nav-item.active" ? nav.find((item) => item.classList.contains("active")) || null : null; },
    addEventListener(event, callback) { if (event === "DOMContentLoaded") callback(); },
  };
  const calls = []; const eventSources = [];
  const backend = {
    projects: options.projects || [
      { id: "demo", name: "Demo", path: "/repo/demo", enabled: 1, dev_enabled: 1 },
      { id: "no-dev", name: "NoDev", path: "/repo/nodev", enabled: 1, dev_enabled: 0 },
    ],
    models: options.models || { models: [{ id: "cx/gpt-5.6-sol", name: "GPT-5.6 Sol", selectable: true, cost_status: "free", supported_efforts: ["low", "high"], default_effort: "high" }], default_model: "cx/gpt-5.6-sol", catalog_error: null },
    profiles: options.profiles || [{ id: "nexus-f36b", name: "Nexus autonomous mission tests", timeout_seconds: 300, source: "builtin" }],
    executionEnabled: Boolean(options.executionEnabled),
    executionStatusFails: Boolean(options.executionStatusFails),
    sessions: options.sessions || [], turns: new Map(), nextSession: 1, nextTurn: 1,
    missions: options.missions || [],
    createError: null, createStatus: 400,
    changedFiles: options.changedFiles || [],
    review: options.review || { base_sha: "a", head_sha: "a", review_id: "review-1", files: [] },
    reviewError: null,
    integrationEnabled: Boolean(options.integrationEnabled), integrationApplyFails: Boolean(options.integrationApplyFails), integrationReview: null,
    integrationPreflight: { ready: true, issues: [] }, integrationHistory: options.integrationHistory || [],
    reviewCreateError: options.reviewCreateError || null, reviewCreateStatus: options.reviewCreateStatus || 400,
    integrationHistoryError: options.integrationHistoryError || null, reviewCreateCount: 0,
  };
  backend.sessions.forEach((session) => { if (!backend.turns.has(session.session_id)) backend.turns.set(session.session_id, []); });
  function response(payload, status = 200) { return Promise.resolve({ ok: status >= 200 && status < 300, status, text: () => Promise.resolve(JSON.stringify(payload)) }); }
  const fetch = (url, opts = {}) => {
    const parsed = new URL(url, "http://nexus.test"); const method = opts.method || "GET";
    calls.push({ path: parsed.pathname + parsed.search, method, body: opts.body && JSON.parse(opts.body) });
    if (parsed.pathname === "/api/projects") return response({ projects: backend.projects });
    if (parsed.pathname === "/api/dev/models") return response(backend.models);
    if (parsed.pathname === "/api/dev/test-profiles") return response({ profiles: backend.profiles });
    if (parsed.pathname === "/api/dev/integration-status") return response({ integration_enabled: backend.integrationEnabled });
    if (parsed.pathname === "/api/dev/execution-status") {
      return backend.executionStatusFails ? response({ detail: { message: "status unavailable" } }, 500) : response({ execution_enabled: backend.executionEnabled });
    }
    if (parsed.pathname === "/api/dev/sessions" && method === "GET") {
      const projectId = parsed.searchParams.get("project_id");
      const sessions = projectId ? backend.sessions.filter((s) => s.project_id === projectId) : backend.sessions;
      return response({ sessions });
    }
    if (parsed.pathname === "/api/dev/sessions" && method === "POST") {
      if (backend.createError) return response({ detail: { code: "DEV_MODE_DISABLED", message: backend.createError } }, backend.createStatus);
      const body = JSON.parse(opts.body);
      const session = { session_id: "DEV-" + backend.nextSession++, project_id: body.project_id, backend: body.backend || "omniroute", status: "ACTIVE", branch: "nexus/dev/x", base_sha: "abc123", worktree_path: "/wt/x" };
      backend.sessions.unshift(session); backend.turns.set(session.session_id, []); return response(session);
    }
    const sessionMatch = parsed.pathname.match(/^\/api\/dev\/sessions\/([^/]+)$/);
    if (sessionMatch && method === "GET") {
      const id = decodeURIComponent(sessionMatch[1]); const session = backend.sessions.find((item) => item.session_id === id);
      if (!session) return response({ detail: { code: "SESSION_NOT_FOUND", message: "unknown" } }, 404);
      return response(session);
    }
    const turnsMatch = parsed.pathname.match(/^\/api\/dev\/sessions\/([^/]+)\/turns$/);
    if (turnsMatch && method === "GET") {
      const id = decodeURIComponent(turnsMatch[1]);
      return response({ turns: (backend.turns.get(id) || []) });
    }
    const changedFilesMatch = parsed.pathname.match(/^\/api\/dev\/sessions\/([^/]+)\/changed-files$/);
    if (changedFilesMatch && method === "GET") {
      return response({ files: backend.changedFiles });
    }
    const reviewMatch = parsed.pathname.match(/^\/api\/dev\/sessions\/([^/]+)\/diff$/);
    if (reviewMatch && method === "GET") return backend.reviewError ? response({ detail: { message: backend.reviewError } }, 500) : response(backend.review);
    if (parsed.pathname.match(/^\/api\/dev\/sessions\/[^/]+\/reviews$/) && method === "POST") {
      backend.reviewCreateCount++;
      if (backend.reviewCreateError) return response({ detail: { message: backend.reviewCreateError } }, backend.reviewCreateStatus);
      backend.integrationReview = { review_id: "f".repeat(64), dev_session_id: "DEV-1", project_id: "demo", base_sha: "a".repeat(40), target_head: "a".repeat(40), target_branch: "main", status: "REVIEWED", snapshot: { snapshot_hash: "b".repeat(64), diff: { review_id: backend.review.review_id, files: [{ path: "approved.txt", status: "untracked", patch: "+approved" }], additions: 1, deletions: 0 } } };
      backend.integrationHistory.unshift(backend.integrationReview); return response(backend.integrationReview);
    }
    if (parsed.pathname.match(/^\/api\/dev\/sessions\/[^/]+\/preflight$/) && method === "GET") return response(backend.integrationPreflight);
    if (parsed.pathname.match(/^\/api\/dev\/sessions\/[^/]+\/apply$/) && method === "POST") {
      if (!JSON.parse(opts.body).confirmed) return response({ detail: { message: "confirmation required" } }, 400);
      if (backend.integrationApplyFails) {
        backend.integrationReview.status = "APPLY_UNCERTAIN";
        backend.integrationReview.error_message = "Outcome uncertain; inspect destination state.";
        return response({ detail: { message: "Apply outcome uncertain" } }, 500);
      }
      backend.integrationReview.status = "APPLIED"; backend.integrationReview.result_sha = "c".repeat(40);
      return response({ review_id: backend.integrationReview.review_id, status: "APPLIED", commit_sha: "c".repeat(40), branch: "main" });
    }
    if (parsed.pathname.match(/^\/api\/dev\/sessions\/[^/]+\/reject$/) && method === "POST") {
      backend.integrationReview.status = "REJECTED"; return response({ review_id: backend.integrationReview.review_id, status: "REJECTED" });
    }
    if (parsed.pathname.match(/^\/api\/dev\/sessions\/[^/]+\/integrations$/) && method === "GET") return backend.integrationHistoryError ? response({ detail: { message: backend.integrationHistoryError } }, 503) : response({ integrations: backend.integrationHistory });
    const turnDetailMatch = parsed.pathname.match(/^\/api\/dev\/sessions\/([^/]+)\/turns\/([^/]+)$/);
    if (turnDetailMatch && method === "GET") {
      const id = decodeURIComponent(turnDetailMatch[1]); const turnId = decodeURIComponent(turnDetailMatch[2]);
      const turn = (backend.turns.get(id) || []).find((item) => item.turn_id === turnId);
      if (!turn) return response({ detail: { code: "TURN_NOT_FOUND", message: "unknown" } }, 404);
      return response(turn);
    }
    if (turnsMatch && method === "POST") {
      const id = decodeURIComponent(turnsMatch[1]);
      if (!backend.executionEnabled) return response({ detail: { code: "DEV_EXECUTION_DISABLED", message: "disabled" } }, 403);
      const body = JSON.parse(opts.body);
      const turn = { turn_id: "DEVTURN-" + backend.nextTurn++, dev_session_id: id, prompt: body.prompt, status: "PENDING", response_text: null, error_message: null };
      backend.turns.get(id).push(turn);
      return response(turn);
    }
    const missionsMatch = parsed.pathname.match(/^\/api\/dev\/sessions\/([^/]+)\/missions$/);
    if (missionsMatch && method === "GET") return response({ missions: backend.missions });
    if (missionsMatch && method === "POST") {
      const body = JSON.parse(opts.body);
      const mission = {
        mission_id: "DEVMISSION-1", dev_session_id: decodeURIComponent(missionsMatch[1]),
        description: body.description, requested_model: body.model, test_profile: body.test_profile,
        status: "READY_FOR_REVIEW", attempt_count: 1, tests: [{ attempt_number: 1, status: "PASSED" }],
        result: { human_approval_required: true, integration_applied: false }, error_code: null, error_message: null,
      };
      backend.missions.unshift(mission); return response(mission);
    }
    if (parsed.pathname.endsWith("/interrupt") && method === "POST") return response({ status: "INTERRUPT_REQUESTED" });
    const approvalsMatch = parsed.pathname.match(/^\/api\/dev\/sessions\/([^/]+)\/approvals$/);
    if (approvalsMatch && method === "GET") return response({ approvals: backend.approvals || [] });
    const decideMatch = parsed.pathname.match(/^\/api\/dev\/sessions\/([^/]+)\/approvals\/([^/]+)$/);
    if (decideMatch && method === "POST") {
      const decision = JSON.parse(opts.body).decision;
      const approval = (backend.approvals || []).find((item) => item.approval_id === decodeURIComponent(decideMatch[2]));
      if (approval) approval.status = decision === "accept" ? "APPROVED" : "DECLINED";
      return response(approval || {});
    }
    return response({ detail: { message: "Unexpected test route: " + parsed.pathname } }, 404);
  };
  class EventSource {
    constructor(url) { this.url = url; this.closed = false; this.readyState = EventSource.CONNECTING; eventSources.push(this); }
    close() { this.closed = true; this.readyState = EventSource.CLOSED; }
    emit(data, id) { this.readyState = EventSource.OPEN; if (this.onmessage) this.onmessage({ data: JSON.stringify(data), lastEventId: id || "" }); }
    // Simulates the browser itself giving up and closing the connection
    // (as opposed to a transient error where the browser keeps retrying).
    failPermanently() { this.readyState = EventSource.CLOSED; if (this.onerror) this.onerror(); }
    // Simulates a transient drop the browser is still auto-retrying.
    failTransiently() { this.readyState = EventSource.CONNECTING; if (this.onerror) this.onerror(); }
  }
  EventSource.CONNECTING = 0; EventSource.OPEN = 1; EventSource.CLOSED = 2;
  const window = {
    NexusMode: { mode: "overview" }, EventSource,
    location: { href: "http://nexus.test/nexus", pathname: "/nexus", search: "", hash: "" },
  };
  const context = {
    document, window, fetch, URL, URLSearchParams, console,
    setInterval() { return 1; }, clearInterval() {}, setTimeout(callback) { callback(); return 1; }, clearTimeout() {},
  };
  vm.runInNewContext(devSource, context);
  return { elements, body, nav, window, backend, calls, eventSources };
}

async function flush() { await new Promise((resolve) => setImmediate(resolve)); await new Promise((resolve) => setImmediate(resolve)); }

function allText(node) {
  if (!node.children.length) return node.textContent || "";
  return node.children.map(allText).join(" ");
}

test("nav entry and ws-dev section exist in the template", () => {
  assert.match(nexusTemplateSource, /id="nav-dev"[^>]*data-target="dev-view"/);
  assert.match(nexusTemplateSource, /id="ws-dev"/);
  assert.match(nexusTemplateSource, /id="dev-mission-start"/);
});

test("autonomous mission posts bounded model and trusted profile then stops for review", async () => {
  const ui = setup({ executionEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter();
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click");
  await flush();
  ui.elements["dev-mission-description"].value = "implement bounded feature";
  ui.elements["dev-mission-test-profile"].value = "nexus-f36b";
  ui.elements["dev-mission-test-profile"].dispatch("change");
  ui.elements["dev-mission-start"].dispatch("click");
  await flush();
  const call = ui.calls.find((item) => item.method === "POST" && item.path === "/api/dev/sessions/DEV-1/missions");
  assert.equal(call.body.description, "implement bounded feature");
  assert.deepEqual(call.body.allowed_models, ["cx/gpt-5.6-sol"]);
  assert.equal(call.body.test_profile, "nexus-f36b");
  assert.match(ui.elements["dev-mission-progress"].textContent, /READY_FOR_REVIEW/);
  assert.match(ui.elements["dev-mission-progress"].textContent, /human review/i);
  assert.equal(ui.elements["dev-mission-stop"].disabled, true);
});

test("entering Development Mode loads only dev_enabled projects and checks the execution gate", async () => {
  const ui = setup();
  await ui.window.NexusDev.enter();
  assert.equal(ui.body.dataset.view, "dev");
  assert.ok(ui.calls.some((call) => call.path === "/api/dev/execution-status"));
  assert.ok(ui.calls.some((call) => call.path === "/api/projects"));
  // Only the dev_enabled project is offered -- "no-dev" must never appear.
  const options = ui.elements["dev-project-select"].children.map((opt) => opt.value).filter(Boolean);
  assert.deepEqual(options, ["demo"]);
});

test("execution-disabled gate shows an explicit banner and keeps the Run control unusable even with a session", async () => {
  const ui = setup({ executionEnabled: false, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter();
  assert.match(ui.elements["dev-gate-banner"].textContent, /disabled/i);
  assert.equal(ui.elements["dev-gate-banner"].hidden, false);
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  // Select the only session rendered.
  ui.elements["dev-session-list"].children[0].dispatch("click");
  await flush();
  assert.equal(ui.elements["dev-run-button"].disabled, true, "Run must stay disabled while the server gate is off");
  assert.match(ui.elements["dev-run-button"].title, /disabled on this server/i);
});

test("execution-enabled gate shows a positive banner and allows Run once a session is ACTIVE", async () => {
  const ui = setup({ executionEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter();
  assert.match(ui.elements["dev-gate-banner"].textContent, /enabled/i);
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click");
  await flush();
  assert.equal(ui.elements["dev-run-button"].disabled, false);
});

test("creating a session calls the real API and selects it", async () => {
  const ui = setup({ executionEnabled: false });
  await ui.window.NexusDev.enter();
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-create-session"].dispatch("click");
  await flush();
  assert.ok(ui.calls.some((call) => call.method === "POST" && call.path === "/api/dev/sessions" && call.body.project_id === "demo"));
  assert.equal(ui.backend.sessions.length, 1);
  assert.match(ui.elements["dev-session-detail"].children.map((n) => n.children.map((c) => c.textContent).join(" ")).join(" "), /DEV-1/);
});

test("running a turn posts to the dev turns endpoint and streams events via SSE", async () => {
  const ui = setup({ executionEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter();
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click");
  await flush();
  ui.elements["dev-prompt-input"].value = "add a function";
  ui.elements["dev-run-button"].dispatch("click");
  await flush();
  assert.ok(ui.calls.some((call) => call.method === "POST" && call.path === "/api/dev/sessions/DEV-1/turns" && call.body.prompt === "add a function"));
  assert.equal(ui.eventSources.length, 1);
  assert.match(ui.eventSources[0].url, /\/api\/dev\/sessions\/DEV-1\/events\?turn_id=DEVTURN-1/);

  const stream = ui.eventSources[0];
  const turn = ui.backend.turns.get("DEV-1")[0];
  turn.status = "COMPLETED"; turn.response_text = "done";
  stream.emit({ type: "nexus/turnEnded" }, "1");
  await flush();
  assert.match(ui.elements["dev-turn-result"].textContent, /done/);
  assert.match(ui.elements["dev-activity-status"].textContent, /Completed/);
  assert.equal(ui.elements["dev-run-button"].disabled, false, "a finished turn must free the Run control again");
});

test("an SSE disconnect does not cancel the turn -- it only reconciles via the turn-detail endpoint", async () => {
  const ui = setup({ executionEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter();
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click");
  await flush();
  ui.elements["dev-prompt-input"].value = "task";
  ui.elements["dev-run-button"].dispatch("click");
  await flush();
  const stream = ui.eventSources[0];
  assert.equal(stream.closed, false);
  if (stream.onerror) stream.onerror();
  // No interrupt call was made -- a browser-side disconnect must never
  // reach the interrupt endpoint.
  assert.ok(!ui.calls.some((call) => call.path.endsWith("/interrupt")));
});

test("a transient SSE error shows Reconnecting without touching the execution status badge", async () => {
  const ui = setup({ executionEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter();
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click");
  await flush();
  ui.elements["dev-prompt-input"].value = "task";
  ui.elements["dev-run-button"].dispatch("click");
  await flush();
  assert.match(ui.elements["dev-activity-status"].textContent, /Running/);
  const stream = ui.eventSources[0];
  stream.failTransiently();
  assert.match(ui.elements["dev-connection-status"].textContent, /Reconnecting/i);
  // The execution badge must still say Running -- a dropped connection is
  // never presented as a Failed turn.
  assert.match(ui.elements["dev-activity-status"].textContent, /Running/);
});

test("a permanent SSE close falls back to polling and reconciles the real COMPLETED state without a manual refresh", async () => {
  const ui = setup({ executionEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter();
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click");
  await flush();
  ui.elements["dev-prompt-input"].value = "task";
  ui.elements["dev-run-button"].dispatch("click");
  await flush();
  const stream = ui.eventSources[0];
  // The backend finishes the turn while the stream is already dead --
  // this is the "conclusão ocorreu offline" scenario.
  const turn = ui.backend.turns.get("DEV-1")[0];
  turn.status = "COMPLETED"; turn.response_text = "done while disconnected";
  stream.failPermanently();
  assert.match(ui.elements["dev-connection-status"].textContent, /Connection lost/i);
  await flush(); await flush();
  assert.match(ui.elements["dev-turn-result"].textContent, /done while disconnected/);
  assert.match(ui.elements["dev-activity-status"].textContent, /Completed/);
});

test("the fallback poll never starts a new turn -- it only reads the existing one", async () => {
  const ui = setup({ executionEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter();
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click");
  await flush();
  ui.elements["dev-prompt-input"].value = "task";
  ui.elements["dev-run-button"].dispatch("click");
  await flush();
  const stream = ui.eventSources[0];
  stream.failPermanently();
  await flush(); await flush();
  assert.equal(ui.calls.filter((call) => call.method === "POST" && call.path.endsWith("/turns")).length, 1, "only the original Run click may have created a turn");
});

test("reconnecting (a live message arrives again) clears Connection lost and stops the fallback poll", async () => {
  const ui = setup({ executionEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter();
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click");
  await flush();
  ui.elements["dev-prompt-input"].value = "task";
  ui.elements["dev-run-button"].dispatch("click");
  await flush();
  const stream = ui.eventSources[0];
  stream.failTransiently();
  assert.match(ui.elements["dev-connection-status"].textContent, /Reconnecting/i);
  stream.emit({ method: "turn/started" }, "1");
  assert.match(ui.elements["dev-connection-status"].textContent, /Live/i);
});

test("switching sessions during an active turn closes the old stream -- no duplicate listeners", async () => {
  const ui = setup({
    executionEnabled: true,
    sessions: [
      { session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt1" },
      { session_id: "DEV-2", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-2", base_sha: "y", worktree_path: "/wt2" },
    ],
  });
  await ui.window.NexusDev.enter();
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  const sessionButtons = ui.elements["dev-session-list"].children;
  const first = sessionButtons.find((b) => b.textContent.includes("DEV-1"));
  const second = sessionButtons.find((b) => b.textContent.includes("DEV-2"));
  first.dispatch("click");
  await flush();
  ui.elements["dev-prompt-input"].value = "task";
  ui.elements["dev-run-button"].dispatch("click");
  await flush();
  const firstStream = ui.eventSources[0];
  assert.equal(firstStream.closed, false);
  second.dispatch("click");
  await flush();
  assert.equal(firstStream.closed, true, "the previous session's stream must be closed, not left open in the background");
});

test("leaving Development Mode (navigating to Overview) closes the active stream", async () => {
  const ui = setup({ executionEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter();
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click");
  await flush();
  ui.elements["dev-prompt-input"].value = "task";
  ui.elements["dev-run-button"].dispatch("click");
  await flush();
  const stream = ui.eventSources[0];
  assert.equal(stream.closed, false);
  ui.window.NexusDev.exit();
  assert.equal(stream.closed, true);
});

test("stop still works for a turn reconnected after a transient disconnect", async () => {
  const ui = setup({ executionEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter();
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click");
  await flush();
  ui.elements["dev-prompt-input"].value = "task";
  ui.elements["dev-run-button"].dispatch("click");
  await flush();
  const stream = ui.eventSources[0];
  stream.failTransiently();
  stream.emit({ method: "turn/started" }, "1");
  ui.elements["dev-stop-button"].dispatch("click");
  await flush();
  assert.ok(ui.calls.some((call) => call.method === "POST" && call.path.startsWith("/api/dev/sessions/DEV-1/interrupt")));
});

test("stop button calls the interrupt endpoint for the active turn", async () => {
  const ui = setup({ executionEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter();
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click");
  await flush();
  ui.elements["dev-prompt-input"].value = "task";
  ui.elements["dev-run-button"].dispatch("click");
  await flush();
  ui.elements["dev-stop-button"].dispatch("click");
  await flush();
  assert.ok(ui.calls.some((call) => call.method === "POST" && call.path.startsWith("/api/dev/sessions/DEV-1/interrupt")));
});

test("model search narrows the approved Development model list", async () => {
  const models = { models: Array.from({ length: 60 }, (_, i) => ({ id: "route-" + i, name: "Route " + i, selectable: true, supported_efforts: [] })), default_model: null };
  const ui = setup({ models });
  await ui.window.NexusDev.enter();
  assert.ok(ui.elements["dev-model-select"].children.length <= 51, "bounded result list (<=50 + auto option)");
  ui.elements["dev-model-search"].value = "route-5";
  ui.elements["dev-model-search"].dispatch("input");
  const values = ui.elements["dev-model-select"].children.map((o) => o.value).filter(Boolean);
  assert.ok(values.every((v) => v.includes("route-5")));
});

test("effort select only offers values reported by the backend for the selected model, never fabricated", async () => {
  const ui = setup();
  await ui.window.NexusDev.enter();
  ui.elements["dev-model-select"].value = "cx/gpt-5.6-sol"; ui.elements["dev-model-select"].dispatch("change");
  const options = ui.elements["dev-effort-select"].children.map((o) => o.value).filter(Boolean);
  assert.deepEqual(options, ["low", "high"]);
});

test("leaving and re-entering Development Mode restores the previous nav/view state on exit", async () => {
  const ui = setup();
  await ui.window.NexusDev.enter();
  assert.equal(ui.body.dataset.view, "dev");
  assert.equal(ui.nav.find((item) => item.id === "nav-dev").classList.contains("active"), true);
  ui.window.NexusDev.exit();
  assert.equal(ui.body.dataset.view, "overview");
  assert.equal(ui.window.NexusMode.mode, "overview");
});

test("execution status check failure is treated as disabled, never silently enabled", async () => {
  const ui = setup({ executionStatusFails: true });
  await ui.window.NexusDev.enter();
  assert.match(ui.elements["dev-gate-banner"].textContent, /could not confirm/i);
});

test("selecting a session shows its project authorization status", async () => {
  const ui = setup({ sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter();
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click");
  await flush();
  const detailText = ui.elements["dev-session-detail"].children.map((n) => n.children.map((c) => c.textContent).join(" ")).join(" ");
  assert.match(detailText, /Authorized/);
});

test("revoked project authorization is shown distinctly, not as a silent pass", async () => {
  const ui = setup({
    projects: [{ id: "demo", name: "Demo", path: "/repo/demo", enabled: 1, dev_enabled: 0 }],
    sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }],
  });
  await ui.window.NexusDev.enter();
  // The session exists even though "demo" is no longer in the dev_enabled
  // dropdown (authorization was revoked after session creation) -- select
  // it directly through the already-loaded (unfiltered) session list.
  const sessionButton = ui.elements["dev-session-list"].children.find((c) => c.textContent && c.textContent.includes("DEV-1"));
  assert.ok(sessionButton, "seeded session should be listed");
  sessionButton.dispatch("click");
  await flush();
  const detailText = ui.elements["dev-session-detail"].children.map((n) => n.children.map((c) => c.textContent).join(" ")).join(" ");
  assert.match(detailText, /NOT authorized/);
});

test("changed files are loaded and rendered after selecting a session", async () => {
  const ui = setup({
    sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }],
    changedFiles: [{ path: "src/calc.py", status: "modified" }, { path: "tests/test_calc.py", status: "added" }],
  });
  await ui.window.NexusDev.enter();
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click");
  await flush();
  assert.ok(ui.calls.some((call) => call.path === "/api/dev/sessions/DEV-1/changed-files"));
  assert.ok(ui.calls.some((call) => call.path === "/api/dev/sessions/DEV-1/diff"));
  const text = allText(ui.elements["dev-changed-files"]);
  assert.match(text, /src\/calc\.py/);
  assert.match(text, /tests\/test_calc\.py/);
});

test("diff review selects files and safely renders highlighted patch text", async () => {
  const ui = setup({
    sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }],
    review: { review_id: "review-a", additions: 1, deletions: 1, files: [{ path: "src/a.py", status: "modified", patch: "@@ -1 +1 @@\n-old\n+new" }] },
  });
  await ui.window.NexusDev.enter(); ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change"); await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click"); await flush();
  assert.match(allText(ui.elements["dev-review-files"]), /src\/a.py/);
  assert.match(allText(ui.elements["dev-review-patch"]), /old/);
  assert.match(allText(ui.elements["dev-review-patch"]), /new/);
  assert.match(ui.elements["dev-review-patch"].children[0].children[2].className, /dev-review-line-add/);
});

test("manual review refresh updates review content while preserving the review controls", async () => {
  const ui = setup({ sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter(); ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change"); await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click"); await flush();
  ui.backend.review = { review_id: "review-b", files: [{ path: "new.py", status: "untracked", patch: "+new\n" }] };
  ui.elements["dev-review-refresh"].dispatch("click"); await flush();
  assert.match(allText(ui.elements["dev-review-files"]), /new.py/);
  assert.match(ui.elements["dev-review-summary"].textContent, /Review refreshed\. Worktree changes detected/);
  assert.equal(ui.elements["dev-review-refresh"].disabled, false);
});

test("review refresh reports an unchanged diff and a genuinely empty diff", async () => {
  const ui = setup({
    sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }],
    review: { review_id: "same", files: [{ path: "same.py", status: "modified", patch: "+same" }] },
  });
  await ui.window.NexusDev.enter(); ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change"); await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click"); await flush();
  ui.elements["dev-review-refresh"].dispatch("click"); await flush();
  assert.match(ui.elements["dev-review-summary"].textContent, /No new changes since the previous refresh/);
  ui.backend.review = { review_id: "empty", files: [], additions: 0, deletions: 0 };
  ui.elements["dev-review-refresh"].dispatch("click"); await flush();
  assert.match(ui.elements["dev-review-summary"].textContent, /No changes to review/);
});

test("refresh without a selected session gives a clear explanation", async () => {
  const ui = setup();
  ui.elements["dev-review-refresh"].dispatch("click");
  assert.match(ui.elements["dev-review-summary"].textContent, /No session selected/);
});

test("Review changes without a selected session explains why creation cannot proceed", async () => {
  const ui = setup();
  ui.elements["dev-create-review"].dispatch("click");
  assert.match(ui.elements["dev-integration-message"].textContent, /No session selected/);
  assert.equal(ui.backend.reviewCreateCount, 0);
});

test("review refresh errors remain visible without clearing the last successful review", async () => {
  const ui = setup({
    sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }],
    review: { review_id: "review-a", files: [{ path: "kept.py", status: "modified", patch: "-old\n+new" }] },
  });
  await ui.window.NexusDev.enter(); ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change"); await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click"); await flush();
  ui.backend.reviewError = "diff unavailable";
  ui.elements["dev-review-refresh"].dispatch("click"); await flush();
  assert.match(ui.elements["dev-review-summary"].textContent, /Could not load.*diff unavailable/);
  assert.match(allText(ui.elements["dev-review-files"]), /kept.py/);
});

test("integration Apply stays disabled when the independent gate is off", async () => {
  const ui = setup({ sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter();
  assert.match(ui.elements["dev-integration-gate"].textContent, /OFF/);
  assert.equal(ui.elements["dev-apply-review"].disabled, true);
  assert.equal(ui.elements["dev-create-review"].disabled, true, "review actions are unavailable without a selected session");
  assert.match(ui.elements["dev-integration-summary"].textContent, /No session selected/);
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change"); await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click"); await flush();
  ui.elements["dev-create-review"].dispatch("click"); await flush();
  assert.match(ui.elements["dev-integration-summary"].textContent, /Integration disabled by policy/);
  assert.equal(ui.elements["dev-apply-review"].disabled, true);
});

test("Review changes creates and displays the exact persisted review ID", async () => {
  const ui = setup({ sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter(); ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change"); await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click"); await flush();
  ui.elements["dev-create-review"].dispatch("click"); await flush();
  const reviewId = "f".repeat(64);
  assert.ok(ui.calls.some((call) => call.method === "POST" && call.path === "/api/dev/sessions/DEV-1/reviews"));
  assert.match(ui.elements["dev-integration-message"].textContent, new RegExp("Review created and persisted.*" + reviewId));
  assert.match(ui.elements["dev-integration-summary"].textContent, new RegExp(reviewId));
});

test("rapid Review changes double click sends one creation request", async () => {
  const ui = setup({ sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter(); ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change"); await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click"); await flush();
  ui.elements["dev-create-review"].dispatch("click"); ui.elements["dev-create-review"].dispatch("click"); await flush();
  assert.equal(ui.backend.reviewCreateCount, 1);
});

test("HTTP review creation failure is explicit and preserves the prior persisted review", async () => {
  const prior = { review_id: "e".repeat(64), dev_session_id: "DEV-1", project_id: "demo", base_sha: "a", target_head: "a", target_branch: "main", status: "REVIEWED", snapshot: { diff: { review_id: "review-1", files: [] } } };
  const ui = setup({ integrationHistory: [prior], reviewCreateError: "turn is still active", sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter(); ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change"); await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click"); await flush();
  ui.elements["dev-create-review"].dispatch("click"); await flush();
  assert.match(ui.elements["dev-integration-message"].textContent, /Could not create review snapshot: turn is still active/);
  assert.match(ui.elements["dev-integration-summary"].textContent, new RegExp(prior.review_id));
});

test("ambiguous review creation stays blocked when persisted history cannot be checked", async () => {
  const ui = setup({ reviewCreateError: "server disconnected", reviewCreateStatus: 500, integrationHistoryError: "history unavailable", sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter(); ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change"); await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click"); await flush();
  ui.elements["dev-create-review"].dispatch("click"); await flush();
  assert.equal(ui.backend.reviewCreateCount, 1);
  assert.equal(ui.elements["dev-create-review"].disabled, true);
  assert.match(ui.elements["dev-integration-message"].textContent, /stays blocked to prevent a blind duplicate/);
});

test("persisted review is restored from history when an existing session is selected again", async () => {
  const ui = setup({ integrationEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter(); ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change"); await flush();
  const select = () => ui.elements["dev-session-list"].children[0].dispatch("click");
  select(); await flush();
  ui.elements["dev-create-review"].dispatch("click"); await flush();
  select(); await flush();
  assert.match(ui.elements["dev-integration-summary"].textContent, new RegExp("f".repeat(64)));
  assert.match(allText(ui.elements["dev-integration-history-list"]), /REVIEWED/);
});

test("reviewing a changed diff invalidates its old persisted preflight", async () => {
  const ui = setup({ integrationEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter(); ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change"); await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click"); await flush();
  ui.elements["dev-create-review"].dispatch("click"); await flush();
  ui.elements["dev-integration-preflight"].dispatch("click"); await flush();
  ui.backend.review = { review_id: "changed-snapshot", files: [{ path: "new.py", status: "untracked", patch: "+new" }] };
  ui.elements["dev-review-refresh"].dispatch("click"); await flush();
  assert.equal(ui.elements["dev-integration-preflight"].disabled, true);
  assert.equal(ui.elements["dev-apply-review"].disabled, true);
  assert.match(ui.elements["dev-integration-summary"].textContent, /Review outdated/);
});

test("preflight READY is explicit and still requires human confirmation", async () => {
  const ui = setup({ integrationEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter(); ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change"); await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click"); await flush();
  ui.elements["dev-create-review"].dispatch("click"); await flush();
  ui.elements["dev-integration-preflight"].dispatch("click"); await flush();
  assert.match(ui.elements["dev-integration-message"].textContent, /Preflight READY/);
  assert.equal(ui.elements["dev-apply-review"].disabled, true);
});

test("integration Apply requires a persisted review, ready preflight, and explicit confirmation", async () => {
  const ui = setup({ integrationEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter(); ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change"); await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click"); await flush();
  assert.equal(ui.elements["dev-create-review"].disabled, false, "active session enables review even with empty integration history");
  assert.equal(ui.elements["dev-integration-preflight"].disabled, true, "preflight stays disabled until a persisted review exists");
  ui.elements["dev-create-review"].dispatch("click"); await flush();
  assert.ok(ui.calls.some((call) => call.path === "/api/dev/sessions/DEV-1/reviews" && call.method === "POST"));
  ui.elements["dev-integration-preflight"].dispatch("click"); await flush();
  assert.ok(ui.calls.some((call) => call.path.includes("/preflight?review_id=")));
  assert.equal(ui.elements["dev-apply-review"].disabled, true);
  ui.elements["dev-apply-confirm"].checked = true; ui.elements["dev-apply-confirm"].dispatch("change");
  assert.equal(ui.elements["dev-apply-review"].disabled, false);
  ui.elements["dev-apply-review"].dispatch("click"); await flush();
  const apply = ui.calls.find((call) => call.path === "/api/dev/sessions/DEV-1/apply");
  assert.equal(apply.method, "POST"); assert.equal(apply.body.confirmed, true);
  assert.match(ui.elements["dev-integration-message"].textContent, /Integrated as commit/);
});

test("integration preflight block leaves Apply disabled and gives a conflict instruction", async () => {
  const ui = setup({ integrationEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  ui.backend.integrationPreflight = { ready: false, issues: [{ code: "TARGET_DIRTY", message: "Commit or move local changes first." }] };
  await ui.window.NexusDev.enter(); ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change"); await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click"); await flush();
  ui.elements["dev-create-review"].dispatch("click"); await flush(); ui.elements["dev-integration-preflight"].dispatch("click"); await flush();
  assert.equal(ui.elements["dev-apply-review"].disabled, true);
  assert.match(ui.elements["dev-integration-message"].textContent, /Commit or move local changes/);
});

test("failed or uncertain Apply clears stale readiness and disables repeat Apply", async () => {
  const ui = setup({ integrationEnabled: true, integrationApplyFails: true, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter(); ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change"); await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click"); await flush();
  assert.equal(ui.elements["dev-create-review"].disabled, false);
  ui.elements["dev-create-review"].dispatch("click"); await flush(); ui.elements["dev-integration-preflight"].dispatch("click"); await flush();
  ui.elements["dev-apply-confirm"].checked = true; ui.elements["dev-apply-confirm"].dispatch("change");
  assert.equal(ui.elements["dev-apply-review"].disabled, false);
  ui.elements["dev-apply-review"].dispatch("click"); await flush();
  assert.match(ui.elements["dev-integration-message"].textContent, /Apply failed/);
  assert.equal(ui.elements["dev-integration-preflight"].disabled, true);
  assert.equal(ui.elements["dev-apply-confirm"].disabled, true);
  assert.equal(ui.elements["dev-apply-review"].disabled, true);
  assert.match(allText(ui.elements["dev-integration-history-list"]), /APPLY_UNCERTAIN/);
});

test("reject proposal reports rejection without presenting Apply success", async () => {
  const ui = setup({ integrationEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter(); ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change"); await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click"); await flush();
  ui.elements["dev-create-review"].dispatch("click"); await flush(); ui.elements["dev-reject-review"].dispatch("click"); await flush();
  assert.equal(ui.backend.integrationReview.status, "REJECTED");
  assert.match(ui.elements["dev-integration-message"].textContent, /Proposal rejected/);
  assert.doesNotMatch(ui.elements["dev-integration-message"].textContent, /Integrated as commit/);
});

test("changed files refresh again once a turn finishes", async () => {
  const ui = setup({
    executionEnabled: true,
    sessions: [{ session_id: "DEV-1", project_id: "demo", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }],
    changedFiles: [],
  });
  await ui.window.NexusDev.enter();
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click");
  await flush();
  ui.backend.changedFiles = [{ path: "new_file.py", status: "added" }];
  ui.backend.review = { review_id: "review-after-turn", files: [{ path: "new_file.py", status: "untracked", patch: "+new" }] };
  ui.elements["dev-prompt-input"].value = "add a function";
  ui.elements["dev-run-button"].dispatch("click");
  await flush();
  const turn = ui.backend.turns.get("DEV-1")[0];
  turn.status = "COMPLETED"; turn.response_text = "done";
  ui.eventSources[0].emit({ type: "nexus/turnEnded" }, "1");
  await flush();
  const text = allText(ui.elements["dev-changed-files"]);
  assert.match(text, /new_file\.py/);
  assert.match(allText(ui.elements["dev-review-files"]), /new_file\.py/);
});

test("Codex Native is the default Development backend for models and new sessions", async () => {
  const ui = setup({ executionEnabled: true });
  await ui.window.NexusDev.enter();
  await flush();
  assert.ok(ui.calls.some((call) => call.path === "/api/dev/models?backend=codex_native"));
  assert.ok(!ui.calls.some((call) => call.path === "/api/dev/models?backend=omniroute"));
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-create-session"].dispatch("click");
  await flush();
  const body = ui.calls.find((call) => call.method === "POST" && call.path === "/api/dev/sessions").body;
  assert.equal(body.backend, "codex_native");
  assert.equal(body.requested_model, "cx/gpt-5.6-sol");
  assert.match(nexusTemplate(), /<option value="codex_native">Codex Native \(local login, no OmniRoute\)<\/option>/);
});

test("choosing OmniRoute reloads its catalog and creates a legacy session", async () => {
  const ui = setup({ executionEnabled: true });
  await ui.window.NexusDev.enter();
  await flush();
  ui.elements["dev-backend-select"].value = "omniroute"; ui.elements["dev-backend-select"].dispatch("change");
  await flush();
  assert.ok(ui.calls.some((call) => call.path === "/api/dev/models?backend=omniroute"));
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-create-session"].dispatch("click");
  await flush();
  const body = ui.calls.filter((call) => call.method === "POST" && call.path === "/api/dev/sessions").pop().body;
  assert.deepEqual(body, { project_id: "demo" });
});

test("a pending Codex approval is shown with its reason and decided only by an explicit click", async () => {
  const ui = setup({ executionEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", backend: "codex_native", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  ui.backend.approvals = [{ approval_id: "APPROVAL-1", dev_session_id: "DEV-1", turn_id: "DEVTURN-1", kind: "command", status: "PENDING",
    summary: { command: "python -m pytest -q", reason: "Sandbox blocked the pytest cache" } }];
  await ui.window.NexusDev.enter();
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click");
  await flush();
  const row = ui.elements["dev-approvals"].children[0];
  assert.match(row.children[0].textContent, /Command · PENDING · python -m pytest -q · reason: Sandbox blocked the pytest cache/);
  assert.ok(!ui.calls.some((call) => call.method === "POST" && /\/approvals\//.test(call.path)), "nothing is decided without a click");
  const approve = row.children.find((child) => child.textContent === "Approve once");
  assert.ok(row.children.find((child) => child.textContent === "Decline"));
  approve.dispatch("click");
  await flush();
  const decision = ui.calls.find((call) => call.method === "POST" && call.path === "/api/dev/sessions/DEV-1/approvals/APPROVAL-1");
  assert.deepEqual(decision.body, { decision: "accept" });
  assert.match(ui.elements["dev-approvals"].children[0].children[0].textContent, /APPROVED/);
});

test("command executions stream with their exit code and output", async () => {
  const ui = setup({ executionEnabled: true, sessions: [{ session_id: "DEV-1", project_id: "demo", backend: "codex_native", status: "ACTIVE", branch: "nexus/dev/DEV-1", base_sha: "x", worktree_path: "/wt" }] });
  await ui.window.NexusDev.enter();
  ui.elements["dev-project-select"].value = "demo"; ui.elements["dev-project-select"].dispatch("change");
  await flush();
  ui.elements["dev-session-list"].children[0].dispatch("click");
  await flush();
  ui.elements["dev-prompt-input"].value = "fix and test";
  ui.elements["dev-run-button"].dispatch("click");
  await flush();
  const stream = ui.eventSources[0];
  stream.emit({ method: "item/completed", params: { item: { type: "commandExecution", command: "python -m pytest -q", exitCode: 0, aggregatedOutput: "1 passed in 0.02s" } } }, "1");
  stream.emit({ method: "item/commandExecution/requestApproval", id: 3, params: {} }, "2");
  await flush();
  const lines = ui.elements["dev-activity-events"].children.map((child) => child.textContent);
  assert.ok(lines.some((line) => /commandExecution: python -m pytest -q → exit 0\n1 passed in 0.02s/.test(line)));
  assert.ok(lines.some((line) => /Approval requested/.test(line)));
  assert.ok(ui.calls.some((call) => call.path === "/api/dev/sessions/DEV-1/approvals"));
});

function nexusTemplate() {
  return require("node:fs").readFileSync(require("node:path").join(__dirname, "../../nexus/web/templates/nexus.html"), "utf8");
}
