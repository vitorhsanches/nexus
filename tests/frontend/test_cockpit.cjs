const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const script = fs.readFileSync(path.join(root, 'nexus/web/static/js/cockpit.js'), 'utf8');
const template = fs.readFileSync(path.join(root, 'nexus/web/templates/cockpit.html'), 'utf8');

const { TextNode, Element, el, nodeWith } = require('./fake_dom.cjs');

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
  else if (kind === 'native') body = nodeWith('div', 'pane-body terminal-body', kicker);
  else body = nodeWith('div', 'pane-body terminal-body', kicker);
  const footer = nodeWith('div', 'pane-footer', el('span', 'foot-path'), el('span'));
  pane.append(header, meta, body, footer);
  return pane;
}

function harness(initialAgents, { gridWidth = 1200, discoveredCatalog = null, projects = null, withNative = false, compact = false } = {}) {
  const rootNode = el('body');
  const ids = [...template.matchAll(/id="([^"]+)"/g)].map(match => match[1]);
  const byId = Object.fromEntries(ids.map(id => [id, el('div')]));
  for (const [id, node] of Object.entries(byId)) { node.id = id; rootNode.append(node); }
  const grid = byId['pane-grid'];
  grid.clientWidth = gridWidth;
  grid.append(buildPane('maestro'), ...(withNative ? [buildPane('native')] : []), buildPane('agent-a'), buildPane('agent-b'), buildPane('review'));
  if (compact) {
    for (const pane of grid.querySelectorAll(':scope > .pane')) {
      if (['maestro', 'native', 'review'].includes(pane.dataset.pane)) pane.dataset.workspaceInactive = 'true';
    }
  }
  const overview = el('div');
  for (let index = 0; index < 3; index++) overview.append(nodeWith('div', 'overview-stat', el('strong')));
  rootNode.append(overview);
  const document = {
    getElementById: id => byId[id] || null,
    createElement: tag => el(tag), createTextNode: value => new TextNode(value),
    querySelectorAll: selector => rootNode.querySelectorAll(selector), addEventListener() {},
  };
  const calls = [];
  const approvedModels = [{ id: 'free/model', cost_status: 'free' }, { id: 'paid/model', cost_status: 'low_cost' }];
  const box = { agents: initialAgents, models: approvedModels,
    discoveredCatalog: discoveredCatalog || {
      models: [
        { id: 'free/model', name: 'Free model', provider: 'test-provider', availability: 'omniroute-listed-unverified', cost_status: 'free', approval_status: 'approved', approval_reason: 'Qualified', can_execute: true },
        { id: 'unknown/model', name: 'Unknown model', provider: 'other-provider', availability: 'omniroute-listed-unverified', cost_status: 'unknown', approval_status: 'not_qualified', approval_reason: 'Not qualified', can_execute: false },
      ], approved_models: approvedModels, default_model: 'free/model', catalog_error: null,
    },
    catalogError: null,
    projects: projects || [{ id: 'demo', name: 'Projeto real', enabled: 1, dev_enabled: 1 }],
    profiles: [{ id: 'unit', name: 'Unitários', timeout_seconds: 120 }],
    diff: { base_sha: 'base-12345678', head_sha: 'head-abcdefgh',
      files: [{ path: 'nexus/example.py', status: 'M', patch: '--- a/nexus/example.py\n+++ b/nexus/example.py' }] },
    maestro: { conversation_id: 'MAESTRO-1', busy: false, planning: { authorized: false, model: null }, messages: [] },
    authorizations: [],
    aiSettings: { project_id: 'demo', catalog_error: null, executors: {
      codex: { name: 'OpenAI Codex via OmniRoute', connected: true, enabled: true, executable: true, status: 'Conectado', detail: 'OAuth ativo.' },
      claude_code: { name: 'Claude Code via OmniRoute', connected: false, enabled: true, executable: false, status: 'Não conectado', detail: 'OAuth indisponível.' },
    }, roles: {
      maestro: { selection: null, valid: false, models: [{ id: 'free/planning', name: 'Planning', provider: 'codex', cost_status: 'free', efforts: ['medium'], routes_by_effort: { medium: 'free/planning' } }] },
      development: { selection: null, valid: false, models: [{ id: 'free/model', name: 'Coding', provider: 'codex', cost_status: 'free', efforts: ['low'], routes_by_effort: { low: 'free/model' } }] },
      review: { selection: null, valid: false, models: [{ id: 'free/review', name: 'Review', provider: 'codex', cost_status: 'free', efforts: ['high'], routes_by_effort: { high: 'free/review' } }] },
    } },
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
    calls.push({ url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null, authorization: options.headers?.Authorization });
    let data;
    if (options.method === 'DELETE' && url.startsWith('/api/workspace/cost-authorizations/')) {
      box.authorizations = box.authorizations.map(auth => ({ ...auth, status: 'REVOKED' }));
      return { ok: true, json: async () => ({ status: 'REVOKED' }) };
    }
    if (options.method === 'POST') {
      if (box.postWait) await box.postWait;
      if (box.postError) throw box.postError;
      if (url === '/api/dev/model-management/activation') {
        const body = JSON.parse(options.body);
        box.discoveredCatalog.models = box.discoveredCatalog.models.map(model => model.id === body.model_id
          ? { ...model, active: body.active, can_execute: body.active, management_status: body.active ? 'active' : 'inactive' } : model);
        box.discoveredCatalog.approved_models = box.discoveredCatalog.models.filter(model => model.can_execute)
          .map(model => ({ id: model.id, cost_status: model.cost_status }));
        data = box.discoveredCatalog;
      } else if (url === '/api/dev/ai-settings') {
        const body = JSON.parse(options.body);
        box.aiSettings.roles[body.role].selection = { executor: 'codex', provider: body.executor, model_id: body.model_id, effort: '' };
        box.aiSettings.roles[body.role].valid = true;
        data = box.aiSettings;
      } else if (url === '/api/dev/ai-settings/providers') {
        const body = JSON.parse(options.body);
        box.aiSettings.executors[body.provider].enabled = body.enabled;
        box.aiSettings.executors[body.provider].executable = body.enabled;
        data = box.aiSettings;
      } else if (url === '/api/dev/ai-settings/clear') {
        const body = JSON.parse(options.body);
        box.aiSettings.roles[body.role].selection = null;
        box.aiSettings.roles[body.role].valid = false;
        data = box.aiSettings;
      } else if (url === '/api/dev/model-management/candidate') {
        const body = JSON.parse(options.body);
        box.discoveredCatalog.models = box.discoveredCatalog.models.map(model => model.id === body.model_id
          ? { ...model, candidate: body.candidate, management_status: body.candidate ? 'candidate' : 'not_qualified' } : model);
        data = box.discoveredCatalog;
      } else if (url === '/api/dev/model-management/qualifications') {
        const body = JSON.parse(options.body);
        box.discoveredCatalog.models = box.discoveredCatalog.models.map(model => model.id === body.model_id
          ? { ...model, qualification_status: 'qualified', approval_status: 'approved', qualification: { status: 'QUALIFIED' }, active: false, management_status: 'inactive' } : model);
        data = box.discoveredCatalog;
      } else if (url === '/api/workspace/maestro/authorize') {
        box.maestro.planning = { authorized: true, model: JSON.parse(options.body).model, cost_status: 'free' };
        data = box.maestro;
      } else if (url === '/api/workspace/maestro/messages') {
        const body = JSON.parse(options.body);
        box.maestro.messages.push({ author: 'user', text: body.text, status: 'COMPLETED' },
          { author: 'maestro', text: 'Plano pronto', status: 'COMPLETED', plan: { plan_id: 'PLAN-1', source: 'deterministic' } });
        data = box.maestro;
      } else if (url === '/api/workspace/cost-authorizations') {
        const body = JSON.parse(options.body);
        box.authorizations.push({ authorization_id: 'AUTH-1', project_id: body.project_id, model: body.model,
          cost_status: 'low_cost', max_uses: body.max_uses, uses_reserved: 0, uses_remaining: body.max_uses,
          max_concurrent: body.max_concurrent, active_executions: 0, expires_at: new Date(Date.now() + body.validity_seconds * 1000).toISOString(), status: 'ACTIVE' });
        data = box.authorizations.at(-1);
      } else if (url.startsWith('/api/workspace/cost-authorizations/')) {
        box.authorizations = box.authorizations.map(auth => ({ ...auth, status: 'REVOKED' }));
        data = { status: 'REVOKED' };
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
    else if (url === '/api/dev/ai-settings/admin-check') data = { authenticated: Boolean(options.headers?.Authorization) };
    else if (url.startsWith('/api/dev/ai-settings/providers?')) data = { project_id: 'demo', executors: box.aiSettings.executors };
    else if (url.startsWith('/api/dev/ai-settings?')) data = box.aiSettings;
    else if (url === '/api/dev/model-management/admin-status') data = { admin_auth_configured: true };
    else if (url.startsWith('/api/dev/model-catalog')) {
      if (box.catalogError) throw box.catalogError;
      data = box.discoveredCatalog;
    }
    else if (url === '/api/dev/models') data = { models: box.models };
    else if (url.startsWith('/api/dev/test-profiles?')) {
      if (box.profileError) throw box.profileError;
      data = { profiles: box.profiles };
    }
    else if (url.startsWith('/api/workspace/state?')) data = { panes: box.agents, execution_enabled: true };
    else if (url.startsWith('/api/workspace/maestro?')) data = box.maestro;
    else if (url.startsWith('/api/workspace/cost-authorizations?')) data = { authorizations: box.authorizations };
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
  return { byId, grid, box, calls, settle, refresh, sources, tick, resize, context };
}

test('compact cockpit header keeps project counts and actions above the grid', () => {
  const header = template.match(/<header class="topbar">([\s\S]*?)<\/header>/)?.[1];
  assert.ok(header);
  for (const id of ['breadcrumb-title', 'workspace-id', 'execution-count', 'data-status', 'focus-maestro']) {
    assert.match(header, new RegExp(`id="${id}"`));
  }
  assert.equal((header.match(/class="overview-stat"/g) || []).length, 3);
  assert.doesNotMatch(template, /<section class="mission-bar"|<section class="overview"/);
});

test('pane dividers still resize both axes and restore the automatic layout', () => {
  const app = harness([], { gridWidth: 700, withNative: true });
  const columns = app.grid.style.gridTemplateColumns;
  const rows = app.grid.style.gridTemplateRows;
  const vertical = app.grid.querySelector('.grid-divider.vertical');
  const horizontal = app.grid.querySelector('.grid-divider.horizontal');
  assert.ok(vertical);
  assert.ok(horizontal);
  assert.equal(app.grid.querySelector('[data-pane="maestro"]').classList.contains('featured'), true);
  assert.match(horizontal.style.left, /px$/);
  assert.notEqual(horizontal.style.left, '0px', 'the row divider does not cross the tall pane');
  vertical.dispatch('keydown', vertical, { key: 'ArrowRight' });
  horizontal.dispatch('keydown', horizontal, { key: 'ArrowDown' });
  assert.notEqual(app.grid.style.gridTemplateColumns, columns);
  assert.notEqual(app.grid.style.gridTemplateRows, rows);
  const keyboardColumns = app.grid.style.gridTemplateColumns;
  vertical.setPointerCapture = () => {};
  vertical.removeEventListener = (name, callback) => {
    vertical.listeners[name] = (vertical.listeners[name] || []).filter(listener => listener !== callback);
  };
  vertical.dispatch('pointerdown', vertical, { pointerId: 1, clientX: 0 });
  vertical.dispatch('pointermove', vertical, { pointerId: 1, clientX: 24 });
  vertical.dispatch('pointerup', vertical, { pointerId: 1, clientX: 24 });
  assert.notEqual(app.grid.style.gridTemplateColumns, keyboardColumns);
  app.byId['restore-layout'].dispatch('click');
  assert.equal(app.grid.style.gridTemplateColumns, columns);
  assert.equal(app.grid.style.gridTemplateRows, rows);
});

test('five visible panes form a tall lead pane and a two-by-two companion grid', async () => {
  const app = harness([agent(1), agent(2)], { withNative: true });
  await app.settle();
  assert.equal(app.grid.dataset.visible, '5');
  assert.equal(app.grid.dataset.columns, '3');
  assert.equal(app.grid.dataset.rows, '2');
  assert.equal(app.grid.querySelector('[data-pane="maestro"]').classList.contains('featured'), true);
  const divider = app.grid.querySelector('.grid-divider.horizontal');
  assert.ok(divider);
  assert.notEqual(divider.style.left, '0px');
});

test('compact workspace shows only five real agent panes in a resizable three plus two grid', async () => {
  const app = harness([agent(1), agent(2), agent(3), agent(4), agent(5)], { compact: true });
  await app.settle();
  assert.equal(app.grid.dataset.visible, '5');
  assert.equal(app.grid.dataset.columns, '3');
  assert.equal(app.grid.dataset.rows, '2');
  assert.equal(app.grid.style.display, 'block');
  const visible = [...app.grid.querySelectorAll(':scope > .pane')].filter(pane => pane.dataset.workspaceInactive !== 'true');
  assert.equal(visible.length, 5);
  assert.equal(visible[0].style.top, visible[1].style.top);
  assert.equal(visible[1].style.top, visible[2].style.top);
  assert.equal(visible[3].style.top, visible[4].style.top);
  assert.notEqual(visible[0].style.top, visible[3].style.top);
  assert.notEqual(visible[0].style.width, visible[1].style.width);
  assert.ok(Number.parseFloat(visible[0].style.width) > Number.parseFloat(visible[1].style.width));
  assert.equal(visible[3].style.width, visible[4].style.width);
  const before = visible[0].style.width;
  app.grid.querySelector('.grid-divider.vertical').dispatch('keydown', undefined, { key: 'ArrowRight' });
  assert.notEqual(visible[0].style.width, before);
  app.byId['restore-layout'].dispatch('click');
  assert.equal(visible[0].style.width, before);
});

test('two panes can switch to vertical rows, resize their heights, and fit the viewport', () => {
  const app = harness([], { gridWidth: 700 });
  assert.equal(app.grid.dataset.columns, '2');
  assert.equal(app.grid.dataset.rows, '1');
  assert.equal(app.byId['toggle-layout'].hidden, false);
  app.byId['toggle-layout'].dispatch('click');
  assert.equal(app.grid.dataset.columns, '1');
  assert.equal(app.grid.dataset.rows, '2');
  const before = app.grid.style.gridTemplateRows;
  const divider = app.grid.querySelector('.grid-divider.horizontal');
  assert.ok(divider);
  divider.dispatch('keydown', divider, { key: 'ArrowDown' });
  assert.notEqual(app.grid.style.gridTemplateRows, before);
  app.resize(700, 400);
  assert.equal(app.grid.dataset.rows, '1');
  assert.equal(app.byId['toggle-layout'].hidden, true);
  app.resize(700, 600);
  assert.equal(app.grid.dataset.rows, '2');
  app.byId['restore-layout'].dispatch('click');
  assert.equal(app.grid.dataset.rows, '1');
});

test('persistent cost authorization captures bounded consent and can be revoked', async () => {
  const app = harness([]);
  await app.settle();
  app.byId['cost-auth-model'].value = 'paid/model';
  app.byId['cost-auth-model'].dispatch('change');
  app.byId['cost-auth-uses'].value = '10';
  app.byId['cost-auth-days'].value = '7';
  app.byId['cost-auth-concurrency'].value = '1';
  app.byId['cost-auth-confirm'].checked = true;
  app.byId['cost-auth-confirm'].dispatch('change');
  await app.settle();
  assert.equal(app.byId['cost-auth-create'].disabled, false);
  app.byId['cost-auth-create'].dispatch('click');
  await app.settle();
  const create = app.calls.find(call => call.url === '/api/workspace/cost-authorizations' && call.method === 'POST');
  assert.deepEqual(create.body, { project_id: 'demo', model: 'paid/model', confirm_cost: true, max_uses: 10,
    validity_seconds: 7 * 86400, max_concurrent: 1 });
  assert.match(app.byId['cost-auth-list'].textContent, /10\/10 usos restantes/);
  const revoke = app.byId['cost-auth-list'].querySelector('.cost-auth-revoke');
  assert.ok(revoke);
  revoke.dispatch('click');
  await app.settle();
  assert.ok(app.calls.some(call => call.url.includes('/api/workspace/cost-authorizations/AUTH-1?project_id=demo') && call.method === 'DELETE'));
});

test('AI settings show real executor status and save only with operator token', async () => {
  const app = harness([]);
  await app.settle();
  app.byId['settings-ai-button'].dispatch('click');
  await app.settle();
  assert.doesNotMatch(app.byId['ai-executors'].textContent, /Claude Code/);
  const role = app.byId['ai-roles'].children[1];
  const fields = role.children[1];
  fields.children[1].value = 'free/model';
  fields.children[1].dispatch('change');
  role.children[2].children[0].dispatch('click');
  await app.settle();
  assert.equal(app.calls.some(call => call.url === '/api/dev/ai-settings' && call.method === 'POST'), false);
  app.byId['ai-admin-token'].value = 'x'.repeat(32);
  app.byId['ai-admin-unlock'].dispatch('click');
  await app.settle();
  role.children[2].children[0].dispatch('click');
  await app.settle();
  const save = app.calls.find(call => call.url === '/api/dev/ai-settings' && call.method === 'POST');
  assert.deepEqual(save.body, { project_id: 'demo', role: 'development', executor: 'codex', model_id: 'free/model' });
  assert.equal(save.authorization, `Bearer ${'x'.repeat(32)}`);
  assert.match(app.byId['ai-roles'].textContent, /Preferência salva/);
  app.byId['ai-roles'].children[1].children[2].children[1].dispatch('click');
  await app.settle();
  assert.equal(app.box.aiSettings.roles.development.selection, null);
  assert.equal(app.calls.some(call => call.url.includes('/api/workspace/panes/') && call.method === 'POST'), false);
});

test('connected provider can be disabled without deleting saved model choice', async () => {
  const app = harness([]);
  await app.settle();
  app.byId['settings-ai-button'].dispatch('click');
  await app.settle();
  app.byId['ai-admin-token'].value = 'x'.repeat(32);
  app.byId['ai-admin-unlock'].dispatch('click');
  await app.settle();
  app.byId['ai-executors'].querySelector('button').dispatch('click');
  await app.settle();
  assert.equal(app.box.aiSettings.executors.codex.enabled, false);
  assert.equal(app.calls.find(call => call.url === '/api/dev/ai-settings/providers').body.provider, 'codex');
});

test('agent and mission offer only confirmed efforts for the exact cc route', async () => {
  const selected = { ...agent(1), model: 'cc/claude-sonnet-5-low', latest_mission: null,
    brief: { task: 'Tarefa aprovada', authorization: { applies: true } } };
  const route = { id: selected.model, name: 'Sonnet', cost_status: 'free',
    supported_efforts: ['low', 'high'], confirmed_efforts: ['low'] };
  const app = harness([selected], { discoveredCatalog: {
    models: [{ ...route, provider: 'claude', approval_status: 'approved', active: true, can_execute: true }],
    approved_models: [route], default_model: selected.model, catalog_error: null,
  } });
  await app.settle();
  const form = app.grid.querySelector('[data-pane="PANE-1"]').querySelector('.cockpit-composer');
  const effort = form.querySelector('.cockpit-compose-controls').children[0];
  assert.deepEqual([...effort.children].map(item => item.value).filter(Boolean), ['low']);
  assert.equal(form.querySelector('.cockpit-send').disabled, true);
  effort.value = 'low'; effort.dispatch('change');
  form.querySelector('.cockpit-input').value = 'Executar';
  form.requestSubmit();
  await app.settle();
  const sent = app.calls.find(call => call.url === '/api/workspace/panes/PANE-1/messages' && call.method === 'POST');
  assert.equal(sent.body.effort, 'low');
});

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
  const link = [...app.byId['pane-nav'].children].find(button => button.dataset.paneTarget === 'PANE-8');
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

test('dynamic catalog shows discovered approval states while selectors remain approved-only', async () => {
  const app = harness([agent(1)], {
    discoveredCatalog: {
      models: [
        { id: 'approved/model', name: 'Approved Model', provider: 'provider-a', availability: 'omniroute-listed-unverified', cost_status: 'free', approval_status: 'approved', approval_reason: 'Qualified', can_execute: true },
        { id: 'new/model', name: 'New Model', provider: 'provider-b', availability: 'omniroute-listed-unverified', cost_status: 'unknown', approval_status: 'not_qualified', approval_reason: 'Not qualified', can_execute: false },
        { id: 'blocked/model', name: 'Blocked Model', provider: 'provider-c', availability: 'omniroute-listed-unverified', cost_status: 'unknown', approval_status: 'blocked', approval_reason: 'Blocked by policy', can_execute: false },
      ],
      approved_models: [{ id: 'approved/model', cost_status: 'free' }],
      default_model: 'approved/model', catalog_error: null,
    },
  });
  await app.settle();
  const catalog = app.byId['model-catalog-list'];
  assert.match(catalog.textContent, /Approved Model.*Aprovado para execução/s);
  assert.match(catalog.textContent, /New Model.*Não qualificado/s);
  assert.match(catalog.textContent, /Blocked Model.*Bloqueado/s);
  assert.match(catalog.textContent, /provider-b/);
  assert.match(catalog.textContent, /inferência não verificada/);
  const planning = app.grid.querySelector('[data-pane="maestro"]')
    .querySelector('.cockpit-composer').querySelector('.cockpit-model-select');
  assert.deepEqual([...planning.children].filter(option => option.value).map(option => option.value), ['free/planning', 'approved/model']);

  app.box.discoveredCatalog = {
    models: [{ id: 'approved/new', name: 'Refreshed Model', provider: 'provider-d', availability: 'listed', cost_status: 'standard', approval_status: 'approved', approval_reason: 'Qualified', can_execute: true }],
    approved_models: [{ id: 'approved/new', cost_status: 'standard' }], default_model: null, catalog_error: null,
  };
  app.byId['model-catalog-refresh'].dispatch('click');
  await app.settle();
  assert.match(catalog.textContent, /Refreshed Model/);
  assert.ok(app.calls.some(call => call.url === '/api/dev/model-catalog?refresh=true' && call.method === 'GET'));
  assert.deepEqual([...planning.children].filter(option => option.value).map(option => option.value), ['free/planning', 'approved/new']);
});

test('model settings filter discovered models and keep candidates non-executable', async () => {
  const app = harness([], {
    discoveredCatalog: {
      models: [
        { id: 'qualified/model', name: 'Qualified', provider: 'provider-a', availability: 'listed', cost_status: 'free', approval_status: 'approved', approval_reason: 'Qualified', qualification_status: 'qualified', active: true, management_status: 'active', can_execute: true },
        { id: 'new/model', name: 'New', provider: 'provider-b', availability: 'listed', cost_status: 'unknown', approval_status: 'not_qualified', approval_reason: 'Not qualified', qualification_status: 'not_qualified', active: false, candidate: false, management_status: 'not_qualified', can_execute: false },
      ], approved_models: [{ id: 'qualified/model', cost_status: 'free' }], default_model: 'qualified/model', catalog_error: null,
    },
  });
  await app.settle();
  const cards = app.byId['model-catalog-list'];
  assert.match(cards.textContent, /Ativo/);
  const markCandidate = [...cards.children].find(card => card.textContent.includes('New')).querySelector('button');
  markCandidate.dispatch('click');
  await app.settle();
  assert.ok(app.calls.some(call => call.url === '/api/dev/model-management/candidate' && call.body.model_id === 'new/model'));
  assert.match(cards.textContent, /Candidato · não libera execução/);
  assert.deepEqual(app.box.discoveredCatalog.approved_models.map(model => model.id), ['qualified/model']);

  app.byId['model-filter-provider'].value = 'provider-a';
  app.byId['model-filter-provider'].dispatch('change');
  assert.match(cards.textContent, /Qualified/);
  assert.doesNotMatch(cards.textContent, /New/);
  app.byId['model-filter-provider'].value = '';
  app.byId['model-filter-provider'].dispatch('change');
  const deactivate = [...cards.children].find(card => card.textContent.includes('Qualified')).querySelector('button');
  deactivate.dispatch('click');
  await app.settle();
  assert.ok(app.calls.some(call => call.url === '/api/dev/model-management/activation' && call.body.model_id === 'qualified/model' && call.body.active === false));
  assert.deepEqual(app.box.discoveredCatalog.approved_models, []);
});

test('model qualification requires an explicit admin token and starts inactive', async () => {
  const app = harness([], { discoveredCatalog: {
    models: [{ id: 'provider/new-code', name: 'New Code', provider: 'provider', availability: 'listed', cost_status: 'unknown', approval_status: 'not_qualified', qualification_status: 'not_qualified', appserver_compatible: true, supported_efforts: ['low'], discovered: true, active: false, can_execute: false }],
    approved_models: [], default_model: null, catalog_error: null,
  } });
  await app.settle();
  const cards = app.byId['model-catalog-list'];
  const card = cards.children[0];
  const unlock = app.byId['model-admin-token']; unlock.value = 'a'.repeat(32);
  app.byId['model-admin-unlock'].dispatch('click'); await app.settle();
  assert.match(app.byId['model-admin-status'].textContent, /desbloqueadas/);
  const inputs = [...card.querySelectorAll('input')];
  inputs.find(input => input.value === 'standard-coding').checked = true;
  inputs.find(input => input.value === 'standard-coding').dispatch('change');
  inputs.find(input => input.value === 'low').checked = true;
  inputs.find(input => input.value === 'low').dispatch('change');
  const qualify = [...card.querySelectorAll('button')].find(button => button.textContent.includes('qualificar'));
  qualify.dispatch('click'); await app.settle();
  const request = app.calls.find(call => call.url === '/api/dev/model-management/qualifications');
  assert.equal(request.authorization, `Bearer ${'a'.repeat(32)}`);
  assert.deepEqual(request.body.capabilities, ['standard-coding']);
  assert.deepEqual(request.body.efforts, ['low']);
  assert.equal(app.box.discoveredCatalog.models[0].active, false);
});

test('catalog failure fails closed and missing authorized projects are stated', async () => {
  const app = harness([], { projects: [] });
  await app.settle();
  assert.match(app.byId['model-catalog-project-note'].textContent, /Nenhum projeto autorizado/);
  assert.match(app.byId['data-status'].textContent, /Nenhum projeto autorizado/);
  const planning = app.grid.querySelector('[data-pane="maestro"]')
    .querySelector('.cockpit-composer').querySelector('.cockpit-model-select');
  app.box.catalogError = new Error('OmniRoute indisponível');
  app.byId['model-catalog-refresh'].dispatch('click');
  await app.settle();
  assert.match(app.byId['model-catalog-status'].textContent, /OmniRoute indisponível/);
  assert.match(app.byId['model-catalog-list'].textContent, /Atualize o catálogo/);
  assert.equal([...planning.children].filter(option => option.value).length, 0);
  assert.equal(app.grid.querySelector('[data-pane="maestro"]').querySelector('.cockpit-authorize').disabled, true);
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

test('Maestro planning requires and reuses a bounded persistent cost authorization', async () => {
  const app = harness([agent(1)]);
  app.box.authorizations = [{ authorization_id: 'AUTH-1', model: 'paid/model', cost_status: 'low_cost', status: 'ACTIVE',
    uses_remaining: 4, max_uses: 5, max_concurrent: 1, active_executions: 0, expires_at: new Date(Date.now() + 86400000).toISOString() }];
  await app.settle();
  await app.refresh();
  const form = app.grid.querySelector('[data-pane="maestro"]').querySelector('.cockpit-composer');
  const select = form.querySelector('select');
  select.value = 'paid/model';
  select.dispatch('change');
  const authorize = form.querySelector('.cockpit-authorize');
  assert.equal(authorize.disabled, false);
  authorize.dispatch('click');
  await app.settle();
  const posts = app.calls.filter(call => call.url === '/api/workspace/maestro/authorize');
  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].body, { project_id: 'demo', model: 'paid/model', confirm_cost: false });
  assert.match(form.querySelector('.cockpit-compose-note').textContent, /IA autorizada/);
});

test('agent execution reuses the active project/model authorization and displays remaining limits', async () => {
  const paidAgent = agent(1);
  paidAgent.model = 'paid/model';
  const app = harness([paidAgent]);
  app.box.authorizations = [{ authorization_id: 'AUTH-AGENT', model: 'paid/model', cost_status: 'low_cost', status: 'ACTIVE',
    uses_remaining: 3, max_uses: 5, max_concurrent: 2, active_executions: 0, expires_at: new Date(Date.now() + 86400000).toISOString() }];
  await app.settle();
  await app.refresh();
  const pane = app.grid.querySelector('[data-pane="PANE-1"]');
  assert.match(pane.composer.note.textContent, /3\/5 usos restantes/);
  assert.equal(pane.composer.controls.querySelector('.cockpit-cost-confirm').hidden, true);
  pane.composer.input.value = 'Executar tarefa autorizada';
  pane.composer.form.requestSubmit();
  await app.settle();
  const post = app.calls.find(call => call.url.endsWith('/messages') && call.method === 'POST');
  assert.deepEqual(post.body, { text: 'Executar tarefa autorizada' });
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

test('regression: cost authorization model list renders and keeps its selection with real HTMLCollection semantics', async () => {
  const app = harness([]);
  await app.settle();
  const select = app.byId['cost-auth-model'];
  // A real HTMLCollection has no Array methods; this is what threw
  // "costAuthModel.children.some is not a function" and aborted every refresh.
  assert.equal(typeof select.children.some, 'undefined');
  assert.equal(typeof select.children.map, 'undefined');
  assert.match(app.byId['data-status'].textContent, /atualização automática/);
  select.value = 'paid/model';
  select.dispatch('change');
  await app.refresh();
  assert.doesNotMatch(app.byId['data-status'].textContent, /Falha ao carregar/);
  assert.equal(select.value, 'paid/model', 'the chosen model survives a re-render');
  assert.deepEqual([...select.children].map(option => option.value), ['', 'paid/model']);
});

test('the Codex Native pane is a system pane: kept across refreshes and project switches, never closable, and the host API reaches add-ons', async () => {
  const app = harness([{ pane_id: 'PANE-1', title: 'Agente A', role: 'dev', model: 'free/model', brief: null,
    session: { session_id: 'DEV-1', status: 'ACTIVE', branch: 'b' }, messages: [], dependencies: [], latest_mission: null }], { withNative: true });
  await app.settle();
  const nativeFor = () => app.grid.querySelector('[data-pane="native"]');
  assert.ok(nativeFor(), 'the native pane is part of the grid');
  assert.equal(nativeFor().querySelector('.close'), null, 'the Codex Native pane has no close control');
  assert.equal(nativeFor().dataset.title, 'Codex Native');
  const seen = [];
  const host = app.context.window.NexusCockpit;
  assert.equal(host.projectId(), 'demo');
  host.onProject(projectId => seen.push(projectId));
  assert.deepEqual(seen, ['demo'], 'a late add-on is told the current project immediately');
  await app.refresh();
  assert.ok(nativeFor(), 'refresh never removes the native pane');
  assert.deepEqual([...app.byId['pane-nav'].children].map(button => button.dataset.paneTarget).includes('native'), true);
  assert.equal(Object.isFrozen(host), true, 'add-ons cannot replace the host API');
});
