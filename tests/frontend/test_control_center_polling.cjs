/* F2.4B: Overview polling concurrency/suspension tests for
 * nexus/web/static/js/control-center.js. Scope is narrow and deliberate --
 * only the behavior F2.4B changed (overviewPollingSuspended(),
 * mapWithConcurrency(), and the immediate-refresh-on-return-to-Overview
 * hook in the nav click handler) is exercised here; control-center.js's
 * own rendering logic is unchanged and out of scope.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const controlCenterSource = fs.readFileSync(path.join(root, "nexus/web/static/js/control-center.js"), "utf8");

class Element {
  constructor(id) {
    this.id = id || ""; this.children = []; this.parentNode = null; this.attributes = {};
    this.listeners = {}; this.hidden = false; this.disabled = false; this.value = "";
    this.textContent = ""; this.dataset = {}; this.className = ""; this.checked = false; this.style = {};
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
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  append(...children) { children.forEach((child) => this.appendChild(child)); }
  replaceChildren(...children) { this.children.forEach((child) => { child.parentNode = null; }); this.children = []; this.append(...children); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this); }
  scrollIntoView() {}
}

function setup(options = {}) {
  const ids = [
    "activity-list", "command-feedback", "command-palette", "nexus-command-input",
    "overview-open-program", "overview-program-meta", "overview-program-name",
    "overview-program-phases", "overview-program-progress", "overview-runtime-badge",
    "recovery-list", "routing-count", "routing-policy", "routing-rows", "run-btn",
    "run-disabled-reason", "terminal-clear", "terminal-event-count", "terminal-follow",
    "terminal-output", "terminal-run-id", "view-mode-badge", "top-run", "top-project",
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, new Element(id)]));
  const navDev = new Element("nav-dev"); navDev.setAttribute("data-target", "dev-view");
  const navChat = new Element("nav-chat"); navChat.setAttribute("data-target", "chat-view");
  const navOverview = new Element("nav-overview"); navOverview.setAttribute("data-target", "overview-dashboard");
  navOverview.classList.add("active");
  const nav = [navOverview, navChat, navDev];
  const body = { dataset: { view: options.initialView || "overview" }, setAttribute(name, value) { if (name === "data-view") this.dataset.view = value; } };
  const document = {
    body,
    getElementById(id) { return elements[id] || null; },
    createElement(tag) { return new Element(tag); },
    querySelectorAll(selector) { return selector === ".nav-item" ? nav : []; },
    querySelector() { return null; },
  };
  const calls = [];
  const inFlight = { current: 0, max: 0 };
  const fetchImpl = (url) => {
    calls.push(url);
    inFlight.current += 1;
    inFlight.max = Math.max(inFlight.max, inFlight.current);
    return new Promise((resolve) => {
      setTimeout(() => {
        inFlight.current -= 1;
        resolve({ ok: true, json: () => Promise.resolve(options.responses && options.responses[url] || {}) });
      }, 0);
    });
  };
  const window = { NexusChat: null, NexusDev: null, NexusRun: null, NexusProgram: null };
  // A real (not immediately-synchronous) macrotask queue is needed here,
  // unlike the simpler dev.js harness, specifically so concurrency can be
  // observed: mapWithConcurrency's bound only means something if "in
  // flight" requests can overlap in time, which requires fetch's own
  // setTimeout(...,0) above to be a genuine Node timer, not a fake one
  // that resolves before the next request is even issued.
  const context = {
    document, window, fetch: fetchImpl, console,
    setInterval() { return 1; }, clearInterval() {},
    setTimeout, clearTimeout,
  };
  vm.runInNewContext(controlCenterSource, context);
  return { elements, body, nav, window, calls, inFlight, document };
}

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setTimeout(resolve, 5));
  await new Promise((resolve) => setImmediate(resolve));
}

// A concurrency-bounded burst over many items needs several full
// event-loop passes to drain (each batch's own setTimeout(0) round trip),
// not just one settle() -- wait until the in-flight counter (and the call
// count) stop changing, rather than guessing a fixed number of ticks.
async function drain(ui, maxMs = 2000) {
  var lastCalls = -1;
  var lastInFlight = -1;
  var elapsed = 0;
  while (elapsed < maxMs) {
    await new Promise((resolve) => setTimeout(resolve, 10));
    elapsed += 10;
    if (ui.calls.length === lastCalls && ui.inFlight.current === 0 && lastInFlight === 0) return;
    lastCalls = ui.calls.length;
    lastInFlight = ui.inFlight.current;
  }
}

// setup() itself triggers one unconditional loadOverview()/loadRouting()
// at module load time (control-center.js's own top-level call, unchanged
// by F2.4B) -- let that fully settle before a test starts measuring its
// own, deliberate call, or the two overlap and double-count in-flight
// requests.
async function setupSettled(options) {
  const ui = setup(options);
  await drain(ui);
  ui.calls.length = 0;
  ui.inFlight.max = 0;
  return ui;
}

test("Overview polling makes real requests while the Overview view is active", async () => {
  const ui = await setupSettled({ initialView: "overview" });
  ui.window.NexusControlCenter.refresh();
  await settle();
  assert.ok(ui.calls.some((url) => url === "/api/programs"));
});

test("Overview polling is suspended (zero requests) while Development Mode is active", async () => {
  const ui = await setupSettled({ initialView: "dev" });
  ui.window.NexusControlCenter.refresh();
  await settle();
  assert.deepEqual(ui.calls, [], "loadOverview must not fetch anything while data-view is 'dev'");
});

test("Overview polling is suspended while Chat is active", async () => {
  const ui = await setupSettled({ initialView: "chat" });
  ui.window.NexusControlCenter.refresh();
  await settle();
  assert.deepEqual(ui.calls, []);
});

test("returning to Overview from the nav bar triggers an immediate refresh, not a stale wait for the next interval", async () => {
  const ui = await setupSettled({ initialView: "dev" });
  ui.window.NexusDev = { exit: () => { ui.body.dataset.view = "overview"; } };
  const overviewNav = ui.nav.find((item) => item.getAttribute("data-target") === "overview-dashboard");
  overviewNav.dispatch("click");
  await settle();
  assert.ok(ui.calls.some((url) => url === "/api/programs"), "switching back to Overview must refresh immediately");
});

test("checkpoint fetches in loadFeaturedDetails are bounded in concurrency, never one unbounded burst", async () => {
  var programs = [];
  var runs = Array.from({ length: 24 }, (_, i) => ({ id: "run-" + i, status: "COMPLETED" }));
  const ui = await setupSettled({
    initialView: "overview",
    responses: {
      "/api/programs": { programs },
      "/api/operational/runs": { runs },
      "/api/operational/agents": { agents: [] },
    },
  });
  ui.window.NexusControlCenter.refresh();
  await drain(ui);
  // 24 runs, each a /api/runs/<id>/checkpoints call -- but never more than
  // a handful in flight at once (the old code fired all of them via a
  // single Promise.all, i.e. up to 40 at once for a large run list).
  const checkpointCalls = ui.calls.filter((url) => url.includes("/checkpoints"));
  assert.ok(checkpointCalls.length >= 20, "all runs must still eventually be fetched: " + checkpointCalls.length);
  assert.ok(ui.inFlight.max <= 6, "checkpoint fetches must be concurrency-bounded, saw max in-flight = " + ui.inFlight.max);
});

test("a suspended Overview poll makes zero requests even with many runs queued", async () => {
  var runs = Array.from({ length: 24 }, (_, i) => ({ id: "run-" + i, status: "COMPLETED" }));
  const ui = await setupSettled({
    initialView: "dev",
    responses: { "/api/operational/runs": { runs } },
  });
  ui.window.NexusControlCenter.refresh();
  await settle();
  assert.deepEqual(ui.calls, []);
});
