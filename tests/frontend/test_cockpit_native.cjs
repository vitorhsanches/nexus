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
    models: { models: [
      { id: 'gpt-x', name: 'GPT X', selectable: true, supported_efforts: ['low', 'high'] },
      { id: 'not-ready', name: 'Not ready', selectable: false },
    ], default_model: 'gpt-x', catalog_error: null },
    sessions: [
      { session_id: 'DEV-NATIVE', project_id: 'demo', status: 'ACTIVE', backend: 'codex_native', requested_model: 'gpt-x', branch: 'nexus/dev/DEV-NATIVE', created_at: 't1' },
      { session_id: 'DEV-LEGACY', project_id: 'demo', status: 'ACTIVE', backend: 'omniroute', requested_model: 'cx/old', branch: 'nexus/dev/DEV-LEGACY', created_at: 't0' },
    ],
    turns: { 'DEV-NATIVE': [], 'DEV-LEGACY': [] },
    approvals: { 'DEV-NATIVE': [], 'DEV-LEGACY': [] },
    changed: { 'DEV-NATIVE': [], 'DEV-LEGACY': [] },
    diff: { 'DEV-NATIVE': { base_sha: 'aaaaaaaaaaaa', head_sha: 'bbbbbbbbbbbb', files: [] }, 'DEV-LEGACY': { files: [] } },
    history: [],
    fail: {}, hold: {},
    ...overrides,
  };
  const reply = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
  const fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    const body = options.body ? JSON.parse(options.body) : null;
    calls.push({ method, url, body });
    assert.doesNotMatch(url, /\/(apply|reject)\b/, 'the cockpit must never call Apply or Reject');
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
    if (url === '/api/dev/models?backend=codex_native') return reply(server.models);
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
      if (method === 'GET' && rest === '/turns') return reply({ turns: server.turns[id] });
      if (method === 'GET' && rest.startsWith('/turns/')) return reply(server.turns[id].find(turn => rest.endsWith(turn.turn_id)));
      if (method === 'POST' && rest === '/turns') {
        const turn = { turn_id: 'TURN-1', status: 'RUNNING', prompt: body.prompt, response_text: null, created_at: 't3' };
        server.turns[id].push(turn);
        return reply(turn);
      }
      if (method === 'POST' && rest === '/interrupt') return reply({ status: 'INTERRUPT_REQUESTED' });
      if (rest === '/approvals' && method === 'GET') return reply({ approvals: server.approvals[id] });
      if (rest.startsWith('/approvals/') && method === 'POST') {
        const approval = server.approvals[id].find(item => rest.endsWith(item.approval_id));
        approval.status = body.decision === 'accept' ? 'APPROVED' : 'DECLINED';
        return reply(approval);
      }
      if (rest === '/changed-files') return reply({ files: server.changed[id] });
      if (rest === '/diff') return reply(server.diff[id]);
      if (rest === '/integrations') return reply({ integrations: server.history });
      if (rest === '/reviews' && method === 'POST') {
        server.review = { review_id: 'a'.repeat(64), status: 'REVIEWED', snapshot: { approval_ready: true, diff: { files: server.diff[id].files } } };
        server.history = [{ review_id: 'a'.repeat(64), status: 'REVIEWED' }];
        return reply(server.review);
      }
      if (rest === '/preflight') return reply({ ready: false, issues: [{ code: 'INTEGRATION_DISABLED' }] });
    }
    throw new Error(`Unexpected API call: ${method} ${url}`);
  };
  const context = {
    document, fetch, window: { EventSource: overrides.noEventSource ? undefined : FakeEventSource,
      NexusCockpit: { projectId: () => server.project, notify: message => toasts.push(message), onProject: callback => listeners.push(callback) } },
    setTimeout(callback, delay) { const timer = { callback, delay }; timers.push(timer); return timer; },
    clearTimeout(timer) { const index = timers.indexOf(timer); if (index >= 0) timers.splice(index, 1); },
  };
  vm.runInNewContext(script, context, { filename: 'cockpit-native.js' });
  const settle = async () => { for (let count = 0; count < 12; count++) await new Promise(resolve => setImmediate(resolve)); };
  return {
    byId, calls, toasts, timers, sources, server, settle,
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

test('the review snapshot and preflight are explicit, read-only and never apply', async () => {
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
  assert.match(app.text('native-review-note'), /continua na interface Development/);
  app.byId['native-review-preflight'].dispatch('click');
  await app.settle();
  assert.match(app.text('native-preflight'), /Preflight BLOQUEADO · INTEGRATION_DISABLED/);
  assert.match(app.text('native-history'), /aaaaaaaaaaaa · REVIEWED/);
  assert.equal(app.posts.length, 1);
  assert.ok(nativeMarkup.includes('href="/nexus#development"'), 'direct access to the Development view for Apply/Reject');
  assert.ok(app.calls.every(call => !/\/(apply|reject)/.test(call.url)));
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

test('the module never builds HTML from data and has no apply, reject or eval path', () => {
  assert.doesNotMatch(script, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function/);
  assert.doesNotMatch(script, /\/api\/[^'"`\s]*\/(apply|reject)/i);
  assert.doesNotMatch(script, /localStorage|sessionStorage/);
});
