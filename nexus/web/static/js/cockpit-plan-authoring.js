// Manual two-agent Codex Native plan authoring for the Dark Terminal cockpit.
// Planning, approval and execution remain separate server-authorized actions.
(() => {
  const host = window.NexusCockpit;
  const root = document.getElementById('native-plan-authoring');
  if (!host || !root) return;

  const $ = id => document.getElementById(id);
  const STORAGE_KEY = 'nexus.cockpit.native-plan-ids.v1';
  const AGENTS = [
    { key: 'agent-a', title: 'Agente A', role: 'backend', task: 'plan-agent-a-task', criteria: 'plan-agent-a-criteria' },
    { key: 'agent-b', title: 'Agente B', role: 'tester', task: 'plan-agent-b-task', criteria: 'plan-agent-b-criteria' },
  ];
  const state = {
    projectId: null, generation: 0, models: [], catalogError: '', loading: false,
    plans: [], drafts: Object.create(null), busy: new Set(), error: '',
  };

  const node = (tag, className, text) => {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = String(text);
    return item;
  };
  const api = async (path, options = {}) => {
    const response = await fetch(path, {
      method: options.method || 'GET',
      headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}) },
      ...(options.body ? { body: JSON.stringify(options.body) } : {}),
    });
    let data = null;
    try { data = await response.json(); } catch (_error) { data = null; }
    if (!response.ok) {
      const detail = data?.detail;
      const message = typeof detail === 'string' ? detail : detail?.message;
      const code = typeof detail === 'object' ? detail?.code : '';
      throw new Error([code, message || `HTTP ${response.status}`].filter(Boolean).join(': '));
    }
    if (!data || typeof data !== 'object') throw new Error('Resposta inválida do Nexus.');
    return data;
  };
  const isStale = generation => generation !== state.generation;
  const errorText = error => error?.message || 'Falha de rede. Verifique a conexão e consulte o estado salvo antes de repetir.';
  const isNetworkError = error => /failed to fetch|network|conexão/i.test(error?.message || '');
  const storedPlans = projectId => {
    try {
      const all = JSON.parse(window.localStorage?.getItem(STORAGE_KEY) || '{}');
      const ids = all && Array.isArray(all[projectId]) ? all[projectId] : [];
      return [...new Set(ids.filter(id => typeof id === 'string' && id.length <= 64))];
    } catch (_error) { return []; }
  };
  const rememberPlan = (projectId, planId) => {
    try {
      const all = JSON.parse(window.localStorage?.getItem(STORAGE_KEY) || '{}');
      const ids = Array.isArray(all[projectId]) ? all[projectId] : [];
      all[projectId] = [...new Set([...ids.filter(id => typeof id === 'string'), planId])].slice(-20);
      window.localStorage?.setItem(STORAGE_KEY, JSON.stringify(all));
    } catch (_error) { /* Persisted plan data remains authoritative; local storage only remembers IDs. */ }
  };
  const selectedModel = modelId => state.models.find(model => model.id === modelId && model.selectable === true) || null;
  const draftFor = planId => (state.drafts[planId] ||= {
    models: Object.create(null), efforts: Object.create(null), confirmCosts: false, executionCosts: false,
  });
  const isPaid = model => !model || model.cost_status !== 'free';
  const criteria = value => value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);

  function setError(message) {
    state.error = message || '';
    const target = $('plan-authoring-error');
    target.textContent = state.error;
    target.hidden = !state.error;
  }

  function clearForm() {
    ['plan-task-text', 'plan-agent-a-task', 'plan-agent-a-criteria',
      'plan-agent-b-task', 'plan-agent-b-criteria'].forEach(id => { $(id).value = ''; });
    $('plan-dependency').value = '';
  }

  function renderCatalog() {
    const target = $('plan-authoring-catalog');
    if (!state.projectId) target.textContent = 'Selecione um projeto autorizado para carregar modelos Codex Native.';
    else if (state.catalogError) target.textContent = `Catálogo Codex Native indisponível: ${state.catalogError}`;
    else if (!state.models.length) target.textContent = 'Nenhum modelo Codex Native selecionável foi anunciado.';
    else target.textContent = `${state.models.length} modelos Codex Native disponíveis. Effort não é selecionado automaticamente.`;
  }

  function addPlanFacts(card, plan) {
    const summary = node('p', 'plan-card-summary', plan.task_text || plan.summary || 'Missão sem descrição.');
    card.append(summary);
    (plan.agents || []).forEach(agent => {
      const section = node('section', 'plan-agent-summary');
      const heading = node('h4', '', `${agent.title || agent.key} · ${agent.role || 'agente'}`);
      const task = node('p', '', agent.task || '');
      const accepted = node('ul', 'plan-criteria-list');
      (agent.acceptance_criteria || []).forEach(item => accepted.append(node('li', '', item)));
      const dependency = (agent.depends_on || []).length
        ? `Depende de: ${agent.depends_on.join(', ')}` : 'Sem dependências declaradas';
      section.append(heading, task, node('p', 'plan-dependency-note', dependency), accepted);
      card.append(section);
    });
  }

  function createSelect(labelText, options, value, onChange, placeholder) {
    const label = node('label', 'plan-field');
    label.append(node('span', '', labelText));
    const select = node('select');
    select.setAttribute('aria-label', labelText);
    const first = node('option', '', placeholder);
    first.value = '';
    select.append(first);
    options.forEach(optionData => {
      const option = node('option', '', optionData.label);
      option.value = optionData.value;
      select.append(option);
    });
    select.value = value || '';
    select.addEventListener('change', () => onChange(select.value));
    label.append(select);
    return label;
  }

  function addExplicitConsent(card, text, checked, onChange) {
    const label = node('label', 'plan-consent');
    const input = node('input');
    input.type = 'checkbox';
    input.checked = Boolean(checked);
    input.addEventListener('change', () => onChange(input.checked));
    label.append(input, node('span', '', text));
    card.append(label);
    return input;
  }

  function actionButton(label, disabled, onClick, className = '') {
    const button = node('button', `plan-action ${className}`.trim(), label);
    button.type = 'button';
    button.disabled = Boolean(disabled);
    button.addEventListener('click', event => { event.preventDefault(); void onClick(); });
    return button;
  }

  function renderProposed(card, plan) {
    const draft = draftFor(plan.plan_id);
    card.append(node('p', 'plan-card-state', 'PROPOSED · ainda sem autorização de execução'));
    const choices = node('div', 'plan-agent-choices');
    const unavailable = [];
    AGENTS.forEach(agent => {
      const modelId = draft.models[agent.key] || '';
      const model = selectedModel(modelId);
      const modelOptions = state.models.map(item => ({
        value: item.id,
        label: `${item.id} · ${item.cost_status || 'custo não confirmado'}`,
      }));
      const block = node('section', 'plan-agent-choice');
      block.append(node('h4', '', `${agent.title} · Codex Native`));
      block.append(createSelect(
        `Modelo de ${agent.title}`,
        modelOptions,
        modelId,
        value => {
          draft.models[agent.key] = value;
          draft.efforts[agent.key] = '';
          draft.confirmCosts = false;
          renderPlans();
        },
        'Escolha um modelo',
      ));
      const efforts = model?.supported_efforts || [];
      if (modelId && !model) unavailable.push(`${agent.title}: modelo ausente do catálogo`);
      block.append(createSelect(
        `Reasoning effort de ${agent.title}`,
        efforts.map(effort => ({ value: effort, label: effort })),
        draft.efforts[agent.key],
        value => { draft.efforts[agent.key] = value; renderPlans(); },
        model ? 'Escolha o effort' : 'Escolha primeiro o modelo',
      ));
      block.append(node('p', 'plan-field-note', model
        ? `Catálogo Native · ${model.cost_status || 'custo desconhecido'} · esforços: ${efforts.join(', ') || 'nenhum'}`
        : 'Backend fixo: Codex Native. Modelo e effort precisam de escolha explícita.'));
      choices.append(block);
    });
    card.append(choices);
    if (unavailable.length) card.append(node('p', 'plan-inline-error', unavailable.join(' · ')));
    const missingEfforts = AGENTS.some(agent => {
      const model = selectedModel(draft.models[agent.key]);
      return !model || !model.supported_efforts?.includes(draft.efforts[agent.key]);
    });
    const selected = AGENTS.map(agent => selectedModel(draft.models[agent.key]));
    const paid = selected.some(isPaid);
    if (paid) addExplicitConsent(
      card,
      'Confirmo os custos dos modelos selecionados que não estão marcados como gratuitos.',
      draft.confirmCosts,
      value => { draft.confirmCosts = value; renderPlans(); },
    );
    card.append(node('p', 'plan-safety-note',
      'Aprovar cria sessões e worktrees isolados, persistindo modelos e efforts; não inicia turnos. O registro inclui 1 turno/agente e timeout contratual de 10 min, mas o timeout não é garantia de duração para turnos Native. O coordinator inicia um turno Native por agente no MVP; revisão humana continua obrigatória e nada é aplicado ao projeto principal.'));
    const approve = actionButton(
      'Aprovar configurações',
      state.loading || state.catalogError || missingEfforts || (paid && !draft.confirmCosts)
        || state.busy.has(`approve:${plan.plan_id}`),
      () => approvePlan(plan),
      'plan-action-primary',
    );
    card.append(approve);
  }

  function renderApproved(card, plan) {
    const approval = plan.approval || {};
    const choices = approval.agent_models || {};
    card.append(node('p', 'plan-card-state', `${plan.status} · configurações persistidas`));
    const list = node('ul', 'plan-approved-list');
    AGENTS.forEach(agent => {
      const choice = choices[agent.key] || {};
      list.append(node('li', '', `${agent.title}: Codex Native · ${choice.model || 'modelo ausente'} · effort ${choice.effort || 'padrão do modelo'} · ${choice.cost_status || 'custo desconhecido'}`));
    });
    card.append(list);
    if (plan.execution) {
      const execution = plan.execution;
      card.append(node('p', 'plan-card-execution', `Execução persistida: ${execution.status}${execution.error_code ? ` · ${execution.error_code}` : ''}`));
      if (execution.status !== 'AWAITING_EXECUTION') return;
    }
    if (plan.status !== 'APPROVED' || plan.execution) return;
    const models = AGENTS.map(agent => selectedModel(choices[agent.key]?.model));
    const unavailable = models.some(model => !model);
    const newlyPaid = models.filter(isPaid).map(model => model.id)
      .filter(id => !(approval.cost_confirmed_for || []).includes(id));
    const draft = draftFor(plan.plan_id);
    if (newlyPaid.length) addExplicitConsent(
      card,
      `Confirmo o custo para iniciar: ${newlyPaid.join(', ')}.`,
      draft.executionCosts,
      value => { draft.executionCosts = value; renderPlans(); },
    );
    const limitsReady = Number.isInteger(approval.max_turns_per_agent)
      && Number.isInteger(approval.mission_timeout_seconds);
    card.append(node('p', 'plan-safety-note',
      `Iniciar autoriza a sequência na ordem aprovada. O coordinator inicia um turno por agente Native; o timeout de missão persistido (${approval.mission_timeout_seconds}s) não é garantia de duração de cada turno. Handoff, resume e revisão continuam explícitos.`));
    card.append(actionButton(
      'Iniciar missão',
      unavailable || !limitsReady || state.loading || Boolean(state.catalogError)
        || (newlyPaid.length > 0 && !draft.executionCosts)
        || state.busy.has(`execute:${plan.plan_id}`),
      () => executePlan(plan),
      'plan-action-primary',
    ));
  }

  function renderPlans() {
    const target = $('plan-authoring-list');
    if (state.loading) { target.replaceChildren(node('p', 'plan-empty', 'Carregando planos salvos…')); return; }
    if (!state.projectId) { target.replaceChildren(); return; }
    if (!state.plans.length) { target.replaceChildren(node('p', 'plan-empty', 'Nenhuma proposta manual registrada neste cockpit para o projeto.')); return; }
    const cards = state.plans.map(entry => {
      const plan = entry.plan;
      const card = node('article', 'plan-card');
      const heading = node('header', 'plan-card-heading');
      heading.append(node('h3', '', `Plano ${plan.plan_id}`), node('span', 'plan-status', plan.status || 'DESCONHECIDO'));
      card.append(heading);
      addPlanFacts(card, plan);
      if (entry.error) card.append(node('p', 'plan-inline-error', entry.error));
      else if (plan.status === 'PROPOSED') renderProposed(card, plan);
      else if (plan.status === 'APPROVED') renderApproved(card, plan);
      else card.append(node('p', 'plan-card-state', `Estado persistido: ${plan.status}`));
      return card;
    });
    target.replaceChildren(...cards);
  }

  function render() {
    $('plan-authoring-project').textContent = state.projectId
      ? `Projeto autorizado selecionado: ${state.projectId}`
      : 'Selecione um projeto autorizado';
    $('plan-create').disabled = !state.projectId || state.loading
      || state.busy.has(`create:${state.projectId}`);
    renderCatalog();
    renderPlans();
    const error = $('plan-authoring-error');
    error.textContent = state.error;
    error.hidden = !state.error;
  }

  async function refreshPlan(planId, projectId, generation) {
    const plan = await api(`/api/workspace/plans/${encodeURIComponent(planId)}`);
    if (plan.project_id !== projectId) throw new Error('O plano retornado não pertence ao projeto selecionado.');
    let execution = plan.execution || null;
    if (execution) {
      execution = await api(`/api/workspace/plans/${encodeURIComponent(planId)}/execution`);
    }
    if (isStale(generation)) return null;
    return { plan: { ...plan, execution }, error: '' };
  }

  async function synchronizeMonitor(projectId, planId, generation) {
    if (isStale(generation)) return;
    if (typeof window.NexusPlanMonitor?.refresh !== 'function') {
      if (!state.error) setError('Plano salvo, mas o monitor está indisponível. Recarregue a página antes de continuar.');
      return;
    }
    try {
      const synchronized = await window.NexusPlanMonitor.refresh(projectId, planId);
      if (!synchronized && !isStale(generation) && !state.error) {
        setError('Plano salvo, mas o monitor não confirmou a atualização. Recarregue o projeto antes de continuar.');
      }
    } catch (_error) {
      if (!isStale(generation) && !state.error) {
        setError('Plano salvo, mas o monitor não respondeu. Recarregue o projeto antes de continuar.');
      }
    }
  }

  async function loadProject(projectId) {
    state.generation++;
    const generation = state.generation;
    state.projectId = projectId || null;
    state.models = [];
    state.catalogError = '';
    state.plans = [];
    state.drafts = Object.create(null);
    state.error = '';
    state.loading = Boolean(projectId);
    clearForm();
    render();
    if (!projectId) { state.loading = false; render(); return; }
    const ids = storedPlans(projectId);
    const [catalog, planResults] = await Promise.all([
      api('/api/dev/models?backend=codex_native')
        .then(data => ({ data, error: '' }))
        .catch(error => ({ data: null, error: errorText(error) })),
      Promise.all(ids.map(planId => refreshPlan(planId, projectId, generation)
        .catch(error => ({ plan: { plan_id: planId }, error: errorText(error) })))),
    ]);
    if (isStale(generation)) return;
    state.models = (catalog.data?.models || []).filter(model => model.selectable === true
      && typeof model.id === 'string' && Array.isArray(model.supported_efforts));
    state.catalogError = catalog.data?.catalog_error || catalog.error;
    state.plans = planResults.filter(Boolean);
    state.loading = false;
    render();
  }

  async function createPlan(event) {
    event?.preventDefault?.();
    const projectId = state.projectId;
    const generation = state.generation;
    const busyKey = `create:${projectId}`;
    if (!projectId || state.busy.has(busyKey)) return;
    const description = $('plan-task-text').value.trim();
    const taskA = $('plan-agent-a-task').value.trim();
    const taskB = $('plan-agent-b-task').value.trim();
    const criteriaA = criteria($('plan-agent-a-criteria').value);
    const criteriaB = criteria($('plan-agent-b-criteria').value);
    const dependency = $('plan-dependency').value;
    if (!description || !taskA || !taskB || !criteriaA.length || !criteriaB.length
        || !['after', 'independent'].includes(dependency)) {
      setError('Preencha a missão, as duas tarefas, critérios de aceite e a relação de execução.');
      return;
    }
    const body = {
      project_id: projectId,
      task_text: description,
      agents: [
        { key: 'agent-a', role: 'backend', title: 'Agente A', task: taskA,
          acceptance_criteria: criteriaA, depends_on: [], touches: [] },
        { key: 'agent-b', role: 'tester', title: 'Agente B', task: taskB,
          acceptance_criteria: criteriaB, depends_on: dependency === 'after' ? ['agent-a'] : [], touches: [] },
      ],
      execution_order: ['agent-a', 'agent-b'],
    };
    state.busy.add(busyKey);
    setError(''); render();
    let changedPlanId = null;
    try {
      const result = await api('/api/workspace/plans/manual', { method: 'POST', body });
      if (result.project_id !== projectId || result.status !== 'PROPOSED') {
        throw new Error('O backend não confirmou a proposta PROPOSED do projeto selecionado.');
      }
      changedPlanId = result.plan_id;
      rememberPlan(projectId, result.plan_id);
      clearForm();
      if (!isStale(generation)) {
        const entry = await refreshPlan(result.plan_id, projectId, generation);
        if (!isStale(generation) && entry) state.plans = [entry, ...state.plans.filter(item => item.plan.plan_id !== result.plan_id)];
      }
      setError('');
    } catch (error) {
      if (!isStale(generation)) setError(isNetworkError(error)
        ? 'Falha de rede: a proposta pode ter sido registrada. Não envie novamente até confirmar o estado do plano.'
        : errorText(error));
    } finally {
      if (changedPlanId) await synchronizeMonitor(projectId, changedPlanId, generation);
      state.busy.delete(busyKey);
      if (!isStale(generation)) render();
    }
  }

  async function approvePlan(plan) {
    const planId = plan.plan_id;
    const projectId = state.projectId;
    const generation = state.generation;
    if (plan.project_id !== projectId || state.busy.has(`approve:${planId}`)) return;
    const draft = draftFor(planId);
    const models = Object.fromEntries(AGENTS.map(agent => [agent.key, draft.models[agent.key]]));
    const efforts = Object.fromEntries(AGENTS.map(agent => [agent.key, draft.efforts[agent.key]]));
    if (AGENTS.some(agent => !selectedModel(models[agent.key])
        || !selectedModel(models[agent.key]).supported_efforts.includes(efforts[agent.key]))) {
      setError('Escolha um modelo e um effort disponível para cada agente.'); return;
    }
    if (AGENTS.some(agent => isPaid(selectedModel(models[agent.key]))) && !draft.confirmCosts) {
      setError('Confirme explicitamente os custos dos modelos que não estão marcados como gratuitos.'); return;
    }
    state.busy.add(`approve:${planId}`); setError(''); render();
    let approved = false;
    try {
      const result = await api(`/api/workspace/plans/${encodeURIComponent(planId)}/approve`, {
        method: 'POST',
        body: {
          agent_models: models, backend: 'codex_native',
          agent_backends: Object.fromEntries(AGENTS.map(agent => [agent.key, 'codex_native'])),
          agent_efforts: efforts,
          max_turns_per_agent: 1, mission_timeout_seconds: 600,
          confirm_costs: Boolean(draft.confirmCosts),
        },
      });
      if (result.plan?.status !== 'APPROVED') throw new Error('O backend não confirmou a aprovação persistida.');
      approved = true;
      state.drafts[planId] = Object.create(null);
      const updated = await refreshPlan(planId, projectId, generation);
      if (!isStale(generation) && updated) state.plans = state.plans.map(item => item.plan.plan_id === planId ? updated : item);
    } catch (error) {
      if (!isStale(generation)) {
        setError(errorText(error));
        try {
          const current = await refreshPlan(planId, projectId, generation);
          if (!isStale(generation) && current) state.plans = state.plans.map(item => item.plan.plan_id === planId ? current : item);
        } catch (_refreshError) { /* Keep the original failure visible; no mutation is retried. */ }
      }
    } finally {
      if (approved) await synchronizeMonitor(projectId, planId, generation);
      state.busy.delete(`approve:${planId}`);
      if (!isStale(generation)) render();
    }
  }

  async function executePlan(plan) {
    const planId = plan.plan_id;
    const projectId = state.projectId;
    const generation = state.generation;
    if (plan.project_id !== projectId || plan.status !== 'APPROVED'
        || state.busy.has(`execute:${planId}`)) return;
    const approval = plan.approval || {};
    const choices = approval.agent_models || {};
    const missingCostConsent = AGENTS.some(agent => {
      const model = selectedModel(choices[agent.key]?.model);
      return isPaid(model) && !(approval.cost_confirmed_for || []).includes(model.id);
    });
    const draft = draftFor(planId);
    if (missingCostConsent && !draft.executionCosts) {
      setError('Confirme explicitamente o custo adicional antes de iniciar.'); return;
    }
    state.busy.add(`execute:${planId}`); setError(''); render();
    let started = false;
    try {
      await api(`/api/workspace/plans/${encodeURIComponent(planId)}/execute`, {
        method: 'POST',
        body: {
          agent_keys: AGENTS.map(agent => agent.key),
          test_profiles: {},
          max_turns_per_agent: approval.max_turns_per_agent,
          mission_timeout_seconds: approval.mission_timeout_seconds,
          confirm_costs: missingCostConsent && draft.executionCosts,
        },
      });
      started = true;
      const updated = await refreshPlan(planId, projectId, generation);
      if (!isStale(generation) && updated) state.plans = state.plans.map(item => item.plan.plan_id === planId ? updated : item);
    } catch (error) {
      if (!isStale(generation)) {
        setError(errorText(error));
        try {
          const current = await refreshPlan(planId, projectId, generation);
          if (!isStale(generation) && current) state.plans = state.plans.map(item => item.plan.plan_id === planId ? current : item);
        } catch (_refreshError) { /* Keep the original failure visible; no mutation is retried. */ }
      }
    } finally {
      if (started) await synchronizeMonitor(projectId, planId, generation);
      state.busy.delete(`execute:${planId}`);
      if (!isStale(generation)) render();
    }
  }

  $('plan-authoring-form').addEventListener('submit', event => { void createPlan(event); });
  host.onProject(projectId => { void loadProject(projectId); });
  render();
})();
