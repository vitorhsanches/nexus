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
    // The relevant handoff record is re-derived from the server on every
    // execution load -- never trusted across polls or reloads on its own.
    handoff: null, handoffBusy: false, handoffError: '',
    resumeArmed: false, resumeBusy: false, resumeError: '',
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
  // A pending dependency-handoff block: the one case where the coordinator
  // itself requires a human-approved transfer before the next agent may
  // launch (coordinator.py's verified_dependency path). Evidence id is
  // read generically (native_review_id or mission_id, whichever the
  // predecessor actually used) -- the HTTP contract accepts both under
  // the historical "source_mission_id" field name (verified in P5.UIC2).
  const pendingHandoffInfo = () => {
    const execution = state.execution;
    if (!execution || execution.status !== 'BLOCKED' || execution.error_code !== 'DEPENDENCY_WORKTREE_NOT_TRANSFERRED') return null;
    const index = execution.current_index;
    const steps = execution.steps;
    if (!Number.isInteger(index) || index < 1 || index >= steps.length) return null;
    const source = steps[index - 1];
    const target = steps[index];
    const evidenceId = source.native_review_id || source.mission_id;
    if (!evidenceId) return null;
    return { source, target, evidenceId };
  };
  const matchingHandoff = (handoffs, info) => handoffs.find(item => item.source_pane_id === info.source.pane_id
    && item.target_pane_id === info.target.pane_id
    && (item.source_native_review_id || item.source_mission_id) === info.evidenceId
    && ['PREVIEWED', 'APPROVED', 'TRANSFERRED'].includes(item.status)) || null;

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
      text = 'Handoff pendente: há alterações no agente anterior aguardando preview, aprovação e transferência humana antes de continuar.';
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

  const renderHandoff = () => {
    const box = $('review-handoff');
    if (!box) return;
    const info = pendingHandoffInfo();
    if (!info) { box.hidden = true; return; }
    box.hidden = false;
    $('review-handoff-source').textContent = info.source.agent_key;
    $('review-handoff-target').textContent = info.target.agent_key;
    const handoff = state.handoff;
    const snapshot = $('review-handoff-snapshot');
    const previewBtn = $('review-handoff-preview');
    const approveBtn = $('review-handoff-approve');
    const transferBtn = $('review-handoff-transfer');
    if (!handoff || handoff.error_code) {
      snapshot.replaceChildren(element('div', '', handoff?.error_code
        ? `O handoff anterior ficou obsoleto (${handoff.error_code}${handoff.error_message ? `: ${handoff.error_message}` : ''}); prepare um novo.`
        : 'Nenhum handoff preparado ainda para esta dependência.'));
      previewBtn.hidden = false; previewBtn.disabled = state.handoffBusy;
      approveBtn.hidden = true; transferBtn.hidden = true;
    } else {
      const evidenceLabel = String(handoff.source_native_review_id || handoff.source_mission_id || '').slice(0, 12);
      snapshot.replaceChildren(
        element('div', '', `Revisão utilizada: ${evidenceLabel}`),
        element('div', '', `${handoff.files.length} arquivo(s): ${handoff.files.map(file => `${file.status} ${file.path}`).join(', ') || '—'}`),
        element('div', '', `Fingerprint SHA-256: ${handoff.snapshot_fingerprint}`),
        element('div', '', `Estado da transferência: ${handoff.status}`),
      );
      previewBtn.hidden = true;
      approveBtn.hidden = handoff.status !== 'PREVIEWED';
      approveBtn.disabled = state.handoffBusy;
      transferBtn.hidden = handoff.status !== 'APPROVED';
      transferBtn.disabled = state.handoffBusy;
    }
    setNote('review-handoff-error', state.handoffError, true);
  };

  const renderResume = () => {
    const box = $('review-resume');
    if (!box) return;
    const execution = state.execution;
    const eligible = Boolean(execution?.resume?.eligible);
    if (!eligible) { box.hidden = true; state.resumeArmed = false; return; }
    box.hidden = false;
    const button = $('review-resume-button');
    button.disabled = state.resumeBusy;
    button.textContent = state.resumeArmed ? 'Confirmar retomada' : 'Retomar execução';
    $('review-resume-cancel').hidden = !state.resumeArmed || state.resumeBusy;
    setNote('review-resume-error', state.resumeError, true);
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

  const render = () => {
    renderPlanPicker(); renderSteps(); renderHandoffNote(); renderHandoff(); renderResume(); renderFinal(); renderBind(); renderChrome();
  };

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
      await loadHandoffFor(generation, planId);
    } catch (error) {
      if (stale(generation) || state.selectedPlanId !== planId) return;
      // A plan with no execution authorization yet is not a failure.
      if (error.code === 'EXECUTION_NOT_AUTHORIZED') state.execution = null;
      else state.executionError = error.message;
      state.handoff = null;
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

  // The relevant handoff (if any) is re-derived from GET .../handoffs on
  // every execution load -- never carried forward across a poll, an action,
  // a reload or a project switch on client memory alone.
  const loadHandoffFor = async (generation, planId) => {
    const info = pendingHandoffInfo();
    if (!info) { state.handoff = null; return; }
    try {
      const data = await api(`/api/workspace/plans/${encodeURIComponent(planId)}/handoffs`);
      if (stale(generation) || state.selectedPlanId !== planId) return;
      state.handoff = matchingHandoff(data.handoffs || [], info);
    } catch (error) {
      if (stale(generation) || state.selectedPlanId !== planId) return;
      state.handoff = null;
      state.handoffError = `Falha ao consultar handoffs: ${error.message}`;
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
    state.handoff = null; state.handoffBusy = false; state.handoffError = '';
    state.resumeArmed = false; state.resumeBusy = false; state.resumeError = '';
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

  const loadProject = async (projectId, preferredPlanId = null) => {
    state.generation++;
    const generation = state.generation;
    stopPolling();
    Object.assign(state, {
      projectId, plans: [], plansError: '', plansLoading: Boolean(projectId),
      panesByPlan: new Map(), selectedPlanId: null, execution: null, executionError: '', executionLoading: false,
      reviews: { sessionId: null, items: [], error: '', loading: false }, bindBusy: false, bindError: '',
      handoff: null, handoffBusy: false, handoffError: '', resumeArmed: false, resumeBusy: false, resumeError: '',
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
        const preferred = state.plans.find(plan => plan.plan_id === preferredPlanId)
          || state.plans.find(plan => plan.execution && !TERMINAL_EXECUTION.has(plan.execution.status))
          || state.plans[0];
        if (preferred) selectPlan(preferred.plan_id);
      }
    }
  };

  // The authoring module calls this only after a confirmed plan mutation.
  // Reload both server-owned plan and pane state, then select that plan.
  window.NexusPlanMonitor = Object.freeze({
    refresh: async (projectId, planId) => {
      if (!projectId || projectId !== state.projectId || !planId) return false;
      await loadProject(projectId, planId);
      return state.projectId === projectId && state.selectedPlanId === planId
        && state.plans.some(plan => plan.plan_id === planId);
    },
  });

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
      await loadHandoffFor(generation, planId);
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

  // ---- handoff: preview, approve, transfer -- each an explicit, single
  // click, never chained automatically into the next step. ------------------
  const previewHandoff = async () => {
    const info = pendingHandoffInfo();
    if (!info || state.handoffBusy) return;
    const planId = state.selectedPlanId;
    const generation = state.generation;
    state.handoffBusy = true; state.handoffError = ''; renderHandoff();
    try {
      const result = await api('/api/workspace/handoffs/preview', { method: 'POST', body: {
        source_pane_id: info.source.pane_id, target_pane_id: info.target.pane_id, source_mission_id: info.evidenceId,
      } });
      if (stale(generation) || state.selectedPlanId !== planId) return;
      state.handoff = result;
      host.notify('Handoff preparado. Revise o snapshot antes de aprovar.');
    } catch (error) {
      if (stale(generation) || state.selectedPlanId !== planId) return;
      state.handoffError = error.message;
    } finally {
      state.handoffBusy = false;
      if (!stale(generation) && state.selectedPlanId === planId) renderHandoff();
    }
  };
  const approveHandoff = async () => {
    const handoff = state.handoff;
    if (!handoff || handoff.status !== 'PREVIEWED' || handoff.error_code || state.handoffBusy) return;
    const planId = state.selectedPlanId;
    const generation = state.generation;
    state.handoffBusy = true; state.handoffError = ''; renderHandoff();
    try {
      // Exactly the fingerprint and pane ids the preview itself returned --
      // never recomputed or guessed by this pane.
      const result = await api(`/api/workspace/handoffs/${encodeURIComponent(handoff.handoff_id)}/approve`, { method: 'POST', body: {
        fingerprint: handoff.snapshot_fingerprint, source_pane_id: handoff.source_pane_id, target_pane_id: handoff.target_pane_id,
      } });
      if (stale(generation) || state.selectedPlanId !== planId) return;
      state.handoff = result;
      host.notify('Handoff aprovado.');
    } catch (error) {
      if (stale(generation) || state.selectedPlanId !== planId) return;
      // A stale/changed snapshot surfaces here as the server's own error
      // (e.g. HANDOFF_SNAPSHOT_CHANGED) -- shown as-is, never retried.
      state.handoffError = error.message;
    } finally {
      state.handoffBusy = false;
      if (!stale(generation) && state.selectedPlanId === planId) renderHandoff();
    }
  };
  const transferHandoff = async () => {
    const handoff = state.handoff;
    if (!handoff || handoff.status !== 'APPROVED' || handoff.error_code || state.handoffBusy) return;
    const planId = state.selectedPlanId;
    const generation = state.generation;
    state.handoffBusy = true; state.handoffError = ''; renderHandoff();
    try {
      const result = await api(`/api/workspace/handoffs/${encodeURIComponent(handoff.handoff_id)}/transfer`, { method: 'POST' });
      if (stale(generation) || state.selectedPlanId !== planId) return;
      state.handoff = result;
      host.notify('Alterações transferidas.');
      // Refresh the plan's own execution now, not just on the next poll
      // tick, so resume eligibility reflects the transfer immediately.
      await loadExecution(generation, planId);
      return;
    } catch (error) {
      if (stale(generation) || state.selectedPlanId !== planId) return;
      state.handoffError = error.message;
    } finally {
      state.handoffBusy = false;
      if (!stale(generation) && state.selectedPlanId === planId) renderHandoff();
    }
  };

  // ---- resume: an explicit two-click confirmation, never chained after
  // transfer and never launching a turn directly from this pane. -----------
  const armResume = () => { state.resumeArmed = true; state.resumeError = ''; renderResume(); };
  const cancelResume = () => { state.resumeArmed = false; state.resumeError = ''; renderResume(); };
  const confirmResume = async () => {
    const execution = state.execution;
    if (!execution?.resume?.eligible || state.resumeBusy) return;
    const planId = state.selectedPlanId;
    const generation = state.generation;
    state.resumeBusy = true; state.resumeError = ''; renderResume();
    try {
      const result = await api(`/api/workspace/plans/${encodeURIComponent(planId)}/resume`, { method: 'POST' });
      if (stale(generation) || state.selectedPlanId !== planId) return;
      state.execution = result;
      state.resumeArmed = false;
      host.notify('Execução retomada; acompanhando o próximo agente.');
      render();
      schedulePoll(generation);
    } catch (error) {
      if (stale(generation) || state.selectedPlanId !== planId) return;
      // The real backend eligibility error is shown; the UI stays
      // conservative and offers no fallback action of its own.
      state.resumeError = error.message;
      state.resumeArmed = false;
    } finally {
      state.resumeBusy = false;
      if (!stale(generation) && state.selectedPlanId === planId) renderResume();
    }
  };
  const onResumeClick = () => { if (state.resumeArmed) void confirmResume(); else armResume(); };

  $('review-plan-select')?.addEventListener('change', event => { selectPlan(event.currentTarget.value); });
  $('review-bind-select')?.addEventListener('change', renderBind);
  $('review-bind-refresh')?.addEventListener('click', () => {
    state.reviews.items = [];
    void loadReviewsForAwaitingStep(state.generation);
  });
  $('review-bind-submit')?.addEventListener('click', () => { void bindReview(); });
  $('review-handoff-preview')?.addEventListener('click', () => { void previewHandoff(); });
  $('review-handoff-approve')?.addEventListener('click', () => { void approveHandoff(); });
  $('review-handoff-transfer')?.addEventListener('click', () => { void transferHandoff(); });
  $('review-resume-button')?.addEventListener('click', onResumeClick);
  $('review-resume-cancel')?.addEventListener('click', cancelResume);

  host.onProject(projectId => { void loadProject(projectId); });
  render();
})();
