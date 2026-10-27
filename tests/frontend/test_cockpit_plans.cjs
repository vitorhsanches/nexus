const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const { TextNode, el } = require('./fake_dom.cjs');

const root = path.resolve(__dirname, '../..');
const script = fs.readFileSync(path.join(root, 'nexus/web/static/js/cockpit-plans.js'), 'utf8');
const template = fs.readFileSync(path.join(root, 'nexus/web/templates/cockpit.html'), 'utf8');
const reviewMarkup = template.slice(template.indexOf('<article class="pane" data-pane="review"'), template.indexOf('</article>', template.indexOf('<article class="pane" data-pane="review"')));

function buildDom() {
  const byId = {};
  for (const match of reviewMarkup.matchAll(/<(\w+)([^>]*?)\sid="(review-[^"]+)"([^>]*)>/g)) {
    const node = el(match[1]);
    node.id = match[3];
    const attributes = `${match[2]} ${match[4]}`;
    node.hidden = /\shidden(\s|$)/.test(attributes);
    node.disabled = /\sdisabled(\s|$)/.test(attributes);
    byId[node.id] = node;
  }
  byId['review-status-badge'].append(el('i'), new TextNode(' Aguardando'));
  return byId;
}

function harness(overrides = {}) {
  const byId = buildDom();
  const document = {
    getElementById: id => byId[id] || null,
    createElement: tag => el(tag), createTextNode: value => new TextNode(value),
    querySelector: selector => (selector === '[data-pane="review"]' ? el('article') : null),
  };
  const calls = [];
  const toasts = [];
  const timers = [];
  const listeners = [];
  const server = {
    project: 'demo',
    plans: [],
    panes: [],
    executions: {}, // plan_id -> execution object (coordinator.state shape)
    reviews: {}, // session_id -> integrations list
    fail: {}, // "METHOD url-substring" -> {code, message}
    ...overrides,
  };
  const reply = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
  const fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    const body = options.body ? JSON.parse(options.body) : null;
    calls.push({ method, url, body });
    assert.doesNotMatch(url, /\/(apply|reject|transfer|resume|handoffs\/preview|handoffs\/[^/]+\/approve)\b/,
      'the plan monitor must never call Apply/Reject, transfer, resume or create/approve a handoff');
    assert.ok(!(method === 'POST' && url.endsWith('/reviews')), 'the plan monitor must never create a review itself');
    const failure = Object.entries(server.fail).find(([pattern]) => `${method} ${url}`.includes(pattern));
    if (failure) {
      const [, spec] = failure;
      return reply({ detail: { code: spec.code, message: spec.message } }, spec.status || 409);
    }
    if (method === 'GET' && url.startsWith('/api/workspace/plans?')) return reply({ plans: server.plans });
    if (method === 'GET' && url.startsWith('/api/workspace/state?')) return reply({ panes: server.panes, coordination: [], execution_enabled: true });
    const executionMatch = url.match(/^\/api\/workspace\/plans\/([^/]+)\/execution$/);
    if (method === 'GET' && executionMatch) {
      const execution = server.executions[executionMatch[1]];
      if (!execution) return reply({ detail: { code: 'EXECUTION_NOT_AUTHORIZED', message: 'This plan has no execution authorization.' } }, 409);
      return reply(execution);
    }
    const bindMatch = url.match(/^\/api\/workspace\/plans\/([^/]+)\/steps\/([^/]+)\/review$/);
    if (method === 'POST' && bindMatch) {
      const [, planId, agentKey] = bindMatch;
      const execution = server.executions[planId];
      const step = execution.steps.find(item => item.agent_key === agentKey);
      step.status = 'READY_FOR_REVIEW';
      step.native_review_id = body.review_id;
      return reply(execution);
    }
    const integrationsMatch = url.match(/^\/api\/dev\/sessions\/([^/]+)\/integrations$/);
    if (method === 'GET' && integrationsMatch) return reply({ integrations: server.reviews[integrationsMatch[1]] || [] });
    throw new Error(`Unexpected API call: ${method} ${url}`);
  };
  const context = {
    document, fetch,
    window: { NexusCockpit: { projectId: () => server.project, notify: message => toasts.push(message), onProject: callback => listeners.push(callback) } },
    setTimeout(callback, delay) { const timer = { callback, delay }; timers.push(timer); return timer; },
    clearTimeout(timer) { const index = timers.indexOf(timer); if (index >= 0) timers.splice(index, 1); },
  };
  vm.runInNewContext(script, context, { filename: 'cockpit-plans.js' });
  const settle = async () => { for (let count = 0; count < 12; count++) await new Promise(resolve => setImmediate(resolve)); };
  return {
    byId, calls, toasts, timers, server, settle,
    get posts() { return calls.filter(call => call.method === 'POST'); },
    async selectProject(id = 'demo') { listeners.forEach(callback => callback(id)); await settle(); },
    async selectPlan(id) { byId['review-plan-select'].value = id; byId['review-plan-select'].dispatch('change'); await settle(); },
    async selectReview(id) { byId['review-bind-select'].value = id; byId['review-bind-select'].dispatch('change'); await settle(); },
    async tick(delay) {
      const index = timers.findIndex(timer => timer.delay === delay);
      assert.notEqual(index, -1, `no timer with delay ${delay}`);
      timers.splice(index, 1)[0].callback();
      await settle();
    },
    text: id => byId[id].textContent,
  };
}

const AGENTS = [
  { key: 'api', role: 'backend', title: 'API', model: 'free/model', depends_on: [] },
  { key: 'tests', role: 'tester', title: 'Tests', model: 'free/model', depends_on: ['api'] },
];
const plan = (overrides = {}) => ({
  plan_id: 'WSPLAN-1', project_id: 'demo', task_text: 'duas agentes nativos', status: 'APPROVED',
  agents: AGENTS, execution: null, ...overrides,
});
const panesFor = (planId, backends = { api: 'codex_native', tests: 'codex_native' }) => [
  { pane_id: 'PANE-API', plan_id: planId, plan_agent_key: 'api', backend: backends.api, model: 'free/model',
    session: { session_id: 'DEV-API', status: 'ACTIVE' } },
  { pane_id: 'PANE-TESTS', plan_id: planId, plan_agent_key: 'tests', backend: backends.tests, model: 'free/model',
    session: { session_id: 'DEV-TESTS', status: 'ACTIVE' } },
];
const execution = (overrides = {}) => ({
  plan_id: 'WSPLAN-1', project_id: 'demo', status: 'RUNNING', current_index: 0, error_code: null, error_message: null, result: null,
  steps: [
    { agent_key: 'api', ordinal: 0, pane_id: 'PANE-API', status: 'RUNNING', mission_id: null, native_turn_id: 'TURN-1', native_review_id: null, error_code: null, error_message: null },
    { agent_key: 'tests', ordinal: 1, pane_id: 'PANE-TESTS', status: 'PENDING', mission_id: null, native_turn_id: null, native_review_id: null, error_code: null, error_message: null },
  ],
  ...overrides,
});

// -- no project / empty states ----------------------------------------------

test('without a project nothing is requested and the picker is disabled', async () => {
  const app = harness();
  await app.settle();
  assert.equal(app.calls.length, 0);
  assert.equal(app.byId['review-plan-select'].disabled, true);
  assert.match(app.text('review-plan-note'), /Selecione um projeto/);
});

test('a project with no plans shows an informational empty state, not an error', async () => {
  const app = harness();
  await app.selectProject();
  assert.match(app.text('review-plan-note'), /Nenhum plano/);
  assert.equal(app.text('review-steps'), 'Nenhum plano selecionado.');
});

// -- two-agent plan: initial and running states -----------------------------

test('a two-agent plan renders role, backend, model and status per step', async () => {
  const plans = [plan({ execution: null })];
  const executions = { 'WSPLAN-1': execution() };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions });
  await app.selectProject();
  const steps = app.text('review-steps');
  assert.match(steps, /API · backend/);
  assert.match(steps, /Backend codex_native/);
  assert.match(steps, /Modelo free\/model/);
  assert.match(steps, /Executando/); // RUNNING
  assert.match(steps, /Pendente/); // PENDING
  assert.match(steps, /turno ativo/);
  assert.match(steps, /depende de api/);
  assert.equal(app.text('review-meta-status'), 'Executando');
  assert.equal(app.text('review-meta-step'), '1/2');
});

test('polling refreshes the selected plan\'s execution on the existing cadence', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': execution() };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions });
  await app.selectProject();
  const before = app.calls.filter(call => call.url.endsWith('/execution')).length;
  executions['WSPLAN-1'] = execution({ steps: [
    { ...execution().steps[0], status: 'AWAITING_NATIVE_REVIEW' }, execution().steps[1],
  ] });
  await app.tick(3000);
  const after = app.calls.filter(call => call.url.endsWith('/execution')).length;
  assert.equal(after, before + 1);
  assert.match(app.text('review-steps'), /Aguardando revisão/);
});

// -- AWAITING_NATIVE_REVIEW and binding --------------------------------------

test('AWAITING_NATIVE_REVIEW shows the bind form populated from existing reviews', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': execution({ steps: [
    { agent_key: 'api', ordinal: 0, pane_id: 'PANE-API', status: 'AWAITING_NATIVE_REVIEW', mission_id: null, native_turn_id: 'TURN-1', native_review_id: null, error_code: null, error_message: null },
    { agent_key: 'tests', ordinal: 1, pane_id: 'PANE-TESTS', status: 'PENDING', mission_id: null, native_turn_id: null, native_review_id: null, error_code: null, error_message: null },
  ] }) };
  const reviews = { 'DEV-API': [{ review_id: 'a'.repeat(64), status: 'REVIEWED', created_at: 't1' }] };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions, reviews });
  await app.selectProject();
  assert.equal(app.byId['review-bind'].hidden, false);
  assert.equal(app.text('review-bind-agent'), 'api');
  assert.ok(app.calls.some(call => call.url === '/api/dev/sessions/DEV-API/integrations'));
  assert.equal(app.byId['review-bind-submit'].disabled, true); // nothing selected yet
});

test('binding a review posts the exact review id and never auto-advances further than the server says', async () => {
  const plans = [plan()];
  const stepAwaiting = () => [
    { agent_key: 'api', ordinal: 0, pane_id: 'PANE-API', status: 'AWAITING_NATIVE_REVIEW', mission_id: null, native_turn_id: 'TURN-1', native_review_id: null, error_code: null, error_message: null },
    { agent_key: 'tests', ordinal: 1, pane_id: 'PANE-TESTS', status: 'PENDING', mission_id: null, native_turn_id: null, native_review_id: null, error_code: null, error_message: null },
  ];
  const executions = { 'WSPLAN-1': execution({ steps: stepAwaiting() }) };
  const reviewId = 'b'.repeat(64);
  const reviews = { 'DEV-API': [{ review_id: reviewId, status: 'REVIEWED', created_at: 't1' }] };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions, reviews });
  await app.selectProject();
  await app.selectReview(reviewId);
  assert.equal(app.byId['review-bind-submit'].disabled, false);
  app.byId['review-bind-submit'].dispatch('click');
  await app.settle();
  const post = app.posts.find(call => call.url === '/api/workspace/plans/WSPLAN-1/steps/api/review');
  assert.ok(post, 'the exact bind route was called');
  assert.deepEqual(post.body, { review_id: reviewId });
  assert.match(app.text('review-steps'), /Pronto/); // READY_FOR_REVIEW, from the server's own response
  assert.ok(app.toasts.some(message => /Revisão vinculada/.test(message)));
  assert.match(app.text('review-bind-note'), /vinculada ao passo api/);
});

test('an invalid review surfaces the exact server error and binds nothing', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': execution({ steps: [
    { agent_key: 'api', ordinal: 0, pane_id: 'PANE-API', status: 'AWAITING_NATIVE_REVIEW', mission_id: null, native_turn_id: 'TURN-1', native_review_id: null, error_code: null, error_message: null },
    { agent_key: 'tests', ordinal: 1, pane_id: 'PANE-TESTS', status: 'PENDING', mission_id: null, native_turn_id: null, native_review_id: null, error_code: null, error_message: null },
  ] }) };
  const reviewId = 'c'.repeat(64);
  const reviews = { 'DEV-API': [{ review_id: reviewId, status: 'REJECTED', created_at: 't1' }] };
  const app = harness({
    plans, panes: panesFor('WSPLAN-1'), executions, reviews,
    fail: { 'POST /api/workspace/plans/WSPLAN-1/steps/api/review': { code: 'NATIVE_REVIEW_NOT_READY', message: 'Review is REJECTED; it cannot be bound.' } },
  });
  await app.selectProject();
  await app.selectReview(reviewId);
  app.byId['review-bind-submit'].dispatch('click');
  await app.settle();
  assert.match(app.text('review-bind-error'), /Review is REJECTED/);
  assert.equal(app.byId['review-bind-error'].classList.contains('is-error'), true);
  // The step must still show AWAITING_NATIVE_REVIEW -- nothing was bound.
  assert.match(app.text('review-steps'), /Aguardando revisão/);
});

// -- handoff pending, failures, uncertain results ----------------------------

test('a handoff pending block is shown as read-only, with no transfer or resume control anywhere', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': execution({
    status: 'BLOCKED', error_code: 'DEPENDENCY_WORKTREE_NOT_TRANSFERRED', error_message: 'not transferred',
  }) };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions });
  await app.selectProject();
  assert.match(app.text('review-handoff-note'), /Handoff pendente/);
  assert.doesNotMatch(reviewMarkup, /transfer|resume/i);
});

test('a failed plan is shown as an error, never retried automatically, and polling stops', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': execution({ status: 'FAILED', error_code: 'TURN_FAILED', error_message: 'boom' }) };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions });
  await app.selectProject();
  assert.match(app.text('review-handoff-note'), /Falhou/);
  assert.equal(app.byId['review-handoff-note'].classList.contains('is-error'), true);
  assert.equal(app.timers.length, 0, 'a terminal plan must not keep polling');
});

test('a result-uncertain plan is distinguished from a plain failure', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': execution({ status: 'RESULT_UNCERTAIN', error_code: 'COORDINATOR_INTERRUPTED', error_message: 'crash' }) };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions });
  await app.selectProject();
  assert.match(app.text('review-handoff-note'), /Resultado incerto/);
});

test('a completed plan shows the final banner and explicitly states integration is not automatic', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': execution({ status: 'AWAITING_REVIEW', current_index: 2, result: { human_approval_required: true, integration_applied: false } }) };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions });
  await app.selectProject();
  assert.match(app.text('review-final'), /nunca é automática/);
  assert.equal(app.timers.length, 0, 'a completed plan must not keep polling');
});

// -- project switching, reload, network failure ------------------------------

test('switching projects discards the previous project\'s plan, execution and polling', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': execution() };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions, project: 'demo' });
  await app.selectProject('demo');
  assert.equal(app.timers.length, 1);
  app.server.project = 'other';
  app.server.plans = [];
  app.server.panes = [];
  await app.selectProject('other');
  assert.equal(app.byId['review-plan-select'].disabled, true);
  assert.match(app.text('review-plan-note'), /Nenhum plano/);
  assert.equal(app.timers.length, 0, 'no stale poll for the previous project should remain armed');
});

test('a reload rebuilds every state exclusively from fresh API responses, never from client memory', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': execution({ status: 'BLOCKED', error_code: 'AWAITING_NATIVE_REVIEW' }) };
  const first = harness({ plans, panes: panesFor('WSPLAN-1'), executions });
  await first.selectProject();
  // A fresh harness is the test-level equivalent of a full page reload: a
  // brand new script instance with no shared state, driven only by the API.
  const second = harness({ plans, panes: panesFor('WSPLAN-1'), executions });
  await second.selectProject();
  assert.equal(second.text('review-meta-status'), first.text('review-meta-status'));
  assert.equal(second.text('review-steps'), first.text('review-steps'));
  assert.doesNotMatch(script, /localStorage|sessionStorage/);
});

test('a network failure is shown without crashing and without hiding the picker', async () => {
  const plans = [plan()];
  const app = harness({
    plans, panes: panesFor('WSPLAN-1'),
    fail: { 'GET /api/workspace/plans/WSPLAN-1/execution': { code: 'SERVER_ERROR', message: 'Nexus está indisponível' } },
  });
  await app.selectProject();
  assert.match(app.text('review-steps'), /Falha ao consultar a execução: Nexus está indisponível/);
  assert.equal(app.byId['review-plan-select'].disabled, false);
});

// -- legacy OmniRoute-only plans ----------------------------------------------

test('a legacy OmniRoute-only plan renders normally and never shows the native bind form', async () => {
  const plans = [plan({ agents: [
    { key: 'api', role: 'backend', title: 'API', model: 'free/model', depends_on: [] },
    { key: 'tests', role: 'tester', title: 'Tests', model: 'free/model', depends_on: ['api'] },
  ] })];
  const executions = { 'WSPLAN-1': execution({ steps: [
    { agent_key: 'api', ordinal: 0, pane_id: 'PANE-API', status: 'READY_FOR_REVIEW', mission_id: 'DEVMISSION-1', native_turn_id: null, native_review_id: null, error_code: null, error_message: null },
    { agent_key: 'tests', ordinal: 1, pane_id: 'PANE-TESTS', status: 'RUNNING', mission_id: 'DEVMISSION-2', native_turn_id: null, native_review_id: null, error_code: null, error_message: null },
  ] }) };
  const app = harness({ plans, panes: panesFor('WSPLAN-1', { api: 'omniroute', tests: 'omniroute' }), executions });
  await app.selectProject();
  assert.match(app.text('review-steps'), /Backend omniroute/);
  assert.equal(app.byId['review-bind'].hidden, true);
  assert.ok(!app.calls.some(call => call.url.includes('/integrations')));
});
