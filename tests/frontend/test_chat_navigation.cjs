const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const chatSource = fs.readFileSync(path.join(root, "nexus/web/static/js/chat.js"), "utf8");
const chatCssSource = fs.readFileSync(path.join(root, "nexus/web/static/css/chat.css"), "utf8");
const nexusTemplateSource = fs.readFileSync(path.join(root, "nexus/web/templates/nexus.html"), "utf8");

class Element {
  constructor(id) {
    this.id = id || ""; this.children = []; this.parentNode = null; this.attributes = {};
    this.listeners = {}; this.hidden = false; this.disabled = false; this.value = "";
    this.textContent = ""; this.dataset = {}; this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 1000;
    this.className = ""; this.classList = {
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
  dispatch(key, event = {}) { if (this.listeners[key]) this.listeners[key](event); if (key === "change" && this.onchange) this.onchange(event); }
  focus() { this.focused = true; }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  append(...children) { children.forEach((child) => this.appendChild(child)); }
  replaceChildren(...children) { this.children.forEach((child) => { child.parentNode = null; }); this.children = []; this.append(...children); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((child) => child !== this); this.parentNode = null; }
  insertBefore(node, ref) {
    if (node.parentNode) node.parentNode.children = node.parentNode.children.filter((child) => child !== node);
    node.parentNode = this;
    const index = ref ? this.children.indexOf(ref) : -1;
    if (index === -1) this.children.push(node); else this.children.splice(index, 0, node);
    return node;
  }
  get firstChild() { return this.children.length ? this.children[0] : null; }
  querySelector(selector) {
    for (const child of this.children) {
      if (selector === ".chat-empty-state" && child.className === selector.slice(1)) return child;
      const nested = child.querySelector(selector); if (nested) return nested;
    }
    return null;
  }
  get innerText() { return this.textContent; }
}

function setup(options = {}) {
  const ids = [
    "chat-return", "chat-message-input", "chat-context-toggle", "chat-layout", "chat-backend-status",
    "chat-status-text", "chat-composer", "chat-transcript", "chat-empty-state", "chat-project-select",
    "chat-project-value", "chat-workspace-detail", "chat-session-detail", "chat-session-value", "chat-model-placeholder",
    "chat-composer-help", "chat-send-button", "chat-stop-button", "chat-new-conversation", "chat-session-groups",
    "chat-route-detail", "chat-session-state-disconnected", "chat-session-state-loading", "chat-session-state-empty",
    "chat-session-state-error", "view-mode-badge", "ws-chat", "top-project", "chat-load-older",
    "chat-model-select", "chat-model-error", "chat-backend-select",
    "chat-model-search", "chat-model-filter", "chat-model-result-count", "chat-effort-select", "chat-unknown-cost-confirm", "chat-model-favorite",
    "chat-activity-status", "chat-activity-model", "chat-activity-elapsed", "chat-activity-events",
    "chat-session-model-note",
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, new Element(id)]));
  const states = ["disconnected", "loading", "empty", "error"].map((value) => {
    const node = elements["chat-session-state-" + value]; node.setAttribute("data-session-state", value); return node;
  });
  const nav = [new Element("nav-overview"), new Element("nav-chat")]; nav[0].classList.add("active");
  const run = new Element("ws-run"); const program = new Element("ws-program");
  const body = { dataset: { view: "detail" }, setAttribute(name, value) { if (name === "data-view") this.dataset.view = value; } };
  const document = {
    body, activeElement: new Element("prior-focus"),
    getElementById(id) { return elements[id] || null; },
    createElement() { return new Element(""); },
    querySelectorAll(selector) { return selector === "[data-session-state]" ? states : selector === ".nav-item" ? nav : []; },
    querySelector(selector) { return selector === ".nav-item.active" ? nav.find((item) => item.classList.contains("active")) || null : null; },
  };
  const calls = []; const eventSources = [];
  const backend = {
    projects: [{ id: "demo", name: "Demo", path: "/repo/demo", enabled: 1 }],
    models: options.models || { models: [{ id: "cx/gpt-5.6-sol", name: "GPT-5.6 Sol", selectable: true, availability: "omniroute-listed-unverified", cost_status: "unknown", supported_efforts: ["low", "high"], default_effort: "high" }, { id: "oc/mimo-v2.5-free", name: "MiMo", selectable: true, availability: "omniroute-listed-unverified", cost_status: "unknown", supported_efforts: [] }], default_model: "cx/gpt-5.6-sol" },
    modelsFail: Boolean(options.modelsFail),
    sessions: options.sessions || [], turns: new Map(), createError: null, createStatus: 400, sessionsListFail: false, nextSessionsResponse: null, detailFailures: new Set(), nextSession: 1, networkFailAfterRecording: false, nextTurn: 1,
    historyPages: new Map(), historyErrors: new Set(),
    // session_id -> true while that session's active turn's own
    // App-Server process still legitimately refuses a concurrent
    // thread_resume for history (HTTP 409 HISTORY_UNAVAILABLE) -- this is
    // unconditional (happens at thread_resume time, regardless of query
    // params), matching the real backend; tests clear the flag to
    // simulate the turn finishing and the conflict going away.
    historyThreadBusy: new Set(),
  };
  function response(payload, status = 200) { return Promise.resolve({ ok: status >= 200 && status < 300, status, text: () => Promise.resolve(JSON.stringify(payload)) }); }
  const fetch = (url, options = {}) => {
    const parsed = new URL(url, "http://nexus.test"); const method = options.method || "GET";
    calls.push({ path: parsed.pathname + parsed.search, method, body: options.body && JSON.parse(options.body) });
    if (parsed.pathname === "/api/projects") return response({ projects: backend.projects });
    if (parsed.pathname === "/api/chat/models") return backend.modelsFail ? response({ detail: { message: "model list unavailable" } }, 503) : response(backend.models);
    if (parsed.pathname === "/api/chat/sessions" && method === "GET") {
      if (backend.nextSessionsResponse) { const pending = backend.nextSessionsResponse; backend.nextSessionsResponse = null; return pending; }
      return backend.sessionsListFail ? response({ detail: { code: "SESSION_LIST_UNAVAILABLE", message: "Session list temporarily unavailable" } }, 503) : response({ sessions: backend.sessions });
    }
    if (parsed.pathname === "/api/chat/sessions" && method === "POST") {
      if (backend.createError) return response({ detail: { code: "PROJECT_DISABLED", message: backend.createError } }, backend.createStatus);
      const createBody = JSON.parse(options.body);
      const project = createBody.project_id;
      const session = { session_id: "CHAT-" + backend.nextSession++, project_id: project, status: "CREATED", model: null, requested_model: createBody.model, requested_effort: createBody.effort, provider: null, turns: [] };
      backend.sessions.unshift(session); backend.turns.set(session.session_id, []); return response(session);
    }
    const sessionMatch = parsed.pathname.match(/^\/api\/chat\/sessions\/([^/]+)$/);
    if (sessionMatch && method === "GET") {
      const id = decodeURIComponent(sessionMatch[1]); const session = backend.sessions.find((item) => item.session_id === id);
      if (backend.detailFailures.has(id)) return response({ detail: { code: "SESSION_DETAIL_UNAVAILABLE", message: "Session detail temporarily unavailable" } }, 503);
      if (!session) return response({ detail: { code: "SESSION_NOT_FOUND", message: "Unknown session" } }, 404);
      return response({ ...session, turns: (backend.turns.get(id) || []).slice().reverse() });
    }
    const turnsMatch = parsed.pathname.match(/^\/api\/chat\/sessions\/([^/]+)\/turns$/);
    if (turnsMatch && method === "POST") {
      const body = JSON.parse(options.body); const id = decodeURIComponent(turnsMatch[1]);
      const turn = { turn_id: "TURN-" + backend.nextTurn++, session_id: id, idempotency_key: body.idempotency_key, status: "PENDING", response_text: null, error_message: null };
      backend.turns.get(id).unshift(turn);
      if (backend.networkFailAfterRecording) { backend.networkFailAfterRecording = false; return Promise.reject(new Error("connection reset")); }
      return response(turn);
    }
    if (parsed.pathname.endsWith("/interrupt") && method === "POST") return response({ status: "INTERRUPT_REQUESTED" });
    const historyMatch = parsed.pathname.match(/^\/api\/chat\/sessions\/([^/]+)\/history$/);
    if (historyMatch && method === "GET") {
      const id = decodeURIComponent(historyMatch[1]);
      if (backend.historyErrors.has(id)) return response({ detail: { message: "History backend unavailable" } }, 404);
      if (backend.historyThreadBusy.has(id)) {
        return response({ detail: { code: "HISTORY_UNAVAILABLE", message: "The persisted Codex thread could not be resumed for history retrieval." } }, 409);
      }
      // The real history API never accepts a Nexus-native turn_id (it is
      // Codex-native only) -- chat.js must never send one. Fail loudly if
      // it ever does, rather than silently matching it like a real Codex
      // turn id would.
      if (parsed.searchParams.get("turn_id")) {
        return response({ detail: { code: "TURN_NOT_FOUND", message: "Turn not found in this session's Codex thread." } }, 404);
      }
      const pages = backend.historyPages.get(id) || [{ items: [], next_cursor: null, source: "codex_thread" }];
      const cursor = parsed.searchParams.get("cursor");
      const pageIndex = cursor ? Number(cursor) : 0;
      const page = pages[pageIndex] || { items: [], next_cursor: null, source: "codex_thread" };
      return response({ session_id: id, items: page.items, turns: page.turns || [], next_cursor: page.next_cursor, source: page.source || "codex_thread" });
    }
    return response({ detail: { message: "Unexpected test route: " + parsed.pathname } }, 404);
  };
  class EventSource {
    constructor(url) { this.url = url; this.closed = false; eventSources.push(this); }
    close() { this.closed = true; }
    emit(data, id) { if (this.onmessage) this.onmessage({ data: JSON.stringify(data), lastEventId: id || "" }); }
  }
  const window = {
    NexusMode: { mode: "run" }, EventSource, crypto: { randomUUID: () => "key-123" },
    location: { href: "http://nexus.test/nexus", pathname: "/nexus", search: "", hash: "" },
    history: { replaceState(_a, _b, url) { window.location.href = "http://nexus.test" + url; window.location.search = new URL(url, "http://nexus.test").search; } },
  };
  const context = {
    document, window, fetch, URL, URLSearchParams, console,
    requestAnimationFrame(callback) { callback(); return 1; }, cancelAnimationFrame() {},
    setInterval() { return 1; }, clearInterval() {},
    setTimeout(callback) { callback(); return 1; },
  };
  elements["chat-unknown-cost-confirm"].checked = true;
  vm.runInNewContext(chatSource, context);
  return { elements, body, nav, run, program, window, backend, calls, eventSources };
}

test("project selection, real session creation, reopen and URL recovery use API contracts", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  assert.equal(ui.elements["chat-project-select"].children.length, 2);
  ui.elements["chat-project-select"].value = "demo";
  ui.elements["chat-project-select"].dispatch("change");
  assert.equal(ui.elements["chat-new-conversation"].disabled, false, "loaded model catalog and selected project leave plus enabled");
  ui.elements["chat-new-conversation"].dispatch("click");
  await new Promise((resolve) => setImmediate(resolve)); await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.backend.sessions.length, 1);
  assert.equal(ui.elements["chat-message-input"].disabled, false);
  assert.ok(ui.calls.some((call) => call.path === "/api/chat/sessions" && call.method === "POST"));
  assert.match(ui.window.location.href, /chat_session=CHAT-1/);
  assert.equal(ui.elements["chat-session-value"].textContent, "CHAT-1 · CREATED");
  ui.elements["chat-message-input"].value = "first message";
  let prevented = false;
  ui.elements["chat-message-input"].dispatch("keydown", { key: "Enter", preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.calls.filter((call) => call.method === "POST" && call.path.endsWith("/turns")).length, 1);
});

test("new-session model choice is retained across composer updates and is separate from an opened session", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  const picker = ui.elements["chat-model-select"];
  picker.value = "oc/mimo-v2.5-free"; picker.dispatch("change");
  ui.backend.sessions = [{ session_id: "CHAT-ORIGINAL", project_id: "demo", status: "ACTIVE", requested_model: "cx/gpt-5.6-sol", model: "cx/gpt-5.6-sol", provider: "omniroute" }];
  ui.backend.turns.set("CHAT-ORIGINAL", []);
  await ui.window.NexusChat.selectSession("CHAT-ORIGINAL");
  assert.equal(picker.value, "oc/mimo-v2.5-free", "composer state must not overwrite the new-session draft");
  assert.match(ui.elements["chat-model-placeholder"].textContent, /cx\/gpt-5.6-sol/, "opened conversation model remains visible");
  assert.match(ui.elements["chat-session-model-note"].textContent, /fixed/i);
  picker.value = "oc/mimo-v2.5-free"; picker.dispatch("change");
  ui.elements["chat-project-select"].value = "demo"; ui.elements["chat-project-select"].dispatch("change");
  ui.elements["chat-new-conversation"].dispatch("click");
  await new Promise((resolve) => setImmediate(resolve)); await new Promise((resolve) => setImmediate(resolve));
  const creation = ui.calls.find((call) => call.method === "POST" && call.path === "/api/chat/sessions").body;
  assert.equal(creation.project_id, "demo");
  assert.equal(creation.model, "oc/mimo-v2.5-free");
  assert.equal(creation.effort, null);
  assert.match(ui.elements["chat-session-value"].textContent, /CHAT-1/);
});

test("supported effort is sent with the exact selected model and activated in the new session", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.elements["chat-project-select"].value = "demo"; ui.elements["chat-project-select"].dispatch("change");
  ui.elements["chat-model-select"].value = "cx/gpt-5.6-sol"; ui.elements["chat-model-select"].dispatch("change");
  ui.elements["chat-effort-select"].value = "low"; ui.elements["chat-effort-select"].dispatch("change");
  ui.elements["chat-new-conversation"].dispatch("click");
  await new Promise((resolve) => setImmediate(resolve)); await new Promise((resolve) => setImmediate(resolve));
  const body = ui.calls.find((call) => call.method === "POST" && call.path === "/api/chat/sessions").body;
  assert.equal(body.project_id, "demo");
  assert.equal(body.model, "cx/gpt-5.6-sol");
  assert.equal(body.effort, "low");
  assert.match(ui.elements["chat-session-value"].textContent, /CHAT-1/);
  assert.match(ui.elements["chat-model-placeholder"].textContent, /effort low/);
});

test("rapid double click on plus creates one session and selects it", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.elements["chat-project-select"].value = "demo"; ui.elements["chat-project-select"].dispatch("change");
  ui.elements["chat-new-conversation"].dispatch("click");
  ui.elements["chat-new-conversation"].dispatch("click");
  await new Promise((resolve) => setImmediate(resolve)); await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.calls.filter((call) => call.method === "POST" && call.path === "/api/chat/sessions").length, 1);
  assert.equal(ui.backend.sessions.length, 1);
  assert.equal(ui.elements["chat-session-value"].textContent, "CHAT-1 · CREATED");
});

test("disabled plus explains that a project must be selected", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  assert.equal(ui.elements["chat-new-conversation"].disabled, true);
  assert.match(ui.elements["chat-new-conversation"].title, /select a project/i);
});

test("POST response selects and displays the session even if list refresh fails", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.elements["chat-project-select"].value = "demo"; ui.elements["chat-project-select"].dispatch("change");
  ui.backend.sessionsListFail = true;
  ui.backend.detailFailures.add("CHAT-1");
  ui.elements["chat-new-conversation"].dispatch("click");
  await new Promise((resolve) => setImmediate(resolve)); await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.elements["chat-session-value"].textContent, "CHAT-1 · CREATED");
  assert.match(ui.elements["chat-session-groups"].children.map((section) => section.children.map((child) => child.textContent).join(" ")).join(" "), /CHAT-1/);
  assert.equal(ui.calls.filter((call) => call.method === "GET" && call.path === "/api/chat/sessions/CHAT-1").length, 0, "session selection must use the persisted POST response");
});

test("a stale in-flight session list cannot hide a newly created session", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.elements["chat-project-select"].value = "demo"; ui.elements["chat-project-select"].dispatch("change");
  let resolveStaleList;
  ui.backend.nextSessionsResponse = new Promise((resolve) => { resolveStaleList = resolve; });
  const staleRefresh = ui.window.NexusChat.loadSessions();
  ui.elements["chat-new-conversation"].dispatch("click");
  await new Promise((resolve) => setImmediate(resolve)); await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.elements["chat-session-value"].textContent, "CHAT-1 · CREATED");
  resolveStaleList({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ sessions: [] })) });
  await staleRefresh;
  assert.equal(ui.elements["chat-session-value"].textContent, "CHAT-1 · CREATED");
  assert.match(ui.elements["chat-session-groups"].children.map((section) => section.children.map((child) => child.textContent).join(" ")).join(" "), /CHAT-1/);
});

test("plus button keeps a consistent hit area and centers its glyph", () => {
  const buttonRule = chatCssSource.match(/\.chat-new-button\s*\{([^}]+)\}/);
  assert.ok(buttonRule, "plus button CSS rule exists");
  for (const declaration of ["display: inline-flex", "align-items: center", "justify-content: center", "box-sizing: border-box", "padding: 0", "width: 28px", "height: 28px"]) {
    assert.ok(buttonRule[1].includes(declaration), "missing centered hit-area declaration: " + declaration);
  }
  assert.match(nexusTemplateSource, /id="chat-new-conversation"[^>]*>\+/s, "button uses the plain plus glyph");
});

test("sessions still load and remain navigable when model discovery fails", async () => {
  const ui = setup({ modelsFail: true, sessions: [{ session_id: "CHAT-LEGACY", project_id: "demo", status: "ACTIVE", model: "cc/claude-sonnet-5-low" }] });
  ui.backend.turns.set("CHAT-LEGACY", []);
  await ui.window.NexusChat.ready;
  assert.ok(ui.backend.sessions.length);
  assert.match(ui.elements["chat-model-error"].textContent, /existing sessions remain accessible/i);
  await ui.window.NexusChat.selectSession("CHAT-LEGACY");
  assert.match(ui.elements["chat-session-value"].textContent, /CHAT-LEGACY/);
  ui.elements["chat-project-select"].value = "demo"; ui.elements["chat-project-select"].dispatch("change");
  ui.elements["chat-new-conversation"].dispatch("click");
  assert.equal(ui.calls.filter((call) => call.method === "POST" && call.path === "/api/chat/sessions").length, 0, "must not create with empty/unloaded model");
});

test("empty model catalog blocks creation with a nearby explanation", async () => {
  const ui = setup({ models: { models: [], default_model: null } }); await ui.window.NexusChat.ready;
  ui.elements["chat-project-select"].value = "demo"; ui.elements["chat-project-select"].dispatch("change");
  ui.elements["chat-new-conversation"].dispatch("click");
  assert.equal(ui.calls.filter((call) => call.method === "POST" && call.path === "/api/chat/sessions").length, 0);
  assert.match(ui.elements["chat-model-error"].textContent, /model choices are unavailable/i);
});

test("unknown-cost model selection requires explicit confirmation", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.elements["chat-unknown-cost-confirm"].checked = false;
  ui.elements["chat-project-select"].value = "demo"; ui.elements["chat-project-select"].dispatch("change");
  ui.elements["chat-new-conversation"].dispatch("click");
  assert.equal(ui.calls.filter((call) => call.method === "POST" && call.path === "/api/chat/sessions").length, 0);
  assert.match(ui.elements["chat-model-error"].textContent, /unknown cost/i);
});

test("creation validation errors preserve the previous conversation and show API code", async () => {
  const ui = setup({ sessions: [{ session_id: "CHAT-OLD", project_id: "demo", status: "ACTIVE", requested_model: "cx/gpt-5.6-sol", model: "cx/gpt-5.6-sol" }] });
  ui.backend.turns.set("CHAT-OLD", []); await ui.window.NexusChat.ready;
  await ui.window.NexusChat.selectSession("CHAT-OLD");
  ui.backend.createError = "Project is disabled";
  ui.elements["chat-new-conversation"].dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.elements["chat-session-value"].textContent, "CHAT-OLD · ACTIVE");
  assert.match(ui.elements["chat-model-error"].textContent, /PROJECT_DISABLED: Project is disabled/);
  assert.match(ui.elements["chat-status-text"].textContent, /Project is disabled/);
});

test("server error marks creation uncertain and blocks duplicate retries", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.elements["chat-project-select"].value = "demo"; ui.elements["chat-project-select"].dispatch("change");
  ui.backend.createError = "temporary failure"; ui.backend.createStatus = 503;
  ui.elements["chat-new-conversation"].dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));
  ui.elements["chat-new-conversation"].dispatch("click");
  assert.equal(ui.calls.filter((call) => call.method === "POST" && call.path === "/api/chat/sessions").length, 1);
  assert.equal(ui.elements["chat-new-conversation"].disabled, true);
  assert.match(ui.elements["chat-status-text"].textContent, /may have created it/i);
});

test("large OmniRoute catalog stays bounded and search preserves exact route IDs", async () => {
  const models = Array.from({ length: 340 }, (_, index) => ({
    id: index === 339 ? "cx/gpt-5.6-sol-high" : "omni/route-" + String(index).padStart(3, "0"),
    name: index === 339 ? "cx/gpt-5.6-sol-high" : "omni/route-" + String(index).padStart(3, "0"),
    selectable: true, availability: "omniroute-listed-unverified", cost_status: "unknown", supported_efforts: [],
  }));
  const ui = setup({ models: { models, default_model: null } }); await ui.window.NexusChat.ready;
  assert.ok(ui.elements["chat-model-select"].children.length <= 50);
  ui.elements["chat-model-search"].value = "cx/gpt-5.6-sol-high";
  ui.elements["chat-model-search"].dispatch("input");
  assert.deepEqual(ui.elements["chat-model-select"].children.map((option) => option.value), ["cx/gpt-5.6-sol-high"]);
});

test("Enter sends, Shift+Enter inserts a newline, and IME composition does not submit", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-KEYS", project_id: "demo", status: "ACTIVE", requested_model: "cx/gpt-5.6-sol", model: "cx/gpt-5.6-sol" }];
  ui.backend.turns.set("CHAT-KEYS", []); await ui.window.NexusChat.selectSession("CHAT-KEYS");
  const input = ui.elements["chat-message-input"];
  input.value = "hello";
  let prevented = false;
  input.dispatch("keydown", { key: "Enter", preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.calls.filter((call) => call.path.endsWith("/turns") && call.method === "POST").length, 1);
  const shift = { key: "Enter", shiftKey: true, preventDefault() { throw new Error("Shift+Enter should preserve newline behavior"); } };
  input.dispatch("keydown", shift);
  const composing = { key: "Enter", isComposing: true, preventDefault() { throw new Error("IME composition should not submit"); } };
  input.dispatch("keydown", composing);
});

test("two real turn submissions preserve text, idempotency keys and stream actual output", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.elements["chat-project-select"].value = "demo"; ui.elements["chat-project-select"].dispatch("change");
  ui.elements["chat-new-conversation"].dispatch("click");
  await new Promise((resolve) => setImmediate(resolve)); await new Promise((resolve) => setImmediate(resolve));
  const input = ui.elements["chat-message-input"];
  input.value = "Return PONG"; ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(ui.calls.filter((call) => call.method === "POST" && call.path.endsWith("/turns")).map((call) => call.body), [{ text: "Return PONG", idempotency_key: "key-123" }]);
  const stream = ui.eventSources[0];
  stream.emit({ method: "item/agentMessage/delta", params: { delta: "PONG" } }, "1");
  stream.emit({ method: "item/agentMessage/delta", params: { delta: "PONG" } }, "1");
  const assistant = ui.elements["chat-transcript"].children.find((node) => node.className.includes("chat-message-assistant"));
  assert.ok(assistant, JSON.stringify(ui.elements["chat-transcript"].children.map((node) => ({ className: node.className, textContent: node.textContent }))));
  assert.equal(assistant.children[1].textContent, "PONG");
  stream.emit({ method: "item/completed", params: { item: { id: "msg-1", type: "agentMessage", text: "PONG" } } }, "2");
  const firstTurn = ui.backend.turns.get("CHAT-1")[0]; firstTurn.status = "COMPLETED"; firstTurn.response_text = "PONG";
  stream.emit({ method: "turn/completed", params: { turn: { id: "codex-turn-1", status: "completed" } } }, "3");
  await new Promise((resolve) => setImmediate(resolve));
  input.value = "What did I ask you to return?"; ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.backend.turns.get("CHAT-1").length, 2);
  assert.equal(ui.calls.filter((call) => call.method === "POST" && call.path.endsWith("/turns")).length, 2);
  assert.equal(ui.calls.find((call) => call.path.endsWith("/turns") && call.method === "POST").body.text, "Return PONG");
});

test("execution activity panel reflects backend lifecycle and reported tool paths", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-ACTIVITY", project_id: "demo", status: "ACTIVE", requested_model: "cx/gpt-5.6-sol", model: "cx/gpt-5.6-sol" }];
  ui.backend.turns.set("CHAT-ACTIVITY", []); await ui.window.NexusChat.selectSession("CHAT-ACTIVITY");
  ui.elements["chat-message-input"].value = "inspect this"; ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const stream = ui.eventSources[0];
  stream.emit({ method: "turn/started", params: { turn: { status: "inProgress" } } }, "1");
  stream.emit({ method: "item/started", params: { item: { id: "tool-path", type: "commandExecution", status: "running", path: "src/main.py" } } }, "2");
  assert.match(ui.elements["chat-activity-status"].textContent, /Running/i);
  assert.match(ui.elements["chat-activity-model"].textContent, /cx\/gpt-5.6-sol/);
  assert.match(ui.elements["chat-activity-events"].children.map((item) => item.textContent).join(" "), /src\/main.py/);
});

test("execution status shows the four canonical states distinctly and keeps the final response accessible", async () => {
  const completedUi = setup(); await completedUi.window.NexusChat.ready;
  completedUi.backend.sessions = [{ session_id: "CHAT-STATE-OK", project_id: "demo", status: "ACTIVE", requested_model: "cx/gpt-5.6-sol", model: "cx/gpt-5.6-sol" }];
  completedUi.backend.turns.set("CHAT-STATE-OK", []); await completedUi.window.NexusChat.selectSession("CHAT-STATE-OK");
  completedUi.elements["chat-message-input"].value = "done soon"; completedUi.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const okTurn = completedUi.backend.turns.get("CHAT-STATE-OK")[0];
  okTurn.status = "COMPLETED"; okTurn.response_text = "all good";
  completedUi.eventSources[0].emit({ type: "nexus/turnEnded" }, "");
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(completedUi.elements["chat-activity-status"].textContent, /^Completed$/);
  assert.equal(completedUi.elements["chat-activity-status"].className, "chat-activity-status chat-activity-status-completed");
  // The final response must still be visible and reachable in the transcript.
  const okAssistant = completedUi.elements["chat-transcript"].children.find((node) => node.className.includes("chat-message-assistant"));
  assert.ok(okAssistant && okAssistant.children[1].textContent === "all good");

  const failedUi = setup(); await failedUi.window.NexusChat.ready;
  failedUi.backend.sessions = [{ session_id: "CHAT-STATE-FAIL", project_id: "demo", status: "ACTIVE", requested_model: "cx/gpt-5.6-sol", model: "cx/gpt-5.6-sol" }];
  failedUi.backend.turns.set("CHAT-STATE-FAIL", []); await failedUi.window.NexusChat.selectSession("CHAT-STATE-FAIL");
  failedUi.elements["chat-message-input"].value = "will fail"; failedUi.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const failTurn = failedUi.backend.turns.get("CHAT-STATE-FAIL")[0];
  failTurn.status = "FAILED"; failTurn.error_message = "boom";
  failedUi.eventSources[0].emit({ type: "nexus/turnEnded" }, "");
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(failedUi.elements["chat-activity-status"].textContent, /^Failed$/);
  assert.equal(failedUi.elements["chat-activity-status"].className, "chat-activity-status chat-activity-status-failed");

  const interruptedUi = setup(); await interruptedUi.window.NexusChat.ready;
  interruptedUi.backend.sessions = [{ session_id: "CHAT-STATE-INT", project_id: "demo", status: "ACTIVE", requested_model: "cx/gpt-5.6-sol", model: "cx/gpt-5.6-sol" }];
  interruptedUi.backend.turns.set("CHAT-STATE-INT", []); await interruptedUi.window.NexusChat.selectSession("CHAT-STATE-INT");
  interruptedUi.elements["chat-message-input"].value = "stop me"; interruptedUi.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const intTurn = interruptedUi.backend.turns.get("CHAT-STATE-INT")[0];
  intTurn.status = "CANCELLED";
  interruptedUi.eventSources[0].emit({ type: "nexus/turnEnded" }, "");
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(interruptedUi.elements["chat-activity-status"].textContent, /^Interrupted$/);
  assert.equal(interruptedUi.elements["chat-activity-status"].className, "chat-activity-status chat-activity-status-interrupted");
});

test("Enter never submits twice: empty message, no session, and a turn already in flight are all no-ops", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  const input = ui.elements["chat-message-input"];
  // No session selected yet: Enter must not attempt to send.
  input.value = "premature";
  input.dispatch("keydown", { key: "Enter", preventDefault() {} });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.calls.filter((call) => call.path.endsWith("/turns") && call.method === "POST").length, 0);

  ui.backend.sessions = [{ session_id: "CHAT-NODUP", project_id: "demo", status: "ACTIVE", requested_model: "cx/gpt-5.6-sol", model: "cx/gpt-5.6-sol" }];
  ui.backend.turns.set("CHAT-NODUP", []); await ui.window.NexusChat.selectSession("CHAT-NODUP");

  // Empty (whitespace-only) message: Enter must not send.
  input.value = "   ";
  input.dispatch("keydown", { key: "Enter", preventDefault() {} });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.calls.filter((call) => call.path.endsWith("/turns") && call.method === "POST").length, 0);

  // A real send, then a second Enter while that turn is still in flight
  // (activeTurnId set, no terminal event yet) must not submit again.
  input.value = "first message";
  input.dispatch("keydown", { key: "Enter", preventDefault() {} });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.calls.filter((call) => call.path.endsWith("/turns") && call.method === "POST").length, 1);
  input.value = "second message, should be rejected";
  input.dispatch("keydown", { key: "Enter", preventDefault() {} });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.calls.filter((call) => call.path.endsWith("/turns") && call.method === "POST").length, 1, "a turn already in progress must block a second send");
});

test("reopened session restores real user prompts and assistant responses from the history API, in order", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-HISTORY", project_id: "demo", status: "ACTIVE", provider: "omniroute", model: "cc/example", codex_thread_id: "thread-1" }];
  ui.backend.turns.set("CHAT-HISTORY", [{ turn_id: "TURN-H", status: "COMPLETED", response_text: "Actual answer", idempotency_key: "key-h" }]);
  ui.backend.historyPages.set("CHAT-HISTORY", [{
    items: [
      { turn_id: "TURN-H", item_id: "item-user-1", type: "userMessage", text: "What is the real question?" },
      { turn_id: "TURN-H", item_id: "item-asst-1", type: "agentMessage", text: "Actual answer" },
    ],
    next_cursor: null, source: "codex_thread",
  }]);
  await ui.window.NexusChat.selectSession("CHAT-HISTORY");
  await new Promise((resolve) => setImmediate(resolve));
  const text = ui.elements["chat-transcript"].children.map((node) => node.children.map((child) => child.textContent).join(" ")).join("\n");
  assert.match(text, /What is the real question\?/);
  assert.match(text, /Actual answer/);
  assert.ok(text.indexOf("What is the real question?") < text.indexOf("Actual answer"), "user prompt must precede the assistant response");
  assert.doesNotMatch(text, /history API is not available/);
  assert.doesNotMatch(text, /fake prompt/);
});

test("history fetch failure falls back to turn summaries with an explicit, honest notice", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-NOHIST", project_id: "demo", status: "ACTIVE", codex_thread_id: "thread-2" }];
  ui.backend.turns.set("CHAT-NOHIST", [{ turn_id: "TURN-N", status: "COMPLETED", response_text: "Fallback answer", idempotency_key: "key-n" }]);
  ui.backend.historyErrors.add("CHAT-NOHIST");
  await ui.window.NexusChat.selectSession("CHAT-NOHIST");
  await new Promise((resolve) => setImmediate(resolve));
  const text = ui.elements["chat-transcript"].children.map((node) => node.children.map((child) => child.textContent).join(" ")).join("\n");
  assert.match(text, /Fallback answer/);
  assert.match(text, /history API is not available yet/);
  assert.doesNotMatch(text, /fake prompt/);
});

test("history auto-paginates forward from the start of the thread (verified backend order), bounded by a cap", async () => {
  // Backend T4 (nexus/chat/service.py::get_history) always pages
  // sort_direction="asc" starting from the beginning when no cursor is
  // given, and next_cursor advances FORWARD (towards more recent items) --
  // there is no "start at the end, page backward" mode. Restoring a
  // session therefore auto-paginates forward, bounded by a cap, with a
  // manual "Load more" control for anything left over.
  const ui = setup(); await ui.window.NexusChat.ready;
  const cap = ui.window.NexusChat.historyAutoPageCap;
  const pages = [];
  for (let i = 0; i < cap + 1; i++) {
    pages.push({
      items: [
        { turn_id: "TURN-" + i, item_id: "item-user-" + i, type: "userMessage", text: "question " + i },
        { turn_id: "TURN-" + i, item_id: "item-asst-" + i, type: "agentMessage", text: "answer " + i },
      ],
      next_cursor: i < cap ? String(i + 1) : null,
      source: "codex_thread",
    });
  }
  ui.backend.sessions = [{ session_id: "CHAT-PAGED", project_id: "demo", status: "ACTIVE", codex_thread_id: "thread-3" }];
  ui.backend.turns.set("CHAT-PAGED", [{ turn_id: "TURN-" + (cap - 1), status: "COMPLETED", response_text: "answer " + (cap - 1), idempotency_key: "key-p" }]);
  ui.backend.historyPages.set("CHAT-PAGED", pages);
  await ui.window.NexusChat.selectSession("CHAT-PAGED");
  for (let i = 0; i < cap + 2; i++) await new Promise((resolve) => setImmediate(resolve));

  const transcript = ui.elements["chat-transcript"];
  const textNow = () => transcript.children.map((node) => node.children.map((child) => child.textContent).join(" ")).join("\n");
  // Exactly `cap` pages (0..cap-1) are auto-loaded; the last page (index
  // `cap`) is left for the manual control.
  assert.match(textNow(), /question 0/);
  assert.match(textNow(), new RegExp("question " + (cap - 1)));
  assert.doesNotMatch(textNow(), new RegExp("question " + cap + "\\b"));
  assert.ok(textNow().indexOf("question 0") < textNow().indexOf("question " + (cap - 1)), "earlier pages must stay before later ones, in order");
  assert.equal(ui.elements["chat-load-older"].hidden, false, "one page remains beyond the auto-load cap");

  ui.window.NexusChat.loadMoreHistory();
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(textNow(), new RegExp("question " + cap + "\\b"), "manual Load more must fetch the next (more recent) page");
  assert.ok(textNow().indexOf("question " + (cap - 1)) < textNow().indexOf("question " + cap), "the manually-loaded page must be appended after existing content, never before it");
  assert.equal(ui.elements["chat-load-older"].hidden, true, "no next_cursor left after the final page");
  assert.ok(ui.calls.some((call) => call.path === "/api/chat/sessions/CHAT-PAGED/history?cursor=" + cap + "&limit=50"));
});

test("history turn-lifecycle metadata renders an interrupted/failed status badge, once", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-TURNSTATUS", project_id: "demo", status: "ACTIVE", codex_thread_id: "thread-7" }];
  ui.backend.turns.set("CHAT-TURNSTATUS", [{ turn_id: "TURN-S", status: "CANCELLED", response_text: null, idempotency_key: "key-s" }]);
  ui.backend.historyPages.set("CHAT-TURNSTATUS", [{
    items: [{ turn_id: "TURN-S", item_id: "item-user-s", type: "userMessage", text: "long task" }],
    turns: [{ turn_id: "TURN-S", status: "interrupted", error: "Interrupted by user" }],
    next_cursor: null, source: "codex_thread",
  }]);
  await ui.window.NexusChat.selectSession("CHAT-TURNSTATUS");
  await new Promise((resolve) => setImmediate(resolve));
  const badges = ui.elements["chat-transcript"].children.filter((node) => node.className.indexOf("chat-turn-status") === 0);
  assert.equal(badges.length, 1);
  assert.equal(badges[0].className, "chat-turn-status chat-turn-status-interrupted");
});

test("live SSE never duplicates an item already rendered from history", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-DEDUP", project_id: "demo", status: "ACTIVE", codex_thread_id: "thread-4" }];
  ui.backend.turns.set("CHAT-DEDUP", [{ turn_id: "TURN-D", status: "RUNNING", response_text: null, idempotency_key: "key-d" }]);
  ui.backend.historyPages.set("CHAT-DEDUP", [{
    items: [{ turn_id: "TURN-D", item_id: "item-tool-1", type: "commandExecution", command: "ls", output: "a.txt" }],
    next_cursor: null, source: "codex_thread",
  }]);
  await ui.window.NexusChat.selectSession("CHAT-DEDUP");
  await new Promise((resolve) => setImmediate(resolve));
  const cardsAfterHistory = ui.elements["chat-transcript"].children.filter((node) => node.className === "chat-tool-event");
  assert.equal(cardsAfterHistory.length, 1);
  const stream = ui.eventSources[0];
  stream.emit({ method: "item/completed", params: { item: { id: "item-tool-1", type: "commandExecution", command: "ls", aggregatedOutput: "a.txt" } } }, "1");
  const cardsAfterReplay = ui.elements["chat-transcript"].children.filter((node) => node.className === "chat-tool-event");
  assert.equal(cardsAfterReplay.length, 1, "the same item_id must reuse the existing card, never duplicate it");
});

test("SSE replay gaps close the stream and reconcile durable turn state", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-GAP", project_id: "demo", status: "ACTIVE" }];
  ui.backend.turns.set("CHAT-GAP", []);
  await ui.window.NexusChat.selectSession("CHAT-GAP");
  ui.elements["chat-message-input"].value = "recover after a gap";
  ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const turn = ui.backend.turns.get("CHAT-GAP")[0];
  turn.status = "COMPLETED"; turn.response_text = "reconciled answer";
  const stream = ui.eventSources[0];
  stream.emit({ type: "nexus/replayGap", turnId: turn.turn_id }, "");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(stream.closed, true);
  // Required reconciliation behavior: a replay gap must fetch the
  // authoritative history, not merely assume nothing was missed -- this is
  // the real GET /history endpoint, not a guess. It must never filter by
  // the Nexus turn_id (the history API's turn_id is Codex-native only; see
  // the T4.2 fix), so the request carries no turn_id at all.
  assert.ok(ui.calls.some((call) => call.method === "GET" && call.path === "/api/chat/sessions/CHAT-GAP/history?limit=50"));
  assert.ok(!ui.calls.some((call) => call.method === "GET" && call.path.includes("/history") && call.path.includes("turn_id=")));
  assert.ok(ui.calls.some((call) => call.method === "GET" && call.path === "/api/chat/sessions/CHAT-GAP"));
  assert.match(ui.elements["chat-status-text"].textContent, /Turn completed/);
});

test("history recovery never leaves a duplicate of the optimistically-rendered user prompt or assistant reply", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-OPTIMISTIC-DEDUP", project_id: "demo", status: "ACTIVE" }];
  ui.backend.turns.set("CHAT-OPTIMISTIC-DEDUP", []);
  await ui.window.NexusChat.selectSession("CHAT-OPTIMISTIC-DEDUP");
  ui.elements["chat-message-input"].value = "echo test";
  ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const turn = ui.backend.turns.get("CHAT-OPTIMISTIC-DEDUP")[0];
  turn.request_text = "echo test";
  // The gap happens before ANY live SSE delta/item-completed event for this
  // turn arrived (e.g. the connection dropped right at open) -- so neither
  // the user prompt nor the assistant reply were ever confirmed by item_id,
  // only optimistically rendered (sendMessage()'s own prompt echo; no
  // assistant bubble exists at all yet, since no delta ever streamed).
  turn.status = "COMPLETED"; turn.response_text = "echo test reply";
  ui.backend.historyPages.set("CHAT-OPTIMISTIC-DEDUP", [{
    items: [
      { turn_id: "01a149f5-codex-turn", item_id: "01a149f5-item-user", type: "userMessage", text: "echo test" },
      { turn_id: "01a149f5-codex-turn", item_id: "01a149f5-item-asst", type: "agentMessage", text: "echo test reply" },
    ],
    next_cursor: null, source: "codex_thread",
  }]);
  const stream = ui.eventSources[0];
  stream.emit({ type: "nexus/replayGap", turnId: turn.turn_id }, "");
  await new Promise((resolve) => setImmediate(resolve));
  const userMessages = ui.elements["chat-transcript"].children.filter((node) => node.className.includes("chat-message-user") && node.children[1].textContent === "echo test");
  const assistantMessages = ui.elements["chat-transcript"].children.filter((node) => node.className.includes("chat-message-assistant") && node.children[1].textContent === "echo test reply");
  assert.equal(userMessages.length, 1, "the user prompt must appear exactly once, not once optimistic + once recovered");
  assert.equal(assistantMessages.length, 1, "the assistant reply must appear exactly once, not once recovered + once from the response_text fallback");
  assert.match(ui.elements["chat-status-text"].textContent, /Turn completed/);
});

test("a textually-identical prompt in an unrelated earlier turn is never mistaken for this turn's authoritative copy", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-NO-FALSE-DEDUP", project_id: "demo", status: "ACTIVE" }];
  ui.backend.turns.set("CHAT-NO-FALSE-DEDUP", []);
  await ui.window.NexusChat.selectSession("CHAT-NO-FALSE-DEDUP");
  // An earlier, unrelated, already-completed turn with the exact same text
  // as the one about to be sent -- its optimistic DOM node is still in the
  // transcript (same shape sendMessage() always leaves behind).
  ui.elements["chat-message-input"].value = "ping";
  ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const firstTurn = ui.backend.turns.get("CHAT-NO-FALSE-DEDUP")[0];
  firstTurn.status = "COMPLETED"; firstTurn.response_text = "pong";
  const stream = ui.eventSources[0];
  stream.emit({ type: "nexus/turnEnded" }, "");
  await new Promise((resolve) => setImmediate(resolve));

  // Now a second, unrelated turn with the IDENTICAL text, recovered via a
  // real replay gap (same content, different turn, different -- Codex
  // native -- item ids).
  ui.elements["chat-message-input"].value = "ping";
  ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const secondTurn = ui.backend.turns.get("CHAT-NO-FALSE-DEDUP")[0];
  secondTurn.request_text = "ping"; secondTurn.status = "COMPLETED"; secondTurn.response_text = "pong";
  ui.backend.historyPages.set("CHAT-NO-FALSE-DEDUP", [{
    items: [
      { turn_id: "codex-turn-2", item_id: "item-user-2", type: "userMessage", text: "ping" },
      { turn_id: "codex-turn-2", item_id: "item-asst-2", type: "agentMessage", text: "pong" },
    ],
    next_cursor: null, source: "codex_thread",
  }]);
  const stream2 = ui.eventSources[1];
  stream2.emit({ type: "nexus/replayGap", turnId: secondTurn.turn_id }, "");
  await new Promise((resolve) => setImmediate(resolve));

  const userMessages = ui.elements["chat-transcript"].children.filter((node) => node.className.includes("chat-message-user") && node.children[1].textContent === "ping");
  const assistantMessages = ui.elements["chat-transcript"].children.filter((node) => node.className.includes("chat-message-assistant") && node.children[1].textContent === "pong");
  // Both turns' own "ping"/"pong" must survive -- the first turn's
  // optimistic node must NOT have been deleted as a false "duplicate" of
  // the second turn's unrelated recovered copy.
  assert.equal(userMessages.length, 2, "two distinct turns' identical prompts must both remain");
  assert.equal(assistantMessages.length, 2, "two distinct turns' identical replies must both remain");
});

test("two independently gap-recovered turns with byte-identical text never get their bubbles cross-attributed", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-CROSS-ATTR", project_id: "demo", status: "ACTIVE" }];
  ui.backend.turns.set("CHAT-CROSS-ATTR", []);
  await ui.window.NexusChat.selectSession("CHAT-CROSS-ATTR");

  // Turn 1: text "ping" -> "pong", recovered via its OWN independent replay
  // gap (its optimistic nodes are dropped in favor of its OWN
  // history-sourced, item_id-keyed ones -- exactly like the dedicated
  // dedup test above).
  ui.elements["chat-message-input"].value = "ping";
  ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const firstTurn = ui.backend.turns.get("CHAT-CROSS-ATTR")[0];
  firstTurn.request_text = "ping"; firstTurn.status = "COMPLETED"; firstTurn.response_text = "pong";
  ui.backend.historyPages.set("CHAT-CROSS-ATTR", [{
    items: [
      { turn_id: "codex-turn-1", item_id: "item-user-1", type: "userMessage", text: "ping" },
      { turn_id: "codex-turn-1", item_id: "item-asst-1", type: "agentMessage", text: "pong" },
    ],
    next_cursor: null, source: "codex_thread",
  }]);
  ui.eventSources[0].emit({ type: "nexus/replayGap", turnId: firstTurn.turn_id }, "");
  await new Promise((resolve) => setImmediate(resolve));

  // Turn 2: BYTE-IDENTICAL text "ping" -> "pong", ALSO independently
  // recovered via its own replay gap. If dropOptimisticDuplicates() ever
  // accepted text equality alone as proof of turn identity, this is where
  // it would bind turn 2's state.bubbles entry to turn 1's authoritative
  // DOM node instead of its own.
  ui.elements["chat-message-input"].value = "ping";
  ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const secondTurn = ui.backend.turns.get("CHAT-CROSS-ATTR")[0];
  secondTurn.request_text = "ping"; secondTurn.status = "COMPLETED"; secondTurn.response_text = "pong";
  ui.backend.historyPages.set("CHAT-CROSS-ATTR", [{
    items: [
      { turn_id: "codex-turn-2", item_id: "item-user-2", type: "userMessage", text: "ping" },
      { turn_id: "codex-turn-2", item_id: "item-asst-2", type: "agentMessage", text: "pong" },
    ],
    next_cursor: null, source: "codex_thread",
  }]);
  ui.eventSources[1].emit({ type: "nexus/replayGap", turnId: secondTurn.turn_id }, "");
  await new Promise((resolve) => setImmediate(resolve));

  const pingsBefore = ui.elements["chat-transcript"].children.filter((node) => node.className.includes("chat-message-user") && node.children[1].textContent === "ping");
  const pongsBefore = ui.elements["chat-transcript"].children.filter((node) => node.className.includes("chat-message-assistant") && node.children[1].textContent === "pong");
  assert.equal(pingsBefore.length, 2, "both turns' identical prompts must remain distinct, not collapsed into one");
  assert.equal(pongsBefore.length, 2, "both turns' identical replies must remain distinct, not collapsed into one");

  // The real-world consequence of wrong cross-turn attribution: a LATE
  // update legitimately targeting turn 2 (e.g. a delayed reconciliation)
  // must only ever change turn 2's own bubble, never corrupt turn 1's
  // already-settled, unrelated message.
  secondTurn.response_text = "pong-updated-for-turn-2-only";
  ui.eventSources[1].emit({ type: "nexus/noTurn" }, "");
  await new Promise((resolve) => setImmediate(resolve));

  const turn1Text = ui.elements["chat-transcript"].children.find((node) => node.dataset && node.dataset.turnId === "item-asst-1");
  assert.ok(turn1Text, "turn 1's own authoritative assistant node must still exist, untouched");
  assert.equal(turn1Text.children[1].textContent, "pong", "turn 1's message must not be corrupted by an update meant for turn 2");
  assert.ok(
    ui.elements["chat-transcript"].children.some((node) => node.children[1] && node.children[1].textContent === "pong-updated-for-turn-2-only"),
    "turn 2's own update must still be visible somewhere"
  );
});

test("replay gap while the turn is still RUNNING defers history reconciliation past a legitimate 409, then recovers it once the turn completes", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-DEFER", project_id: "demo", status: "ACTIVE" }];
  ui.backend.turns.set("CHAT-DEFER", []);
  await ui.window.NexusChat.selectSession("CHAT-DEFER");
  ui.elements["chat-message-input"].value = "long running task";
  ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const turn = ui.backend.turns.get("CHAT-DEFER")[0];
  // The turn's own App-Server process still holds the thread resumed, so
  // ANY concurrent history fetch for this session legitimately 409s
  // (HISTORY_UNAVAILABLE) -- unconditionally, regardless of query params,
  // matching the real backend -- and must not be treated as a hard failure.
  ui.backend.historyThreadBusy.add("CHAT-DEFER");
  ui.backend.historyPages.set("CHAT-DEFER", [{
    items: [
      { turn_id: "codex-native-turn-id", item_id: "item-missed-tool", type: "commandExecution", command: "ls", aggregatedOutput: "a.txt" },
      { turn_id: "codex-native-turn-id", item_id: "item-missed-asst", type: "agentMessage", text: "done" },
    ],
    next_cursor: null, source: "codex_thread",
  }]);
  const stream = ui.eventSources[0];
  stream.emit({ type: "nexus/replayGap", turnId: turn.turn_id }, "");
  await new Promise((resolve) => setImmediate(resolve));
  // While still RUNNING: the conflicted fetch must not have rendered
  // anything, and the turn must still read as active/uncertain, not silently
  // dropped.
  assert.equal(ui.elements["chat-transcript"].children.filter((node) => node.className === "chat-tool-event").length, 0);
  assert.match(ui.elements["chat-status-text"].textContent, /still reports this turn as active/);
  // Never filtered by the Nexus turn_id -- that's the T4.2 defect.
  assert.ok(!ui.calls.some((call) => call.path.includes("/history") && call.path.includes("turn_id=")));

  // The turn finishes and the concurrency conflict goes away. A fresh
  // terminal-ish notification (here: nexus/noTurn) is what re-enters
  // reconcileAfterTerminal() in production -- it must pick up the deferred
  // history retry rather than rely on a new replay gap.
  turn.status = "COMPLETED"; turn.response_text = "done";
  ui.backend.historyThreadBusy.delete("CHAT-DEFER");
  stream.emit({ type: "nexus/noTurn" }, "");
  await new Promise((resolve) => setImmediate(resolve));

  const toolCards = ui.elements["chat-transcript"].children.filter((node) => node.className === "chat-tool-event");
  assert.equal(toolCards.length, 1, "the missed tool output must be recovered once the conflict clears");
  assert.match(toolCards[0].children[1].textContent, /a\.txt/);
  assert.match(ui.elements["chat-status-text"].textContent, /Turn completed/);
  assert.ok(!ui.calls.some((call) => call.path.includes("/history") && call.path.includes("turn_id=")), "recovery must never have sent a turn_id filter");
});

test("a real history failure during recovery is surfaced, never silently treated as success", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-REALFAIL", project_id: "demo", status: "ACTIVE" }];
  ui.backend.turns.set("CHAT-REALFAIL", []);
  await ui.window.NexusChat.selectSession("CHAT-REALFAIL");
  ui.elements["chat-message-input"].value = "will fail to recover";
  ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const turn = ui.backend.turns.get("CHAT-REALFAIL")[0];
  turn.status = "COMPLETED"; turn.response_text = "done anyway";
  // A genuine, unrelated failure (not the active-turn conflict). An
  // unconditional empty catch would make this indistinguishable from a
  // successful, merely-empty recovery -- it must not be.
  ui.backend.historyErrors.add("CHAT-REALFAIL");
  const stream = ui.eventSources[0];
  stream.emit({ type: "nexus/replayGap", turnId: turn.turn_id }, "");
  await new Promise((resolve) => setImmediate(resolve));
  // The history endpoint was genuinely attempted (not skipped)...
  assert.ok(ui.calls.some((call) => call.method === "GET" && call.path.startsWith("/api/chat/sessions/CHAT-REALFAIL/history")));
  // ...it must NOT have been silently retried-as-pending like the 409 case
  // (this was a real 404, not an active-turn conflict)...
  assert.equal(ui.backend.turns.get("CHAT-REALFAIL")[0].status, "COMPLETED");
  // ...the turn's own response_text still surfaces, never held hostage by
  // the failed recovery...
  const body = ui.elements["chat-transcript"].children.find((node) => node.className.includes("chat-message-assistant"));
  assert.ok(body && body.children[1].textContent === "done anyway");
  // ...and nothing was fabricated: no tool/history items were invented to
  // paper over the failed fetch.
  assert.equal(ui.elements["chat-transcript"].children.filter((node) => node.className === "chat-tool-event").length, 0);
});

test("recovery follows pagination past the first page to find items beyond it", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-PAGED-RECOVER", project_id: "demo", status: "ACTIVE" }];
  ui.backend.turns.set("CHAT-PAGED-RECOVER", []);
  await ui.window.NexusChat.selectSession("CHAT-PAGED-RECOVER");
  ui.elements["chat-message-input"].value = "deep history";
  ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const turn = ui.backend.turns.get("CHAT-PAGED-RECOVER")[0];
  ui.backend.historyThreadBusy.add("CHAT-PAGED-RECOVER");
  // Page 0 (the first 50-item page) is already exhausted and does NOT
  // contain the turn that triggered the gap; only page 1 does. Recovery
  // must not assume page 0 is enough.
  ui.backend.historyPages.set("CHAT-PAGED-RECOVER", [
    { items: [{ turn_id: "codex-turn-older", item_id: "item-old-1", type: "userMessage", text: "an earlier message" }], next_cursor: "1", source: "codex_thread" },
    { items: [{ turn_id: "codex-turn-new", item_id: "item-new-tool", type: "commandExecution", command: "ls", aggregatedOutput: "deep.txt" }], next_cursor: null, source: "codex_thread" },
  ]);
  const stream = ui.eventSources[0];
  stream.emit({ type: "nexus/replayGap", turnId: turn.turn_id }, "");
  await new Promise((resolve) => setImmediate(resolve));
  turn.status = "COMPLETED"; turn.response_text = "done";
  ui.backend.historyThreadBusy.delete("CHAT-PAGED-RECOVER");
  stream.emit({ type: "nexus/noTurn" }, "");
  await new Promise((resolve) => setImmediate(resolve));
  const toolCards = ui.elements["chat-transcript"].children.filter((node) => node.className === "chat-tool-event");
  assert.equal(toolCards.length, 1, "the item on the second page must still be recovered");
  assert.match(toolCards[0].children[1].textContent, /deep\.txt/);
  const userMessages = ui.elements["chat-transcript"].children.filter((node) => node.className.includes("chat-message-user"));
  assert.ok(userMessages.some((node) => node.children[1].textContent === "deep history"));
});

test("hitting the recovery page cap is never reported as a clean, complete success", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-CAP-HIT", project_id: "demo", status: "ACTIVE" }];
  ui.backend.turns.set("CHAT-CAP-HIT", []);
  await ui.window.NexusChat.selectSession("CHAT-CAP-HIT");
  ui.elements["chat-message-input"].value = "very deep history";
  ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const turn = ui.backend.turns.get("CHAT-CAP-HIT")[0];
  ui.backend.historyThreadBusy.add("CHAT-CAP-HIT");
  // More pages than HISTORY_RECOVERY_PAGE_CAP (20): page 25 still exists
  // and is never reached, so real newer items remain genuinely unread
  // when recovery gives up at its bound.
  const pages = [];
  for (let i = 0; i < 25; i += 1) {
    pages.push({ items: [{ turn_id: "codex-turn-" + i, item_id: "item-" + i, type: "userMessage", text: "filler " + i }], next_cursor: String(i + 1), source: "codex_thread" });
  }
  pages[24].next_cursor = null; // true end, never reached
  ui.backend.historyPages.set("CHAT-CAP-HIT", pages);
  const stream = ui.eventSources[0];
  stream.emit({ type: "nexus/replayGap", turnId: turn.turn_id }, "");
  await new Promise((resolve) => setImmediate(resolve));
  turn.status = "COMPLETED"; turn.response_text = "done";
  ui.backend.historyThreadBusy.delete("CHAT-CAP-HIT");
  const historyCallsBeforeRetry = ui.calls.filter((call) => call.method === "GET" && call.path.startsWith("/api/chat/sessions/CHAT-CAP-HIT/history")).length;
  stream.emit({ type: "nexus/noTurn" }, "");
  await new Promise((resolve) => setImmediate(resolve));

  // Exactly HISTORY_RECOVERY_PAGE_CAP (20) history fetches for the actual
  // (post-409) recovery attempt -- bounded, never unbounded pagination.
  const historyCallsAfterRetry = ui.calls.filter((call) => call.method === "GET" && call.path.startsWith("/api/chat/sessions/CHAT-CAP-HIT/history")).length;
  assert.equal(historyCallsAfterRetry - historyCallsBeforeRetry, ui.window.NexusChat.historyRecoveryPageCap, "recovery must stop at exactly its documented page bound");
  // Never silently reported as a clean success: the status must say so.
  assert.doesNotMatch(ui.elements["chat-status-text"].textContent, /^Turn completed\.$/);
  assert.match(ui.elements["chat-status-text"].textContent, /page limit|more messages|unread/i);
  // The condition is detectable and recoverable: the existing "Load more
  // messages" affordance (driven by the same historyNextCursor) must be
  // visible, since real, newer items are still known to be unread.
  assert.equal(ui.elements["chat-load-older"].hidden, false, "Load more messages must surface so the user can continue past the cap");
});

test("overlapping recovery pages never render the same item twice", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-OVERLAP", project_id: "demo", status: "ACTIVE" }];
  ui.backend.turns.set("CHAT-OVERLAP", []);
  await ui.window.NexusChat.selectSession("CHAT-OVERLAP");
  ui.elements["chat-message-input"].value = "overlap check";
  ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const turn = ui.backend.turns.get("CHAT-OVERLAP")[0];
  ui.backend.historyThreadBusy.add("CHAT-OVERLAP");
  // Page 1 repeats the same item_id that page 0 already rendered (a
  // realistic edge case if the thread grew between page fetches).
  ui.backend.historyPages.set("CHAT-OVERLAP", [
    { items: [{ turn_id: "codex-turn-a", item_id: "item-shared", type: "agentMessage", text: "shared" }], next_cursor: "1", source: "codex_thread" },
    { items: [
      { turn_id: "codex-turn-a", item_id: "item-shared", type: "agentMessage", text: "shared" },
      { turn_id: "codex-turn-b", item_id: "item-unique", type: "agentMessage", text: "unique" },
    ], next_cursor: null, source: "codex_thread" },
  ]);
  const stream = ui.eventSources[0];
  stream.emit({ type: "nexus/replayGap", turnId: turn.turn_id }, "");
  await new Promise((resolve) => setImmediate(resolve));
  turn.status = "COMPLETED"; turn.response_text = "done";
  ui.backend.historyThreadBusy.delete("CHAT-OVERLAP");
  stream.emit({ type: "nexus/noTurn" }, "");
  await new Promise((resolve) => setImmediate(resolve));
  const assistantMessages = ui.elements["chat-transcript"].children.filter((node) => node.className.includes("chat-message-assistant"));
  assert.equal(assistantMessages.filter((node) => node.children[1].textContent === "shared").length, 1, "the repeated item_id must render exactly once");
  assert.equal(assistantMessages.filter((node) => node.children[1].textContent === "unique").length, 1);
});

test("recovery across multiple sequential turns preserves chronological ordering", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-ORDER", project_id: "demo", status: "ACTIVE" }];
  ui.backend.turns.set("CHAT-ORDER", []);
  await ui.window.NexusChat.selectSession("CHAT-ORDER");
  ui.elements["chat-message-input"].value = "first turn";
  ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const turn = ui.backend.turns.get("CHAT-ORDER")[0];
  ui.backend.historyThreadBusy.add("CHAT-ORDER");
  ui.backend.historyPages.set("CHAT-ORDER", [{
    items: [
      { turn_id: "codex-turn-1", item_id: "item-u1", type: "userMessage", text: "first turn" },
      { turn_id: "codex-turn-1", item_id: "item-a1", type: "agentMessage", text: "first reply" },
      { turn_id: "codex-turn-2", item_id: "item-u2", type: "userMessage", text: "second turn" },
      { turn_id: "codex-turn-2", item_id: "item-a2", type: "agentMessage", text: "second reply" },
    ],
    next_cursor: null, source: "codex_thread",
  }]);
  const stream = ui.eventSources[0];
  stream.emit({ type: "nexus/replayGap", turnId: turn.turn_id }, "");
  await new Promise((resolve) => setImmediate(resolve));
  turn.status = "COMPLETED"; turn.response_text = "first reply";
  ui.backend.historyThreadBusy.delete("CHAT-ORDER");
  stream.emit({ type: "nexus/noTurn" }, "");
  await new Promise((resolve) => setImmediate(resolve));
  const texts = ui.elements["chat-transcript"].children
    .filter((node) => node.className.includes("chat-message-"))
    .map((node) => node.children[1] && node.children[1].textContent)
    .filter(Boolean);
  const order = ["first turn", "first reply", "second turn", "second reply"];
  const indices = order.map((text) => texts.indexOf(text));
  assert.ok(indices.every((index) => index !== -1), JSON.stringify({ texts, order }));
  assert.ok(indices.every((index, position) => position === 0 || index > indices[position - 1]), "messages must stay in chronological order");
});

test("switching sessions mid-recovery never appends the earlier session's items to the new one", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [
    { session_id: "CHAT-SWITCH-OLD", project_id: "demo", status: "ACTIVE" },
    { session_id: "CHAT-SWITCH-NEW", project_id: "demo", status: "ACTIVE" },
  ];
  ui.backend.turns.set("CHAT-SWITCH-OLD", []);
  ui.backend.turns.set("CHAT-SWITCH-NEW", []);
  await ui.window.NexusChat.selectSession("CHAT-SWITCH-OLD");
  ui.elements["chat-message-input"].value = "old session task";
  ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const oldTurn = ui.backend.turns.get("CHAT-SWITCH-OLD")[0];
  ui.backend.historyThreadBusy.add("CHAT-SWITCH-OLD");
  // Two pages so recovery is still mid-flight (awaiting page 2) when the
  // session switch happens between the two awaited fetches below.
  ui.backend.historyPages.set("CHAT-SWITCH-OLD", [
    { items: [{ turn_id: "codex-old-1", item_id: "item-old-p0", type: "agentMessage", text: "old page zero" }], next_cursor: "1", source: "codex_thread" },
    { items: [{ turn_id: "codex-old-2", item_id: "item-old-p1", type: "agentMessage", text: "old page one" }], next_cursor: null, source: "codex_thread" },
  ]);
  const oldStream = ui.eventSources[0];
  oldStream.emit({ type: "nexus/replayGap", turnId: oldTurn.turn_id }, "");
  await new Promise((resolve) => setImmediate(resolve));
  oldTurn.status = "COMPLETED"; oldTurn.response_text = "old done";
  ui.backend.historyThreadBusy.delete("CHAT-SWITCH-OLD");
  // Start the deferred recovery, but switch sessions before awaiting it --
  // its own first page fetch (page 0) resolves, then loadGeneration no
  // longer matches, so page one's render (and any further pagination)
  // must never reach the DOM.
  oldStream.emit({ type: "nexus/noTurn" }, "");
  await ui.window.NexusChat.selectSession("CHAT-SWITCH-NEW");
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  const newTranscriptTexts = ui.elements["chat-transcript"].children
    .map((node) => node.children[1] && node.children[1].textContent)
    .filter(Boolean);
  assert.ok(!newTranscriptTexts.includes("old page zero"), "the old session's recovered item must not leak into the new session's transcript");
  assert.ok(!newTranscriptTexts.includes("old page one"));
});

test("a dropped terminal SSE event is still recovered by the bounded status poll", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-MISSEDTERM", project_id: "demo", status: "ACTIVE" }];
  ui.backend.turns.set("CHAT-MISSEDTERM", []);
  await ui.window.NexusChat.selectSession("CHAT-MISSEDTERM");
  ui.elements["chat-message-input"].value = "slow task";
  ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const turn = ui.backend.turns.get("CHAT-MISSEDTERM")[0];
  // Simulate the real turn/completed (or nexus/turnEnded) notification being
  // dropped: the turn reports RUNNING for its first two status polls, then
  // COMPLETED from the third poll onward.
  let reads = 0;
  Object.defineProperty(turn, "status", {
    get() { reads += 1; return reads < 3 ? "RUNNING" : "COMPLETED"; },
    set() {},
  });
  turn.response_text = "finished despite the dropped terminal event";
  const stream = ui.eventSources[0];
  // The real turn/completed and nexus/turnEnded notifications never arrive;
  // only a single generic reconciliation trigger (mirroring e.g. the
  // EventSource-unsupported or interrupt-result paths) starts the bounded
  // poll, which must keep retrying on its own until it observes the turn
  // has actually finished.
  stream.emit({ type: "nexus/noTurn" }, "");
  for (let i = 0; i < 10 && reads < 3; i += 1) await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(reads >= 3, "the poll must keep retrying past the first RUNNING reads");
  assert.match(ui.elements["chat-status-text"].textContent, /Turn completed/);
  const assistant = ui.elements["chat-transcript"].children.find((node) => node.className.includes("chat-message-assistant"));
  assert.ok(assistant);
  assert.equal(assistant.children[1].textContent, "finished despite the dropped terminal event");
});

test("the status poll is bounded and eventually surfaces uncertainty instead of polling forever", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-STUCK", project_id: "demo", status: "ACTIVE" }];
  ui.backend.turns.set("CHAT-STUCK", []);
  await ui.window.NexusChat.selectSession("CHAT-STUCK");
  ui.elements["chat-message-input"].value = "never finishes";
  ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const turn = ui.backend.turns.get("CHAT-STUCK")[0]; // status stays "PENDING" forever
  const stream = ui.eventSources[0];
  const sessionCallsBefore = ui.calls.filter((call) => call.method === "GET" && call.path === "/api/chat/sessions/CHAT-STUCK").length;
  stream.emit({ type: "nexus/noTurn" }, "");
  for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setImmediate(resolve));
  const sessionCallsAfter = ui.calls.filter((call) => call.method === "GET" && call.path === "/api/chat/sessions/CHAT-STUCK").length;
  // Bounded: a fixed, finite number of polls, never an unbounded/infinite loop.
  assert.ok(sessionCallsAfter - sessionCallsBefore <= 61, "poll attempts must stay within the documented bound");
  assert.match(ui.elements["chat-status-text"].textContent, /still reports this turn as active/);
});

test("tool output is bounded, redacted and rendered as plain text; event IDs deduplicate", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-TOOLS", project_id: "demo", status: "ACTIVE" }]; ui.backend.turns.set("CHAT-TOOLS", []);
  await ui.window.NexusChat.selectSession("CHAT-TOOLS");
  ui.elements["chat-message-input"].value = "inspect"; ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const event = { method: "item/completed", params: { item: { id: "tool-1", type: "commandExecution", command: "echo API_KEY=secret", aggregatedOutput: "<img src=x onerror=alert(1)>" } } };
  ui.eventSources[0].emit(event, "7"); ui.eventSources[0].emit(event, "7");
  const cards = ui.elements["chat-transcript"].children.filter((node) => node.className === "chat-tool-event");
  assert.equal(cards.length, 1);
  assert.match(cards[0].children[1].textContent, /API_KEY=\[redacted\]/);
  assert.match(cards[0].children[1].textContent, /<img/);
  assert.equal(cards[0].children[1].children.length, 0);
  ui.eventSources[0].onerror();
  assert.equal(ui.eventSources[0].closed, false, "EventSource remains open for native reconnect");
  assert.match(ui.elements["chat-status-text"].textContent, /reconnecting/);
});

test("redaction preserves harmless QA markers and ordinary text while still redacting real secrets (tool output)", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-REDACT", project_id: "demo", status: "ACTIVE" }]; ui.backend.turns.set("CHAT-REDACT", []);
  await ui.window.NexusChat.selectSession("CHAT-REDACT");
  ui.elements["chat-message-input"].value = "inspect"; ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const output = [
    "qa-marker-token=verify-9f3a",                              // harmless QA marker: must survive
    "please remember your token for later",                      // ordinary prose mentioning "token": must survive
    "password=hunter2",                                          // real short credential: must redact
    "Authorization: Bearer abc123xyz.def456",                    // bearer header: must redact exactly once
    "api_key=sk-abcdefghijklmnopqrstuvwx",                        // recognizable API-key format: must redact
    "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dGhpc2lzYXNpZ25hdHVyZQ", // JWT: must redact
    "-----BEGIN PRIVATE KEY-----\nMIIBVQIBADANBgkqhkiG\n-----END PRIVATE KEY-----", // PEM block: must redact
  ].join("\n");
  const event = { method: "item/completed", params: { item: { id: "tool-redact-1", type: "commandExecution", command: "cat creds.txt", aggregatedOutput: output } } };
  ui.eventSources[0].emit(event, "9");
  const cards = ui.elements["chat-transcript"].children.filter((node) => node.className === "chat-tool-event");
  const text = cards[cards.length - 1].children[1].textContent;
  assert.match(text, /qa-marker-token=verify-9f3a/, "harmless QA marker must be preserved");
  assert.match(text, /please remember your token for later/, "ordinary prose must be preserved");
  assert.match(text, /password=\[redacted\]/, "real credential must be redacted");
  assert.doesNotMatch(text, /hunter2/);
  assert.match(text, /Authorization: Bearer \[redacted\]/, "bearer header must be redacted");
  assert.doesNotMatch(text, /\[redacted\]\s*\[redacted\]/, "no double-redaction");
  assert.doesNotMatch(text, /abc123xyz/);
  assert.match(text, /api_key=\[redacted\]/, "api key must be redacted");
  assert.doesNotMatch(text, /sk-abcdefghijklmnopqrstuvwx/);
  assert.doesNotMatch(text, /eyJhbGciOiJIUzI1NiJ9/, "JWT must be redacted");
  assert.doesNotMatch(text, /MIIBVQIBADANBgkqhkiG/, "PEM private key body must be redacted");
});

test("redaction is consistent for live user messages and for historical messages replayed from the history API", async () => {
  const live = setup(); await live.window.NexusChat.ready;
  live.backend.sessions = [{ session_id: "CHAT-REDACT-LIVE", project_id: "demo", status: "ACTIVE" }];
  live.backend.turns.set("CHAT-REDACT-LIVE", []);
  await live.window.NexusChat.selectSession("CHAT-REDACT-LIVE");
  live.elements["chat-message-input"].value = "qa-marker-token=verify-9f3a and my password=hunter2";
  live.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const liveUser = live.elements["chat-transcript"].children.find((node) => node.className.includes("chat-message-user"));
  assert.match(liveUser.children[1].textContent, /qa-marker-token=verify-9f3a/);
  assert.match(liveUser.children[1].textContent, /password=\[redacted\]/);

  const historical = setup(); await historical.window.NexusChat.ready;
  historical.backend.sessions = [{ session_id: "CHAT-REDACT-HIST", project_id: "demo", status: "ACTIVE", codex_thread_id: "thread-redact" }];
  historical.backend.turns.set("CHAT-REDACT-HIST", [{ turn_id: "TURN-RH", status: "COMPLETED", response_text: "noted", idempotency_key: "key-rh" }]);
  historical.backend.historyPages.set("CHAT-REDACT-HIST", [{
    items: [
      { turn_id: "TURN-RH", item_id: "item-u", type: "userMessage", text: "qa-marker-token=verify-9f3a and my password=hunter2" },
      { turn_id: "TURN-RH", item_id: "item-a", type: "agentMessage", text: "noted" },
    ],
    next_cursor: null, source: "codex_thread",
  }]);
  await historical.window.NexusChat.selectSession("CHAT-REDACT-HIST");
  await new Promise((resolve) => setImmediate(resolve));
  const histText = historical.elements["chat-transcript"].children.map((node) => node.children.map((child) => child.textContent).join(" ")).join("\n");
  assert.match(histText, /qa-marker-token=verify-9f3a/);
  assert.match(histText, /password=\[redacted\]/);
  assert.doesNotMatch(histText, /hunter2/);
});

test("redaction guard cannot be bypassed by smuggling a fake [redacted] marker before a real secret", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-REDACT-BYPASS", project_id: "demo", status: "ACTIVE" }]; ui.backend.turns.set("CHAT-REDACT-BYPASS", []);
  await ui.window.NexusChat.selectSession("CHAT-REDACT-BYPASS");
  ui.elements["chat-message-input"].value = "inspect"; ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  const output = [
    "password=[redacted]EXAMPLE_SECRET",
    "token=[redacted]-EXAMPLE_SECRET",
    "authorization=[redacted]EXAMPLE_SECRET",
  ].join("\n");
  const event = { method: "item/completed", params: { item: { id: "tool-bypass-1", type: "commandExecution", command: "cat creds.txt", aggregatedOutput: output } } };
  ui.eventSources[0].emit(event, "11");
  const cards = ui.elements["chat-transcript"].children.filter((node) => node.className === "chat-tool-event");
  const text = cards[cards.length - 1].children[1].textContent;
  assert.doesNotMatch(text, /EXAMPLE_SECRET/, "smuggled secret after a fake [redacted] marker must not leak");
  assert.match(text, /password=\[redacted\]/);
  assert.match(text, /token=\[redacted\]/);
  assert.match(text, /authorization=\[redacted\]/);
});

test("ambiguous send is reconciled by idempotency key before another submission", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-UNCERTAIN", project_id: "demo", status: "ACTIVE" }];
  ui.backend.turns.set("CHAT-UNCERTAIN", []);
  await ui.window.NexusChat.selectSession("CHAT-UNCERTAIN");
  ui.backend.networkFailAfterRecording = true;
  ui.elements["chat-message-input"].value = "possibly sent";
  ui.window.NexusChat.sendMessage(); await new Promise((resolve) => setImmediate(resolve)); await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.backend.turns.get("CHAT-UNCERTAIN").length, 1);
  assert.equal(ui.calls.filter((call) => call.method === "POST" && call.path.endsWith("/turns")).length, 1);
  assert.equal(ui.eventSources.length, 1, "the existing turn is attached rather than submitted again");
  assert.equal(ui.elements["chat-send-button"].disabled, true);
  assert.match(ui.elements["chat-status-text"].textContent, /reconciled with server turn/);
});

test("interrupt calls the real endpoint and failed project creation surfaces backend diagnostics", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-STOP", project_id: "demo", status: "ACTIVE" }]; ui.backend.turns.set("CHAT-STOP", []);
  await ui.window.NexusChat.selectSession("CHAT-STOP");
  ui.elements["chat-message-input"].value = "slow"; ui.window.NexusChat.sendMessage();
  await new Promise((resolve) => setImmediate(resolve));
  ui.elements["chat-stop-button"].dispatch("click"); await new Promise((resolve) => setImmediate(resolve));
  assert.ok(ui.calls.some((call) => call.method === "POST" && call.path.includes("/interrupt?turn_id=TURN-1")));
  ui.backend.createError = "Project is disabled";
  ui.elements["chat-new-conversation"].dispatch("click"); await new Promise((resolve) => setImmediate(resolve));
  assert.match(ui.elements["chat-status-text"].textContent, /Project is disabled/);
});

test("Chat navigation preserves active Run and Program views", () => {
  for (const [mode, view] of [["run", "detail"], ["program", "detail"]]) {
    const ui = setup(); ui.window.NexusMode.mode = mode; ui.body.dataset.view = view;
    if (mode === "run") ui.run.classList.add("visible"); else ui.program.classList.add("visible");
    ui.window.NexusChat.enter();
    assert.equal(ui.body.dataset.view, "chat"); assert.equal(ui.window.NexusMode.mode, mode);
    ui.window.NexusChat.exit();
    assert.equal(ui.body.dataset.view, view); assert.equal(ui.window.NexusMode.mode, mode);
    assert.equal(ui.run.classList.contains("visible"), mode === "run");
    assert.equal(ui.program.classList.contains("visible"), mode === "program");
  }
});

test("historical user and assistant messages are rendered as safe text, never as HTML", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-XSS", project_id: "demo", status: "ACTIVE", codex_thread_id: "thread-5" }];
  ui.backend.turns.set("CHAT-XSS", [{ turn_id: "TURN-X", status: "COMPLETED", response_text: "ok", idempotency_key: "key-x" }]);
  ui.backend.historyPages.set("CHAT-XSS", [{
    items: [
      { turn_id: "TURN-X", item_id: "item-user-x", type: "userMessage", text: "<img src=x onerror=alert(1)>" },
      { turn_id: "TURN-X", item_id: "item-asst-x", type: "agentMessage", text: "<script>alert(2)</script>" },
    ],
    next_cursor: null, source: "codex_thread",
  }]);
  await ui.window.NexusChat.selectSession("CHAT-XSS");
  await new Promise((resolve) => setImmediate(resolve));
  const messages = ui.elements["chat-transcript"].children.filter((node) => node.className.indexOf("chat-message") === 0);
  assert.ok(messages.length >= 2);
  messages.forEach((node) => { assert.equal(node.children.length <= 2 ? true : node.children.every((child) => child.children.length === 0), true); });
  const text = messages.map((node) => node.children.map((child) => child.textContent).join(" ")).join("\n");
  assert.match(text, /<img src=x onerror=alert\(1\)>/);
  assert.match(text, /<script>alert\(2\)<\/script>/);
});

test("empty conversation (new thread, no items) shows the empty state, never a fabricated message", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  ui.backend.sessions = [{ session_id: "CHAT-EMPTY2", project_id: "demo", status: "CREATED", codex_thread_id: "thread-6" }];
  ui.backend.turns.set("CHAT-EMPTY2", []);
  ui.backend.historyPages.set("CHAT-EMPTY2", [{ items: [], next_cursor: null, source: "codex_thread" }]);
  await ui.window.NexusChat.selectSession("CHAT-EMPTY2");
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(ui.elements["chat-transcript"].querySelector(".chat-empty-state"));
});

test("frontend uses real API endpoints and never persists chat in browser storage", () => {
  assert.match(chatSource, /\/chat\/sessions\/.*\/events\?turn_id=/);
  assert.match(chatSource, /idempotency_key/);
  assert.match(chatSource, /\/interrupt\?turn_id=/);
  assert.match(chatSource, /\/chat\/sessions\/.*\/history/);
  assert.doesNotMatch(chatSource, /localStorage|sessionStorage|innerHTML\s*=/);
});

test("Codex Native is the default backend: models and the new session both request it explicitly", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  assert.ok(ui.calls.some((call) => call.path.startsWith("/api/chat/models?backend=codex_native")), "native catalog is requested first");
  assert.ok(!ui.calls.some((call) => call.path.startsWith("/api/chat/models?backend=omniroute")));
  assert.match(nexusTemplateSource, /<option value="codex_native">Codex Native \(local login, no OmniRoute\)<\/option>/);
  ui.elements["chat-project-select"].value = "demo"; ui.elements["chat-project-select"].dispatch("change");
  ui.elements["chat-model-select"].value = "cx/gpt-5.6-sol"; ui.elements["chat-model-select"].dispatch("change");
  ui.elements["chat-unknown-cost-confirm"].checked = true;
  ui.elements["chat-new-conversation"].dispatch("click");
  await new Promise((resolve) => setImmediate(resolve)); await new Promise((resolve) => setImmediate(resolve));
  const body = ui.calls.find((call) => call.method === "POST" && call.path === "/api/chat/sessions").body;
  assert.equal(body.backend, "codex_native");
});

test("switching the backend selector reloads that backend's catalog and creates sessions for it", async () => {
  const ui = setup(); await ui.window.NexusChat.ready;
  const selector = ui.elements["chat-backend-select"];
  selector.value = "omniroute"; selector.dispatch("change");
  await new Promise((resolve) => setImmediate(resolve)); await new Promise((resolve) => setImmediate(resolve));
  assert.ok(ui.calls.some((call) => call.path.startsWith("/api/chat/models?backend=omniroute&refresh=true")));
  ui.elements["chat-project-select"].value = "demo"; ui.elements["chat-project-select"].dispatch("change");
  ui.elements["chat-model-select"].value = "cx/gpt-5.6-sol"; ui.elements["chat-model-select"].dispatch("change");
  ui.elements["chat-unknown-cost-confirm"].checked = true;
  ui.elements["chat-new-conversation"].dispatch("click");
  await new Promise((resolve) => setImmediate(resolve)); await new Promise((resolve) => setImmediate(resolve));
  const body = ui.calls.find((call) => call.method === "POST" && call.path === "/api/chat/sessions").body;
  assert.equal(body.backend, "omniroute");
});
