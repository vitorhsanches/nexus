const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const script = fs.readFileSync(path.join(root, 'nexus/web/static/js/cockpit.js'), 'utf8');
const template = fs.readFileSync(path.join(root, 'nexus/web/templates/cockpit.html'), 'utf8');

class TextNode {
  constructor(value) { this.value = String(value); this.parentNode = null; }
  get textContent() { return this.value; }
  set textContent(value) { this.value = String(value); }
  cloneNode() { return new TextNode(this.value); }
}

class Element {
  constructor(tag = 'div', className = '') {
    this.tagName = tag.toUpperCase(); this.className = className; this.id = '';
    this.children = []; this.parentNode = null; this.dataset = {}; this.attributes = {};
    this.listeners = {}; this.hidden = false; this.disabled = false; this.scrollTop = 0;
    this.value = ''; this.checked = false;
    this.style = { removeProperty(name) { delete this[name]; } };
    this.clientWidth = 1200; this.clientHeight = 600;
    this._text = '';
    this.classList = {
      contains: name => this.className.split(/\s+/).includes(name),
      add: (...names) => { this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...names])].join(' '); },
      remove: (...names) => { this.className = this.className.split(/\s+/).filter(name => name && !names.includes(name)).join(' '); },
      toggle: (name, force) => {
        const enabled = force === undefined ? !this.classList.contains(name) : force;
        if (enabled) this.classList.add(name); else this.classList.remove(name);
        return enabled;
      },
    };
  }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get lastChild() { return this.children.at(-1) || null; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  append(...nodes) {
    for (const node of nodes) {
      const child = typeof node === 'string' ? new TextNode(node) : node;
      if (child.parentNode) child.parentNode.children = child.parentNode.children.filter(item => item !== child);
      child.parentNode = this; this.children.push(child);
    }
  }
  replaceChildren(...nodes) { this.children.forEach(child => { child.parentNode = null; }); this.children = []; this._text = ''; this.append(...nodes); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(item => item !== this); this.parentNode = null; }
  cloneNode(deep) {
    const copy = new Element(this.tagName, this.className);
    copy.id = this.id; copy.dataset = { ...this.dataset }; copy.attributes = { ...this.attributes };
    copy.hidden = this.hidden; copy._text = this._text; copy.value = this.value; copy.checked = this.checked;
    if (deep) copy.append(...this.children.map(child => child.cloneNode(true)));
    return copy;
  }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  dispatch(name, target = this, extra = {}) {
    const event = { target, currentTarget: this, button: 0, preventDefault() {}, stopPropagation() {}, ...extra };
    for (const callback of this.listeners[name] || []) callback(event);
  }
  requestSubmit() { this.dispatch('submit'); }
  focus() {}
  scrollIntoView() {}
  matches(selector) {
    if (selector === ':last-child') return this.parentNode?.children.at(-1) === this;
    const attribute = selector.match(/\[data-pane="([^"]+)"\]/);
    if (attribute && this.dataset.pane !== attribute[1]) return false;
    const withoutAttribute = selector.replace(/\[data-pane="[^"]+"\]/, '');
    const classes = [...withoutAttribute.matchAll(/\.([\w-]+)/g)].map(match => match[1]);
    if (classes.some(name => !this.classList.contains(name))) return false;
    const tag = withoutAttribute.split(/[.:[\s]/)[0];
    return !tag || this.tagName === tag.toUpperCase();
  }
  descendants() { return this.children.filter(child => child instanceof Element).flatMap(child => [child, ...child.descendants()]); }
  querySelectorAll(selector) {
    if (selector.startsWith(':scope > ')) return this.children.filter(child => child instanceof Element && child.matches(selector.slice(9)));
    if (selector.includes(' > ')) {
      const [parent, child] = selector.split(' > ');
      return this.querySelectorAll(parent).flatMap(node => node.children.filter(item => item instanceof Element && item.matches(child.replace(':last-child', ''))
        && (!child.endsWith(':last-child') || node.children.at(-1) === item)));
    }
    if (selector.includes(' ')) {
      const [parent, child] = selector.split(' ');
      return this.querySelectorAll(parent).flatMap(node => node.querySelectorAll(child));
    }
    return this.descendants().filter(node => node.matches(selector));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { for (let node = this; node; node = node.parentNode) if (node instanceof Element && node.matches(selector)) return node; return null; }
}

const el = (tag, className = '', text) => {
  const node = new Element(tag, className);
  if (text !== undefined) node.textContent = text;
  return node;
};
const nodeWith = (tag, className, ...children) => { const node = el(tag, className); node.append(...children); return node; };

function buildPane(kind) {
  const pane = el('article', `pane${kind === 'maestro' ? ' selected' : ''}`);
  pane.dataset.pane = kind;
  const badge = nodeWith('span', 'pane-status idle', el('i'), new TextNode(' Ocioso'));
  const identity = nodeWith('div', 'pane-identity', el('span', 'pane-icon'), nodeWith('div', '', el('strong'), el('span')));
  const tools = nodeWith('div', 'pane-tools', badge, el('button', 'minimize'), el('button', 'expand'));
  const header = nodeWith('div', 'pane-header', identity, tools);
  const meta = el('div', 'pane-meta');
  const kicker = nodeWith('div', 'terminal-kicker', el('span'), el('span', 'right'));
  let body;
  if (kind === 'maestro') body = nodeWith('div', 'pane-body maestro-body', kicker, nodeWith('div', 'maestro-copy', el('h3'), el('p')));
  else if (kind === 'review') body = nodeWith('div', 'pane-body', kicker, el('div', 'review-steps'));
  else body = nodeWith('div', 'pane-body terminal-body', kicker);
  const footer = nodeWith('div', 'pane-footer', el('span', 'foot-path'), el('span'));
  pane.append(header, meta, body, footer);
  return pane;
}

function harness(initialAgents, { gridWidth = 1200 } = {}) {
  const rootNode = el('body');
  const ids = [...template.matchAll(/id="([^"]+)"/g)].map(match => match[1]);
  const byId = Object.fromEntries(ids.map(id => [id, el('div')]));
  for (const [id, node] of Object.entries(byId)) { node.id = id; rootNode.append(node); }
  const grid = byId['pane-grid'];
  grid.clientWidth = gridWidth;
  grid.append(buildPane('maestro'), buildPane('agent-a'), buildPane('agent-b'), buildPane('review'));
  const overview = el('div');
  for (let index = 0; index < 3; index++) overview.append(nodeWith('div', 'overview-stat', el('strong')));
  rootNode.append(overview);
  const document = {
    getElementById: id => byId[id] || null,
    createElement: tag => el(tag), createTextNode: value => new TextNode(value),
    querySelectorAll: selector => rootNode.querySelectorAll(selector), addEventListener() {},
  };
  const calls = [];
  const box = { agents: initialAgents, models: [{ id: 'free/model', cost_status: 'free' }, { id: 'paid/model', cost_status: 'low_cost' }],
    projects: [{ id: 'demo', name: 'Projeto real', enabled: 1, dev_enabled: 1 }],
    profiles: [{ id: 'unit', name: 'Unitários', timeout_seconds: 120 }],
    diff: { base_sha: 'base-12345678', head_sha: 'head-abcdefgh',
      files: [{ path: 'nexus/example.py', status: 'M', patch: '--- a/nexus/example.py\n+++ b/nexus/example.py' }] },
    maestro: { conversation_id: 'MAESTRO-1', busy: false, planning: { authorized: false, model: null }, messages: [] },
    postError: null, postWait: null, profileError: null };
  const timers = [];
  const frames = new Map();
  let nextFrameId = 0;
  const windowListeners = {};
  const sources = [];
  class FakeEventSource {
    static CLOSED = 2;
    constructor(url) { this.url = url; this.closed = false; this.readyState = 1; sources.push(this); }
    close() { this.closed = true; }
    emit(payload, id = '') { this.onmessage?.({ data: JSON.stringify(payload), lastEventId: id }); }
    fail() { this.onerror?.(); }
    failClosed() { this.readyState = FakeEventSource.CLOSED; this.onerror?.(); }
  }
  const fetch = async (url, options = {}) => {
    calls.push({ url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null });
    let data;
    if (options.method === 'POST') {
      if (box.postWait) await box.postWait;
      if (box.postError) throw box.postError;
      if (url === '/api/workspace/maestro/authorize') {
        box.maestro.planning = { authorized: true, model: JSON.parse(options.body).model, cost_status: 'free' };
        data = box.maestro;
      } else if (url === '/api/workspace/maestro/messages') {
        const body = JSON.parse(options.body);
        box.maestro.messages.push({ author: 'user', text: body.text, status: 'COMPLETED' },
          { author: 'maestro', text: 'Plano pronto', status: 'COMPLETED', plan: { plan_id: 'PLAN-1', source: 'deterministic' } });
        data = box.maestro;
      } else if (/\/api\/workspace\/panes\/[^/]+\/messages/.test(url)) {
        const paneId = url.split('/')[4];
        const pane = box.agents.find(item => item.pane_id === paneId);
        pane.messages.push({ message_id: 'MSG-1', text: JSON.parse(options.body).text, status: 'COMPLETED', response_text: 'Resposta real persistida', outcome: 'conversation' });
        data = pane;
      } else if (/\/api\/workspace\/panes\/[^/]+\/missions$/.test(url)) {
        const paneId = url.split('/')[4];
        const pane = box.agents.find(item => item.pane_id === paneId);
        pane.latest_mission = { mission_id: 'MISSION-NEW', status: 'PENDING', attempt_count: 0, tests: [], changed_files: [] };
        pane.mission_count = (pane.mission_count || 0) + 1;
        data = { pane, mission: pane.latest_mission };
      } else if (/\/api\/dev\/sessions\/[^/]+\/missions\/[^/]+\/interrupt$/.test(url)) {
        data = { mission_id: url.split('/')[6], interrupt_requested: true };
      } else throw new Error(`Unexpected POST: ${url}`);
      return { ok: true, json: async () => data };
    }
    if (url === '/api/projects') data = { projects: box.projects };
    else if (url === '/api/dev/models') data = { models: box.models };
    else if (url.startsWith('/api/dev/test-profiles?')) {
      if (box.profileError) throw box.profileError;
      data = { profiles: box.profiles };
    }
    else if (url.startsWith('/api/workspace/state?')) data = { panes: box.agents, execution_enabled: true };
    else if (url.startsWith('/api/workspace/maestro?')) data = box.maestro;
    else if (url.endsWith('/history')) data = { events: [{ kind: 'MISSION_STARTED', created_at: '2026-10-22' }] };
    else if (/\/api\/dev\/sessions\/[^/]+\/diff$/.test(url)) data = box.diff;
    else throw new Error(`Unexpected API call: ${url}`);
    return { ok: true, json: async () => data };
  };
  const context = {
    document, fetch, window: { matchMedia: () => ({ matches: false }),
      addEventListener(name, callback) { windowListeners[name] = callback; }, EventSource: FakeEventSource },
    ResizeObserver: class { observe() {} },
    setTimeout(callback, delay) { const timer = { callback, delay }; timers.push(timer); return timer; },
    clearTimeout(timer) { const index = timers.indexOf(timer); if (index >= 0) timers.splice(index, 1); },
    requestAnimationFrame(callback) { const id = ++nextFrameId; frames.set(id, callback); return id; },
    cancelAnimationFrame(id) { frames.delete(id); },
  };
  vm.runInNewContext(script, context, { filename: 'cockpit.js' });
  const settle = async () => { for (let count = 0; count < 8; count++) await new Promise(resolve => setImmediate(resolve)); };
  const refresh = async () => {
    const index = timers.findIndex(timer => timer.delay === 15000);
    assert.notEqual(index, -1, 'refresh timer was scheduled');
    timers.splice(index, 1)[0].callback();
    await settle();
  };
  const tick = async delay => {
    const index = timers.findIndex(timer => timer.delay === delay);
    assert.notEqual(index, -1);
    timers.splice(index, 1)[0].callback();
    await settle();
  };
  const resize = (width, height = grid.clientHeight) => {
    grid.clientWidth = width;
    grid.clientHeight = height;
    windowListeners.resize();
    const frame = [...frames.values()].at(-1);
    frames.clear();
    frame?.();
  };
  return { byId, grid, box, calls, settle, refresh, sources, tick, resize };
}

const agent = index => ({
  pane_id: `PANE-${index}`, title: `Agente ${index}`, role_label: 'Implementação', model: 'free/model',
  layout: { position: index }, session: { session_id: `DEV-${index}`, status: 'ACTIVE', branch: `dev/agent-${index}` },
  mission_count: 1, latest_mission: { mission_id: `MISSION-${index}`, status: 'RUNNING', tests: [], changed_files: [] },
  messages: [], dependencies: [],
});

test('agent SSE renders incremental text once and reconciles completion by GET', async () => {
  const running = { ...agent(1), active_turn_id: 'TURN-1' };
  const app = harness([running]);
  await app.settle();
  const source = app.sources[0];
  assert.match(source.url, /\/api\/dev\/sessions\/DEV-1\/events\?turn_id=TURN-1/);
  const pane = app.grid.querySelector('[data-pane="PANE-1"]');
  const delta = { method: 'item/agentMessage/delta', params: { delta: 'Olá' } };
  source.emit(delta, 'DEV-1:TURN-1:1');
  source.emit(delta, 'DEV-1:TURN-1:1');
  assert.match(pane.querySelector('.cockpit-live').textContent, /Olá/);
  assert.doesNotMatch(pane.querySelector('.cockpit-live').textContent, /OláOlá/);
  app.box.agents[0] = { ...running, active_turn_id: null, messages: [{ text: 'Oi', status: 'COMPLETED', response_text: 'Olá' }] };
  source.emit({ type: 'nexus/turnEnded' }, 'DEV-1:TURN-1:2');
  await app.settle();
  assert.equal(source.closed, true);
  assert.equal(pane.querySelector('.cockpit-live'), null);
  assert.match(pane.querySelector('.cockpit-conversation').textContent, /Olá/);
  assert.equal(app.sources.length, 1);
});

test('Maestro SSE uses chat session and exact turn rather than conversation ID', async () => {
  const app = harness([agent(1)]);
  app.box.maestro = { conversation_id: 'CONV-1', chat_session_id: 'CHAT-1', active_turn_id: 'CHAT-TURN-1',
    busy: true, planning: { authorized: true, model: 'free/model' }, messages: [] };
  await app.settle();
  await app.refresh();
  const source = app.sources.find(item => item.url.includes('/chat/sessions/CHAT-1/'));
  assert.ok(source);
  assert.match(source.url, /turn_id=CHAT-TURN-1/);
  source.emit({ method: 'item/agentMessage/delta', params: { delta: 'Plano' } }, 'CHAT-1:CHAT-TURN-1:1');
  assert.match(app.grid.querySelector('[data-pane="maestro"]').querySelector('.cockpit-live').textContent, /Plano/);
  app.box.maestro = { ...app.box.maestro, busy: false, active_turn_id: null,
    messages: [{ author: 'maestro', text: 'Plano persistido', status: 'COMPLETED' }] };
  source.emit({ type: 'nexus/turnSnapshot', turn: { status: 'COMPLETED', response_text: 'Plano persistido' } });
  await app.settle();
  assert.equal(source.closed, true);
  assert.equal(app.grid.querySelector('[data-pane="maestro"]').querySelector('.cockpit-live'), null);
});

test('replay gap discards volatile text and uses persisted GET without restarting the turn', async () => {
  const app = harness([{ ...agent(1), active_turn_id: 'TURN-1' }]);
  await app.settle();
  const source = app.sources[0];
  source.emit({ method: 'item/agentMessage/delta', params: { delta: 'Incompleto' } }, '1');
  source.emit({ type: 'nexus/replayGap' });
  await app.settle();
  assert.equal(source.closed, true);
  assert.equal(app.sources.length, 1);
  assert.doesNotMatch(app.grid.querySelector('[data-pane="PANE-1"]').querySelector('.cockpit-live').textContent, /Incompleto/);
  assert.ok(app.calls.filter(call => call.url.startsWith('/api/workspace/state?')).length >= 2);
});

test('streams close for minimized and removed panes while other pane state remains intact', async () => {
  const app = harness([{ ...agent(1), active_turn_id: 'TURN-1' }, { ...agent(2), active_turn_id: 'TURN-2' }]);
  await app.settle();
  assert.equal(app.sources.length, 2);
  const first = app.grid.querySelector('[data-pane="PANE-1"]');
  const draft = first.querySelector('textarea');
  draft.value = 'Rascunho';
  app.grid.dispatch('click', first.querySelector('.minimize'));
  assert.equal(app.sources[0].closed, true);
  assert.equal(app.sources[1].closed, false);
  assert.equal(draft.value, 'Rascunho');
  app.grid.dispatch('click', first.querySelector('.minimize'));
  assert.equal(app.sources.length, 3);
  app.grid.dispatch('click', first.querySelector('.close'));
  assert.equal(app.sources[2].closed, true);
  assert.equal(app.sources[1].closed, false);
});

test('new turn replaces the stream and stale events cannot change its pane', async () => {
  const app = harness([{ ...agent(1), active_turn_id: 'TURN-1' }]);
  await app.settle();
  const old = app.sources[0];
  app.box.agents[0] = { ...app.box.agents[0], active_turn_id: 'TURN-2' };
  await app.refresh();
  assert.equal(old.closed, true);
  const current = app.sources[1];
  assert.match(current.url, /turn_id=TURN-2/);
  old.emit({ method: 'item/agentMessage/delta', params: { delta: 'velho' } }, 'old');
  current.emit({ method: 'item/agentMessage/delta', params: { delta: 'novo' } }, 'new');
  const live = app.grid.querySelector('[data-pane="PANE-1"]').querySelector('.cockpit-live').textContent;
  assert.match(live, /novo/);
  assert.doesNotMatch(live, /velho/);
});

test('permanently closed SSE reconnects with its cursor and retains polling fallback', async () => {
  const app = harness([{ ...agent(1), active_turn_id: 'TURN-1' }]);
  await app.settle();
  const source = app.sources[0];
  source.emit({ method: 'item/agentMessage/delta', params: { delta: 'um' } }, 'DEV-1:TURN-1:7');
  source.failClosed();
  assert.equal(source.closed, true);
  assert.equal(app.sources.length, 1);
  await app.tick(1000);
  assert.equal(app.sources.length, 2);
  assert.match(app.sources[1].url, /cursor=DEV-1%3ATURN-1%3A7/);
  app.sources[1].emit({ method: 'item/agentMessage/delta', params: { delta: ' dois' } }, 'DEV-1:TURN-1:8');
  assert.match(app.grid.querySelector('[data-pane="PANE-1"]').querySelector('.cockpit-live').textContent, /um dois/);
});

test('at most four streams stay open and switching project closes previous connections', async () => {
  const app = harness(Array.from({ length: 8 }, (_, index) => ({ ...agent(index + 1), active_turn_id: `TURN-${index + 1}` })));
  app.box.projects.push({ id: 'other', name: 'Outro projeto', enabled: 1, dev_enabled: 1 });
  await app.settle();
  assert.ok(app.sources.filter(source => !source.closed).length <= 4);
  const otherProject = app.byId['project-nav'].children[1];
  otherProject.dispatch('click');
  await app.settle();
  assert.equal(app.sources.slice(0, 4).every(source => source.closed), true);
  assert.ok(app.sources.filter(source => !source.closed).length <= 4);
});

test('selected active pane takes a stream slot immediately without recreating retained connections', async () => {
  const agents = Array.from({ length: 8 }, (_, index) => ({ ...agent(index + 1), active_turn_id: `TURN-${index + 1}` }));
  const app = harness(agents, { gridWidth: 1600 });
  await app.settle();
  assert.equal(app.sources.filter(source => !source.closed).length, 4);
  assert.equal(app.sources.some(source => source.url.includes('/DEV-6/')), false);
  const retained = app.sources.find(source => source.url.includes('/DEV-1/'));
  const pane = app.grid.querySelector('[data-pane="PANE-6"]');
  app.grid.dispatch('click', pane);
  assert.equal(pane.classList.contains('selected'), true);
  assert.equal(app.sources.filter(source => !source.closed).length, 4);
  assert.ok(app.sources.some(source => !source.closed && source.url.includes('/DEV-6/')));
  assert.equal(retained.closed, false);
  const count = app.sources.length;
  app.grid.dispatch('click', pane);
  assert.equal(app.sources.length, count);
  assert.ok(app.calls.every(call => call.method === 'GET'));
});

test('switching groups closes hidden streams and connects visible agents without launching turns', async () => {
  const agents = Array.from({ length: 8 }, (_, index) => ({ ...agent(index + 1), active_turn_id: `TURN-${index + 1}` }));
  const app = harness(agents, { gridWidth: 1600 });
  await app.settle();
  const original = app.sources.filter(source => !source.closed);
  app.byId['next-group'].dispatch('click');
  assert.ok(original.every(source => source.closed));
  assert.equal(app.sources.filter(source => !source.closed).length, 2);
  assert.ok(app.sources.some(source => !source.closed && source.url.includes('/DEV-7/')));
  assert.ok(app.sources.some(source => !source.closed && source.url.includes('/DEV-8/')));
  const count = app.sources.length;
  app.byId['next-group'].dispatch('click');
  assert.equal(app.sources.length, count);
  app.byId['previous-group'].dispatch('click');
  assert.equal(app.sources.filter(source => !source.closed).length, 4);
  assert.ok(app.calls.every(call => call.method === 'GET'));
  assert.equal(app.box.agents.every(item => item.active_turn_id), true);
});

test('sidebar selection jumps to a hidden active agent and minimization releases only its stream', async () => {
  const agents = Array.from({ length: 8 }, (_, index) => ({ ...agent(index + 1), active_turn_id: `TURN-${index + 1}` }));
  const app = harness(agents, { gridWidth: 1600 });
  await app.settle();
  const firstGroup = app.sources.slice();
  const link = app.byId['pane-nav'].children.find(button => button.dataset.paneTarget === 'PANE-8');
  app.byId['pane-nav'].dispatch('click', link);
  const pane = app.grid.querySelector('[data-pane="PANE-8"]');
  assert.equal(pane.hidden, false);
  assert.equal(pane.classList.contains('selected'), true);
  assert.ok(firstGroup.every(source => source.closed));
  const selectedStream = app.sources.find(source => !source.closed && source.url.includes('/DEV-8/'));
  const neighbor = app.sources.find(source => !source.closed && source.url.includes('/DEV-7/'));
  assert.ok(selectedStream && neighbor);
  app.grid.dispatch('click', pane.querySelector('.minimize'));
  assert.equal(selectedStream.closed, true);
  assert.equal(neighbor.closed, false);
  app.grid.dispatch('click', pane.querySelector('.minimize'));
  assert.equal(neighbor.closed, false);
  assert.ok(app.sources.some(source => !source.closed && source.url.includes('/DEV-8/')));
});

test('maximize, restore and responsive resize keep stream membership aligned with visible panes', async () => {
  const agents = Array.from({ length: 8 }, (_, index) => ({ ...agent(index + 1), active_turn_id: `TURN-${index + 1}` }));
  const app = harness(agents, { gridWidth: 1600 });
  await app.settle();
  const pane = app.grid.querySelector('[data-pane="PANE-3"]');
  app.grid.dispatch('click', pane.querySelector('.expand'));
  assert.equal(app.sources.filter(source => !source.closed).length, 1);
  const kept = app.sources.find(source => !source.closed);
  assert.match(kept.url, /\/DEV-3\//);
  app.grid.dispatch('click', pane.querySelector('.expand'));
  assert.equal(kept.closed, false);
  assert.equal(app.sources.filter(source => !source.closed).length, 4);
  app.resize(600);
  assert.equal(app.sources.filter(source => !source.closed).length, 0);
  const count = app.sources.length;
  app.resize(600);
  assert.equal(app.sources.length, count);
  app.resize(1600);
  assert.equal(app.sources.filter(source => !source.closed).length, 4);
  assert.ok(app.calls.every(call => call.method === 'GET'));
});

test('cockpit reads existing project, workspace, Maestro and history APIs with GET only', async () => {
  const app = harness([agent(1), agent(2)]);
  await app.settle();
  assert.equal(app.byId['project-name'].textContent, 'Projeto real');
  assert.equal(app.byId['pane-count'].textContent, '04', app.byId['data-status'].textContent);
  assert.ok(app.calls.some(call => call.url.startsWith('/api/workspace/state?')));
  assert.ok(app.calls.some(call => call.url.includes('/api/workspace/panes/PANE-1/history')));
  assert.ok(app.calls.every(call => call.method === 'GET'));
  assert.doesNotMatch(script, /innerHTML|outerHTML|insertAdjacentHTML|document\.write/);
});

test('refresh preserves pane instance, selection and minimization while updating real status', async () => {
  const app = harness([agent(1), agent(2)]);
  await app.settle();
  const pane = app.grid.querySelector('[data-pane="PANE-1"]');
  app.grid.dispatch('click', pane);
  app.grid.dispatch('click', pane.querySelector('.minimize'));
  assert.equal(pane.classList.contains('selected'), true);
  assert.equal(pane.classList.contains('minimized'), true);
  app.box.agents[0] = { ...app.box.agents[0], latest_mission: { ...app.box.agents[0].latest_mission, status: 'READY_FOR_REVIEW' } };
  await app.refresh();
  assert.equal(app.grid.querySelector('[data-pane="PANE-1"]'), pane);
  assert.equal(pane.classList.contains('selected'), true);
  assert.equal(pane.classList.contains('minimized'), true);
  assert.match(pane.querySelector('.pane-status').textContent, /READY_FOR_REVIEW/);
});

test('multiple agents form groups and closed panes can be shown again without creating sessions', async () => {
  const app = harness(Array.from({ length: 6 }, (_, index) => agent(index + 1)));
  await app.settle();
  assert.equal(app.byId['pane-count'].textContent, '08');
  assert.equal(app.byId['group-controls'].hidden, false);
  app.byId['next-group'].dispatch('click');
  assert.match(app.byId['group-label'].textContent, /Grupo 2\/2/);
  app.byId['previous-group'].dispatch('click');
  const pane = app.grid.querySelector('[data-pane="PANE-1"]');
  app.grid.dispatch('click', pane.querySelector('.close'));
  assert.equal(app.byId['pane-count'].textContent, '07');
  app.byId['add-pane'].dispatch('click');
  assert.equal(app.byId['pane-count'].textContent, '08');
  assert.ok(app.calls.every(call => call.method === 'GET'));
});

test('Maestro sends with Enter, preserves multiline draft and displays persisted deterministic reply', async () => {
  const app = harness([agent(1), agent(2)]);
  await app.settle();
  const pane = app.grid.querySelector('[data-pane="maestro"]');
  const form = pane.querySelector('.cockpit-composer');
  const input = form.querySelector('textarea');
  input.value = 'Primeira linha\nSegunda linha';
  input.dispatch('keydown', input, { key: 'Enter', shiftKey: true });
  assert.equal(app.calls.filter(call => call.method === 'POST').length, 0);
  input.dispatch('keydown', input, { key: 'Enter', shiftKey: false });
  await app.settle();
  assert.equal(app.calls.filter(call => call.url === '/api/workspace/maestro/messages' && call.method === 'POST').length, 1);
  assert.equal(input.value, '');
  assert.match(pane.querySelector('.cockpit-conversation').textContent, /Plano pronto.*deterministic/);
  assert.equal(app.grid.querySelector('[data-pane="maestro"]'), pane);
});

test('Maestro planning authorization uses exact catalog model and explicit non-free consent', async () => {
  const app = harness([agent(1)]);
  await app.settle();
  const form = app.grid.querySelector('[data-pane="maestro"]').querySelector('.cockpit-composer');
  const select = form.querySelector('select');
  select.value = 'paid/model';
  select.dispatch('change');
  const authorize = form.querySelector('.cockpit-authorize');
  assert.equal(authorize.disabled, true);
  form.querySelector('.cockpit-confirm input').checked = true;
  form.querySelector('.cockpit-confirm input').dispatch('change');
  assert.equal(authorize.disabled, false);
  authorize.dispatch('click');
  await app.settle();
  const posts = app.calls.filter(call => call.url === '/api/workspace/maestro/authorize');
  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].body, { project_id: 'demo', model: 'paid/model', confirm_cost: true });
  assert.match(form.querySelector('.cockpit-compose-note').textContent, /IA autorizada/);
});

test('agent messages reuse persisted plan approval and preserve draft, pane, scroll and minimization on refresh', async () => {
  const approved = agent(1);
  approved.brief = { authorization: { applies: true } };
  const app = harness([approved, agent(2)]);
  await app.settle();
  const pane = app.grid.querySelector('[data-pane="PANE-1"]');
  const form = pane.querySelector('.cockpit-composer');
  const input = form.querySelector('textarea');
  const body = pane.querySelector('.terminal-body');
  body.scrollTop = 45;
  input.value = 'Rascunho';
  app.grid.dispatch('click', pane);
  app.grid.dispatch('click', pane.querySelector('.minimize'));
  await app.refresh();
  assert.equal(pane.querySelector('textarea'), input);
  assert.equal(input.value, 'Rascunho');
  assert.equal(body.scrollTop, 45);
  assert.equal(pane.classList.contains('selected'), true);
  assert.equal(pane.classList.contains('minimized'), true);
  form.requestSubmit();
  await app.settle();
  const posts = app.calls.filter(call => call.url === '/api/workspace/panes/PANE-1/messages' && call.method === 'POST');
  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].body, { text: 'Rascunho' });
  assert.match(body.textContent, /Resposta real persistida/);
});

test('pending and ambiguous requests never cause automatic duplicate POST', async () => {
  const app = harness([agent(1)]);
  await app.settle();
  const form = app.grid.querySelector('[data-pane="maestro"]').querySelector('.cockpit-composer');
  const input = form.querySelector('textarea');
  input.value = 'Mensagem única';
  let release;
  app.box.postWait = new Promise(resolve => { release = resolve; });
  form.requestSubmit();
  form.requestSubmit();
  assert.equal(app.calls.filter(call => call.url === '/api/workspace/maestro/messages' && call.method === 'POST').length, 1);
  release();
  await app.settle();
  app.box.postWait = null;
  app.box.postError = new Error('Conexão interrompida');
  input.value = 'Resultado incerto';
  const getsBefore = app.calls.filter(call => call.method === 'GET').length;
  form.requestSubmit();
  await app.settle();
  assert.equal(input.value, 'Resultado incerto');
  assert.match(form.querySelector('.cockpit-compose-error').textContent, /Resultado pode ser incerto/);
  assert.equal(app.calls.filter(call => call.method === 'GET').length > getsBefore, true);
  assert.equal(app.calls.filter(call => call.url === '/api/workspace/maestro/messages' && call.method === 'POST').length, 2);
});

test('unapproved agent requires exact model and cost consent before a direct turn', async () => {
  const paid = agent(1);
  paid.model = 'paid/model';
  const app = harness([paid]);
  await app.settle();
  const pane = app.grid.querySelector('[data-pane="PANE-1"]');
  const form = pane.querySelector('.cockpit-composer');
  form.querySelector('textarea').value = 'Explique o plano';
  form.requestSubmit();
  await app.settle();
  assert.equal(app.calls.filter(call => call.method === 'POST').length, 0);
  assert.match(form.querySelector('.cockpit-compose-error').textContent, /Confirme o uso/);
  form.querySelector('.cockpit-model-confirm input').checked = true;
  form.requestSubmit();
  await app.settle();
  assert.match(form.querySelector('.cockpit-compose-error').textContent, /Confirme o custo/);
  form.querySelector('.cockpit-cost-confirm input').checked = true;
  form.requestSubmit();
  await app.settle();
  const posts = app.calls.filter(call => call.url === '/api/workspace/panes/PANE-1/messages' && call.method === 'POST');
  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].body, { text: 'Explique o plano', confirm_model: 'paid/model', confirm_cost: true });
  assert.equal(form.querySelector('.cockpit-model-confirm input').checked, false);
  assert.equal(form.querySelector('.cockpit-cost-confirm input').checked, false);
});

test('busy agent stays read-only and a failed agent POST preserves the draft for reconciliation', async () => {
  const busy = agent(1);
  busy.busy = true;
  const app = harness([busy]);
  await app.settle();
  const pane = app.grid.querySelector('[data-pane="PANE-1"]');
  const form = pane.querySelector('.cockpit-composer');
  const input = form.querySelector('textarea');
  input.value = 'Não duplicar';
  form.requestSubmit();
  assert.equal(app.calls.filter(call => call.method === 'POST').length, 0);
  busy.busy = false;
  busy.brief = { authorization: { applies: true } };
  await app.refresh();
  app.box.postError = new Error('Resposta perdida');
  form.requestSubmit();
  await app.settle();
  assert.equal(input.value, 'Não duplicar');
  assert.match(form.querySelector('.cockpit-compose-error').textContent, /Resultado pode ser incerto/);
  assert.equal(app.calls.filter(call => call.url === '/api/workspace/panes/PANE-1/messages' && call.method === 'POST').length, 1);
});

test('model changes clear previous per-turn model and cost confirmations', async () => {
  const app = harness([agent(1)]);
  await app.settle();
  const form = app.grid.querySelector('[data-pane="PANE-1"]').querySelector('.cockpit-composer');
  const model = form.querySelector('.cockpit-model-confirm input');
  const cost = form.querySelector('.cockpit-cost-confirm input');
  model.checked = true;
  cost.checked = true;
  app.box.agents[0].model = 'paid/model';
  await app.refresh();
  assert.equal(model.checked, false);
  assert.equal(cost.checked, false);
  assert.equal(form.querySelector('.cockpit-cost-confirm').hidden, false);
});

test('mission creation requires trusted profile plus exact model and cost consent, then reconciles by GET', async () => {
  const idle = { ...agent(1), model: 'paid/model', mission_count: 0, latest_mission: null };
  const app = harness([idle]);
  await app.settle();
  const pane = app.grid.querySelector('[data-pane="PANE-1"]');
  const form = pane.querySelector('.cockpit-composer');
  const description = form.querySelector('.cockpit-mission-input');
  const profile = form.querySelector('.cockpit-mission-profile');
  const start = form.querySelector('.cockpit-mission-start');
  description.value = 'Implementar com segurança';
  description.dispatch('input');
  profile.value = 'unit';
  profile.dispatch('change');
  start.dispatch('click');
  await app.settle();
  assert.equal(app.calls.filter(call => call.url.endsWith('/missions') && call.method === 'POST').length, 0);
  assert.match(form.querySelector('.cockpit-mission-error').textContent, /Confirme o uso/);
  form.querySelector('.cockpit-model-confirm input').checked = true;
  start.dispatch('click');
  await app.settle();
  assert.match(form.querySelector('.cockpit-mission-error').textContent, /Confirme o custo/);
  form.querySelector('.cockpit-cost-confirm input').checked = true;
  start.dispatch('click');
  await app.settle();
  const posts = app.calls.filter(call => call.url === '/api/workspace/panes/PANE-1/missions' && call.method === 'POST');
  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].body, { description: 'Implementar com segurança', test_profile: 'unit', confirm_model: 'paid/model', confirm_cost: true });
  assert.equal(description.value, '');
  assert.equal(form.querySelector('.cockpit-model-confirm input').checked, false);
  assert.equal(form.querySelector('.cockpit-cost-confirm input').checked, false);
  assert.match(form.querySelector('.cockpit-mission-status').textContent, /PENDING/);
  assert.equal(form.querySelector('.cockpit-mission-interrupt').hidden, false);
});

test('planned mission can use its persisted authorization without extra instructions or confirmations', async () => {
  const planned = { ...agent(1), mission_count: 0, latest_mission: null,
    brief: { task: 'Executar tarefa aprovada', authorization: { applies: true } } };
  const app = harness([planned]);
  await app.settle();
  const form = app.grid.querySelector('[data-pane="PANE-1"]').querySelector('.cockpit-composer');
  const profile = form.querySelector('.cockpit-mission-profile');
  profile.value = 'unit';
  profile.dispatch('change');
  assert.equal(form.querySelector('.cockpit-mission-start').disabled, false);
  form.querySelector('.cockpit-mission-start').dispatch('click');
  await app.settle();
  const post = app.calls.find(call => call.url === '/api/workspace/panes/PANE-1/missions' && call.method === 'POST');
  assert.deepEqual(post.body, { test_profile: 'unit' });
  assert.equal(form.querySelector('.cockpit-model-confirm').hidden, true);
  assert.equal(form.querySelector('.cockpit-cost-confirm').hidden, true);
});

test('active mission exposes one safe interrupt and terminal missions refresh current worktree diff', async () => {
  const running = { ...agent(1), latest_mission: { mission_id: 'MISSION-1', status: 'TESTING', attempt_count: 1,
    tests: [{ status: 'RUNNING', exit_code: null }], changed_files: [] } };
  const app = harness([running]);
  await app.settle();
  const form = app.grid.querySelector('[data-pane="PANE-1"]').querySelector('.cockpit-composer');
  const interrupt = form.querySelector('.cockpit-mission-interrupt');
  assert.equal(interrupt.hidden, false);
  assert.match(form.querySelector('.cockpit-mission-status').textContent, /TESTING.*Testes: RUNNING/);
  interrupt.dispatch('click');
  interrupt.dispatch('click');
  await app.settle();
  interrupt.dispatch('click');
  await app.settle();
  const interrupts = app.calls.filter(call => call.url === '/api/dev/sessions/DEV-1/missions/MISSION-1/interrupt' && call.method === 'POST');
  assert.equal(interrupts.length, 1);
  assert.equal(interrupt.disabled, true);
  assert.match(interrupt.textContent, /Interrupção solicitada/);

  app.box.agents[0] = { ...running, latest_mission: { mission_id: 'MISSION-1', status: 'READY_FOR_REVIEW', attempt_count: 1,
    tests: [{ status: 'PASSED', exit_code: 0 }], changed_files: [{ path: 'nexus/example.py', status: 'M' }] } };
  await app.refresh();
  const inspect = form.querySelector('.cockpit-mission-inspect');
  assert.equal(interrupt.hidden, true);
  assert.equal(inspect.hidden, false);
  assert.match(form.querySelector('.cockpit-mission-status').textContent, /READY_FOR_REVIEW.*PASSED.*1 arquivo.*nenhuma alteração será aplicada/s);
  inspect.dispatch('click');
  await app.settle();
  const diff = form.querySelector('.cockpit-mission-diff');
  assert.match(diff.textContent, /--- a\/nexus\/example.py/);
  assert.match(diff.textContent, /worktree desta sessão contra a base persistida \(base-12345678 → head-abcdefgh\)/);
  assert.equal(app.calls.filter(call => call.url === '/api/dev/sessions/DEV-1/diff' && call.method === 'GET').length, 1);
  app.box.diff = { base_sha: 'base-new', head_sha: 'head-new', files: [{ path: 'nexus/updated.py', status: 'M', patch: '+++ b/nexus/updated.py' }] };
  inspect.dispatch('click');
  await app.settle();
  assert.equal(diff.hidden, false);
  assert.match(diff.textContent, /base-new → head-new/);
  assert.match(diff.textContent, /nexus\/updated.py/);
  assert.equal(app.calls.filter(call => call.url === '/api/dev/sessions/DEV-1/diff' && call.method === 'GET').length, 2);
  assert.equal(app.calls.some(call => /apply|merge|integrat/i.test(call.url)), false);
});

test('persisted interrupt request remains visible after refresh and cockpit reopen', async () => {
  const persisted = { ...agent(1), latest_mission: { mission_id: 'MISSION-PERSISTED', status: 'RUNNING',
    interrupt_requested: true, attempt_count: 1, tests: [], changed_files: [] } };
  const app = harness([persisted]);
  await app.settle();
  const getInterrupt = current => current.grid.querySelector('[data-pane="PANE-1"]')
    .querySelector('.cockpit-mission-interrupt');
  assert.equal(getInterrupt(app).disabled, true);
  assert.equal(getInterrupt(app).textContent, 'Interrupção solicitada');
  await app.refresh();
  assert.equal(getInterrupt(app).disabled, true);
  assert.equal(getInterrupt(app).textContent, 'Interrupção solicitada');

  const reopened = harness([persisted]);
  await reopened.settle();
  assert.equal(getInterrupt(reopened).disabled, true);
  assert.equal(getInterrupt(reopened).textContent, 'Interrupção solicitada');
});

test('ambiguous mission failure preserves the draft and never retries automatically', async () => {
  const idle = { ...agent(1), mission_count: 0, latest_mission: null, brief: { authorization: { applies: true } } };
  const app = harness([idle]);
  await app.settle();
  const form = app.grid.querySelector('[data-pane="PANE-1"]').querySelector('.cockpit-composer');
  const description = form.querySelector('.cockpit-mission-input');
  description.value = 'Não duplicar missão';
  description.dispatch('input');
  const profile = form.querySelector('.cockpit-mission-profile');
  profile.value = 'unit';
  profile.dispatch('change');
  app.box.postError = new Error('Resposta perdida');
  form.querySelector('.cockpit-mission-start').dispatch('click');
  await app.settle();
  assert.equal(description.value, 'Não duplicar missão');
  assert.match(form.querySelector('.cockpit-mission-error').textContent, /resultado pode ser incerto/i);
  assert.equal(app.calls.filter(call => call.url === '/api/workspace/panes/PANE-1/missions' && call.method === 'POST').length, 1);
  assert.ok(app.calls.filter(call => call.method === 'GET').length > 4);
});
