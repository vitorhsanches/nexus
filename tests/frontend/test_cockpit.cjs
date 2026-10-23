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
    copy.hidden = this.hidden; copy._text = this._text;
    if (deep) copy.append(...this.children.map(child => child.cloneNode(true)));
    return copy;
  }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  dispatch(name, target = this) {
    const event = { target, currentTarget: this, button: 0, preventDefault() {}, stopPropagation() {} };
    for (const callback of this.listeners[name] || []) callback(event);
  }
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
  if (kind === 'maestro') body = nodeWith('div', 'pane-body', kicker, nodeWith('div', 'maestro-copy', el('h3'), el('p')));
  else if (kind === 'review') body = nodeWith('div', 'pane-body', kicker, el('div', 'review-steps'));
  else body = nodeWith('div', 'pane-body terminal-body', kicker);
  const footer = nodeWith('div', 'pane-footer', el('span', 'foot-path'), el('span'));
  pane.append(header, meta, body, footer);
  return pane;
}

function harness(initialAgents) {
  const rootNode = el('body');
  const ids = [...template.matchAll(/id="([^"]+)"/g)].map(match => match[1]);
  const byId = Object.fromEntries(ids.map(id => [id, el('div')]));
  for (const [id, node] of Object.entries(byId)) { node.id = id; rootNode.append(node); }
  const grid = byId['pane-grid'];
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
  const box = { agents: initialAgents };
  const timers = [];
  const fetch = async (url, options = {}) => {
    calls.push({ url, method: options.method || 'GET' });
    let data;
    if (url === '/api/projects') data = { projects: [{ id: 'demo', name: 'Projeto real', enabled: 1, dev_enabled: 1 }] };
    else if (url.startsWith('/api/workspace/state?')) data = { panes: box.agents };
    else if (url.startsWith('/api/workspace/maestro?')) data = { conversation_id: 'MAESTRO-1', busy: false, planning: { model: 'free/model' }, messages: [] };
    else if (url.endsWith('/history')) data = { events: [{ kind: 'MISSION_STARTED', created_at: '2026-10-22' }] };
    else throw new Error(`Unexpected API call: ${url}`);
    return { ok: true, json: async () => data };
  };
  const context = {
    document, fetch, window: { matchMedia: () => ({ matches: false }), addEventListener() {} },
    ResizeObserver: class { observe() {} },
    setTimeout(callback, delay) { const timer = { callback, delay }; timers.push(timer); return timer; },
    clearTimeout(timer) { const index = timers.indexOf(timer); if (index >= 0) timers.splice(index, 1); },
    requestAnimationFrame() { return 1; }, cancelAnimationFrame() {},
  };
  vm.runInNewContext(script, context, { filename: 'cockpit.js' });
  const settle = async () => { for (let count = 0; count < 8; count++) await new Promise(resolve => setImmediate(resolve)); };
  const refresh = async () => {
    const index = timers.findIndex(timer => timer.delay === 15000);
    assert.notEqual(index, -1, 'refresh timer was scheduled');
    timers.splice(index, 1)[0].callback();
    await settle();
  };
  return { byId, grid, box, calls, settle, refresh };
}

const agent = index => ({
  pane_id: `PANE-${index}`, title: `Agente ${index}`, role_label: 'Implementação', model: 'free/model',
  layout: { position: index }, session: { session_id: `DEV-${index}`, status: 'ACTIVE', branch: `dev/agent-${index}` },
  mission_count: 1, latest_mission: { mission_id: `MISSION-${index}`, status: 'RUNNING', tests: [], changed_files: [] },
  messages: [], dependencies: [],
});

test('cockpit reads existing project, workspace, Maestro and history APIs with GET only', async () => {
  const app = harness([agent(1), agent(2)]);
  await app.settle();
  assert.equal(app.byId['project-name'].textContent, 'Projeto real');
  assert.equal(app.byId['pane-count'].textContent, '04');
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
