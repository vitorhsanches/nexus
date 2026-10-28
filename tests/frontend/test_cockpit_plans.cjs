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
    handoffs: {}, // plan_id -> [handoff record]
    nextHandoffId: 1,
    fail: {}, // "METHOD url-substring" -> {code, message, status}
    ...overrides,
  };
  const reply = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
  const planIdForPane = paneId => server.panes.find(pane => pane.pane_id === paneId)?.plan_id;
  const fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    const body = options.body ? JSON.parse(options.body) : null;
    calls.push({ method, url, body });
    assert.doesNotMatch(url, /\/(apply|reject)\b/, 'the plan monitor must never call Apply or Reject');
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
    const handoffsListMatch = url.match(/^\/api\/workspace\/plans\/([^/]+)\/handoffs$/);
    if (method === 'GET' && handoffsListMatch) return reply({ handoffs: server.handoffs[handoffsListMatch[1]] || [] });
    if (method === 'POST' && url === '/api/workspace/handoffs/preview') {
      const planId = planIdForPane(body.source_pane_id);
      const handoff = {
        handoff_id: `HANDOFF-${server.nextHandoffId++}`, plan_id: planId,
        source_pane_id: body.source_pane_id, target_pane_id: body.target_pane_id,
        source_backend: 'codex_native', source_native_review_id: body.source_mission_id, source_mission_id: null,
        snapshot_fingerprint: 'f'.repeat(64), status: 'PREVIEWED', error_code: null, error_message: null,
        files: [{ path: 'api.py', status: 'modified', sha256: 'a'.repeat(64), size: 10 }],
        created_at: 't-preview', approved_at: null, started_at: null, finished_at: null, result: null,
      };
      server.handoffs[planId] = [handoff, ...(server.handoffs[planId] || [])];
      return reply(handoff);
    }
    const approveMatch = url.match(/^\/api\/workspace\/handoffs\/([^/]+)\/approve$/);
    if (method === 'POST' && approveMatch) {
      const handoff = Object.values(server.handoffs).flat().find(item => item.handoff_id === approveMatch[1]);
      if (body.fingerprint !== handoff.snapshot_fingerprint) {
        return reply({ detail: { code: 'HANDOFF_SNAPSHOT_CHANGED', message: 'Snapshot changed; create and approve a new preview.' } }, 409);
      }
      handoff.status = 'APPROVED'; handoff.approved_at = 't-approve';
      return reply(handoff);
    }
    const transferMatch = url.match(/^\/api\/workspace\/handoffs\/([^/]+)\/transfer$/);
    if (method === 'POST' && transferMatch) {
      const handoff = Object.values(server.handoffs).flat().find(item => item.handoff_id === transferMatch[1]);
      handoff.status = 'TRANSFERRED'; handoff.started_at = 't-transfer'; handoff.finished_at = 't-transfer';
      handoff.result = { files: handoff.files, verified: true, integration_applied: false };
      const planId = handoff.plan_id;
      const execution = server.executions[planId];
      if (execution) execution.resume = { eligible: true, pending_agent: execution.steps[execution.current_index]?.agent_key };
      return reply(handoff);
    }
    const resumeMatch = url.match(/^\/api\/workspace\/plans\/([^/]+)\/resume$/);
    if (method === 'POST' && resumeMatch) {
      const execution = server.executions[resumeMatch[1]];
      if (!execution?.resume?.eligible) {
        return reply({ detail: { code: 'RESUME_NOT_ALLOWED', message: 'Only an unresolved handoff block can be resumed.' } }, 409);
      }
      execution.status = 'RUNNING'; execution.error_code = null; execution.error_message = null;
      execution.steps[execution.current_index].status = 'STARTING';
      execution.resume = { eligible: false };
      return reply(execution);
    }
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
    async click(id) { byId[id].dispatch('click'); await settle(); },
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
const EVIDENCE_REVIEW_ID = 'r'.repeat(64);
const blockedExecution = (overrides = {}) => ({
  plan_id: 'WSPLAN-1', project_id: 'demo', status: 'BLOCKED', current_index: 1,
  error_code: 'DEPENDENCY_WORKTREE_NOT_TRANSFERRED', error_message: 'The exact source evidence has no completed handoff.',
  result: null, resume: { eligible: false },
  steps: [
    { agent_key: 'api', ordinal: 0, pane_id: 'PANE-API', status: 'READY_FOR_REVIEW', mission_id: null, native_turn_id: 'TURN-1', native_review_id: EVIDENCE_REVIEW_ID, error_code: null, error_message: null },
    { agent_key: 'tests', ordinal: 1, pane_id: 'PANE-TESTS', status: 'BLOCKED', mission_id: null, native_turn_id: null, native_review_id: null, error_code: 'DEPENDENCY_WORKTREE_NOT_TRANSFERRED', error_message: 'not transferred' },
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

test('a handoff pending block is shown as read-only until the operator acts, with no automatic preview/approve/transfer/resume', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': blockedExecution() };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions });
  await app.selectProject();
  assert.match(app.text('review-handoff-note'), /Handoff pendente/);
  assert.equal(app.byId['review-handoff'].hidden, false);
  assert.equal(app.byId['review-handoff-preview'].hidden, false);
  assert.equal(app.byId['review-handoff-approve'].hidden, true);
  assert.equal(app.byId['review-handoff-transfer'].hidden, true);
  assert.equal(app.byId['review-resume'].hidden, true); // not eligible before a transfer
  assert.ok(!app.posts.some(call => /handoffs\/preview|\/approve$|\/transfer$|\/resume$/.test(call.url)),
    'nothing is posted automatically from polling or rendering alone');
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

// -- P5.UIC2: handoff preview / approve / transfer, and resume --------------

test('preview is called only on explicit click and shows origin, target, review, files and fingerprint', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': blockedExecution() };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions });
  await app.selectProject();
  assert.equal(app.posts.some(call => call.url === '/api/workspace/handoffs/preview'), false);
  await app.click('review-handoff-preview');
  const post = app.posts.find(call => call.url === '/api/workspace/handoffs/preview');
  assert.ok(post, 'preview was called after the explicit click');
  assert.deepEqual(post.body, { source_pane_id: 'PANE-API', target_pane_id: 'PANE-TESTS', source_mission_id: EVIDENCE_REVIEW_ID });
  assert.equal(app.byId['review-handoff-source'].textContent, 'api');
  assert.equal(app.byId['review-handoff-target'].textContent, 'tests');
  const snapshot = app.text('review-handoff-snapshot');
  assert.match(snapshot, new RegExp(EVIDENCE_REVIEW_ID.slice(0, 12)));
  assert.match(snapshot, /modified api\.py/);
  assert.match(snapshot, /f{64}/);
  assert.match(snapshot, /PREVIEWED/);
  assert.equal(app.byId['review-handoff-preview'].hidden, true);
  assert.equal(app.byId['review-handoff-approve'].hidden, false);
});

test('a rejected preview shows the real server error and offers no approve control', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': blockedExecution() };
  const app = harness({
    plans, panes: panesFor('WSPLAN-1'), executions,
    fail: { 'POST /api/workspace/handoffs/preview': { code: 'HANDOFF_TARGET_DIRTY', message: 'The target worktree already contains changes.' } },
  });
  await app.selectProject();
  await app.click('review-handoff-preview');
  assert.match(app.text('review-handoff-error'), /already contains changes/);
  assert.equal(app.byId['review-handoff-error'].classList.contains('is-error'), true);
  assert.equal(app.byId['review-handoff-approve'].hidden, true);
  assert.equal(app.byId['review-handoff-preview'].hidden, false); // still offered: nothing was created
});

test('approval sends exactly the fingerprint and panes the preview returned', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': blockedExecution() };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions });
  await app.selectProject();
  await app.click('review-handoff-preview');
  await app.click('review-handoff-approve');
  const post = app.posts.find(call => call.url.endsWith('/approve'));
  assert.ok(post);
  assert.deepEqual(post.body, { fingerprint: 'f'.repeat(64), source_pane_id: 'PANE-API', target_pane_id: 'PANE-TESTS' });
  assert.match(app.text('review-handoff-snapshot'), /APPROVED/);
  assert.equal(app.byId['review-handoff-approve'].hidden, true);
  assert.equal(app.byId['review-handoff-transfer'].hidden, false);
});

test('a diverged fingerprint at approval time is shown as the real server error, never retried silently', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': blockedExecution() };
  const app = harness({
    plans, panes: panesFor('WSPLAN-1'), executions,
    // Simulates the source drifting between preview and approval: the
    // server itself is the one that recomputes and rejects the mismatch
    // (handoff.approve() re-verifies against the live worktree) -- this
    // pane only has to show that real error, never recompute anything.
    fail: { 'POST /api/workspace/handoffs/HANDOFF-1/approve': { code: 'HANDOFF_SNAPSHOT_CHANGED', message: 'Snapshot changed; create and approve a new preview.' } },
  });
  await app.selectProject();
  await app.click('review-handoff-preview');
  await app.click('review-handoff-approve');
  assert.match(app.text('review-handoff-error'), /Snapshot changed/);
  assert.equal(app.byId['review-handoff-transfer'].hidden, true);
});

test('transfer is a separate explicit click, never chained automatically after approve', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': blockedExecution() };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions });
  await app.selectProject();
  await app.click('review-handoff-preview');
  await app.click('review-handoff-approve');
  assert.equal(app.posts.some(call => call.url.endsWith('/transfer')), false, 'approve alone must never transfer');
  await app.click('review-handoff-transfer');
  const post = app.posts.find(call => call.url.endsWith('/transfer'));
  assert.ok(post);
  assert.equal(post.body, null, 'transfer takes no body, no file-copy logic of its own');
  assert.match(app.text('review-handoff-snapshot'), /TRANSFERRED/);
  assert.equal(app.byId['review-handoff-transfer'].hidden, true);
  assert.equal(app.byId['review-handoff-approve'].hidden, true);
  assert.equal(app.byId['review-handoff-preview'].hidden, true);
});

test('resume only becomes available after a transferred handoff and needs two explicit clicks', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': blockedExecution() };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions });
  await app.selectProject();
  assert.equal(app.byId['review-resume'].hidden, true);
  await app.click('review-handoff-preview');
  await app.click('review-handoff-approve');
  await app.click('review-handoff-transfer');
  assert.equal(app.byId['review-resume'].hidden, false);
  assert.equal(app.byId['review-resume-button'].textContent, 'Retomar execução');
  assert.equal(app.posts.some(call => call.url.endsWith('/resume')), false);
  await app.click('review-resume-button'); // arm
  assert.equal(app.byId['review-resume-button'].textContent, 'Confirmar retomada');
  assert.equal(app.posts.some(call => call.url.endsWith('/resume')), false, 'the first click only confirms, never posts');
  await app.click('review-resume-button'); // confirm
  const post = app.posts.find(call => call.url.endsWith('/resume'));
  assert.ok(post);
  assert.equal(post.body, null, 'the frontend never launches a turn itself, only asks the coordinator to resume');
  assert.ok(app.toasts.some(message => /retomada/.test(message)));
  assert.match(app.text('review-steps'), /Iniciando/); // the second agent's step moved to STARTING
});

test('resume cancel disarms the confirmation without posting anything', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': blockedExecution() };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions });
  await app.selectProject();
  await app.click('review-handoff-preview');
  await app.click('review-handoff-approve');
  await app.click('review-handoff-transfer');
  await app.click('review-resume-button'); // arm
  await app.click('review-resume-cancel');
  assert.equal(app.byId['review-resume-button'].textContent, 'Retomar execução');
  assert.equal(app.posts.some(call => call.url.endsWith('/resume')), false);
});

test('a backend-rejected resume shows the real error and keeps the UI conservative', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': blockedExecution() };
  const app = harness({
    plans, panes: panesFor('WSPLAN-1'), executions,
    fail: { 'POST /api/workspace/plans/WSPLAN-1/resume': { code: 'RESUME_CONCURRENT', message: 'The plan, approval or handoff changed before the resume claim.' } },
  });
  await app.selectProject();
  await app.click('review-handoff-preview');
  await app.click('review-handoff-approve');
  await app.click('review-handoff-transfer');
  await app.click('review-resume-button');
  await app.click('review-resume-button');
  assert.match(app.text('review-resume-error'), /resume claim/);
  assert.equal(app.byId['review-resume-button'].textContent, 'Retomar execução'); // disarmed, not stuck armed
});

test('resume stays unavailable while the backend reports it ineligible, even if a handoff looks transferred', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': blockedExecution({ resume: { eligible: false } }) };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions });
  await app.selectProject();
  app.server.handoffs['WSPLAN-1'] = [{
    handoff_id: 'HANDOFF-OLD', plan_id: 'WSPLAN-1', source_pane_id: 'PANE-API', target_pane_id: 'PANE-TESTS',
    source_backend: 'codex_native', source_native_review_id: EVIDENCE_REVIEW_ID, source_mission_id: null,
    snapshot_fingerprint: 'f'.repeat(64), status: 'TRANSFERRED', error_code: null, error_message: null,
    files: [{ path: 'api.py', status: 'modified', sha256: 'a'.repeat(64), size: 10 }],
    created_at: 't0', approved_at: 't0', started_at: 't0', finished_at: 't0', result: { files: [], verified: true, integration_applied: false },
  }];
  await app.selectPlan('WSPLAN-1'); // force a fresh load that picks up the seeded handoff
  assert.equal(app.byId['review-resume'].hidden, true, 'the backend\'s own eligibility flag is authoritative, not the handoff status alone');
});

test('a stale/obsolete handoff (evidence no longer matches) is never offered for approval -- a fresh preview is required', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': blockedExecution() };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions });
  await app.selectProject();
  app.server.handoffs['WSPLAN-1'] = [{
    handoff_id: 'HANDOFF-STALE', plan_id: 'WSPLAN-1', source_pane_id: 'PANE-API', target_pane_id: 'PANE-TESTS',
    source_backend: 'codex_native', source_native_review_id: 'z'.repeat(64), source_mission_id: null,
    snapshot_fingerprint: 'e'.repeat(64), status: 'PREVIEWED', error_code: null, error_message: null,
    files: [], created_at: 't-1', approved_at: null, started_at: null, finished_at: null, result: null,
  }];
  await app.selectPlan('WSPLAN-1');
  assert.equal(app.byId['review-handoff-preview'].hidden, false, 'a handoff for a different (superseded) review must not be reused');
  assert.equal(app.byId['review-handoff-approve'].hidden, true);
});

test('a double click on preview sends exactly one request', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': blockedExecution() };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions });
  await app.selectProject();
  app.byId['review-handoff-preview'].dispatch('click');
  app.byId['review-handoff-preview'].dispatch('click');
  await app.settle();
  assert.equal(app.posts.filter(call => call.url === '/api/workspace/handoffs/preview').length, 1);
});

test('a network failure during transfer is shown without crashing and the handoff stays APPROVED, not silently advanced', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': blockedExecution() };
  const app = harness({
    plans, panes: panesFor('WSPLAN-1'), executions,
    fail: { 'POST /api/workspace/handoffs/HANDOFF-1/transfer': { code: 'SERVER_ERROR', message: 'Nexus está indisponível' } },
  });
  await app.selectProject();
  await app.click('review-handoff-preview');
  await app.click('review-handoff-approve');
  await app.click('review-handoff-transfer');
  assert.match(app.text('review-handoff-error'), /indisponível/);
  assert.equal(app.byId['review-resume'].hidden, true);
  assert.equal(app.byId['review-handoff-transfer'].hidden, false);
});

test('reload between checkpoints rebuilds the exact handoff phase from the server alone', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': blockedExecution() };
  const seedHandoffs = { 'WSPLAN-1': [{
    handoff_id: 'HANDOFF-SEED', plan_id: 'WSPLAN-1', source_pane_id: 'PANE-API', target_pane_id: 'PANE-TESTS',
    source_backend: 'codex_native', source_native_review_id: EVIDENCE_REVIEW_ID, source_mission_id: null,
    snapshot_fingerprint: 'f'.repeat(64), status: 'APPROVED', error_code: null, error_message: null,
    files: [{ path: 'api.py', status: 'modified', sha256: 'a'.repeat(64), size: 10 }],
    created_at: 't0', approved_at: 't0', started_at: null, finished_at: null, result: null,
  }] };
  // A brand new harness is the reload: no client memory survives it.
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions, handoffs: seedHandoffs });
  await app.selectProject();
  assert.equal(app.byId['review-handoff-preview'].hidden, true);
  assert.equal(app.byId['review-handoff-approve'].hidden, true);
  assert.equal(app.byId['review-handoff-transfer'].hidden, false, 'the already-approved handoff is picked up straight from the server');
});

test('switching projects mid-operation discards the in-flight handoff/resume state', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': blockedExecution() };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions, project: 'demo' });
  await app.selectProject('demo');
  await app.click('review-handoff-preview');
  app.server.project = 'other';
  app.server.plans = [];
  app.server.panes = [];
  await app.selectProject('other');
  assert.equal(app.byId['review-handoff'].hidden, true);
  assert.equal(app.byId['review-resume'].hidden, true);
});

test('polling the execution alone never previews, approves, transfers or resumes anything', async () => {
  const plans = [plan()];
  const executions = { 'WSPLAN-1': blockedExecution() };
  const app = harness({ plans, panes: panesFor('WSPLAN-1'), executions });
  await app.selectProject();
  await app.click('review-handoff-preview');
  const before = app.posts.length;
  await app.tick(3000); // one execution poll tick
  const after = app.posts.slice(before).filter(call => /handoffs\/preview|\/approve$|\/transfer$|\/resume$/.test(call.url));
  assert.equal(after.length, 0);
  assert.equal(app.posts.length, before, 'the poll tick itself issues no POST at all');
});

test('a legacy OmniRoute dependency block can also be prepared, approved, transferred and resumed through the same controls', async () => {
  const plans = [plan({ agents: [
    { key: 'api', role: 'backend', title: 'API', model: 'free/model', depends_on: [] },
    { key: 'tests', role: 'tester', title: 'Tests', model: 'free/model', depends_on: ['api'] },
  ] })];
  const executions = { 'WSPLAN-1': blockedExecution({ steps: [
    { agent_key: 'api', ordinal: 0, pane_id: 'PANE-API', status: 'READY_FOR_REVIEW', mission_id: 'DEVMISSION-1', native_turn_id: null, native_review_id: null, error_code: null, error_message: null },
    { agent_key: 'tests', ordinal: 1, pane_id: 'PANE-TESTS', status: 'BLOCKED', mission_id: null, native_turn_id: null, native_review_id: null, error_code: 'DEPENDENCY_WORKTREE_NOT_TRANSFERRED', error_message: 'not transferred' },
  ] }) };
  const app = harness({ plans, panes: panesFor('WSPLAN-1', { api: 'omniroute', tests: 'omniroute' }), executions });
  await app.selectProject();
  await app.click('review-handoff-preview');
  const post = app.posts.find(call => call.url === '/api/workspace/handoffs/preview');
  assert.deepEqual(post.body, { source_pane_id: 'PANE-API', target_pane_id: 'PANE-TESTS', source_mission_id: 'DEVMISSION-1' });
  await app.click('review-handoff-approve');
  await app.click('review-handoff-transfer');
  assert.equal(app.byId['review-resume'].hidden, false);
});
