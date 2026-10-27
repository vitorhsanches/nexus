// Maestro Native plan & execution monitor for the Dark Terminal cockpit's
// "Revisão" pane (P5.UIC1).
//
// Read-only over GET /api/workspace/state, /plans and /plans/{id}/execution
// -- coordination.steps is the one source of truth for a step's status,
// never latest_mission or _dependency_status (those stay OmniRoute-only).
// The one write this pane performs is explicit and narrow: binding an
// already-created, already-REVIEWED Codex Native review to the step that is
// AWAITING_NATIVE_REVIEW (POST /api/workspace/plans/{id}/steps/{key}/review,
// added in P5.UIA). It never creates a review, approves or transfers a
// handoff, resumes the sequence, or applies/pushes anything -- transfer and
// resume controls belong to P5.UIC2.
(() => {
  const host = window.NexusCockpit;
  const pane = document.querySelector('[data-pane="review"]');
  if (!host || !pane) return;
  const $ = id => document.getElementById(id);
  const POLL_MS = 3000;
  const TERMINAL_EXECUTION = new Set(['AWAITING_REVIEW', 'FAILED', 'INTERRUPTED', 'RESULT_UNCERTAIN']);

  const STATUS_LABEL = {
    AWAITING_EXECUTION: 'Aguardando autorização', RUNNING: 'Executando', WAITING_DEPENDENCY: 'Aguardando dependência',
    AWAITING_REVIEW: 'Aguardando revisão final', BLOCKED: 'Bloqueado', FAILED: 'Falhou',
    INTERRUPTED: 'Interrompido', RESULT_UNCERTAIN: 'Resultado incerto',
  };
  const STEP_LABEL = {
    PENDING: 'Pendente', STARTING: 'Iniciando', RUNNING: 'Executando',
    AWAITING_NATIVE_REVIEW: 'Aguardando revisão', READY_FOR_REVIEW: 'Pronto', BLOCKED: 'Bloqueado',
    FAILED: 'Falhou', INTERRUPTED: 'Interrompido', RESULT_UNCERTAIN: 'Resultado incerto',
  };
  const STEP_MARK = {
    PENDING: '○', STARTING: '◔', RUNNING: '◑', AWAITING_NATIVE_REVIEW: '◐', READY_FOR_REVIEW: '✓',
    BLOCKED: '◐', FAILED: '✗', INTERRUPTED: '■', RESULT_UNCERTAIN: '✗',
  };
  // The badge's own CSS only styles these three tones -- never invent a
  // fourth; an actual failure is still carried by the text, not the badge.
  const STEP_TONE = {
    PENDING: 'idle', STARTING: 'waiting', RUNNING: 'ready', AWAITING_NATIVE_REVIEW: 'waiting',
    READY_FOR_REVIEW: 'ready', BLOCKED: 'waiting', FAILED: 'waiting', INTERRUPTED: 'idle', RESULT_UNCERTAIN: 'waiting',
  };

  const state = {
    projectId: null, generation: 0,
    plans: [], plansError: '', plansLoading: false,
    panesByPlan: new Map(), // plan_id -> Map(agent_key -> pane)
    selectedPlanId: null,
    execution: null, executionError: '', executionLoading: false,
    pollTimer: null,
    reviews: { sessionId: null, items: [], error: '', loading: false },
    bindBusy: false, bindError: '',
  };

  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
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
      const error = new Error((typeof detail === 'string' ? detail : detail?.message) || `HTTP ${response.status}`);
      error.code = typeof detail === 'object' ? detail?.code : undefined;
      throw error;
    }
    if (data === null || typeof data !== 'object') throw new Error('Resposta inválida do Nexus');
    return data;
  };
  const stale = generation => generation !== state.generation;
  const setNote = (id, message, isError = false) => {
    const node = $(id);
    if (!node) return;
    node.textContent = message || '';
    node.classList.toggle('is-error', Boolean(isError && message));
  };
  const planLabel = plan => `${(plan.task_text || plan.plan_id || '').slice(0, 48)} · ${plan.status}`;

  // ---- derivation (coordination.steps is the one source of truth) --------
  const agentsFor = plan => (Array.isArray(plan?.agents) ? plan.agents : []);
  const paneFor = (planId, agentKey) => state.panesByPlan.get(planId)?.get(agentKey) || null;
  const selectedPlan = () => state.plans.find(plan => plan.plan_id === state.selectedPlanId) || null;
  const stepAwaitingReview = () => (state.execution?.steps || []).find(step => step.status === 'AWAITING_NATIVE_REVIEW') || null;

  // ---- rendering -----------------------------------------------------------
  const renderPlanPicker = () => {
    const select = $('review-plan-select');
    if (!select) return;
    const previous = select.value;
    if (!state.projectId) {
      select.disabled = true;
      select.replaceChildren(element('option', '', 'Selecione um projeto'));
      setNote('review-plan-note', 'Selecione um projeto autorizado.');
      return;
    }
    if (state.plansLoading && !state.plans.length) {
      select.disabled = true;
      select.replaceChildren(element('option', '', 'Carregando planos…'));
      return;
    }
    if (!state.plans.length) {
      select.disabled = true;
      select.replaceChildren(element('option', '', 'Nenhum plano registrado neste projeto'));
      setNote('review-plan-note', state.plansError
        ? `Falha ao carregar planos: ${state.plansError}`
        : 'Nenhum plano foi criado ainda neste projeto.');
      return;
    }
    select.disabled = false;
    select.replaceChildren(...state.plans.map(plan => {
      const option = element('option', '', planLabel(plan));
      option.value = plan.plan_id;
      return option;
    }));
    select.value = state.plans.some(plan => plan.plan_id === state.selectedPlanId) ? state.selectedPlanId : previous;
    setNote('review-plan-note', state.plansError ? `Falha ao atualizar a lista de planos: ${state.plansError}` : '');
  };

  const renderSteps = () => {
    const list = $('review-steps');
    if (!list) return;
    const plan = selectedPlan();
    if (!state.selectedPlanId) {
      list.replaceChildren(element('div', 'cockpit-event', 'Nenhum plano selecionado.'));
      return;
    }
    if (state.executionLoading && !state.execution) {
      list.replaceChildren(element('div', 'cockpit-event', 'Carregando execução…'));
      return;
    }
    if (state.executionError) {
      list.replaceChildren(element('div', 'cockpit-event', `Falha ao consultar a execução: ${state.executionError}`));
      return;
    }
    const execution = state.execution;
    if (!execution) {
      list.replaceChildren(element('div', 'cockpit-event',
        'Este plano ainda não foi autorizado para execução coordenada.'));
      return;
    }
    const agents = agentsFor(plan);
    list.replaceChildren(...execution.steps.map((step, index) => {
      const agent = agents.find(item => item.key === step.agent_key) || {};
      const agentPane = paneFor(execution.plan_id, step.agent_key);
      const row = element('div', 'review-step');
      row.append(element('span', 'step-number', String(index + 1).padStart(2, '0')));
      const body = element('div');
      body.append(element('strong', '', `${agent.title || step.agent_key} · ${agent.role || ''}`.trim()));
      const detailParts = [
        `Backend ${agentPane?.backend || '—'}`,
        `Modelo ${agentPane?.model || agent.model || '—'}`,
        STEP_LABEL[step.status] || step.status,
      ];
      if (agent.depends_on?.length) detailParts.push(`depende de ${agent.depends_on.join(', ')}`);
      if (step.native_turn_id && ['RUNNING', 'STARTING'].includes(step.status)) detailParts.push('turno ativo');
      if (step.error_code) detailParts.push(`${step.error_code}${step.error_message ? `: ${step.error_message}` : ''}`);
      body.append(element('small', '', detailParts.join(' · ')));
      row.append(body);
      row.append(element('span', `step-mark pane-status ${STEP_TONE[step.status] || 'idle'}`, STEP_MARK[step.status] || '○'));
      return row;
    }));
  };

  const renderHandoffNote = () => {
    const execution = state.execution;
    const code = execution?.error_code || '';
    let text = '';
    if (execution?.status === 'BLOCKED' && code === 'DEPENDENCY_WORKTREE_NOT_TRANSFERRED') {
      text = 'Handoff pendente: há alterações no agente anterior aguardando preview, aprovação e transferência humana antes de continuar. (Transferência e retomada chegam na próxima etapa.)';
    } else if (execution?.status === 'BLOCKED' && code === 'MIXED_BACKEND_HANDOFF_NOT_SUPPORTED') {
      text = 'Bloqueado: esta sequência mistura Codex Native com OmniRoute, o que ainda não é suportado.';
    } else if (execution?.status === 'BLOCKED' && code) {
      text = `Bloqueado: ${code}${execution.error_message ? ` — ${execution.error_message}` : ''}.`;
    } else if (execution?.status === 'FAILED' || execution?.status === 'RESULT_UNCERTAIN') {
      text = `${STATUS_LABEL[execution.status]}${execution.error_code ? ` (${execution.error_code})` : ''}${execution.error_message ? `: ${execution.error_message}` : ''}.`;
    }
    setNote('review-handoff-note', text, Boolean(execution?.status === 'FAILED' || execution?.status === 'RESULT_UNCERTAIN'));
  };

  const renderFinal = () => {
    const execution = state.execution;
    let text = '';
    if (execution?.status === 'AWAITING_REVIEW') {
      text = 'Plano concluído: todos os passos chegaram ao fim. A integração nunca é automática — abra a interface Development para revisar, aplicar ou rejeitar.';
    }
    setNote('review-final', text);
  };

  const renderBind = () => {
    const box = $('review-bind');
    if (!box) return;
    const step = stepAwaitingReview();
    if (!step) { box.hidden = true; return; }
    box.hidden = false;
    $('review-bind-agent').textContent = step.agent_key;
    const select = $('review-bind-select');
    const previous = select.value;
    if (state.reviews.loading && !state.reviews.items.length) {
      select.replaceChildren(element('option', '', 'Carregando revisões…'));
    } else if (state.reviews.error) {
      select.replaceChildren(element('option', '', 'Falha ao listar revisões'));
    } else if (!state.reviews.items.length) {
      select.replaceChildren(element('option', '', 'Nenhuma revisão registrada nesta sessão'));
    } else {
      const prompt = element('option', '', 'Selecione uma revisão existente');
      prompt.value = '';
      select.replaceChildren(prompt, ...state.reviews.items.map(review => {
        const option = element('option', '', `${String(review.review_id).slice(0, 12)} · ${review.status} · ${review.created_at || ''}`);
        option.value = review.review_id;
        return option;
      }));
      select.value = state.reviews.items.some(review => review.review_id === previous) ? previous : '';
    }
    $('review-bind-submit').disabled = state.bindBusy || !select.value;
    $('review-bind-refresh').disabled = state.bindBusy || state.reviews.loading;
    setNote('review-bind-error', state.bindError, true);
  };

  const renderChrome = () => {
    const execution = state.execution;
    const plan = selectedPlan();
    $('review-meta-plan').textContent = plan ? plan.plan_id : '—';
    $('review-meta-status').textContent = execution ? (STATUS_LABEL[execution.status] || execution.status) : 'Sem resultado';
    $('review-meta-step').textContent = execution && Number.isInteger(execution.current_index)
      ? `${execution.current_index + 1}/${execution.steps.length}` : '—';
    const badge = $('review-status-badge');
    const awaiting = Boolean(stepAwaitingReview());
    const tone = !execution ? 'idle' : awaiting ? 'waiting' : (STEP_TONE[execution.status] || (TERMINAL_EXECUTION.has(execution.status) ? 'ready' : 'idle'));
    badge.className = `pane-status ${tone}`;
    badge.lastChild.textContent = ` ${awaiting ? 'Aguardando você' : (execution ? (STATUS_LABEL[execution.status] || execution.status) : 'Aguardando')}`;
    $('review-foot').textContent = plan ? `◇  workspace / plano ${plan.plan_id}` : '◇  workspace / revisão';
    $('review-foot-right').textContent = execution ? 'LEITURA + VÍNCULO EXPLÍCITO DE REVISÃO' : 'AGUARDANDO DADOS DO NEXUS';
  };

  const render = () => { renderPlanPicker(); renderSteps(); renderHandoffNote(); renderFinal(); renderBind(); renderChrome(); };

  // ---- loading --------------------------------------------------------------
  const stopPolling = () => { clearTimeout(state.pollTimer); state.pollTimer = null; };
  const schedulePoll = generation => {
    stopPolling();
    if (stale(generation) || !state.selectedPlanId) return;
    if (state.execution && TERMINAL_EXECUTION.has(state.execution.status)) return;
    state.pollTimer = setTimeout(() => { void loadExecution(generation, state.selectedPlanId); }, POLL_MS);
  };

  const loadExecution = async (generation, planId) => {
    state.executionLoading = true;
    try {
      const execution = await api(`/api/workspace/plans/${encodeURIComponent(planId)}/execution`);
      if (stale(generation) || state.selectedPlanId !== planId) return;
      state.execution = execution; state.executionError = '';
    } catch (error) {
      if (stale(generation) || state.selectedPlanId !== planId) return;
      // A plan with no execution authorization yet is not a failure.
      if (error.code === 'EXECUTION_NOT_AUTHORIZED') state.execution = null;
      else state.executionError = error.message;
    } finally {
      if (!stale(generation) && state.selectedPlanId === planId) {
        state.executionLoading = false;
        void loadReviewsForAwaitingStep(generation);
        render();
        schedulePoll(generation);
      }
    }
  };

  const loadReviewsForAwaitingStep = async generation => {
    const step = stepAwaitingReview();
    const agentPane = step ? paneFor(state.selectedPlanId, step.agent_key) : null;
    const sessionId = agentPane?.session?.session_id || null;
    if (!sessionId) { state.reviews = { sessionId: null, items: [], error: '', loading: false }; return; }
    if (state.reviews.sessionId === sessionId && !state.reviews.loading && state.reviews.items.length) return;
    state.reviews = { sessionId, items: state.reviews.sessionId === sessionId ? state.reviews.items : [], error: '', loading: true };
    try {
      const data = await api(`/api/dev/sessions/${encodeURIComponent(sessionId)}/integrations`);
      if (stale(generation) || state.reviews.sessionId !== sessionId) return;
      state.reviews = { sessionId, items: data.integrations || [], error: '', loading: false };
    } catch (error) {
      if (stale(generation) || state.reviews.sessionId !== sessionId) return;
      state.reviews = { sessionId, items: [], error: error.message, loading: false };
    } finally {
      if (!stale(generation)) renderBind();
    }
  };

  const selectPlan = (planId, { skipRender = false } = {}) => {
    if (planId === state.selectedPlanId) return;
    state.generation++;
    const generation = state.generation;
    stopPolling();
    state.selectedPlanId = planId || null;
    state.execution = null; state.executionError = ''; state.executionLoading = false;
    state.reviews = { sessionId: null, items: [], error: '', loading: false };
    state.bindError = '';
    if (!skipRender) render();
    if (state.selectedPlanId) void loadExecution(generation, state.selectedPlanId);
  };

  const buildPaneIndex = panes => {
    const byPlan = new Map();
    for (const item of panes) {
      if (!item.plan_id || !item.plan_agent_key) continue;
      if (!byPlan.has(item.plan_id)) byPlan.set(item.plan_id, new Map());
      byPlan.get(item.plan_id).set(item.plan_agent_key, item);
    }
    return byPlan;
  };

  const loadProject = async projectId => {
    state.generation++;
    const generation = state.generation;
    stopPolling();
    Object.assign(state, {
      projectId, plans: [], plansError: '', plansLoading: Boolean(projectId),
      panesByPlan: new Map(), selectedPlanId: null, execution: null, executionError: '', executionLoading: false,
      reviews: { sessionId: null, items: [], error: '', loading: false }, bindBusy: false, bindError: '',
    });
    render();
    if (!projectId) return;
    try {
      const [plansData, stateData] = await Promise.all([
        api(`/api/workspace/plans?project_id=${encodeURIComponent(projectId)}`),
        api(`/api/workspace/state?project_id=${encodeURIComponent(projectId)}`),
      ]);
      if (stale(generation)) return;
      state.plans = plansData.plans || [];
      state.plansError = '';
      state.panesByPlan = buildPaneIndex(stateData.panes || []);
    } catch (error) {
      if (stale(generation)) return;
      state.plansError = error.message;
    } finally {
      if (!stale(generation)) {
        state.plansLoading = false;
        render();
        const preferred = state.plans.find(plan => plan.execution && !TERMINAL_EXECUTION.has(plan.execution.status))
          || state.plans[0];
        if (preferred) selectPlan(preferred.plan_id);
      }
    }
  };

  // ---- the one explicit write: bind an existing review to the step ---------
  const bindReview = async () => {
    const step = stepAwaitingReview();
    const reviewId = $('review-bind-select').value;
    if (!step || !reviewId || !state.selectedPlanId || state.bindBusy) return;
    const generation = state.generation;
    const planId = state.selectedPlanId;
    state.bindBusy = true; state.bindError = ''; setNote('review-bind-note', ''); renderBind();
    try {
      const execution = await api(
        `/api/workspace/plans/${encodeURIComponent(planId)}/steps/${encodeURIComponent(step.agent_key)}/review`,
        { method: 'POST', body: { review_id: reviewId } },
      );
      if (stale(generation) || state.selectedPlanId !== planId) return;
      state.execution = execution;
      setNote('review-bind-note', `Revisão ${String(reviewId).slice(0, 12)} vinculada ao passo ${step.agent_key}.`);
      host.notify(`Revisão vinculada ao passo ${step.agent_key}.`);
      render();
      schedulePoll(generation);
    } catch (error) {
      if (stale(generation) || state.selectedPlanId !== planId) return;
      // Never retried automatically: the exact API error is shown and the
      // operator decides whether to pick another review or investigate.
      state.bindError = error.message;
      renderBind();
    } finally {
      state.bindBusy = false;
      if (!stale(generation) && state.selectedPlanId === planId) renderBind();
    }
  };

  $('review-plan-select')?.addEventListener('change', event => { selectPlan(event.currentTarget.value); });
  $('review-bind-select')?.addEventListener('change', renderBind);
  $('review-bind-refresh')?.addEventListener('click', () => {
    state.reviews.items = [];
    void loadReviewsForAwaitingStep(state.generation);
  });
  $('review-bind-submit')?.addEventListener('click', () => { void bindReview(); });

  host.onProject(projectId => { void loadProject(projectId); });
  render();
})();
