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
  return { activeElement: null, byId, getElementById: (id) => byId[id] || null, createElement: (tag) => new Element(tag) };
}

function pane(overrides = {}) {
  return {
    pane_id: "PANE-A", project_id: "demo", plan_id: null, origin: "manual", role: "implementer",
    role_label: "Implementação", title: "Agente A", model: "free/model", cost_status: "free",
    layout: { position: 0, collapsed: false, expanded: false, hidden: false },
    session: { session_id: "DEV-A", status: "ACTIVE", branch: "nexus/dev/DEV-A", worktree_path: "C:/wt/DEV-A", base_sha: "abc" },
    mission_count: 0, latest_mission: null, busy: false, brief: null, dependencies: [], messages: [],
    plan_agent_key: null, created_at: "2026-01-01", updated_at: "2026-01-01",
    ...overrides,
  };
}

const MODELS = [{ id: "free/model", cost_status: "free" }, { id: "std/model", cost_status: "standard" },
  { id: "low/model", cost_status: "low_cost" }, { id: "unknown/model", cost_status: "unknown" }];

function plan(overrides = {}) {
  return {
    plan_id: "WSPLAN-1", source: "llm", status: "PROPOSED", complexity: "multi", summary: "orders", explanation: "Dois agentes.",
    fallback_reason: null, agents: [
      { key: "api", role: "backend", title: "API", model: "free/model", cost_status: "free", task: "Build API",
        acceptance_criteria: ["endpoint works"], depends_on: [], touches: ["api/"], independent: true },
      { key: "ui", role: "frontend", title: "Tela", model: "std/model", cost_status: "standard", task: "Build UI",
        acceptance_criteria: ["renders"], depends_on: ["api"], touches: ["web/"], independent: false },
    ],
    ...overrides,
  };
}

function maestroState(messages = [], extra = {}) {
  return {
    conversation_id: "MAESTRO-1", project_id: "demo",
    planning: { authorized: true, model: "free/model", cost_status: "free", mode: "read-only" },
    busy: false, messages, ...extra,
  };
}

function stateWith(panes, extra = {}) {
  return {
    project_id: "demo", panes, hidden_panes: [], limits: { max_open_panes: 6, max_auto_panes: 3 },
    roles: { implementer: "Implementação", tester: "Testes" },
    execution_enabled: true, integration_enabled: false, policy_version: "workspace-policy/1", ...extra,
  };
}

function harness(routes) {
  const calls = [];
  const fetch = async (url, init) => {
    const method = init.method || "GET";
    const body = init.body ? JSON.parse(init.body) : undefined;
    calls.push({ method, url, body });
    const handler = routes[`${method} ${url.split("?")[0]}`];
    const result = typeof handler === "function" ? handler(body, url) : handler;
    if (result === undefined) return { ok: false, status: 404, json: async () => ({ detail: "not found" }) };
    if (result && result.__error) return { ok: false, status: result.status, json: async () => ({ detail: result.detail }) };
    return { ok: true, status: 200, json: async () => result };
  };
  const store = {};
  const storage = { getItem: (key) => store[key] ?? null, setItem: (key, value) => { store[key] = value; } };
  const doc = makeDocument();
  const timers = [];
  const app = ws.createWorkspace({ document: doc, fetch, storage, setTimeout: (_cb, ms) => { timers.push(ms); return timers.length; }, clearTimeout: () => {} });
  return { app, doc, calls, store, timers };
}

const baseRoutes = (box) => ({
  "GET /api/projects": { projects: [{ id: "demo", name: "Demo", enabled: 1, dev_enabled: 1 }, { id: "nexus", name: "Nexus", enabled: 1, dev_enabled: 0 }] },
  "GET /api/dev/models": { models: MODELS, catalog_error: null },
  "GET /api/dev/test-profiles": { profiles: [{ id: "python-unittest", name: "Python unittest" }] },
  "GET /api/workspace/state": () => box.state,
  "GET /api/workspace/maestro": () => box.maestro,
});

const findButton = (node, label) => node.all().find((child) => child.tagName === "BUTTON" && (child.textContent === label || child.getAttribute("aria-label") === label));
const findButtonStarting = (node, prefix) => node.all().find((child) => child.tagName === "BUTTON" && child.textContent.startsWith(prefix));

test("pure helpers: status, density, ordering, honest cost labels and plan sources", () => {
  assert.equal(ws.paneStatus(pane()).label, "Ocioso");
  assert.equal(ws.paneStatus(pane({ latest_mission: { status: "RUNNING" } })).tone, "run");
  assert.equal(ws.paneStatus(pane({ messages: [{ status: "RUNNING" }] })).label, "Respondendo");
  assert.equal(ws.paneStatus(pane({ dependencies: [{ ready: false }] })).label, "Aguardando dependência");
  assert.equal(ws.density([]), "empty");
  assert.equal(ws.density([pane()]), "compact");
  const panes = [pane({ pane_id: "A", layout: { position: 0 } }), pane({ pane_id: "B", layout: { position: 1 } })];
  assert.deepEqual(ws.moveUpdates(panes, "A", 1), [{ pane_id: "A", layout: { position: 1 } }, { pane_id: "B", layout: { position: 0 } }]);
  assert.match(ws.costLabel("standard"), /preço não informado/);
  assert.equal(ws.planSourceLabel({ source: "llm" }).text, "Plano do Maestro (IA)");
  assert.match(ws.planSourceLabel({ source: "deterministic-fallback" }).text, /fallback/);
  assert.match(ws.planSourceLabel({ source: "deterministic" }).text, /sem IA/);
  assert.equal(ws.outcomeLabel({ outcome: "conversation" }).text, "Somente conversa");
  assert.match(ws.outcomeLabel({ outcome: "code_changed" }).text, /verificar/);
  assert.equal(ws.canStartMission(pane({ dependencies: [{ ready: false }] }), { execution_enabled: true }), false);
});

test("mission and message payloads require confirmation unless the approved plan covers the use", () => {
  const authorized = pane({ brief: { task: "t", acceptance_criteria: [], authorization: { applies: true } } });
  assert.deepEqual(ws.missionPayload(authorized, { description: "", testProfile: "p", confirmed: false }).payload, { test_profile: "p" });
  assert.deepEqual(ws.messagePayload(authorized, { text: " oi " }).payload, { text: "oi" });
  assert.match(ws.missionPayload(pane(), { description: "x", testProfile: "p", confirmed: false }).error, /Confirme/);
  assert.deepEqual(ws.missionPayload(pane(), { description: " do ", testProfile: "p", confirmed: true }).payload,
    { test_profile: "p", description: "do", confirm_model: "free/model" });
  assert.match(ws.messagePayload(pane(), { text: "oi", confirmed: false }).error, /Confirme/);
  assert.deepEqual(ws.messagePayload(pane(), { text: "oi", confirmed: true }).payload, { text: "oi", confirm_model: "free/model" });
  const exhausted = pane({ brief: { task: "t", acceptance_criteria: [], authorization: { applies: false } } });
  assert.match(ws.messagePayload(exhausted, { text: "oi" }).error, /Confirme/);
});

test("approval payload needs every model and explicit cost confirmation for paid routes", () => {
  const p = plan();
  assert.match(ws.approvalPayload(p, { models: { api: "free/model" }, maxTurns: 1, timeoutMinutes: 15 }, MODELS).error, /Tela/);
  const unpaid = ws.approvalPayload(p, { models: { api: "free/model", ui: "std/model" }, maxTurns: 2, timeoutMinutes: 10 }, MODELS);
  assert.match(unpaid.error, /std\/model/);
  const ok = ws.approvalPayload(p, { models: { api: "free/model", ui: "std/model" }, maxTurns: 2, timeoutMinutes: 10, confirmCosts: true }, MODELS);
  assert.deepEqual(ok.payload, { agent_models: { api: "free/model", ui: "std/model" }, max_turns_per_agent: 2, mission_timeout_seconds: 600, confirm_costs: true });
  const free = ws.approvalPayload(p, { models: { api: "free/model", ui: "free/model" }, maxTurns: 1, timeoutMinutes: 15 }, MODELS);
  assert.equal(free.payload.confirm_costs, false);
  for (const model of ["low/model", "unknown/model"]) {
    const selection = { api: "free/model", ui: model };
    assert.match(ws.approvalPayload(p, { models: selection, maxTurns: 2, timeoutMinutes: 10 }, MODELS).error, /Confirme o custo/);
    assert.equal(ws.approvalPayload(p, { models: selection, maxTurns: 2, timeoutMinutes: 10, confirmCosts: true }, MODELS).payload.confirm_costs, true);
  }
});

test("manual non-free model needs separate route and financial consent", () => {
  const paid = pane({ model: "low/model", cost_status: "low_cost" });
  const mission = { description: "implement", testProfile: "unit", confirmed: true };
  assert.match(ws.missionPayload(paid, mission).error, /Confirme o custo/);
  assert.deepEqual(ws.missionPayload(paid, { ...mission, confirmCost: true }).payload,
    { description: "implement", test_profile: "unit", confirm_model: "low/model", confirm_cost: true });
  assert.match(ws.messagePayload(paid, { text: "oi", confirmed: true }).error, /Confirme o custo/);
  assert.deepEqual(ws.messagePayload(paid, { text: "oi", confirmed: true, confirmCost: true }).payload,
    { text: "oi", confirm_model: "low/model", confirm_cost: true });
  assert.match(ws.missionPayload(pane({ model: "unknown/model", cost_status: null }), mission).error, /Confirme o custo/);
});

test("low-cost planning and agent controls remain unchecked after reload", async () => {
  const box = { state: stateWith([pane({ model: "low/model", cost_status: "low_cost" })]),
    maestro: maestroState([], { planning: { authorized: false, model: null, cost_status: null, mode: "read-only" } }) };
  for (let reload = 0; reload < 2; reload++) {
    const { app, doc } = harness(baseRoutes(box));
    await app.init();
    const planning = doc.byId["ws-maestro-planning"];
    const select = planning.all().find((node) => node.tagName === "SELECT");
    select.value = "low/model";
    await select.dispatch("change");
    const cost = doc.byId["ws-maestro-planning"].all().find((node) => node.className === "ws-check");
    assert.equal(cost.hidden, false);
    assert.match(cost.text(), /low\/model.*baixo custo.*apenas para planejar/);
    assert.equal(cost.children[0].checked, false);
    const labels = doc.byId["ws-grid"].all().filter((node) => node.className === "ws-check" && !node.hidden);
    assert.ok(labels.some((node) => /Confirmo o uso de low\/model/.test(node.text())));
    assert.ok(labels.some((node) => /Autorizo o custo de low\/model/.test(node.text())));
    assert.ok(labels.every((node) => node.children[0].checked === false));
  }
});

test("changing a plan model clears the prior cost checkbox", async () => {
  const box = { state: stateWith([]), maestro: maestroState([
    { message_id: "m", author: "maestro", text: "Plano", status: "COMPLETED", plan: plan() },
  ]) };
  const { app, doc } = harness(baseRoutes(box));
  await app.init();
  let log = doc.byId["ws-maestro-log"];
  let cost = log.all().find((node) => node.className === "ws-check" && /modelos não gratuitos/.test(node.text()));
  cost.children[0].checked = true;
  await cost.children[0].dispatch("change");
  const select = log.all().find((node) => node.getAttribute("aria-label") === "Modelo de Tela");
  select.value = "low/model";
  await select.dispatch("change");
  log = doc.byId["ws-maestro-log"];
  cost = log.all().find((node) => node.className === "ws-check" && /modelos não gratuitos/.test(node.text()));
  assert.match(cost.text(), /low\/model/);
  assert.equal(cost.children[0].checked, false);
});

test("untrusted text is never rendered as HTML and no apply/merge/delete call exists", () => {
  assert.doesNotMatch(source, /innerHTML|outerHTML|insertAdjacentHTML|document\.write/);
  assert.doesNotMatch(source, /\/apply|\/reject|\/merge|method: "DELETE"/);
  assert.match(template, /href="\/nexus"/);
});

test("init loads the authorized project, Maestro conversation and panes", async () => {
  const box = { state: stateWith([pane(), pane({ pane_id: "PANE-B", title: "Agente B", layout: { position: 1 } })]), maestro: maestroState() };
  const { app, doc, store } = harness(baseRoutes(box));
  await app.init();
  assert.equal(doc.byId["ws-project-select"].value, "demo");
  assert.equal(doc.byId["ws-project-select"].children.find((option) => option.value === "nexus").disabled, true);
  assert.equal(doc.byId["ws-main"].dataset.density, "multi");
  assert.equal(doc.byId["ws-grid"].children.length, 2);
  assert.match(doc.byId["ws-maestro-planning"].text(), /IA: free\/model · gratuito · somente leitura/);
  assert.equal(doc.byId["ws-maestro-send"].disabled, false);
  assert.match(doc.byId["ws-gates"].text(), /Apply desligado/);
  assert.equal(JSON.parse(store["nexus.workspace.v1"]).project, "demo");
});

test("Maestro chat sends messages, shows progress and renders the validated plan with explanation", async () => {
  const box = { state: stateWith([]), maestro: maestroState() };
  const routes = {
    ...baseRoutes(box),
    "POST /api/workspace/maestro/messages": (body) => {
      box.maestro = maestroState([{ message_id: "u1", author: "user", text: body.text, status: "PENDING" }], { busy: true });
      return box.maestro;
    },
  };
  const { app, doc, calls, timers } = harness(routes);
  await app.init();
  doc.byId["ws-maestro-input"].value = "Crie API e tela";
  await app.sendMaestro();
  assert.deepEqual(calls.find((call) => call.url === "/api/workspace/maestro/messages").body, { project_id: "demo", text: "Crie API e tela" });
  assert.match(doc.byId["ws-maestro-log"].text(), /analisando o projeto/);
  assert.equal(doc.byId["ws-maestro-send"].disabled, true);
  assert.equal(timers.at(-1), 2500);
  assert.ok(!calls.some((call) => call.url === "/api/workspace/panes"), "talking to the Maestro never creates panes");

  box.maestro = maestroState([
    { message_id: "u1", author: "user", text: "Crie API e tela", status: "COMPLETED" },
    { message_id: "m1", author: "maestro", text: "Dois agentes: a tela depende da API.", status: "COMPLETED", plan: plan() },
  ]);
  await app.refresh({ force: true });
  const log = doc.byId["ws-maestro-log"];
  assert.match(log.text(), /Dois agentes: a tela depende da API/);
  assert.match(log.text(), /Plano do Maestro \(IA\)/);
  assert.match(log.text(), /depois de API/);
  assert.match(log.text(), /2 agentes · em sequência/);
});

test("approving a plan creates all planned agents in one request with exact models and limits", async () => {
  const box = { state: stateWith([]), maestro: maestroState([{ message_id: "m1", author: "maestro", text: "ok", status: "COMPLETED", plan: plan() }]) };
  const routes = {
    ...baseRoutes(box),
    "POST /api/workspace/plans/WSPLAN-1/approve": (body) => {
      box.state = stateWith([pane({ pane_id: "PANE-API", origin: "maestro", title: "API" }), pane({ pane_id: "PANE-UI", origin: "maestro", title: "Tela", layout: { position: 1 } })]);
      box.maestro = maestroState([{ message_id: "m1", author: "maestro", text: "ok", status: "COMPLETED", plan: plan({ status: "APPROVED" }) }]);
      return { plan: plan({ status: "APPROVED", approval: { errors: [] } }), panes: box.state.panes, body };
    },
  };
  const { app, doc, calls } = harness(routes);
  await app.init();
  const log = doc.byId["ws-maestro-log"];
  const approve = findButtonStarting(log, "Aprovar e criar 2 agentes");
  await approve.dispatch("click");
  assert.ok(!calls.some((call) => call.url.endsWith("/approve")), "paid route needs explicit cost confirmation first");
  assert.match(doc.byId["ws-error"].textContent, /std\/model/);

  const costBox = log.all().find((node) => node.className === "ws-check" && /não gratuitos/.test(node.text()));
  costBox.children[0].checked = true;
  await costBox.children[0].dispatch("change");
  await findButtonStarting(doc.byId["ws-maestro-log"], "Aprovar e criar").dispatch("click");
  const sent = calls.filter((call) => call.url.endsWith("/approve"));
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0].body, { agent_models: { api: "free/model", ui: "std/model" }, max_turns_per_agent: 1, mission_timeout_seconds: 900, confirm_costs: true });
  assert.ok(!calls.some((call) => call.method === "POST" && call.url === "/api/workspace/panes"), "no per-pane create clicks");
  assert.equal(doc.byId["ws-grid"].children.length, 2);
  assert.equal(doc.byId["ws-grid"].children[0].dataset.origin, "maestro");
  assert.ok(!findButtonStarting(doc.byId["ws-maestro-log"], "Aprovar"), "an approved plan cannot be approved again");
});

test("fallback plans are labelled and a planning model needs explicit cost authorization", async () => {
  const box = {
    state: stateWith([]),
    maestro: maestroState([{ message_id: "m1", author: "maestro", text: "inválida", status: "COMPLETED",
      plan: plan({ source: "deterministic-fallback", fallback_reason: "Resposta do Maestro inválida: bad", agents: [plan().agents[0]] }) }],
    { planning: { authorized: false, model: null, cost_status: null, mode: "read-only" } }),
  };
  const routes = { ...baseRoutes(box), "POST /api/workspace/maestro/authorize": (body) => ({ ...box.maestro, planning: { authorized: true, model: body.model, cost_status: "standard", mode: "read-only" }, body }) };
  const { app, doc, calls } = harness(routes);
  await app.init();
  assert.match(doc.byId["ws-maestro-log"].text(), /Proposta determinística — fallback/);
  assert.match(doc.byId["ws-maestro-log"].text(), /Motivo do fallback: Resposta do Maestro inválida/);
  assert.match(doc.byId["ws-maestro-planning"].text(), /IA do Maestro desligada/);
  const planning = doc.byId["ws-maestro-planning"];
  const select = planning.all().find((node) => node.tagName === "SELECT");
  select.value = "std/model";
  await select.dispatch("change");
  const check = doc.byId["ws-maestro-planning"].all().find((node) => node.className === "ws-check");
  assert.equal(check.hidden, false);
  assert.match(check.text(), /apenas para planejar/);
  check.children[0].checked = true;
  await findButton(doc.byId["ws-maestro-planning"], "Ativar Maestro").dispatch("click");
  assert.deepEqual(calls.find((call) => call.url === "/api/workspace/maestro/authorize").body, { project_id: "demo", model: "std/model", confirm_cost: true });
});

test("each agent has its own conversation with outcome labels and plan-authorized sends", async () => {
  const brief = { task: "Build API", acceptance_criteria: ["works"], depends_on: [], summary: "s", authorization: { model: "free/model", applies: true, max_turns: 2, turns_used: 0 } };
  const box = {
    state: stateWith([
      pane({ pane_id: "PANE-A", title: "API", origin: "maestro", brief, messages: [
        { message_id: "1", text: "Explique", status: "COMPLETED", response_text: "Explicação.", outcome: "conversation" },
        { message_id: "2", text: "Corrija", status: "COMPLETED", response_text: "Corrigi.", outcome: "code_changed" },
      ] }),
      pane({ pane_id: "PANE-B", title: "Outro", layout: { position: 1 } }),
    ]),
    maestro: maestroState(),
  };
  const routes = { ...baseRoutes(box), "POST /api/workspace/panes/PANE-A/messages": (body) => ({ body }) };
  const { app, doc, calls } = harness(routes);
  await app.init();
  const [a, b] = doc.byId["ws-grid"].children;
  assert.match(a.text(), /Somente conversa/);
  assert.match(a.text(), /Alterou código — verificar/);
  assert.match(a.text(), /Autorizado pelo plano · 0\/2 usados/);
  assert.doesNotMatch(b.text(), /Explique/);
  assert.match(b.text(), /Sem mensagens ainda/);
  // Technical identifiers stay inside the collapsed details, not the main card text.
  const visible = a.children[1].children.filter((child) => child.tagName !== "DETAILS").map((child) => child.text()).join(" ");
  assert.doesNotMatch(visible, /DEV-A|C:\/wt|PANE-A/);

  const input = a.all().find((node) => node.tagName === "TEXTAREA" && /Mensagem para API/.test(node.getAttribute("aria-label")));
  input.value = "Adicione validação";
  await input.dispatch("input");
  await findButton(a, "Enviar").dispatch("click");
  assert.deepEqual(calls.find((call) => call.url === "/api/workspace/panes/PANE-A/messages").body, { text: "Adicione validação" });
});

test("missions stay blocked while busy or waiting for a dependency; polling never steals an edit", async () => {
  const box = { state: stateWith([pane({ busy: true, latest_mission: { mission_id: "M", status: "RUNNING", requested_model: "free/model", attempt_count: 1, tests: [], changed_files: [], error_code: null, integration_applied: false } }),
    pane({ pane_id: "PANE-W", title: "Tela", layout: { position: 1 }, dependencies: [{ key: "api", title: "API", ready: false }],
      brief: { task: "UI", acceptance_criteria: [], depends_on: ["api"], authorization: { applies: true, max_turns: 1, turns_used: 0 } } })]), maestro: maestroState() };
  const { app, doc } = harness(baseRoutes(box));
  await app.init();
  const [busy, waiting] = doc.byId["ws-grid"].children;
  assert.equal(findButton(busy, "Iniciar missão").disabled, true);
  assert.equal(findButton(busy, "Enviar").disabled, true);
  assert.equal(findButton(waiting, "Executar tarefa").disabled, true);
  assert.match(waiting.text(), /Aguardando revisão de: API/);

  const typing = waiting.all().find((node) => node.tagName === "TEXTAREA");
  doc.activeElement = typing;
  await app.refresh();
  assert.equal(doc.byId["ws-grid"].children[1], waiting, "background refresh keeps the pane being edited");
  await app.refresh({ force: true });
  assert.notEqual(doc.byId["ws-grid"].children[1], waiting);
});

test("selection persists across reload and closing a pane only hides it", async () => {
  const box = { state: stateWith([pane(), pane({ pane_id: "PANE-B", title: "Agente B", layout: { position: 1 } })]), maestro: maestroState() };
  const first = harness(baseRoutes(box));
  await first.app.init();
  first.app.select("PANE-B");
  const routes = { ...baseRoutes(box), "PATCH /api/workspace/panes/PANE-B": (body) => { box.state = stateWith([pane()], { hidden_panes: [pane({ pane_id: "PANE-B" })] }); return body; } };
  const second = harness(routes);
  Object.assign(second.store, first.store);
  await second.app.init();
  assert.equal(second.app.state.selectedPaneId, "PANE-B");
  await findButton(second.doc.byId["ws-grid"].children[1], "Ocultar (a sessão é preservada)").dispatch("click");
  assert.deepEqual(second.calls.find((call) => call.method === "PATCH").body, { layout: { hidden: true } });
  assert.ok(!second.calls.some((call) => call.method === "DELETE"));
  assert.equal(second.doc.byId["ws-hidden-count"].textContent, "1");
});

test("a transient model-discovery failure is retried on refresh so plans become approvable", async () => {
  const box = { state: stateWith([]), maestro: maestroState([{ message_id: "m1", author: "maestro", text: "ok", status: "COMPLETED", plan: plan() }]) };
  let attempts = 0;
  const routes = { ...baseRoutes(box), "GET /api/dev/models": () => (++attempts <= 2 ? { models: [], catalog_error: "Model discovery is unavailable." } : { models: MODELS, catalog_error: null }) };
  const { app, doc, timers } = harness(routes);
  await app.init();
  assert.match(doc.byId["ws-gates"].text(), /Model discovery is unavailable/);
  assert.equal(timers.at(-1), 2500, "retries quickly while discovery is failing");
  await app.refresh();
  assert.equal(attempts, 3);
  assert.doesNotMatch(doc.byId["ws-gates"].text(), /unavailable/);
  const select = doc.byId["ws-maestro-log"].all().find((node) => node.tagName === "SELECT" && /Modelo de API/.test(node.getAttribute("aria-label") || ""));
  assert.equal(select.children.length, MODELS.length + 1);
});
