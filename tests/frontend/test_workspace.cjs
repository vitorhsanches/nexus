const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "../..");
const source = fs.readFileSync(path.join(root, "nexus/web/static/js/workspace.js"), "utf8");
const template = fs.readFileSync(path.join(root, "nexus/web/templates/workspace.html"), "utf8");
const ws = require(path.join(root, "nexus/web/static/js/workspace.js"));

class Element {
  constructor(tag, id) {
    this.tagName = String(tag).toUpperCase(); this.id = id || ""; this.children = []; this.parentNode = null;
    this.attributes = {}; this.listeners = {}; this.dataset = {}; this.hidden = false; this.disabled = false;
    this.value = ""; this.checked = false; this.open = false; this.textContent = ""; this.className = ""; this.title = "";
  }
  setAttribute(key, value) { this.attributes[key] = String(value); if (key === "value") this.value = String(value); }
  getAttribute(key) { return key in this.attributes ? this.attributes[key] : null; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(key, callback) { (this.listeners[key] ||= []).push(callback); }
  async dispatch(key, event = {}) { for (const callback of this.listeners[key] || []) await callback({ preventDefault() {}, stopPropagation() {}, target: this, ...event }); }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  append(...children) { children.forEach((child) => this.appendChild(child)); }
  replaceChildren(...children) { this.children.forEach((child) => { child.parentNode = null; }); this.children = []; this.append(...children); }
  contains(node) { for (let current = node; current; current = current.parentNode) if (current === this) return true; return false; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  all() { return this.children.flatMap((child) => [child, ...child.all()]); }
  text() { return [this.textContent, ...this.children.map((child) => child.text())].join(" "); }
}

function makeDocument() {
  const ids = [...template.matchAll(/id="([^"]+)"/g)].map((match) => match[1]);
  const byId = Object.fromEntries(ids.map((id) => [id, new Element("div", id)]));
  return {
    activeElement: null,
    byId,
    getElementById: (id) => byId[id] || null,
    createElement: (tag) => new Element(tag),
  };
}

function pane(overrides = {}) {
  return {
    pane_id: "PANE-A", project_id: "demo", plan_id: null, origin: "manual", role: "implementer",
    role_label: "Implementação", title: "Agente A", model: "free/model",
    layout: { position: 0, collapsed: false, expanded: false, hidden: false },
    session: { session_id: "DEV-A", status: "ACTIVE", branch: "nexus/dev/DEV-A", worktree_path: "C:/wt/DEV-A", base_sha: "abc" },
    mission_count: 0, latest_mission: null, busy: false, created_at: "2026-01-01", updated_at: "2026-01-01",
    ...overrides,
  };
}

function harness(routes) {
  const calls = [];
  const fetch = async (url, init) => {
    const method = init.method || "GET";
    const body = init.body ? JSON.parse(init.body) : undefined;
    calls.push({ method, url, body });
    const key = `${method} ${url.split("?")[0]}`;
    const handler = routes[key];
    const result = typeof handler === "function" ? handler(body, url) : handler;
    if (result === undefined) return { ok: false, status: 404, json: async () => ({ detail: "not found" }) };
    if (result && result.__error) return { ok: false, status: result.status, json: async () => ({ detail: result.detail }) };
    return { ok: true, status: 200, json: async () => result };
  };
  const store = {};
  const storage = { getItem: (key) => store[key] ?? null, setItem: (key, value) => { store[key] = value; } };
  const doc = makeDocument();
  const timers = [];
  const app = ws.createWorkspace({
    document: doc, fetch, storage,
    setTimeout: (callback, ms) => { timers.push(ms); return timers.length; }, clearTimeout: () => {},
  });
  return { app, doc, calls, store, timers };
}

function stateWith(panes, extra = {}) {
  return {
    project_id: "demo", panes, hidden_panes: [], limits: { max_open_panes: 6, max_auto_panes: 3 },
    roles: { implementer: "Implementação", tester: "Testes" },
    execution_enabled: true, integration_enabled: false, policy_version: "workspace-policy/1", ...extra,
  };
}

const baseRoutes = (state) => ({
  "GET /api/projects": { projects: [
    { id: "demo", name: "Demo", enabled: 1, dev_enabled: 1 },
    { id: "nexus", name: "Nexus", enabled: 1, dev_enabled: 0 },
  ] },
  "GET /api/dev/models": { models: [{ id: "free/model", cost_status: "free" }, { id: "std/model", cost_status: "standard" }], catalog_error: null },
  "GET /api/workspace/plans": { plans: [] },
  "GET /api/dev/test-profiles": { profiles: [{ id: "python-unittest", name: "Python unittest" }] },
  "GET /api/workspace/state": () => state.current,
});

const findButton = (node, label) => node.all().find((child) => child.tagName === "BUTTON" && (child.textContent === label || child.getAttribute("aria-label") === label));

test("pure helpers: status, density, ordering, mission payload and honest cost labels", () => {
  assert.equal(ws.paneStatus(pane()).label, "Ocioso");
  assert.equal(ws.paneStatus(pane({ latest_mission: { status: "RUNNING" } })).tone, "run");
  assert.equal(ws.paneStatus(pane({ latest_mission: { status: "READY_FOR_REVIEW" } })).label, "Aguardando revisão");
  assert.equal(ws.paneStatus(pane({ session: { status: "FAILED" } })).tone, "bad");
  assert.equal(ws.density([]), "empty");
  assert.equal(ws.density([pane()]), "compact");
  assert.equal(ws.density([pane(), pane({ pane_id: "B" })]), "multi");

  const panes = [pane({ pane_id: "A", layout: { position: 0 } }), pane({ pane_id: "B", layout: { position: 1 } })];
  assert.deepEqual(ws.moveUpdates(panes, "A", 1), [
    { pane_id: "A", layout: { position: 1 } }, { pane_id: "B", layout: { position: 0 } },
  ]);
  assert.deepEqual(ws.moveUpdates(panes, "A", -1), []);

  assert.match(ws.missionPayload(pane(), { description: "x", testProfile: "p", confirmed: false }).error, /Confirme/);
  assert.match(ws.missionPayload(pane({ model: null }), { description: "x", testProfile: "p", confirmed: true }).error, /modelo/);
  assert.deepEqual(ws.missionPayload(pane(), { description: " do ", testProfile: "p", confirmed: true }).payload,
    { description: "do", confirm_model: "free/model", test_profile: "p" });

  assert.equal(ws.costLabel("free"), "gratuito");
  assert.match(ws.costLabel("standard"), /preço não informado/);
  assert.match(ws.costLabel(undefined), /desconhecido/);
  assert.equal(ws.canStartMission(pane({ busy: true }), { execution_enabled: true }), false);
  assert.equal(ws.canStartMission(pane(), { execution_enabled: false }), false);
});

test("untrusted text is never rendered as HTML and the page exposes the advanced tools", () => {
  assert.doesNotMatch(source, /innerHTML|outerHTML|insertAdjacentHTML|document\.write/);
  assert.match(template, /href="\/nexus"/);
  assert.doesNotMatch(source, /\/apply|\/reject|method: "DELETE"/);
});

test("init selects the only authorized project, disables unauthorized ones and renders panes", async () => {
  const state = { current: stateWith([pane(), pane({ pane_id: "PANE-B", title: "Agente B", layout: { position: 1 } })]) };
  const { app, doc, store } = harness(baseRoutes(state));
  await app.init();
  const options = doc.byId["ws-project-select"].children;
  assert.equal(doc.byId["ws-project-select"].value, "demo");
  assert.equal(options.find((option) => option.value === "nexus").disabled, true);
  assert.equal(doc.byId["ws-main"].dataset.density, "multi");
  assert.equal(doc.byId["ws-grid"].children.length, 2);
  assert.equal(doc.byId["ws-add-pane"].disabled, false);
  assert.match(doc.byId["ws-gates"].text(), /Apply desligado/);
  assert.equal(JSON.parse(store["nexus.workspace.v1"]).project, "demo");
});

test("selection persists across a reload and closing a pane only hides it", async () => {
  const state = { current: stateWith([pane(), pane({ pane_id: "PANE-B", title: "Agente B", layout: { position: 1 } })]) };
  const first = harness(baseRoutes(state));
  await first.app.init();
  first.app.select("PANE-B");
  assert.equal(first.doc.byId["ws-grid"].children[1].dataset.selected, "true");

  const routes = { ...baseRoutes(state), "PATCH /api/workspace/panes/PANE-B": (body) => { state.current = stateWith([pane()], { hidden_panes: [pane({ pane_id: "PANE-B", title: "Agente B" })] }); return body; } };
  const second = harness(routes);
  Object.assign(second.store, first.store);
  await second.app.init();
  assert.equal(second.app.state.selectedPaneId, "PANE-B");
  await findButton(second.doc.byId["ws-grid"].children[1], "Ocultar (a sessão é preservada)").dispatch("click");
  const patch = second.calls.find((call) => call.method === "PATCH");
  assert.deepEqual(patch.body, { layout: { hidden: true } });
  assert.ok(!second.calls.some((call) => call.method === "DELETE"));
  assert.equal(second.doc.byId["ws-hidden-count"].textContent, "1");
});

test("Maestro plan renders an auditable proposal and creates panes only on explicit click", async () => {
  const state = { current: stateWith([]) };
  const plan = {
    plan_id: "WSPLAN-1", task_text: "backend e frontend", policy_version: "workspace-policy/1", complexity: "multi", limit: 3,
    dropped_by_limit: [], panes: [
      { role: "backend", title: "Backend", model: "free/model", cost_status: "free", cost_authorization_required: false, signals: ["\\bbackend\\b"] },
      { role: "frontend", title: "Frontend", model: "free/model", cost_status: "free", cost_authorization_required: false, signals: ["\\bfrontend\\b"] },
    ],
  };
  const routes = {
    ...baseRoutes(state),
    "POST /api/workspace/plans": plan,
    "POST /api/workspace/panes": (body) => { state.current = stateWith([pane({ pane_id: "PANE-N", role: body.role })]); return pane({ pane_id: "PANE-N" }); },
  };
  const { app, doc, calls } = harness(routes);
  await app.init();
  assert.equal(doc.byId["ws-main"].dataset.density, "empty");
  doc.byId["ws-maestro-input"].value = "backend e frontend";
  await app.submitTask();
  assert.ok(!calls.some((call) => call.url === "/api/workspace/panes"), "planning never creates panes by itself");
  const log = doc.byId["ws-maestro-log"];
  assert.match(log.text(), /2 responsabilidades/);
  await findButton(log, "Criar").dispatch("click");
  const created = calls.find((call) => call.method === "POST" && call.url === "/api/workspace/panes");
  assert.deepEqual(created.body, { project_id: "demo", role: "backend", title: "Backend", model: "free/model", plan_id: "WSPLAN-1" });
  assert.equal(doc.byId["ws-main"].dataset.density, "compact");
});

test("missions require explicit model confirmation and are blocked while the pane is busy", async () => {
  const state = { current: stateWith([pane({ model: "std/model" })]) };
  const routes = { ...baseRoutes(state), "POST /api/workspace/panes/PANE-A/missions": (body) => ({ mission: { mission_id: "M" }, body }) };
  const { app, doc, calls } = harness(routes);
  await app.init();
  const card = doc.byId["ws-grid"].children[0];
  const form = card.all().find((node) => node.className === "ws-pane-form");
  const [textarea, , label, start] = form.children;
  assert.match(label.text(), /Confirmo o uso de std\/model \(custo standard — preço não informado\)/);
  textarea.value = "implemente";
  await start.dispatch("click");
  assert.ok(!calls.some((call) => call.url.endsWith("/missions")), "no mission without confirmation");
  assert.match(doc.byId["ws-error"].textContent, /Confirme/);
  label.children[0].checked = true;
  await start.dispatch("click");
  const sent = calls.find((call) => call.url.endsWith("/missions"));
  assert.deepEqual(sent.body, { description: "implemente", confirm_model: "std/model", test_profile: "python-unittest" });

  state.current = stateWith([pane({ busy: true, latest_mission: {
    mission_id: "M", status: "RUNNING", requested_model: "std/model", attempt_count: 1, tests: [], changed_files: [],
    error_code: null, integration_applied: false,
  } })]);
  await app.refresh({ force: true });
  const busyForm = doc.byId["ws-grid"].children[0].all().find((node) => node.className === "ws-pane-form");
  assert.equal(busyForm.children[3].disabled, true);
});

test("model change goes through PATCH, errors are shown, and polling never steals an active edit", async () => {
  const state = { current: stateWith([pane()]) };
  const routes = {
    ...baseRoutes(state),
    "PATCH /api/workspace/panes/PANE-A": { __error: true, status: 403, detail: { code: "MODEL_NOT_ALLOWED", message: "nope" } },
  };
  const { app, doc, calls, timers } = harness(routes);
  await app.init();
  const card = doc.byId["ws-grid"].children[0];
  const select = card.all().find((node) => node.tagName === "SELECT" && /Modelo de/.test(node.getAttribute("aria-label") || ""));
  select.value = "std/model";
  await select.dispatch("change");
  assert.deepEqual(calls.find((call) => call.method === "PATCH").body, { model: "std/model" });
  assert.match(doc.byId["ws-error"].textContent, /MODEL_NOT_ALLOWED: nope/);
  assert.equal(timers.at(-1), 15000);

  const typing = doc.byId["ws-grid"].children[0].all().find((node) => node.tagName === "TEXTAREA");
  doc.activeElement = typing;
  const before = doc.byId["ws-grid"].children[0];
  await app.refresh();
  assert.equal(doc.byId["ws-grid"].children[0], before, "background refresh keeps the pane being edited");
  await app.refresh({ force: true });
  assert.notEqual(doc.byId["ws-grid"].children[0], before);
});
