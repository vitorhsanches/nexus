const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const { TextNode, el } = require('./fake_dom.cjs');

const root = path.resolve(__dirname, '../..');
const script = fs.readFileSync(path.join(root, 'nexus/web/static/js/cockpit-native.js'), 'utf8');
const template = fs.readFileSync(path.join(root, 'nexus/web/templates/cockpit.html'), 'utf8');
const stylesheet = fs.readFileSync(path.join(root, 'nexus/web/static/css/cockpit.css'), 'utf8');
const nativeMarkup = template.slice(template.indexOf('<article class="pane native-pane"'), template.indexOf('</article>', template.indexOf('<article class="pane native-pane"')));

const SECRET = 'sk-abcdefghijklmnopqrstuvwxyz123456';

function buildDom() {
  const byId = {};
  for (const match of nativeMarkup.matchAll(/<(\w+)([^>]*?)\sid="(native-[^"]+)"([^>]*)>/g)) {
    const node = el(match[1]);
    node.id = match[3];
    const attributes = `${match[2]} ${match[4]}`;
    node.hidden = /\shidden(\s|$)/.test(attributes);
    node.disabled = /\sdisabled(\s|$)/.test(attributes);
    byId[node.id] = node;
  }
  byId['native-status'].append(el('i'), new TextNode(' Sem projeto'));
  return byId;
}

function harness(overrides = {}) {
  const byId = buildDom();
  const document = {
    getElementById: id => byId[id] || null,
    createElement: tag => el(tag), createTextNode: value => new TextNode(value),
  };
  const calls = [];
  const toasts = [];
  const timers = [];
  const sources = [];
  const listeners = [];
  class FakeEventSource {
    static CLOSED = 2;
    constructor(url) { this.url = url; this.closed = false; this.readyState = 1; sources.push(this); }
    close() { this.closed = true; }
    emit(payload, id = '') { this.onmessage?.({ data: typeof payload === 'string' ? payload : JSON.stringify(payload), lastEventId: id }); }
    fail() { this.onerror?.(); }
    failClosed() { this.readyState = FakeEventSource.CLOSED; this.onerror?.(); }
  }
  const server = {
    gate: true, integration: false, project: 'demo',
    maestro: { conversation_id: 'MAESTRO-1', chat_session_id: 'CHAT-PLAN', busy: false,
      planning: { backend: 'codex_native', authorized: true, model: 'gpt-x', cost_status: 'free' }, messages: [] },
    costAuthorizations: [], approvalBodies: [], approvedPanes: [], failApproveAfterCommit: false,
    models: { models: [
      { id: 'gpt-x', name: 'GPT X', selectable: true, cost_status: 'free', supported_efforts: ['low', 'high'] },
      { id: 'not-ready', name: 'Not ready', selectable: false },
    ], default_model: 'gpt-x', catalog_error: null },
    claudeModels: { models: [
      { id: 'claude-opus-4-8', name: 'Opus', selectable: true, cost_status: 'known', supported_efforts: ['low', 'high'] },
    ], default_model: 'claude-opus-4-8', catalog_error: null },
    operations: {}, workspacePanes: [], executions: {}, turnDetails: {}, focusCalls: [], reviewHistory: {},
    sessions: [
      { session_id: 'DEV-NATIVE', project_id: 'demo', status: 'ACTIVE', backend: 'codex_native', requested_model: 'gpt-x', branch: 'nexus/dev/DEV-NATIVE', created_at: 't1' },
      { session_id: 'DEV-LEGACY', project_id: 'demo', status: 'ACTIVE', backend: 'omniroute', requested_model: 'cx/old', branch: 'nexus/dev/DEV-LEGACY', created_at: 't0' },
    ],
    turns: { 'DEV-NATIVE': [], 'DEV-LEGACY': [] },
    approvals: { 'DEV-NATIVE': [], 'DEV-LEGACY': [] },
    changed: { 'DEV-NATIVE': [], 'DEV-LEGACY': [] },
    diff: { 'DEV-NATIVE': { base_sha: 'aaaaaaaaaaaa', head_sha: 'bbbbbbbbbbbb', files: [] }, 'DEV-LEGACY': { files: [] } },
    history: [], preflight: null, preflightQueue: [],
    fail: {}, hold: {},
    ...overrides,
  };
  const reply = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
  const fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    const body = options.body ? JSON.parse(options.body) : null;
    calls.push({ method, url, body });
    if (server.hold[`${method} ${url}`]) await server.hold[`${method} ${url}`];
    const failure = Object.entries(server.fail).find(([pattern]) => `${method} ${url}`.includes(pattern));
    if (failure) {
      const [, spec] = failure;
      if (spec === 'html') return { ok: false, status: 502, json: async () => { throw new SyntaxError('Unexpected token <'); } };
      return reply({ detail: { code: 'X', message: spec } }, 409);
    }
    const sessionMatch = url.match(/^\/api\/dev\/sessions\/([^/?]+)(\/[^?]*)?(\?.*)?$/);
    if (url === '/api/dev/execution-status') return reply({ execution_enabled: server.gate });
    if (url === '/api/dev/integration-status') return reply({ integration_enabled: server.integration });
    if (url.startsWith('/api/workspace/maestro?')) return reply(server.maestro);
    if (url.startsWith('/api/workspace/cost-authorizations?')) return reply({ authorizations: server.costAuthorizations });
    if (/^\/api\/workspace\/plans\/[^/]+$/.test(url) && method === 'GET') {
      const id = decodeURIComponent(url.split('/').at(-1));
      return reply(server.maestro.messages.find(item => item.plan?.plan_id === id)?.plan || { plan_id: id, status: 'PROPOSED' });
    }
    if (method === 'POST' && url === '/api/workspace/maestro/authorize') {
      server.maestro = { ...server.maestro, chat_session_id: 'CHAT-PLAN-NEW', planning: {
        backend: 'codex_native', authorized: true, model: body.model, cost_status: 'free',
      } };
      return reply(server.maestro);
    }
    if (method === 'POST' && url === '/api/workspace/maestro/messages') {
      server.maestro.messages = [...server.maestro.messages,
        { author: 'user', text: body.text, status: 'COMPLETED' },
        { author: 'maestro', text: 'Proposta persistida', status: 'COMPLETED', plan: server.nextPlan },
      ];
      return reply(server.maestro);
    }
    if (method === 'POST' && /^\/api\/workspace\/plans\/[^/]+\/approve$/.test(url)) {
      server.approvalBodies.push(body);
      server.approvedPanes = body.selected_agent_keys.map((key, index) => ({ pane_id: `PANE-CHILD-${index + 1}`, plan_agent_key: key }));
      const plan = server.maestro.messages.find(item => item.plan?.plan_id === decodeURIComponent(url.split('/')[4]))?.plan;
      if (plan) { plan.status = 'APPROVED'; plan.approval = { panes: server.approvedPanes.map(pane => ({ key: pane.plan_agent_key, pane_id: pane.pane_id })) }; }
      server.failApproveAfterCommit = false;
      if (server.returnApproveUncertain) {
        server.returnApproveUncertain = false;
        return reply({ detail: { code: 'UNCERTAIN', message: 'timeout after commit' } }, 502);
      }
      return reply({ plan, panes: server.approvedPanes });
    }
    if (url.startsWith('/api/workspace/state?')) return reply({ panes: server.workspacePanes });
    const executionMatch = url.match(/^\/api\/workspace\/plans\/([^/]+)\/execution$/);
    if (executionMatch && method === 'GET') {
      const execution = server.executions[decodeURIComponent(executionMatch[1])];
      return execution ? reply(execution) : reply({ detail: { code: 'EXECUTION_NOT_AUTHORIZED', message: 'not started' } }, 404);
    }
    if (method === 'POST' && /^\/api\/workspace\/plans\/[^/]+\/execute$/.test(url)) {
      const planId = decodeURIComponent(url.split('/')[4]);
      server.executeBodies = [...(server.executeBodies || []), body];
      server.executions[planId] = server.nextExecution || {
        plan_id: planId, status: 'RUNNING', agent_keys: body.agent_keys,
        steps: [{ agent_key: body.agent_keys[0], pane_id: 'PANE-CHILD', status: 'RUNNING', native_turn_id: 'TURN-COORD' },
          ...body.agent_keys.slice(1).map(agent_key => ({ agent_key, pane_id: 'PANE-CHILD-2', status: 'PENDING' }))],
      };
      const plan = server.maestro.messages.find(item => item.plan?.plan_id === planId)?.plan;
      if (plan) plan.execution = server.executions[planId];
      return reply(server.executions[planId]);
    }
    if (method === 'POST' && /^\/api\/workspace\/plans\/[^/]+\/steps\/[^/]+\/review$/.test(url)) {
      const planId = decodeURIComponent(url.split('/')[4]);
      const key = decodeURIComponent(url.split('/')[6]);
      const execution = server.executions[planId];
      const step = execution.steps.find(item => item.agent_key === key);
      step.status = 'READY_FOR_REVIEW'; step.native_review_id = body.review_id;
      execution.status = execution.steps.some(item => item.status === 'PENDING') ? 'AWAITING_EXECUTION' : 'AWAITING_REVIEW';
      const plan = server.maestro.messages.find(item => item.plan?.plan_id === planId)?.plan;
      if (plan) plan.execution = execution;
      return reply(execution);
    }
    if (url === '/api/dev/models?backend=codex_native') return reply(server.models);
    if (url === '/api/dev/models?backend=claude_native') return reply(server.claudeModels);
    if (url.startsWith('/api/dev/sessions?project_id=')) return reply({ sessions: server.sessions });
    if (method === 'POST' && url === '/api/dev/sessions') {
      const session = { session_id: 'DEV-NEW', project_id: body.project_id, status: 'ACTIVE', backend: body.backend, requested_model: body.requested_model, branch: 'nexus/dev/DEV-NEW', created_at: 't2' };
      server.sessions.unshift(session);
      server.turns['DEV-NEW'] = []; server.approvals['DEV-NEW'] = []; server.changed['DEV-NEW'] = []; server.diff['DEV-NEW'] = { files: [] };
      return reply(session);
    }
    if (sessionMatch) {
      const id = sessionMatch[1];
      const rest = sessionMatch[2] || '';
      if (!rest) return reply(server.sessions.find(item => item.session_id === id));
      if (method === 'GET' && rest.startsWith('/turns/')) {
        const turnId = decodeURIComponent(rest.slice('/turns/'.length));
        if (server.turnDetails[turnId]) return reply(server.turnDetails[turnId]);
      }
      if (method === 'GET' && rest === '/turns') return reply({ turns: server.turns[id] });
      if (method === 'GET' && rest.startsWith('/turns/')) return reply(server.turns[id].find(turn => rest.endsWith(turn.turn_id)));
      if (method === 'POST' && rest === '/turns') {
        const turn = { turn_id: 'TURN-1', status: 'RUNNING', prompt: body.prompt, response_text: null, created_at: 't3' };
        server.turns[id].push(turn);
        return reply(turn);
      }
      if (method === 'POST' && rest === '/interrupt') return reply({ status: 'INTERRUPT_REQUESTED' });
      if (rest === '/approvals' && method === 'GET') return reply({ approvals: server.approvals[id] });
      if (rest.endsWith('/operation') && method === 'GET') {
        const approvalId = rest.split('/')[2];
        const detail = server.operations[approvalId];
        if (!detail) return reply({ detail: { code: 'OPERATION_UNAVAILABLE', message: 'gone' } }, 409);
        return reply(detail);
      }
      if (rest.startsWith('/approvals/') && method === 'POST') {
        const approval = server.approvals[id].find(item => rest.endsWith(item.approval_id));
        approval.status = body.decision === 'accept' ? 'APPROVED' : 'DECLINED';
        approval.lastDecisionBody = body;
        return reply(approval);
      }
      if (rest === '/changed-files') return reply({ files: server.changed[id] });
      if (rest === '/diff') return reply(server.diff[id]);
      if (rest === '/integrations') return reply({ integrations: server.history });
      if (rest === '/reviews' && method === 'POST') {
        server.review = { review_id: 'a'.repeat(64), status: 'REVIEWED', target_branch: 'main', target_head: 'a'.repeat(40),
          snapshot: { approval_ready: true, target_branch: 'main', target_head: 'a'.repeat(40), diff: server.diff[id] } };
        server.history = [server.review];
        return reply(server.review);
      }
      if (rest.startsWith('/reviews/') && method === 'GET') return reply(server.history.find(row => rest.endsWith(row.review_id)));
      if (rest === '/preflight') return reply(server.preflightQueue.length ? server.preflightQueue.shift()
        : server.preflight || { review_id: 'a'.repeat(64), ready: false, gate_enabled: false,
          issues: [{ code: 'INTEGRATION_DISABLED', message: 'Integração desabilitada.' }] });
      if (rest === '/apply' && method === 'POST') {
        server.review = { ...server.review, status: 'APPLIED', result_sha: 'c'.repeat(40) };
        server.history = [server.review];
        return reply({ review_id: body.review_id, status: 'APPLIED', branch: 'main', commit_sha: 'c'.repeat(40) });
      }
      if (rest === '/reject' && method === 'POST') {
        server.review = { ...server.review, status: 'REJECTED' };
        server.history = [server.review];
        return reply({ review_id: body.review_id, status: 'REJECTED' });
      }
    }
    throw new Error(`Unexpected API call: ${method} ${url}`);
  };
  const context = {
    document, fetch, window: { EventSource: overrides.noEventSource ? undefined : FakeEventSource,
      NexusCockpit: { projectId: () => server.project, notify: message => toasts.push(message), onProject: callback => listeners.push(callback), refreshWorkspace: async () => { server.workspaceRefreshes = (server.workspaceRefreshes || 0) + 1; }, focusPane: async id => { server.focusCalls.push(id); } } },
    setTimeout(callback, delay) { const timer = { callback, delay }; timers.push(timer); return timer; },
    clearTimeout(timer) { const index = timers.indexOf(timer); if (index >= 0) timers.splice(index, 1); },
  };
  vm.runInNewContext(script, context, { filename: 'cockpit-native.js' });
  const settle = async () => { for (let count = 0; count < 12; count++) await new Promise(resolve => setImmediate(resolve)); };
  return {
    byId, calls, toasts, timers, sources, server, settle, mount: context.window.NexusNativePane.mount,
    get posts() { return calls.filter(call => call.method === 'POST'); },
    async selectProject(id = 'demo') { listeners.forEach(callback => callback(id)); await settle(); },
    async selectSession(id) { byId['native-session'].value = id; byId['native-session'].dispatch('change'); await settle(); },
    async tick(delay) {
      const index = timers.findIndex(timer => timer.delay === delay);
      assert.notEqual(index, -1, `no timer with delay ${delay}`);
      timers.splice(index, 1)[0].callback();
      await settle();
    },
    async chooseModel(id = 'gpt-x') { byId['native-model'].value = id; byId['native-model'].dispatch('change'); await settle(); },
    async compose(prompt) {
      byId['native-prompt'].value = prompt; byId['native-prompt'].dispatch('input');
      await settle();
    },
    text: id => byId[id].textContent,
    options: id => [...byId[id].children].map(option => option.value),
  };
}

const runningTurn = { turn_id: 'TURN-1', status: 'RUNNING', prompt: 'corrija', response_text: null, created_at: 't3' };

test('without a project nothing is requested and every action is disabled', async () => {
  const app = harness();
  await app.settle();
  assert.equal(app.calls.length, 0);
  assert.match(app.text('native-status'), /Sem projeto/);
  assert.equal(app.byId['native-create'].disabled, true);
  assert.equal(app.byId['native-send'].disabled, true);
  assert.equal(app.byId['native-prompt'].disabled, true);
});

test('session choices expand in the pane and a selection closes the list', async () => {
  const app = harness();
  await app.settle();
  assert.match(app.text('native-session-summary'), /selecione um projeto/);
  assert.match(app.text('native-session-list'), /Selecione um projeto autorizado/);
  await app.selectProject();
  assert.equal(app.byId['native-session-list'].children.length, 2);
  assert.match(app.text('native-session-summary'), /2 disponível/);
  app.byId['native-session-menu'].open = true;
  app.byId['native-session-list'].children[0].dispatch('click');
  await app.settle();
  assert.equal(app.byId['native-session-menu'].open, false);
  assert.equal(app.byId['native-session'].value, 'DEV-NATIVE');
  assert.match(app.text('native-session-summary'), /DEV-NATIVE/);
});

test('native disclosures are collapsed by default and their contents are not laid out while closed', () => {
  for (const id of ['native-new', 'native-approvals-section', 'native-changes-section', 'native-review-section', 'native-details']) {
    assert.match(nativeMarkup, new RegExp(`<details[^>]*id="${id}"[^>]*>`));
    assert.doesNotMatch(nativeMarkup.match(new RegExp(`<details[^>]*id="${id}"[^>]*>`))[0], /\sopen(?:\s|>)/);
  }
  assert.match(stylesheet, /\.native-pane \.native-section:not\(\[open\]\)>:not\(summary\),\.native-pane \.native-session-menu:not\(\[open\]\)>:not\(summary\)\{display:none\}/);
});

test('the native transcript scrolls above a persistent task entry and bottom settings', () => {
  const consoleAt = nativeMarkup.indexOf('class="native-console"');
  const composerAt = nativeMarkup.indexOf('id="native-composer"');
  const settingsAt = nativeMarkup.indexOf('id="native-settings"');
  assert.ok(consoleAt >= 0 && consoleAt < composerAt && composerAt < settingsAt);
  assert.doesNotMatch(nativeMarkup, /id="native-confirm"/);
  assert.match(stylesheet, /\.native-console\{[^}]*overflow-y:auto/);
  assert.match(stylesheet, /\.native-pane \.native-body\{display:flex;flex-direction:column/);
});

test('selecting a project loads real sessions, gates and the Codex catalog with GET only', async () => {
  const app = harness();
  await app.selectProject();
  assert.equal(app.posts.length, 0, 'loading never creates anything');
  assert.deepEqual(app.calls.map(call => call.url).sort(), [
    '/api/dev/execution-status', '/api/dev/integration-status', '/api/dev/models?backend=codex_native', '/api/dev/sessions?project_id=demo',
  ]);
  assert.deepEqual(app.options('native-model'), ['', 'gpt-x'], 'only selectable catalog models are offered');
  const labels = [...app.byId['native-session'].children].map(option => option.textContent);
  assert.ok(labels.some(label => label.includes('DEV-NATIVE') && label.includes('Codex Native')));
  assert.ok(labels.some(label => label.includes('DEV-LEGACY') && label.includes('OmniRoute (legado)')));
  assert.equal(app.text('native-gate'), '', 'healthy execution status stays out of the transcript');
  assert.match(app.text('native-model-note'), /1 modelo/);
});

test('a session is created only by an explicit click with an explicitly chosen catalog model', async () => {
  const app = harness();
  await app.selectProject();
  assert.equal(app.byId['native-create'].disabled, true, 'no model chosen yet');
  await app.chooseModel('not-ready');
  assert.equal(app.byId['native-create'].disabled, true, 'a non-selectable model cannot be chosen');
  await app.chooseModel('gpt-x');
  assert.equal(app.posts.length, 0);
  assert.equal(app.byId['native-create'].disabled, false);
  app.byId['native-create'].dispatch('click');
  await app.settle();
  assert.deepEqual(app.posts.map(call => [call.url, call.body]), [
    ['/api/dev/sessions', { project_id: 'demo', backend: 'codex_native', requested_model: 'gpt-x' }]]);
  assert.equal(app.byId['native-session'].value, 'DEV-NEW');
  assert.match(app.text('native-meta-session'), /DEV-NEW/);
  assert.match(app.text('native-session-note'), /worktree isolado/);
});

test('switching the runtime reloads the real catalog for that account and creates with the chosen backend', async () => {
  const app = harness();
  await app.selectProject();
  // Codex Native is the default runtime (fake_dom, unlike a real <select>,
  // does not auto-select the first <option>, so the catalog fetched on
  // project load -- asserted next -- is the real signal here).
  assert.deepEqual(app.options('native-model'), ['', 'gpt-x']);

  app.byId['native-runtime'].value = 'claude_native';
  app.byId['native-runtime'].dispatch('change');
  await app.settle();
  assert.ok(app.calls.some(call => call.url === '/api/dev/models?backend=claude_native'), 'the Claude catalog was fetched');
  assert.deepEqual(app.options('native-model'), ['', 'claude-opus-4-8']);

  await app.chooseModel('claude-opus-4-8');
  app.byId['native-create'].dispatch('click');
  await app.settle();
  assert.deepEqual(app.posts.map(call => [call.url, call.body]), [
    ['/api/dev/sessions', { project_id: 'demo', backend: 'claude_native', requested_model: 'claude-opus-4-8' }]]);
});

test('a Claude catalog failure is reported without touching the Codex catalog', async () => {
  const app = harness();
  await app.selectProject();
  app.server.fail['GET /api/dev/models?backend=claude_native'] = 'Claude Code is not signed in.';
  app.byId['native-runtime'].value = 'claude_native';
  app.byId['native-runtime'].dispatch('change');
  await app.settle();
  assert.deepEqual(app.options('native-model'), ['']);
  assert.match(app.text('native-model-note'), /Catálogo Claude Native indisponível: Claude Code is not signed in\./);
  assert.equal(app.byId['native-create'].disabled, true);
});

test('a failed session creation shows the error, selects nothing and is not retried', async () => {
  const app = harness();
  await app.selectProject();
  await app.chooseModel();
  app.server.fail['POST /api/dev/sessions'] = 'Projeto sem autorização de Development';
  app.byId['native-create'].dispatch('click');
  await app.settle();
  assert.equal(app.posts.length, 1);
  assert.match(app.text('native-session-note'), /Não foi possível criar a sessão: Projeto sem autorização/);
  assert.equal(app.byId['native-session'].value, '');
  assert.equal(app.byId['native-create'].disabled, false);
});

test('sending needs an active Codex Native session, an enabled gate, a model and a task', async () => {
  const app = harness();
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  assert.equal(app.byId['native-model'].value, 'gpt-x', 'the session reuses its selected model');
  await app.compose('');
  assert.equal(app.byId['native-send'].disabled, true, 'empty task');
  await app.compose('corrija o bug');
  assert.equal(app.byId['native-send'].disabled, false);
  app.byId['native-effort'].value = 'high';
  app.byId['native-composer'].dispatch('submit');
  await app.settle();
  assert.deepEqual(app.posts.map(call => [call.url, call.body]), [
    ['/api/dev/sessions/DEV-NATIVE/turns', { prompt: 'corrija o bug', model: 'gpt-x', effort: 'high' }]]);
  assert.equal(app.byId['native-prompt'].value, '', 'the draft is cleared after the turn was accepted');
  assert.equal(app.byId['native-send'].disabled, true);
  assert.equal(app.sources.length, 1);
  assert.match(app.sources[0].url, /^\/api\/dev\/sessions\/DEV-NATIVE\/events\?turn_id=TURN-1$/);
  assert.match(app.text('native-status'), /Executando/);
});

test('Enter sends once while Shift+Enter keeps the multiline draft', async () => {
  const app = harness();
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  await app.chooseModel();
  await app.compose('primeira tarefa');
  app.byId['native-prompt'].dispatch('keydown', app.byId['native-prompt'], { key: 'Enter', shiftKey: true });
  await app.settle();
  assert.equal(app.posts.length, 0);
  assert.equal(app.byId['native-prompt'].value, 'primeira tarefa');
  app.byId['native-prompt'].dispatch('keydown', app.byId['native-prompt'], { key: 'Enter' });
  await app.settle();
  assert.equal(app.posts.length, 1);
});

test('a disabled execution gate blocks sending and says how to enable it', async () => {
  const app = harness({ gate: false });
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  await app.chooseModel();
  await app.compose('tarefa');
  assert.equal(app.byId['native-send'].disabled, true);
  assert.match(app.text('native-gate'), /-EnableDevelopment/);
  app.byId['native-composer'].dispatch('submit');
  await app.settle();
  assert.equal(app.posts.length, 0);
});

test('an unknown execution gate fails closed', async () => {
  const app = harness({ fail: { 'GET /api/dev/execution-status': 'indisponível' } });
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  await app.chooseModel();
  await app.compose('tarefa');
  assert.equal(app.byId['native-send'].disabled, true);
  assert.match(app.text('native-gate'), /Estado do servidor indisponível: indisponível/);
});

test('a failed send keeps the draft, is reported as uncertain and is never repeated', async () => {
  const app = harness();
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  await app.chooseModel();
  await app.compose('tarefa importante');
  app.server.fail['POST /api/dev/sessions/DEV-NATIVE/turns'] = 'Turno ativo';
  app.byId['native-composer'].dispatch('submit');
  await app.settle();
  assert.equal(app.byId['native-prompt'].value, 'tarefa importante');
  assert.match(app.text('native-error'), /Turno ativo.*incerto/);
  await app.tick(3000).catch(() => {});
  assert.equal(app.posts.length, 1, 'no automatic retry');
  assert.equal(app.sources.length, 0);
});

test('a double submit while the POST is in flight sends exactly one task', async () => {
  let release;
  const app = harness({ hold: { } });
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  await app.chooseModel();
  await app.compose('uma vez só');
  app.server.hold['POST /api/dev/sessions/DEV-NATIVE/turns'] = new Promise(resolve => { release = resolve; });
  app.byId['native-composer'].dispatch('submit');
  app.byId['native-composer'].dispatch('submit');
  await app.settle();
  release();
  await app.settle();
  assert.equal(app.posts.filter(call => call.url.endsWith('/turns')).length, 1);
});

test('SSE streams text and activity, then reconciles by GET and loads files, diff and approvals', async () => {
  const app = harness();
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  await app.chooseModel();
  await app.compose('corrija');
  app.byId['native-composer'].dispatch('submit');
  await app.settle();
  const [source] = app.sources;
  source.emit({ method: 'turn/started' }, 'DEV-NATIVE:TURN-1:1');
  source.emit({ method: 'item/agentMessage/delta', params: { delta: 'Olá ' } }, 'DEV-NATIVE:TURN-1:2');
  source.emit({ method: 'item/agentMessage/delta', params: { delta: 'mundo' } }, 'DEV-NATIVE:TURN-1:3');
  source.emit({ method: 'item/agentMessage/delta', params: { delta: 'duplicado' } }, 'DEV-NATIVE:TURN-1:3');
  source.emit({ method: 'item/started', params: { item: { type: 'commandExecution', command: 'python -m pytest', status: 'inProgress' } } }, 'DEV-NATIVE:TURN-1:4');
  source.emit('not json', 'DEV-NATIVE:TURN-1:5');
  assert.equal(app.byId['native-live'].hidden, false);
  assert.match(app.text('native-live'), /AO VIVO · RUNNING/);
  assert.match(app.text('native-live'), /Olá mundo/);
  assert.doesNotMatch(app.text('native-live'), /duplicado/);
  assert.match(app.text('native-live'), /commandExecution: python -m pytest/);
  assert.equal(app.text('native-connection'), 'AO VIVO');

  app.server.turns['DEV-NATIVE'][0] = { ...runningTurn, status: 'COMPLETED', response_text: 'Corrigi `add`.' };
  app.server.changed['DEV-NATIVE'] = [{ path: 'calc.py', status: 'M' }];
  app.server.diff['DEV-NATIVE'] = { base_sha: 'aaaaaaaaaaaa', head_sha: 'bbbbbbbbbbbb', files: [{ path: 'calc.py', status: 'M', patch: '-return a - b\n+return a + b' }] };
  source.emit({ type: 'nexus/turnEnded', status: 'COMPLETED' }, 'DEV-NATIVE:TURN-1:6');
  await app.settle();
  assert.equal(source.closed, true);
  assert.equal(app.byId['native-live'].hidden, true);
  assert.match(app.text('native-log'), /Corrigi `add`\./);
  assert.match(app.text('native-status'), /Concluído/);
  assert.match(app.text('native-files'), /M calc\.py/);
  assert.match(app.text('native-diff'), /\+return a \+ b/);
  assert.match(app.text('native-diff-meta'), /Base aaaaaaaaaa → bbbbbbbbbb/);
  assert.equal(app.byId['native-review-create'].disabled, false);
  assert.equal(app.timers.filter(timer => timer.delay === 3000).length, 0, 'polling stops with the turn');
});

test('reconnection: a dropped stream shows the state, resumes from its cursor and polling still settles the turn', async () => {
  const app = harness();
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  await app.chooseModel();
  await app.compose('corrija');
  app.byId['native-composer'].dispatch('submit');
  await app.settle();
  const first = app.sources[0];
  first.emit({ method: 'turn/started' }, 'DEV-NATIVE:TURN-1:7');
  first.fail();
  assert.equal(app.text('native-connection'), 'RECONECTANDO');
  first.failClosed();
  assert.equal(first.closed, true);
  await app.tick(1000);
  assert.equal(app.sources.length, 2);
  assert.match(app.sources[1].url, /turn_id=TURN-1&cursor=DEV-NATIVE%3ATURN-1%3A7$/);
  // The browser stream never decides the outcome: the persisted turn does.
  app.server.turns['DEV-NATIVE'][0] = { ...runningTurn, status: 'FAILED', error_code: 'CODEX_FAILED', error_message: 'sem rede' };
  await app.tick(3000);
  assert.match(app.text('native-status'), /Falhou/);
  assert.match(app.text('native-log'), /CODEX_FAILED: sem rede/);
  assert.equal(app.sources[1].closed, true);
  assert.equal(app.posts.filter(call => call.url.endsWith('/turns')).length, 1, 'reconnecting never repeats the POST');
});

test('without EventSource the pane polls the persisted turn', async () => {
  const app = harness({ noEventSource: true });
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  await app.chooseModel();
  await app.compose('corrija');
  app.byId['native-composer'].dispatch('submit');
  await app.settle();
  assert.equal(app.sources.length, 0);
  assert.equal(app.text('native-connection'), 'CONSULTANDO');
  app.server.turns['DEV-NATIVE'][0] = { ...runningTurn, status: 'COMPLETED', response_text: 'ok' };
  await app.tick(3000);
  assert.match(app.text('native-status'), /Concluído/);
});

test('a replay gap discards live text and reconciles from the persisted record', async () => {
  const app = harness();
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  await app.chooseModel();
  await app.compose('corrija');
  app.byId['native-composer'].dispatch('submit');
  await app.settle();
  app.sources[0].emit({ method: 'item/agentMessage/delta', params: { delta: 'texto volátil' } }, 'DEV-NATIVE:TURN-1:1');
  app.sources[0].emit({ type: 'nexus/replayGap' }, 'DEV-NATIVE:TURN-1:2');
  await app.settle();
  assert.match(app.text('native-live'), /Eventos ao vivo expiraram/);
  assert.doesNotMatch(app.text('native-live'), /texto volátil/);
  assert.equal(app.sources[0].closed, true);
  assert.equal(app.sources.length, 1, 'the turn is not restarted');
});

test('opening a session with a running turn attaches to it instead of starting anything', async () => {
  const app = harness();
  app.server.turns['DEV-NATIVE'] = [runningTurn];
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  assert.equal(app.posts.length, 0);
  assert.equal(app.sources.length, 1);
  assert.match(app.sources[0].url, /turn_id=TURN-1$/);
  assert.equal(app.byId['native-stop'].hidden, false);
});

test('interrupting is an explicit click on the active turn', async () => {
  const app = harness();
  app.server.turns['DEV-NATIVE'] = [runningTurn];
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  assert.equal(app.posts.length, 0);
  app.byId['native-stop'].dispatch('click');
  await app.settle();
  assert.deepEqual(app.posts.map(call => call.url), ['/api/dev/sessions/DEV-NATIVE/interrupt?turn_id=TURN-1']);
  assert.match(app.text('native-error'), /INTERRUPT_REQUESTED/);
});

test('approvals are shown exactly, decided only by a click, and never automatically', async () => {
  const app = harness();
  app.server.turns['DEV-NATIVE'] = [runningTurn];
  app.server.approvals['DEV-NATIVE'] = [
    { approval_id: 'APPROVAL-1', kind: 'command', status: 'PENDING', summary: { command: `python -m pytest --token=${SECRET}`, cwd: 'C:/wt/DEV-NATIVE', reason: 'Rodar testes fora do sandbox' } },
    { approval_id: 'APPROVAL-0', kind: 'file_change', status: 'EXPIRED', summary: { grantRoot: 'C:/wt/DEV-NATIVE' } },
  ];
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  assert.equal(app.text('native-approval-count'), '1');
  assert.match(app.text('native-approvals'), /Comando · PENDING/);
  assert.match(app.text('native-approvals'), /python -m pytest --token=\[redacted\]/);
  assert.doesNotMatch(app.text('native-approvals'), new RegExp(SECRET));
  assert.match(app.text('native-approvals'), /Rodar testes fora do sandbox/);
  assert.match(app.text('native-status'), /Aguardando você/);
  assert.deepEqual(app.toasts, ['Codex Native aguarda sua aprovação']);
  await app.tick(3000);
  await app.tick(3000);
  assert.equal(app.posts.length, 0, 'polling and rendering never decide an approval');

  const buttons = [...app.byId['native-approvals'].children[0].children[0]?.children ?? []];
  assert.equal(buttons.length, 0, 'the decision buttons live in the pending row, not elsewhere');
  const pendingRow = [...app.byId['native-approvals'].children][0];
  const [accept, decline] = [...[...pendingRow.children].at(-1).children];
  assert.equal(accept.textContent, 'Aprovar uma vez');
  assert.equal(decline.textContent, 'Recusar');
  accept.dispatch('click');
  await app.settle();
  assert.deepEqual(app.posts.map(call => [call.url, call.body]), [['/api/dev/sessions/DEV-NATIVE/approvals/APPROVAL-1', { decision: 'accept' }]]);
  assert.equal(app.text('native-approval-count'), '0');
  assert.match(app.text('native-approval-note'), /Aprovado uma vez/);
});

test('a Claude edit approval cannot be accepted before its exact content was fetched and shown', async () => {
  const app = harness();
  app.server.turns['DEV-NATIVE'] = [runningTurn];
  app.server.approvals['DEV-NATIVE'] = [
    { approval_id: 'APPROVAL-1', kind: 'file_change', status: 'PENDING',
      summary: { tool: 'Edit', grantRoot: 'calc.py', reason: 'Edit in the session worktree', operation_sha256: 'a'.repeat(64), operation_bytes: 42 } },
  ];
  app.server.operations['APPROVAL-1'] = { approval_id: 'APPROVAL-1', tool: 'Edit', operation_sha256: 'a'.repeat(64), operation: '{"old":"a - b","new":"a + b"}' };
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  assert.match(app.text('native-approvals'), /Ferramenta: Edit/);

  const pendingRow = [...app.byId['native-approvals'].children][0];
  const [accept] = [...[...pendingRow.children].at(-1).children];
  assert.equal(accept.disabled, true, 'nothing is approvable before the exact operation was shown');
  assert.doesNotMatch(app.text('native-approvals'), /"old":"a - b"/, 'content is not shown until explicitly requested');

  const reveal = [...pendingRow.children].find(node => node.tagName === 'BUTTON' && node.textContent === 'Ver alteração completa');
  reveal.dispatch('click');
  await app.settle();
  assert.ok(app.calls.some(call => call.url === '/api/dev/sessions/DEV-NATIVE/approvals/APPROVAL-1/operation'));
  assert.match(app.text('native-approvals'), /"old":"a - b"/);

  const revealedRow = [...app.byId['native-approvals'].children][0];
  const [acceptNow] = [...[...revealedRow.children].at(-1).children];
  assert.equal(acceptNow.disabled, false, 'approvable once the exact operation was shown');
  acceptNow.dispatch('click');
  await app.settle();
  assert.deepEqual(app.posts.at(-1), { method: 'POST', url: '/api/dev/sessions/DEV-NATIVE/approvals/APPROVAL-1',
    body: { decision: 'accept', operation_sha256: 'a'.repeat(64) } });
});

test('declining posts only a decline; a failed decision is reported and nothing else is decided', async () => {
  const app = harness();
  app.server.turns['DEV-NATIVE'] = [runningTurn];
  app.server.approvals['DEV-NATIVE'] = [
    { approval_id: 'APPROVAL-1', kind: 'command', status: 'PENDING', summary: { command: 'rm -r x' } },
    { approval_id: 'APPROVAL-2', kind: 'file_change', status: 'PENDING', summary: { grantRoot: 'wt' } },
  ];
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  const rows = [...app.byId['native-approvals'].children];
  app.server.fail['POST /api/dev/sessions/DEV-NATIVE/approvals/APPROVAL-1'] = 'Solicitação não está mais pendente';
  [...[...rows[0].children].at(-1).children][1].dispatch('click');
  await app.settle();
  assert.match(app.text('native-approval-note'), /Não foi possível registrar a decisão: Solicitação não está mais pendente/);
  assert.equal(app.posts.length, 1);
  assert.equal(app.posts[0].body.decision, 'decline');
  assert.equal(app.text('native-approval-count'), '2', 'the other request stays pending');
});

test('approval polling failures are shown and do not hide existing state', async () => {
  const app = harness();
  app.server.turns['DEV-NATIVE'] = [runningTurn];
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  app.server.fail['GET /api/dev/sessions/DEV-NATIVE/approvals'] = 'html';
  await app.tick(3000);
  assert.match(app.text('native-approval-note'), /Falha ao consultar aprovações: HTTP 502/);
});

test('files and diff render as inert text, with secrets redacted and truncation stated', async () => {
  const app = harness();
  app.server.changed['DEV-NATIVE'] = [{ path: 'a.py', status: 'M' }, { path: '.env', status: 'A' }];
  app.server.diff['DEV-NATIVE'] = { base_sha: 'aaaaaaaaaaaa', head_sha: 'bbbbbbbbbbbb', truncated: true, files: [
    { path: 'a.py', status: 'M', patch: `+<img src=x onerror=alert(1)>\n+API_KEY=${SECRET}` },
    { path: '.env', status: 'A', patch: null, reason: 'Sensitive file content is omitted.' },
  ] };
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  const diff = app.byId['native-diff'];
  assert.equal(diff.children.length, 0, 'markup in a patch is text, never elements');
  assert.match(diff.textContent, /<img src=x onerror=alert\(1\)>/);
  assert.match(diff.textContent, /API_KEY=\[redacted\]/);
  assert.doesNotMatch(diff.textContent, new RegExp(SECRET));
  assert.match(app.text('native-diff-meta'), /diff truncado/);
  [...app.byId['native-files'].children][1].dispatch('click');
  assert.match(diff.textContent, /Sensitive file content is omitted\./);
});

test('the review snapshot and preflight show the exact destination, diff, and integration block', async () => {
  const app = harness();
  app.server.changed['DEV-NATIVE'] = [{ path: 'calc.py', status: 'M' }];
  app.server.diff['DEV-NATIVE'] = { files: [{ path: 'calc.py', status: 'M', patch: '+x' }] };
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  assert.equal(app.byId['native-review-preflight'].disabled, true);
  assert.equal(app.posts.length, 0);
  app.byId['native-review-create'].dispatch('click');
  await app.settle();
  assert.deepEqual(app.posts.map(call => call.url), ['/api/dev/sessions/DEV-NATIVE/reviews']);
  assert.match(app.text('native-review'), /Revisão aaaaaaaaaaaa · REVIEWED · 1 arquivo\(s\) · pronta para decisão humana/);
  assert.match(app.text('native-review-target'), /Destino: main/);
  assert.match(app.text('native-review-files'), /calc\.py/);
  assert.match(app.text('native-review-diff'), /\+x/);
  assert.match(app.text('native-review-note'), /Inspecione o diff/);
  app.byId['native-review-preflight'].dispatch('click');
  await app.settle();
  assert.match(app.text('native-preflight'), /Preflight BLOQUEADO · INTEGRATION_DISABLED/);
  assert.match(app.text('native-review-blockers'), /Integração desabilitada/);
  assert.match(app.text('native-history'), /aaaaaaaaaaaa · REVIEWED/);
  assert.equal(app.posts.length, 1);
  assert.equal(app.byId['native-review-apply'].disabled, true);
  assert.ok(app.calls.every(call => !/\/(apply|reject)/.test(call.url)));
});

async function createReviewInCockpit(app) {
  app.server.changed['DEV-NATIVE'] = [{ path: 'calc.py', status: 'M' }];
  app.server.diff['DEV-NATIVE'] = { base_sha: 'a'.repeat(40), head_sha: 'a'.repeat(40),
    files: [{ path: 'calc.py', status: 'M', patch: '+def add(a, b):\n+    return a + b' }] };
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  app.byId['native-review-create'].dispatch('click');
  await app.settle();
}

test('Apply requires a reviewed snapshot, ready preflight, explicit confirmation, and a second server preflight', async () => {
  const app = harness({ integration: true,
    preflight: { review_id: 'a'.repeat(64), ready: true, gate_enabled: true, target_branch: 'main', issues: [] } });
  await createReviewInCockpit(app);
  assert.equal(app.byId['native-review-apply'].disabled, true);
  app.byId['native-review-apply'].dispatch('click');
  await app.settle();
  assert.equal(app.posts.filter(call => call.url.endsWith('/apply')).length, 0);
  app.byId['native-review-preflight'].dispatch('click');
  await app.settle();
  assert.equal(app.byId['native-review-apply'].disabled, true, 'preflight alone is not consent');
  app.byId['native-apply-confirm'].checked = true;
  app.byId['native-apply-confirm'].dispatch('change');
  assert.equal(app.byId['native-review-apply'].disabled, false);
  app.byId['native-review-apply'].dispatch('click');
  await app.settle();
  const writes = app.posts.filter(call => call.url.endsWith('/apply'));
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0].body, { review_id: 'a'.repeat(64), confirmed: true });
  const preflights = app.calls.filter(call => call.url.includes('/preflight?'));
  assert.equal(preflights.length, 2, 'the second read occurred immediately before Apply');
  assert.ok(app.calls.indexOf(preflights[1]) < app.calls.indexOf(writes[0]));
  assert.match(app.text('native-review'), /APPLIED/);
  assert.match(app.text('native-history'), /APPLIED/);
  assert.equal(app.byId['native-apply-confirm'].checked, false);
});

test('a destination change between confirmation and Apply blocks the POST', async () => {
  const ready = { review_id: 'a'.repeat(64), ready: true, gate_enabled: true, issues: [] };
  const moved = { review_id: 'a'.repeat(64), ready: false, gate_enabled: true,
    issues: [{ code: 'TARGET_MOVED', message: 'A branch de destino mudou.' }] };
  const app = harness({ integration: true, preflightQueue: [ready, moved] });
  await createReviewInCockpit(app);
  app.byId['native-review-preflight'].dispatch('click');
  await app.settle();
  app.byId['native-apply-confirm'].checked = true;
  app.byId['native-apply-confirm'].dispatch('change');
  app.byId['native-review-apply'].dispatch('click');
  await app.settle();
  assert.equal(app.posts.filter(call => call.url.endsWith('/apply')).length, 0);
  assert.match(app.text('native-review-blockers'), /TARGET_MOVED.*destino mudou/);
  assert.equal(app.byId['native-apply-confirm'].checked, false);
});

test('Apply failure is surfaced, history is reconciled, and no automatic retry occurs', async () => {
  const app = harness({ integration: true,
    preflight: { review_id: 'a'.repeat(64), ready: true, gate_enabled: true, issues: [] },
    fail: { 'POST /api/dev/sessions/DEV-NATIVE/apply': 'Snapshot obsoleto' } });
  await createReviewInCockpit(app);
  app.byId['native-review-preflight'].dispatch('click');
  await app.settle();
  app.byId['native-apply-confirm'].checked = true;
  app.byId['native-apply-confirm'].dispatch('change');
  app.byId['native-review-apply'].dispatch('click');
  await app.settle();
  assert.equal(app.posts.filter(call => call.url.endsWith('/apply')).length, 1);
  assert.match(app.text('native-review-note'), /Resultado do Apply incerto.*Snapshot obsoleto/);
  assert.equal(app.byId['native-review-apply'].disabled, true);
  assert.equal(app.byId['native-apply-confirm'].checked, false);
});

test('Reject is an independent human click and refreshes persisted history without Apply', async () => {
  const app = harness();
  await createReviewInCockpit(app);
  assert.equal(app.byId['native-review-reject'].disabled, false);
  app.byId['native-review-reject'].dispatch('click');
  await app.settle();
  assert.deepEqual(app.posts.filter(call => call.url.endsWith('/reject')).map(call => call.body),
    [{ review_id: 'a'.repeat(64) }]);
  assert.equal(app.posts.filter(call => call.url.endsWith('/apply')).length, 0);
  assert.match(app.text('native-review'), /REJECTED/);
  assert.match(app.text('native-history'), /REJECTED/);
  assert.equal(app.byId['native-review-apply'].disabled, true);
});

test('incomplete diff and active turn block Apply, while reload restores the persisted review', async () => {
  const app = harness({ integration: true,
    preflight: { review_id: 'a'.repeat(64), ready: true, gate_enabled: true, issues: [] } });
  await createReviewInCockpit(app);
  await app.selectSession('DEV-NATIVE');
  assert.match(app.text('native-review'), /REVIEWED/, 'history restores the persisted snapshot');
  app.server.history[0].snapshot.diff.files[0].patch = null;
  app.server.history[0].snapshot.diff.files[0].content_omitted = true;
  await app.selectSession('DEV-NATIVE');
  assert.match(app.text('native-review-blockers'), /diff incompleto/);
  assert.equal(app.byId['native-review-apply'].disabled, true);
  app.server.history[0].snapshot.diff.files[0].patch = '+complete';
  app.server.history[0].snapshot.diff.files[0].content_omitted = false;
  app.server.turns['DEV-NATIVE'] = [runningTurn];
  await app.selectSession('DEV-NATIVE');
  assert.match(app.text('native-review-blockers'), /Execução ativa/);
  assert.equal(app.byId['native-review-apply'].disabled, true);
  assert.equal(app.byId['native-review-reject'].disabled, true);
});

test('a legacy OmniRoute session is labelled and read-only: no sending, no review', async () => {
  const app = harness();
  app.server.changed['DEV-LEGACY'] = [{ path: 'x.py', status: 'M' }];
  app.server.diff['DEV-LEGACY'] = { files: [{ path: 'x.py', status: 'M', patch: '+y' }] };
  await app.selectProject();
  await app.selectSession('DEV-LEGACY');
  await app.chooseModel();
  assert.match(app.text('native-meta-backend'), /omniroute · legado/);
  assert.match(app.text('native-compose-note'), /OmniRoute legada: somente leitura/);
  assert.equal(app.byId['native-prompt'].disabled, true);
  assert.equal(app.byId['native-send'].disabled, true);
  assert.equal(app.byId['native-review-create'].disabled, true);
  assert.match(app.text('native-files'), /M x\.py/, 'its files remain viewable');
  assert.match(app.text('native-log'), /Nenhuma tarefa enviada/);
  app.byId['native-composer'].dispatch('submit');
  await app.settle();
  assert.equal(app.posts.length, 0);
});

test('a running legacy turn is not attached to as if it were Codex Native', async () => {
  const app = harness();
  app.server.turns['DEV-LEGACY'] = [runningTurn];
  await app.selectProject();
  await app.selectSession('DEV-LEGACY');
  assert.equal(app.sources.length, 0);
  assert.equal(app.byId['native-stop'].hidden, true);
});

test('API errors are explained and fail closed: catalog, sessions and non-JSON failures', async () => {
  const app = harness({ fail: { 'GET /api/dev/models': 'html', 'GET /api/dev/sessions?project_id=': 'banco indisponível' } });
  await app.selectProject();
  assert.match(app.text('native-model-note'), /Catálogo Codex Native indisponível: HTTP 502/);
  assert.match(app.text('native-session-note'), /Falha ao listar sessões: banco indisponível/);
  assert.equal(app.byId['native-create'].disabled, true);
  assert.deepEqual(app.options('native-model'), ['']);
});

test('a catalog error reported by Codex is shown and blocks creation', async () => {
  const app = harness({ models: { models: [], default_model: null, catalog_error: 'Codex não está autenticado' } });
  await app.selectProject();
  assert.match(app.text('native-model-note'), /Codex não está autenticado/);
  assert.equal(app.byId['native-create'].disabled, true);
});

test('a session that cannot be opened is reported and leaves nothing selected', async () => {
  const app = harness({ fail: { 'GET /api/dev/sessions/DEV-NATIVE/turns': 'worktree ausente' } });
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  assert.match(app.text('native-session-note'), /Não foi possível abrir a sessão: worktree ausente/);
  assert.equal(app.byId['native-send'].disabled, true);
});

test('switching project or session discards late responses from the previous one', async () => {
  let release;
  const app = harness();
  app.server.hold['GET /api/dev/sessions/DEV-NATIVE/diff'] = new Promise(resolve => { release = resolve; });
  app.server.diff['DEV-NATIVE'] = { files: [{ path: 'stale.py', status: 'M', patch: '+stale' }] };
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  await app.selectSession('DEV-LEGACY');
  release();
  await app.settle();
  assert.doesNotMatch(app.text('native-files'), /stale\.py/);
  assert.match(app.text('native-meta-session'), /DEV-LEGACY/);

  await app.selectProject('other');
  assert.equal(app.sources.length, 0);
  assert.match(app.text('native-meta-session'), /—/);
});

test('changing project closes the live stream and stops polling', async () => {
  const app = harness();
  app.server.turns['DEV-NATIVE'] = [runningTurn];
  await app.selectProject();
  await app.selectSession('DEV-NATIVE');
  assert.equal(app.sources[0].closed, false);
  await app.selectProject('other');
  assert.equal(app.sources[0].closed, true);
  assert.equal(app.timers.filter(timer => timer.delay === 3000).length, 0);
});

test('the module never builds HTML from data or persists review consent in the browser', () => {
  assert.doesNotMatch(script, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function/);
  assert.doesNotMatch(script, /localStorage|sessionStorage/);
});

function fixedPane(paneId) {
  const byId = buildDom();
  const pane = el('article', 'pane native-pane');
  pane.dataset.pane = paneId;
  const icon = el('span', 'pane-icon');
  const identity = el('div', 'pane-identity');
  const identityCopy = el('div');
  identityCopy.append(el('strong'));
  identity.append(icon, identityCopy);
  const runtimeCopy = el('div', 'native-runtime-copy');
  const runtimeTitle = el('strong');
  runtimeTitle.append(new TextNode('Codex Native '), byId['native-connection']);
  runtimeCopy.append(runtimeTitle);
  const body = el('div', 'native-body');
  body.append(byId['native-log']);
  pane.append(identity, runtimeCopy, body);
  for (const [id, node] of Object.entries(byId)) {
    node.dataset.nativeId = id;
    node.id = `${paneId}-${id}`;
    if (!node.parentNode) pane.append(node);
  }
  return { pane, byId };
}

test('two fixed Native panes keep backend, transcript, SSE, approvals and interrupt isolated', async () => {
  const app = harness();
  const claude = { session_id: 'DEV-CLAUDE', project_id: 'demo', status: 'ACTIVE', backend: 'claude_native',
    requested_model: 'sonnet', branch: 'nexus/dev/DEV-CLAUDE', created_at: 't2' };
  app.server.sessions.push(claude);
  app.server.claudeModels.models = [{ id: 'sonnet', name: 'Sonnet', selectable: true, supported_efforts: ['medium'] }];
  app.server.turns['DEV-CLAUDE'] = [];
  app.server.approvals['DEV-CLAUDE'] = [];
  app.server.approvals['DEV-CLAUDE'].push({ approval_id: 'APP-C', status: 'PENDING', kind: 'file_change',
    summary: { tool: 'Edit', operation_sha256: 'digest-c' } });
  app.server.operations['APP-C'] = { operation: 'Edit only Claude worktree', operation_sha256: 'digest-c' };
  app.server.changed['DEV-CLAUDE'] = [];
  app.server.diff['DEV-CLAUDE'] = { files: [] };
  const codexPane = fixedPane('PANE-CODEX');
  const claudePane = fixedPane('PANE-CLAUDE');
  const first = app.mount(codexPane.pane, { projectId: 'demo', sessionId: 'DEV-NATIVE', backend: 'codex_native', effort: 'high' });
  const second = app.mount(claudePane.pane, { projectId: 'demo', sessionId: 'DEV-CLAUDE', backend: 'claude_native', effort: 'medium' });
  await app.settle();
  assert.match(codexPane.pane.dataset.title, /Codex Native/);
  assert.match(claudePane.pane.dataset.title, /Claude Native/);
  assert.equal(codexPane.byId['native-meta-session'].textContent, 'DEV-NATIVE');
  assert.equal(claudePane.byId['native-meta-session'].textContent, 'DEV-CLAUDE');
  assert.equal(codexPane.byId['native-effort'].value, 'high');
  assert.equal(claudePane.byId['native-effort'].value, 'medium');
  assert.equal(codexPane.byId['native-approval-count'].textContent, '0');
  assert.equal(claudePane.byId['native-approval-count'].textContent, '1');
  const approvalRow = claudePane.byId['native-approvals'].children[0];
  const reveal = [...approvalRow.querySelectorAll('button')].find(button => button.textContent === 'Ver alteração completa');
  reveal.dispatch('click');
  await app.settle();
  const accept = [...claudePane.byId['native-approvals'].querySelectorAll('button')].find(button => button.textContent === 'Aprovar uma vez');
  assert.equal(accept.disabled, false);
  accept.dispatch('click');
  await app.settle();
  const decision = app.posts.find(call => call.url.endsWith('/approvals/APP-C'));
  assert.match(decision.url, /DEV-CLAUDE/);
  assert.equal(decision.body.operation_sha256, 'digest-c');
  assert.equal(codexPane.byId['native-approval-count'].textContent, '0');

  codexPane.byId['native-prompt'].value = 'Codex only';
  codexPane.byId['native-prompt'].dispatch('input');
  codexPane.byId['native-composer'].dispatch('submit');
  claudePane.byId['native-prompt'].value = 'Claude only';
  claudePane.byId['native-prompt'].dispatch('input');
  claudePane.byId['native-composer'].dispatch('submit');
  await app.settle();
  const posts = app.posts.filter(call => call.url.endsWith('/turns'));
  assert.equal(posts.length, 2);
  assert.deepEqual(posts.map(call => [call.url, call.body.model, call.body.effort]), [
    ['/api/dev/sessions/DEV-NATIVE/turns', 'gpt-x', 'high'],
    ['/api/dev/sessions/DEV-CLAUDE/turns', 'sonnet', 'medium'],
  ]);
  assert.equal(app.sources.filter(source => !source.closed).length, 2);
  const codexStream = app.sources.find(source => source.url.includes('DEV-NATIVE'));
  const claudeStream = app.sources.find(source => source.url.includes('DEV-CLAUDE'));
  codexStream.emit({ method: 'item/agentMessage/delta', params: { delta: 'Codex stream' } }, 'cursor-c');
  codexStream.failClosed();
  await app.tick(1000);
  assert.match(app.sources.at(-1).url, /DEV-NATIVE.*cursor=cursor-c/);
  assert.equal(claudeStream.closed, false, 'Claude stream must survive Codex reconnect');
  assert.match(codexPane.byId['native-log'].textContent, /Codex only/);
  assert.doesNotMatch(codexPane.byId['native-log'].textContent, /Claude only/);
  assert.match(claudePane.byId['native-log'].textContent, /Claude only/);
  assert.doesNotMatch(claudePane.byId['native-log'].textContent, /Codex only/);

  claudePane.byId['native-stop'].dispatch('click');
  await app.settle();
  assert.equal(app.posts.filter(call => call.url.includes('/interrupt')).length, 1);
  assert.match(app.posts.find(call => call.url.includes('/interrupt')).url, /DEV-CLAUDE/);
  first.dispose(); second.dispose();
  assert.equal(app.sources.filter(source => !source.closed).length, 0);
});

test('a promoted Native pane keeps Development drafts and approves selected Codex/Claude agents in planning mode', async () => {
  const app = harness();
  app.server.maestro.messages = [{ author: 'user', text: 'Implemente e documente', status: 'COMPLETED' }, {
    author: 'maestro', text: 'Plano persistido', status: 'COMPLETED', plan: {
      plan_id: 'PLAN-NATIVE', status: 'PROPOSED', summary: 'Implementar e documentar', explanation: 'Duas tarefas isoladas.',
      agents: [
        { key: 'impl', title: 'Implementação', role: 'implementer', backend: 'codex_native', model: 'gpt-x', effort: 'high',
          task: 'Implemente a função.', acceptance_criteria: ['A função passa nos testes.'], depends_on: [] },
        { key: 'docs', title: 'Documentação', role: 'documenter', backend: 'claude_native', model: 'claude-opus-4-8', effort: 'low',
          task: 'Documente a API.', acceptance_criteria: ['A API está descrita.'], depends_on: ['impl'] },
      ],
    },
  }];
  app.server.returnApproveUncertain = true;
  const native = fixedPane('PANE-MAESTRO');
  const controller = app.mount(native.pane, {
    projectId: 'demo', sessionId: 'DEV-NATIVE', backend: 'codex_native',
    paneId: 'PANE-MAESTRO', maestro: true,
  });
  controller.setMaestro(true);
  await app.settle();
  const devDraft = native.byId['native-prompt'];
  devDraft.value = 'rascunho de Development';
  const tabs = native.pane.querySelectorAll('.native-mode-tab');
  assert.equal(tabs.length, 2);
  tabs[1].dispatch('click');
  await app.settle();
  assert.ok(app.calls.some(call => call.url.includes('/api/workspace/maestro?project_id=demo')));
  assert.ok(app.calls.some(call => call.url === '/api/dev/models?backend=claude_native'));
  assert.equal(devDraft.value, 'rascunho de Development');
  assert.equal(native.pane.querySelector('.native-planning-view').hidden, false);
  const card = native.pane.querySelector('.native-planning-plan');
  assert.ok(card);
  assert.match(card.textContent, /Codex Native|Claude Native/);
  const confirmCost = card.querySelector('.native-planning-cost input');
  assert.ok(confirmCost);
  confirmCost.checked = true;
  confirmCost.dispatch('change');
  await app.settle();
  const refreshedCard = native.pane.querySelector('.native-planning-plan');
  const approve = refreshedCard.querySelector('.cockpit-send');
  assert.equal(approve.disabled, false);
  approve.dispatch('click');
  await app.settle();
  assert.equal(app.server.approvalBodies.length, 1);
  assert.deepEqual(app.server.approvalBodies[0].selected_agent_keys, ['impl', 'docs']);
  assert.deepEqual(app.server.approvalBodies[0].agent_backends, { impl: 'codex_native', docs: 'claude_native' });
  assert.deepEqual(app.server.approvalBodies[0].agent_models, { impl: 'gpt-x', docs: 'claude-opus-4-8' });
  assert.deepEqual(app.server.approvalBodies[0].agent_efforts, { impl: 'high', docs: 'low' });
  assert.equal(app.calls.filter(call => call.method === 'GET' && call.url === '/api/workspace/plans/PLAN-NATIVE').length, 1,
    'an uncertain approval is reconciled by GET');
  assert.equal(app.posts.some(call => /\/api\/dev\/(sessions|missions)/.test(call.url)), false,
    'approval creates panes through the workspace API and starts no turns');
  assert.equal(app.server.workspaceRefreshes, 1, 'created panes are loaded into the cockpit');
  tabs[0].dispatch('click');
  assert.equal(native.pane.querySelector('.native-planning-view').hidden, true);
  assert.equal(devDraft.value, 'rascunho de Development');
  controller.dispose();
});

test('Maestro dispatch requires human confirmation, calls Coordinator for the approved subset, and recovers turn state without replay', async () => {
  const app = harness();
  const plan = {
    plan_id: 'PLAN-DISPATCH', status: 'APPROVED_PARTIAL', summary: 'Entrega nativa',
    agents: [
      { key: 'api', title: 'API', backend: 'codex_native', model: 'gpt-x', effort: 'high', task: 'Implemente', acceptance_criteria: ['Concluído'], depends_on: [] },
      { key: 'claude', title: 'Documentação', backend: 'claude_native', model: 'claude-opus-4-8', effort: 'low', task: 'Documente', acceptance_criteria: ['Pronto'], depends_on: ['api'] },
    ],
    approval: {
      selected_agent_keys: ['api'], agent_models: { api: { backend: 'codex_native', model: 'gpt-x', effort: 'high' } },
      panes: [{ key: 'api', pane_id: 'PANE-CHILD' }], max_turns_per_agent: 2, mission_timeout_seconds: 420,
    },
  };
  app.server.maestro.messages = [{ author: 'maestro', text: 'Plano salvo', status: 'COMPLETED', plan }];
  app.server.workspacePanes = [{ pane_id: 'PANE-CHILD', session: { session_id: 'DEV-CHILD', backend: 'codex_native' } }];
  const native = fixedPane('PANE-MAESTRO');
  const controller = app.mount(native.pane, {
    projectId: 'demo', sessionId: 'DEV-NATIVE', backend: 'codex_native', paneId: 'PANE-MAESTRO', maestro: true,
  });
  controller.setMaestro(true);
  native.pane.querySelectorAll('.native-mode-tab')[1].dispatch('click');
  await app.settle();
  let card = native.pane.querySelector('.native-planning-plan');
  assert.match(card.textContent, /API/);
  assert.match(card.textContent, /claude/);
  let confirmation = card.querySelector('.native-planning-dispatch-confirm input');
  let start = card.querySelector('.cockpit-send');
  assert.equal(start.disabled, true, 'the first dispatch must be confirmed by a person');
  confirmation.checked = true;
  confirmation.dispatch('change');
  await app.settle();
  card = native.pane.querySelector('.native-planning-plan');
  start = card.querySelector('.cockpit-send');
  assert.equal(start.disabled, false);
  start.dispatch('click');
  await app.settle();
  assert.deepEqual(app.server.executeBodies, [{
    agent_keys: ['api'], test_profiles: {}, max_turns_per_agent: 2,
    mission_timeout_seconds: 420, confirm_costs: false,
  }], 'only the persisted selected and created subset is dispatched');
  assert.equal(app.posts.some(call => /\/api\/dev\/sessions\/[^/]+\/turns$/.test(call.url)), false,
    'the pane never bypasses the Coordinator with its own Development turn');
  assert.match(native.pane.querySelector('.native-planning-plan').textContent, /TURN-COORD/);
  assert.match(native.pane.querySelector('.native-planning-plan').textContent, /Codex Native/);

  app.server.turnDetails['TURN-COORD'] = { turn_id: 'TURN-COORD', status: 'RUNNING', response_text: 'Executando' };
  app.server.maestro.messages[0].plan.execution = app.server.executions['PLAN-DISPATCH'];
  const readsBeforeReload = app.calls.filter(call => call.method === 'GET' && call.url.includes('/turns/TURN-COORD')).length;
  await app.tick(2500);
  assert.ok(app.calls.filter(call => call.method === 'GET' && call.url.includes('/turns/TURN-COORD')).length > readsBeforeReload,
    'reload reconciliation reads the persisted real turn');
  assert.equal(app.server.executeBodies.length, 1, 'recovery does not dispatch again');
  const focus = native.pane.querySelector('.native-planning-link');
  focus.dispatch('click');
  await app.settle();
  assert.deepEqual(app.server.focusCalls, ['PANE-CHILD']);
  controller.dispose();
});

test('the promoted pane does not replace an unavailable Claude Native catalog', async () => {
  const app = harness({ claudeModels: { models: [], catalog_error: 'Claude CLI não instalado.' } });
  app.server.maestro.messages = [{ author: 'maestro', text: 'Plano', status: 'COMPLETED', plan: {
    plan_id: 'PLAN-CATALOG', status: 'PROPOSED', summary: 'Uma tarefa',
    agents: [{ key: 'impl', title: 'Implementação', role: 'implementer', backend: 'codex_native', model: 'gpt-x',
      task: 'Implemente.', acceptance_criteria: ['Feito.'], depends_on: [] }],
  } }];
  const native = fixedPane('PANE-MAESTRO');
  const controller = app.mount(native.pane, {
    projectId: 'demo', sessionId: 'DEV-NATIVE', backend: 'codex_native',
    paneId: 'PANE-MAESTRO', maestro: true,
  });
  controller.setMaestro(true);
  await app.settle();
  native.pane.querySelectorAll('.native-mode-tab')[1].dispatch('click');
  await app.settle();
  const card = native.pane.querySelector('.native-planning-plan');
  const runtime = card.querySelector('.native-planning-agent-controls select');
  runtime.value = 'claude_native';
  runtime.dispatch('change');
  await app.settle();
  const current = native.pane.querySelector('.native-planning-plan');
  assert.match(current.textContent, /Claude CLI não instalado/);
  const approval = current.querySelector('.cockpit-send');
  assert.equal(approval.disabled, true);
  assert.equal(app.calls.some(call => call.url.includes('backend=omniroute')), false);
  assert.equal(app.posts.some(call => call.url.includes('/approve')), false);
  controller.dispose();
});

test('one Native session failure does not block the other pane or create a replacement session', async () => {
  const app = harness({
    sessions: [
      { session_id: 'DEV-NATIVE', project_id: 'demo', status: 'ACTIVE', backend: 'codex_native', requested_model: 'gpt-x' },
      { session_id: 'DEV-CLAUDE', project_id: 'demo', status: 'ACTIVE', backend: 'claude_native', requested_model: 'sonnet' },
    ],
    turns: { 'DEV-NATIVE': [], 'DEV-CLAUDE': [] }, approvals: { 'DEV-NATIVE': [], 'DEV-CLAUDE': [] },
    changed: { 'DEV-NATIVE': [], 'DEV-CLAUDE': [] },
    diff: { 'DEV-NATIVE': { files: [] }, 'DEV-CLAUDE': { files: [] } },
    claudeModels: { models: [{ id: 'sonnet', selectable: true, supported_efforts: ['medium'] }] },
    fail: { 'GET /api/dev/sessions/DEV-NATIVE': 'session unavailable' },
  });
  const codexPane = fixedPane('PANE-CODEX-FAIL');
  const claudePane = fixedPane('PANE-CLAUDE-OK');
  const first = app.mount(codexPane.pane, { projectId: 'demo', sessionId: 'DEV-NATIVE', backend: 'codex_native' });
  const second = app.mount(claudePane.pane, { projectId: 'demo', sessionId: 'DEV-CLAUDE', backend: 'claude_native', effort: 'medium' });
  await app.settle();
  assert.match(codexPane.byId['native-session-note'].textContent, /session unavailable/);
  assert.equal(codexPane.byId['native-send'].disabled, true);
  assert.equal(claudePane.byId['native-meta-session'].textContent, 'DEV-CLAUDE');
  assert.equal(app.posts.length, 0);
  first.dispose(); second.dispose();
});
