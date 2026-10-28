const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const { TextNode, el } = require('./fake_dom.cjs');

const root = path.resolve(__dirname, '../..');
const script = fs.readFileSync(path.join(root, 'nexus/web/static/js/cockpit-plan-authoring.js'), 'utf8');
const template = fs.readFileSync(path.join(root, 'nexus/web/templates/cockpit.html'), 'utf8');
const stylesheet = fs.readFileSync(path.join(root, 'nexus/web/static/css/cockpit-plan-authoring.css'), 'utf8');

function buildDom() {
  const ids = {};
  const markup = template.slice(template.indexOf('<section class="plan-authoring"'), template.indexOf('</section>', template.indexOf('<section class="plan-authoring"')));
  for (const match of markup.matchAll(/<([\w-]+)([^>]*?)\sid="([^"]+)"([^>]*)>/g)) {
    const item = el(match[1]);
    item.id = match[3];
    ids[item.id] = item;
  }
  for (const id of ['plan-task-text', 'plan-agent-a-task', 'plan-agent-a-criteria',
    'plan-agent-b-task', 'plan-agent-b-criteria']) ids[id].value = '';
  ids['plan-dependency'].value = '';
  ids['plan-create'].disabled = true;
  return ids;
}

function harness({ storage, holdCreate = null, createFailure = null, networkFailure = false } = {}) {
  const byId = buildDom();
  const document = {
    getElementById: id => byId[id] || null,
    createElement: tag => el(tag),
    createTextNode: value => new TextNode(value),
  };
  const calls = [];
  const listeners = [];
  const local = new Map(Object.entries(storage || {}));
  const localStorage = {
    getItem: key => local.has(key) ? local.get(key) : null,
    setItem: (key, value) => local.set(key, String(value)),
  };
  const server = {
    models: { models: [
      { id: 'gpt-native', selectable: true, cost_status: 'known', supported_efforts: ['high', 'low'] },
      { id: 'revoked', selectable: false, cost_status: 'known', supported_efforts: ['low'] },
    ], catalog_error: null },
    plans: {}, failProject: null,
  };
  const reply = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
  const fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    const body = options.body ? JSON.parse(options.body) : null;
    calls.push({ method, url, body });
    if (method === 'GET' && url === '/api/dev/models?backend=codex_native') return reply(server.models);
    if (method === 'POST' && url === '/api/workspace/plans/manual') {
      if (holdCreate) await holdCreate;
      if (networkFailure) throw new TypeError('Failed to fetch');
      if (createFailure) return reply({ detail: { code: 'DEV_MODE_DISABLED', message: 'Projeto não autorizado.' } }, 403);
      const planId = `WSPLAN-${Object.keys(server.plans).length + 1}`;
      const plan = {
        plan_id: planId, project_id: body.project_id, task_text: body.task_text,
        source: 'manual', status: 'PROPOSED', execution: null, approval: null,
        agents: body.agents, execution_order: body.execution_order,
      };
      server.plans[planId] = plan;
      return reply(plan);
    }
    const planMatch = url.match(/^\/api\/workspace\/plans\/([^/]+)(\/execution)?$/);
    if (method === 'GET' && planMatch) {
      const plan = server.plans[decodeURIComponent(planMatch[1])];
      if (!plan) return reply({ detail: { code: 'PLAN_NOT_FOUND', message: 'Plano ausente.' } }, 404);
      return reply(planMatch[2] ? plan.execution : plan);
    }
    const approveMatch = url.match(/^\/api\/workspace\/plans\/([^/]+)\/approve$/);
    if (method === 'POST' && approveMatch) {
      const plan = server.plans[decodeURIComponent(approveMatch[1])];
      const unavailable = Object.values(body.agent_models).some(modelId =>
        !server.models.models.some(model => model.id === modelId && model.selectable));
      if (unavailable) {
        return reply({ detail: { code: 'MODEL_NOT_ALLOWED', message: 'Modelo revogado.' } }, 403);
      }
      const unsupported = Object.entries(body.agent_models).some(([key, modelId]) =>
        !server.models.models.find(model => model.id === modelId)?.supported_efforts.includes(body.agent_efforts[key]));
      if (unsupported) {
        return reply({ detail: { code: 'EFFORT_NOT_SUPPORTED', message: 'Effort indisponível.' } }, 422);
      }
      plan.status = 'APPROVED';
      plan.approval = { ...body, cost_confirmed_for: ['gpt-native'], agent_models: {
        'agent-a': { model: body.agent_models['agent-a'], backend: 'codex_native', effort: body.agent_efforts['agent-a'], cost_status: 'known' },
        'agent-b': { model: body.agent_models['agent-b'], backend: 'codex_native', effort: body.agent_efforts['agent-b'], cost_status: 'known' },
      } };
      return reply({ plan, panes: [] });
    }
    const executeMatch = url.match(/^\/api\/workspace\/plans\/([^/]+)\/execute$/);
    if (method === 'POST' && executeMatch) {
      const plan = server.plans[decodeURIComponent(executeMatch[1])];
      plan.execution = { status: 'RUNNING', steps: [], ...body };
      return reply(plan.execution);
    }
    throw new Error(`Unexpected API call: ${method} ${url}`);
  };
  const context = {
    document,
    fetch,
    window: {
      localStorage,
      NexusCockpit: {
        projectId: () => null,
        onProject: callback => listeners.push(callback),
      },
    },
  };
  vm.runInNewContext(script, context, { filename: 'cockpit-plan-authoring.js' });
  const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve)); };
  const all = () => Object.values(byId).flatMap(item => [item, ...item.descendants()]);
  return {
    byId, calls, server, local, listeners, settle,
    async selectProject(id) { listeners.forEach(listener => listener(id)); await settle(); },
    findButton(text) { return all().find(item => item.tagName === 'BUTTON' && item.textContent === text); },
    findSelect(label) { return all().find(item => item.tagName === 'SELECT' && item.getAttribute('aria-label') === label); },
    findCheckbox() { return all().find(item => item.tagName === 'INPUT' && item.type === 'checkbox'); },
    text(id) { return byId[id].textContent; },
    get posts() { return calls.filter(call => call.method === 'POST'); },
  };
}

function fillForm(app, dependency = 'after') {
  app.byId['plan-task-text'].value = 'Adicionar validação para a API.';
  app.byId['plan-agent-a-task'].value = 'Implementar a função.';
  app.byId['plan-agent-a-criteria'].value = 'Retorna 2\nTem teste';
  app.byId['plan-agent-b-task'].value = 'Adicionar teste complementar.';
  app.byId['plan-agent-b-criteria'].value = 'Teste cobre a função.';
  app.byId['plan-dependency'].value = dependency;
}

async function createProposal(app, dependency = 'after') {
  await app.selectProject('demo');
  fillForm(app, dependency);
  app.byId['plan-authoring-form'].dispatch('submit');
  await app.settle();
}

function choose(app, label, value) {
  const select = app.findSelect(label);
  assert.ok(select, `missing select: ${label}`);
  select.value = value;
  select.dispatch('change');
}

async function configureAndApprove(app, efforts = ['high', 'low']) {
  choose(app, 'Modelo de Agente A', 'gpt-native');
  choose(app, 'Reasoning effort de Agente A', efforts[0]);
  choose(app, 'Modelo de Agente B', 'gpt-native');
  choose(app, 'Reasoning effort de Agente B', efforts[1]);
  const consent = app.findCheckbox();
  assert.ok(consent, 'paid/unknown model requires explicit cost consent');
  consent.checked = true;
  consent.dispatch('change');
  await app.settle();
  app.findButton('Aprovar configurações').dispatch('click');
  await app.settle();
}

test('manual Native plan UI loads only the Native catalog and creates a PROPOSED dependency', async () => {
  const app = harness();
  await app.settle();
  assert.deepEqual(app.calls, []);
  await createProposal(app);
  assert.deepEqual(app.calls[0], {
    method: 'GET', url: '/api/dev/models?backend=codex_native', body: null,
  });
  const created = app.posts.find(call => call.url === '/api/workspace/plans/manual');
  assert.ok(created);
  assert.equal(created.body.project_id, 'demo');
  assert.deepEqual(created.body.agents[1].depends_on, ['agent-a']);
  assert.deepEqual(created.body.execution_order, ['agent-a', 'agent-b']);
  assert.equal(app.server.plans['WSPLAN-1'].status, 'PROPOSED');
  assert.equal(app.posts.some(call => call.url.endsWith('/approve') || call.url.endsWith('/execute')), false);
  assert.match(app.text('plan-authoring-list'), /PROPOSED/);
  assert.equal(app.findSelect('Modelo de Agente A').children.length, 2);
  assert.equal(app.calls.some(call => call.url === '/api/dev/models'), false);
});

test('independent manual topology remains independent in the persisted proposal', async () => {
  const app = harness();
  await createProposal(app, 'independent');
  const proposal = app.posts.find(call => call.url === '/api/workspace/plans/manual');
  assert.ok(proposal);
  assert.deepEqual(proposal.body.agents.map(agent => agent.depends_on), [[], []]);
  assert.equal(app.posts.some(call => call.url.endsWith('/execute')), false);
});

test('approval is explicit, persists the selected model and distinct efforts, then execution needs a separate click', async () => {
  const app = harness();
  await createProposal(app);
  await configureAndApprove(app);
  assert.ok(app.posts.some(call => call.url.endsWith('/approve')), JSON.stringify(app.calls));
  const approved = app.posts.find(call => call.url.endsWith('/approve'));
  assert.ok(approved);
  assert.equal(approved.body.backend, 'codex_native');
  assert.deepEqual(approved.body.agent_backends, { 'agent-a': 'codex_native', 'agent-b': 'codex_native' });
  assert.deepEqual(approved.body.agent_efforts, { 'agent-a': 'high', 'agent-b': 'low' });
  assert.equal(approved.body.confirm_costs, true);
  assert.equal(app.posts.some(call => call.url.endsWith('/execute')), false);
  assert.equal(app.server.plans['WSPLAN-1'].status, 'APPROVED', JSON.stringify(app.server.plans['WSPLAN-1']));
  app.findButton('Iniciar missão').dispatch('click');
  await app.settle();
  const execution = app.posts.find(call => call.url.endsWith('/execute'));
  assert.ok(execution);
  assert.deepEqual(execution.body.agent_keys, ['agent-a', 'agent-b']);
  assert.deepEqual(execution.body.test_profiles, {});
  assert.equal(execution.body.max_turns_per_agent, 1);
  assert.equal(execution.body.mission_timeout_seconds, 600);
  assert.equal(execution.body.confirm_costs, false);
  assert.ok(app.calls.some(call => call.url.endsWith('/execution')));
  assert.match(app.text('plan-authoring-list'), /Execução persistida: RUNNING/);
});

test('reload rebuilds approved plan and execution from server reads', async () => {
  const first = harness();
  await createProposal(first);
  await configureAndApprove(first);
  first.findButton('Iniciar missão').dispatch('click');
  await first.settle();
  const second = harness({ storage: Object.fromEntries(first.local) });
  second.server.plans = first.server.plans;
  await second.selectProject('demo');
  assert.ok(second.calls.some(call => call.method === 'GET' && call.url === '/api/workspace/plans/WSPLAN-1'));
  assert.ok(second.calls.some(call => call.method === 'GET' && call.url.endsWith('/execution')));
  assert.equal(second.posts.length, 0);
  assert.match(second.text('plan-authoring-list'), /Execução persistida: RUNNING/);
});

test('project switch clears rendered plans and invalid approval never reaches the API', async () => {
  const app = harness();
  await createProposal(app);
  await app.selectProject('other');
  assert.doesNotMatch(app.text('plan-authoring-list'), /WSPLAN-1|Adicionar validação/);
  await app.selectProject('demo');
  choose(app, 'Modelo de Agente A', 'gpt-native');
  choose(app, 'Reasoning effort de Agente A', 'ultra');
  choose(app, 'Modelo de Agente B', 'gpt-native');
  choose(app, 'Reasoning effort de Agente B', 'low');
  app.findButton('Aprovar configurações').dispatch('click');
  await app.settle();
  assert.equal(app.posts.some(call => call.url.endsWith('/approve')), false);
  assert.match(app.text('plan-authoring-error'), /effort disponível/);
});

test('revoked model and newly unsupported effort are rejected by the server and remain PROPOSED', async () => {
  const revoked = harness();
  await createProposal(revoked);
  choose(revoked, 'Modelo de Agente A', 'gpt-native');
  choose(revoked, 'Reasoning effort de Agente A', 'high');
  choose(revoked, 'Modelo de Agente B', 'gpt-native');
  choose(revoked, 'Reasoning effort de Agente B', 'low');
  const consent = revoked.findCheckbox(); consent.checked = true; consent.dispatch('change');
  revoked.server.models.models[0].selectable = false;
  revoked.findButton('Aprovar configurações').dispatch('click');
  await revoked.settle();
  assert.match(revoked.text('plan-authoring-error'), /modelo e um effort disponível/);
  assert.equal(revoked.posts.some(call => call.url.endsWith('/approve')), false);
  assert.equal(revoked.server.plans['WSPLAN-1'].status, 'PROPOSED');

  const effort = harness();
  await createProposal(effort);
  choose(effort, 'Modelo de Agente A', 'gpt-native');
  choose(effort, 'Reasoning effort de Agente A', 'high');
  choose(effort, 'Modelo de Agente B', 'gpt-native');
  choose(effort, 'Reasoning effort de Agente B', 'low');
  const effortConsent = effort.findCheckbox(); effortConsent.checked = true; effortConsent.dispatch('change');
  effort.server.models.models[0].supported_efforts = ['low'];
  effort.findButton('Aprovar configurações').dispatch('click');
  await effort.settle();
  assert.match(effort.text('plan-authoring-error'), /modelo e um effort disponível/);
  assert.equal(effort.posts.some(call => call.url.endsWith('/approve')), false);
  assert.equal(effort.server.plans['WSPLAN-1'].status, 'PROPOSED');
});

test('double submit and authorization failures are surfaced without retry', async () => {
  let release;
  const waiting = new Promise(resolve => { release = resolve; });
  const app = harness({ holdCreate: waiting });
  await app.selectProject('demo');
  fillForm(app);
  app.byId['plan-authoring-form'].dispatch('submit');
  app.byId['plan-authoring-form'].dispatch('submit');
  assert.equal(app.posts.filter(call => call.url === '/api/workspace/plans/manual').length, 1);
  release();
  await app.settle();

  const denied = harness({ createFailure: true });
  await denied.selectProject('demo');
  fillForm(denied);
  denied.byId['plan-authoring-form'].dispatch('submit');
  await denied.settle();
  assert.match(denied.text('plan-authoring-error'), /DEV_MODE_DISABLED: Projeto não autorizado/);
  assert.equal(denied.posts.filter(call => call.url === '/api/workspace/plans/manual').length, 1);

  const network = harness({ networkFailure: true });
  await network.selectProject('demo');
  fillForm(network);
  network.byId['plan-authoring-form'].dispatch('submit');
  await network.settle();
  assert.match(network.text('plan-authoring-error'), /pode ter sido registrada/);
  assert.equal(network.posts.filter(call => call.url === '/api/workspace/plans/manual').length, 1);
});

test('Dark Terminal module is wired once, responsive, and has no automatic Apply path', () => {
  assert.equal((template.match(/cockpit-plan-authoring\.js/g) || []).length, 1);
  assert.match(template, /id="native-plan-authoring"/);
  assert.match(stylesheet, /@media\(max-width:760px\)/);
  assert.doesNotMatch(script, /\/(?:apply|merge|push)\b/);
  assert.doesNotMatch(script, /setInterval|setTimeout/);
});
