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
  const box = { agents: initialAgents, models: [{ id: 'free/model', cost_status: 'free' }, { id: 'paid/model', cost_status: 'low_cost' }],
    maestro: { conversation_id: 'MAESTRO-1', busy: false, planning: { authorized: false, model: null }, messages: [] },
    postError: null, postWait: null };
  const timers = [];
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
      } else throw new Error(`Unexpected POST: ${url}`);
      return { ok: true, json: async () => data };
    }
    if (url === '/api/projects') data = { projects: [{ id: 'demo', name: 'Projeto real', enabled: 1, dev_enabled: 1 }] };
    else if (url === '/api/dev/models') data = { models: box.models };
    else if (url.startsWith('/api/workspace/state?')) data = { panes: box.agents, execution_enabled: true };
    else if (url.startsWith('/api/workspace/maestro?')) data = box.maestro;
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
