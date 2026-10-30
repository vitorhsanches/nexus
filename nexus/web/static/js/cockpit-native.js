// Codex Native Development pane for the Dark Terminal cockpit.
//
// Works only through the existing /api/dev routes (the same ones the
// Development view uses). Every action that creates a session, runs inference,
// decides an approval, records a review, rejects, or applies it is a separate
// explicit human click; nothing is retried or decided automatically.
(() => {
  const host = window.NexusCockpit;
  if (!host) return;
  // A controller owns exactly one pane and one set of async resources. The
  // historical system pane uses the same controller with a movable session.
  const mount = (pane, fixed = null) => {
  const $ = id => fixed
    ? pane.querySelector(`[data-native-id="${id}"]`)
    : document.getElementById(id);
  const POLL_MS = 3000;
  const ACTIVE = ['PENDING', 'RUNNING'];
  const TERMINAL = ['COMPLETED', 'FAILED', 'CANCELLED', 'INTERRUPTED'];
  const TEXT_LIMIT = 20000;
  // The pane itself is runtime-agnostic: a session's own `backend` (persisted
  // at creation, never switched) decides how it is labeled and rendered.
  // `native-runtime` only picks which backend a NEW session is created with.
  const NATIVE_BACKENDS = new Set(['codex_native', 'claude_native']);
  const RUNTIME_LABELS = { codex_native: 'Codex Native', claude_native: 'Claude Native' };
  const DEFAULT_BACKEND = 'codex_native';

  const state = {
    projectId: null, generation: 0, gate: null, integrationGate: null, gateError: '',
    models: [], defaultModel: null, modelsError: '', sessions: [], sessionsError: '',
    session: null, turns: [], approvals: [], seenApprovals: new Set(), files: [], diff: null, changesError: '', selectedFile: null,
    // Claude Native only: approval_id -> {tool, operation, operation_sha256} fetched on demand,
    // never pre-loaded -- the exact content is shown only once a human asks to see it.
    operationDetails: new Map(),
    review: null, preflight: null, history: [], reviewDecisionUncertain: false,
    busy: new Set(), sessionNote: null, approvalPollFailed: false,
    stream: null, cursor: null, activeTurnId: null, live: null, connection: 'idle', pollTimer: null, reconnectTimer: null,
    fixedEffortValid: !fixed?.effort,
  };

  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const redact = value => String(value ?? '')
    .replace(/\b(Bearer\s+)[A-Za-z0-9._~+/-]+=*/gi, '$1[redacted]')
    .replace(/((?:api[_-]?key|token|secret|password|credential)[\w-]*["']?\s*[:=]\s*["']?)[^\s"',;]+/gi, '$1[redacted]')
    .replace(/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g, '[redacted private key]')
    .replace(/\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b/g, '[redacted token]');
  const clip = (value, limit = TEXT_LIMIT) => {
    const text = redact(value);
    return text.length > limit ? `${text.slice(0, limit)}\n… (${text.length - limit} caracteres omitidos)` : text;
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
      throw new Error((typeof detail === 'string' ? detail : detail?.message) || `HTTP ${response.status}`);
    }
    if (data === null || typeof data !== 'object') throw new Error('Resposta inválida do Nexus');
    return data;
  };

  const isNative = session => NATIVE_BACKENDS.has(session?.backend);
  const selectedRuntime = () => {
    if (fixed) return fixed.backend;
    const field = $('native-runtime');
    const value = field ? field.value : '';
    return NATIVE_BACKENDS.has(value) ? value : DEFAULT_BACKEND;
  };
  const turnIsActive = turn => ACTIVE.includes(turn?.status);
  const sessionPath = (suffix = '') => `/api/dev/sessions/${encodeURIComponent(state.session.session_id)}${suffix}`;
  const stale = generation => generation !== state.generation;
  const pendingApprovals = () => state.approvals.filter(item => item.status === 'PENDING');
  const reviewableSnapshot = review => Boolean(review?.snapshot?.approval_ready &&
    !review.snapshot.diff?.truncated && (review.snapshot.diff?.files || []).length &&
    review.snapshot.diff.files.every(file => typeof file.patch === 'string' && !file.truncated && !file.content_omitted));
  const backendLabel = session => RUNTIME_LABELS[session?.backend] || 'OmniRoute (legado)';
  const selectedModel = () => {
    const value = $('native-model').value;
    return state.models.some(model => model.id === value) ? value : '';
  };
  const setSessionNote = (text, error = false) => { state.sessionNote = text ? { text, error } : null; renderSessions(); };
  const setNote = (id, message, isError = false) => {
    const node = $(id);
    node.textContent = message || '';
    node.classList.toggle('is-error', Boolean(isError && message));
  };

  // ---- rendering ---------------------------------------------------------
  const paneStatus = () => {
    if (!state.projectId) return ['Sem projeto', 'idle'];
    if (!state.session) return ['Sem sessão', 'idle'];
    if (state.activeTurnId) return pendingApprovals().length ? ['Aguardando você', 'waiting'] : ['Executando', 'ready'];
    const latest = state.turns.at(-1);
    if (latest?.status === 'FAILED') return ['Falhou', 'waiting'];
    if (latest?.status === 'CANCELLED' || latest?.status === 'INTERRUPTED') return ['Interrompido', 'idle'];
    return [latest ? 'Concluído' : 'Pronto', 'ready'];
  };
  const renderGate = () => {
    let text;
    if (!state.projectId) text = 'Selecione um projeto autorizado.';
    else if (state.gateError) text = `Estado do servidor indisponível: ${state.gateError}. Envio bloqueado.`;
    else if (state.gate === null) text = 'Verificando o estado do servidor…';
    else if (!state.gate) text = 'Execução Development desativada neste servidor (inicie o Nexus com -EnableDevelopment). Você pode criar sessões e revisar, mas não enviar tarefas.';
    else text = '';
    setNote('native-gate', text, Boolean(state.gateError) || state.gate === false);
  };
  const renderModels = () => {
    const select = $('native-model');
    const previous = select.value;
    const prompt = element('option', '', state.modelsError ? 'Catálogo indisponível' : 'Selecione um modelo anunciado');
    prompt.value = '';
    select.replaceChildren(prompt, ...state.models.map(model => {
      const option = element('option', '', model.name && model.name !== model.id ? `${model.name} · ${model.id}` : model.id);
      option.value = model.id;
      return option;
    }));
    select.value = state.models.some(model => model.id === previous) ? previous : '';
    const runtimeLabel = RUNTIME_LABELS[selectedRuntime()];
    setNote('native-model-note', state.modelsError
      ? `Catálogo ${runtimeLabel} indisponível: ${state.modelsError}`
      : `${state.models.length} modelo(s) anunciado(s) pela conta local ${runtimeLabel}. Escolha um explicitamente.`, Boolean(state.modelsError));
    renderEfforts();
  };
  const renderEfforts = () => {
    const select = $('native-effort');
    const previous = select.value;
    const model = state.models.find(item => item.id === selectedModel());
    const efforts = model?.supported_efforts || [];
    const prompt = element('option', '', 'Esforço padrão do modelo');
    prompt.value = '';
    select.replaceChildren(prompt, ...efforts.map(effort => { const option = element('option', '', effort); option.value = effort; return option; }));
    select.value = efforts.includes(previous) ? previous : '';
  };
  const renderSessions = () => {
    const select = $('native-session');
    const prompt = element('option', '', state.sessionsError ? 'Sessões indisponíveis' : (state.sessions.length ? 'Selecione uma sessão' : 'Nenhuma sessão neste projeto'));
    prompt.value = '';
    select.replaceChildren(prompt, ...state.sessions.map(session => {
      const option = element('option', '', `${session.session_id} · ${backendLabel(session)} · ${session.status}`);
      option.value = session.session_id;
      return option;
    }));
    select.value = state.session?.session_id || '';
    const summary = $('native-session-summary');
    summary.textContent = !state.projectId ? 'Sessões · selecione um projeto'
      : state.session ? `Sessão · ${state.session.session_id}`
        : state.sessionsError ? 'Sessões · indisponíveis'
          : `Sessões · ${state.sessions.length} disponível(is)`;
    const list = $('native-session-list');
    if (!state.projectId || !state.sessions.length) {
      list.replaceChildren(element('p', 'native-note', !state.projectId
        ? 'Selecione um projeto autorizado para ver as sessões.'
        : state.sessionsError ? 'Não foi possível carregar as sessões.' : 'Nenhuma sessão ainda. Abra “+ Nova sessão” para criar uma.'));
    } else {
      list.replaceChildren(...state.sessions.map(session => {
        const button = element('button', `native-session-option${session.session_id === state.session?.session_id ? ' is-selected' : ''}`,
          `${session.session_id} · ${backendLabel(session)} · ${session.status}`);
        button.type = 'button';
        button.addEventListener('click', () => {
          $('native-session-menu').open = false;
          void selectSession(session.session_id);
        });
        return button;
      }));
    }
    const note = state.sessionsError ? { text: `Falha ao listar sessões: ${state.sessionsError}`, error: true } : state.sessionNote;
    setNote('native-session-note', note?.text, note?.error);
  };
  const renderLog = () => {
    const log = $('native-log');
    // The scrollable ancestor is .native-body, not #native-log itself --
    // replaceChildren() never touches its parent's scrollTop, so without
    // this the view silently falls behind every time a new turn is appended
    // and the reader is left looking at older content below their window.
    const scrollBody = pane.querySelector('.native-body');
    const stickToBottom = scrollBody ? scrollBody.scrollHeight - scrollBody.clientHeight - scrollBody.scrollTop < 32 : false;
    if (!state.session) {
      log.replaceChildren(element('div', 'cockpit-event', state.projectId ? 'Selecione uma sessão ou crie uma sessão Native.' : 'Selecione um projeto.'));
      return;
    }
    const items = state.turns.slice(-20).map(turn => {
      const item = element('div', 'cockpit-message');
      item.append(element('strong', '', `Você · ${turn.created_at || ''}`), element('p', 'native-text', clip(turn.prompt || '')));
      const response = element('div', 'cockpit-response');
      response.append(element('strong', '', `${backendLabel(state.session)} · ${turn.status}`));
      if (turn.response_text) response.append(element('p', 'native-text', clip(turn.response_text)));
      if (turn.error_code) response.append(element('p', 'cockpit-error-text', `${turn.error_code}: ${clip(turn.error_message || '', 600)}`));
      item.append(response);
      return item;
    });
    log.replaceChildren(...(items.length ? items : [element('div', 'cockpit-event', 'Nenhuma tarefa enviada nesta sessão.')]));
    if (scrollBody && stickToBottom) scrollBody.scrollTop = scrollBody.scrollHeight;
  };
  const renderLive = () => {
    const node = $('native-live');
    const live = state.live;
    node.hidden = !live;
    if (!live) { node.replaceChildren(); return; }
    node.replaceChildren(element('strong', '', `AO VIVO · ${live.status}`));
    if (live.text) node.append(element('pre', 'cockpit-live-text', clip(live.text)));
    if (live.activity) node.append(element('div', 'cockpit-event', clip(live.activity, 300)));
    if (live.notice) node.append(element('div', 'cockpit-error-text', live.notice));
  };
  const renderApprovals = () => {
    const list = $('native-approvals');
    const pending = pendingApprovals();
    $('native-approval-count').textContent = String(pending.length);
    $('native-settings').classList.toggle('has-approvals', pending.length > 0);
    const shown = [...pending, ...state.approvals.filter(item => item.status !== 'PENDING').slice(0, 5)];
    list.replaceChildren(...(shown.length ? shown.map(approval => {
      const summary = approval.summary || {};
      const isOperation = typeof summary.operation_sha256 === 'string';
      const row = element('div', `native-approval is-${String(approval.status).toLowerCase()}`);
      row.append(element('strong', '', `${approval.kind === 'file_change' ? 'Alteração de arquivo' : 'Comando'} · ${approval.status}`));
      [['Comando', summary.command], ['Diretório', summary.cwd], ['Ferramenta', summary.tool],
        ['Acesso de escrita', summary.grantRoot], ['Motivo', summary.reason]]
        .filter(([, value]) => value).forEach(([label, value]) => row.append(element('pre', 'native-approval-detail', `${label}: ${clip(value, 2000)}`)));
      if (approval.status === 'PENDING') {
        const detail = state.operationDetails.get(approval.approval_id);
        if (isOperation && !detail) {
          const reveal = element('button', 'cockpit-send', 'Ver alteração completa');
          reveal.type = 'button';
          reveal.disabled = state.busy.has(`operation:${approval.approval_id}`);
          reveal.addEventListener('click', () => { void loadOperationDetail(approval.approval_id); });
          row.append(reveal);
        } else if (isOperation && detail) {
          row.append(element('pre', 'native-approval-detail', clip(detail.operation, 4000)));
        }
        const actions = element('div', 'native-actions');
        [['accept', 'Aprovar uma vez'], ['decline', 'Recusar']].forEach(([decision, label]) => {
          const button = element('button', 'cockpit-send', label);
          button.type = 'button';
          // Claude Native: never approvable before the exact operation was
          // fetched and shown -- there is nothing to silently trust here.
          button.disabled = state.busy.has(`approval:${approval.approval_id}`) ||
            (decision === 'accept' && isOperation && !detail);
          button.addEventListener('click', () => { void decide(approval.approval_id, decision, detail?.operation_sha256); });
          actions.append(button);
        });
        row.append(actions);
      }
      return row;
    }) : [element('div', 'cockpit-event', 'Nenhuma solicitação de aprovação.')]));
  };
  const renderChanges = () => {
    const list = $('native-files');
    const patchBox = $('native-diff');
    const files = state.diff?.files || [];
    list.replaceChildren(...(files.length ? files.map(file => {
      const button = element('button', `native-file${file.path === state.selectedFile ? ' is-selected' : ''}`, `${file.status || '?'} ${file.path}`);
      button.type = 'button';
      button.addEventListener('click', () => { state.selectedFile = file.path; renderChanges(); });
      return button;
    }) : [element('div', 'cockpit-event', state.changesError ? `Falha ao carregar alterações: ${state.changesError}` : (state.session ? 'Sem alterações no worktree.' : 'Selecione uma sessão.'))]));
    const current = files.find(file => file.path === state.selectedFile) || files[0];
    if (current && !state.selectedFile) state.selectedFile = current.path;
    patchBox.hidden = !current;
    patchBox.textContent = current
      ? clip(current.patch || `${current.path}: ${current.reason || 'conteúdo omitido'}`)
      : '';
    $('native-diff-meta').textContent = state.diff
      ? `Base ${String(state.diff.base_sha || '?').slice(0, 10)} → ${String(state.diff.head_sha || '?').slice(0, 10)}${state.diff.truncated ? ' · diff truncado' : ''}`
      : '';
  };
  const renderReview = () => {
    const summary = $('native-review');
    if (!state.review) summary.textContent = 'Nenhuma revisão registrada nesta sessão do cockpit.';
    else {
      const snapshot = state.review.snapshot || {};
      summary.textContent = `Revisão ${String(state.review.review_id).slice(0, 12)} · ${state.review.status} · ${(snapshot.diff?.files || []).length} arquivo(s) · ${snapshot.approval_ready ? 'pronta para decisão humana' : 'NÃO revisável por completo'}`;
    }
    const snapshot = state.review?.snapshot || {};
    const files = snapshot.diff?.files || [];
    $('native-review-target').textContent = state.review
      ? `Destino: ${snapshot.target_branch || state.review.target_branch || 'desconhecido'} · HEAD ${String(snapshot.target_head || state.review.target_head || '?').slice(0, 12)}`
      : 'Destino: registre ou selecione uma revisão.';
    $('native-review-files').replaceChildren(...files.map(file => element('div', 'native-file',
      `${file.status || '?'} ${file.path}${file.truncated || file.content_omitted ? ` · ${file.reason || 'conteúdo incompleto'}` : ''}`)));
    $('native-review-diff').textContent = files.length
      ? files.map(file => `${file.status || '?'} ${file.path}\n${file.patch == null ? file.reason || 'Patch indisponível' : redact(file.patch)}`).join('\n\n')
      : 'Nenhum arquivo no snapshot.';
    $('native-preflight').textContent = state.preflight
      ? `Preflight ${state.preflight.ready ? 'PRONTO' : 'BLOQUEADO'}${(state.preflight.issues || []).length ? ` · ${(state.preflight.issues || []).map(issue => `${issue.code}${issue.message ? `: ${issue.message}` : ''}`).join('; ')}` : ''}`
      : '';
    const blockers = [];
    if (state.review) {
      if (state.reviewDecisionUncertain) blockers.push('Resultado da decisão incerto. Reabra a sessão e confira o histórico.');
      if (state.review.status !== 'REVIEWED') blockers.push(`Revisão ${state.review.status}: Apply indisponível.`);
      if (!reviewableSnapshot(state.review)) blockers.push('Snapshot ou diff incompleto.');
      if (state.activeTurnId) blockers.push('Execução ativa. Aguarde a conclusão.');
      if (state.integrationGate !== true) blockers.push('Integração desabilitada no servidor.');
      if (!state.preflight || state.preflight.review_id !== state.review.review_id) blockers.push('Preflight atual necessário.');
      else if (!state.preflight.ready) blockers.push(...(state.preflight.issues || []).map(issue => `${issue.code}: ${issue.message || 'bloqueado'}`));
    }
    $('native-review-blockers').textContent = blockers.join(' ');
    $('native-history').replaceChildren(...state.history.slice(0, 5).map(row => element('div', 'cockpit-event',
      `${String(row.review_id).slice(0, 12)} · ${row.status}${row.result_sha ? ` · commit ${String(row.result_sha).slice(0, 10)}` : ''}${row.error_message ? ` · ${row.error_message}` : ''}`)));
  };
  const renderControls = () => {
    const session = state.session;
    const native = isNative(session);
    const active = Boolean(state.activeTurnId);
    $('native-create').disabled = !state.projectId || !selectedModel() || state.busy.has('create');
    const canSend = Boolean(native && session.status === 'ACTIVE' && state.gate === true && !active && selectedModel() && state.fixedEffortValid &&
      (!fixed || selectedModel() === session.requested_model) &&
      $('native-prompt').value.trim() && !state.busy.has('send'));
    $('native-send').disabled = !canSend;
    $('native-prompt').disabled = !native || session?.status !== 'ACTIVE';
    $('native-stop').hidden = !active;
    $('native-stop').disabled = state.busy.has('stop');
    const files = state.diff?.files || [];
    $('native-review-create').disabled = !(native && session.status === 'ACTIVE' && !active && files.length &&
      !state.busy.has('review') && !state.busy.has('apply') && !state.busy.has('reject') && !state.reviewDecisionUncertain);
    const reviewed = Boolean(native && session?.status === 'ACTIVE' && state.review?.status === 'REVIEWED');
    const safeReview = Boolean(reviewed && !active && !state.reviewDecisionUncertain && reviewableSnapshot(state.review) &&
      !state.busy.has('apply') && !state.busy.has('reject'));
    $('native-review-preflight').disabled = !safeReview || state.busy.has('preflight');
    $('native-review-reject').disabled = !reviewed || active || state.reviewDecisionUncertain ||
      state.busy.has('apply') || state.busy.has('reject') || state.busy.has('preflight');
    $('native-review-apply').disabled = !(safeReview && state.integrationGate === true &&
      state.preflight?.ready === true && state.preflight.review_id === state.review.review_id &&
      $('native-apply-confirm').checked && !state.busy.has('preflight'));
    $('native-refresh').disabled = !session || state.busy.has('changes');
    let notice = '';
    if (session && !native) notice = 'Sessão OmniRoute legada: somente leitura aqui. Use a interface Development para operá-la.';
    else if (native && session.status !== 'ACTIVE') notice = `Sessão ${session.status}: envio indisponível.`;
    else if (native && !selectedModel()) notice = 'Escolha um modelo anunciado para enviar tarefas.';
    setNote('native-compose-note', notice);
  };
  const renderChrome = () => {
    const [label, tone] = paneStatus();
    const badge = $('native-status');
    badge.lastChild.textContent = ` ${label}`;
    badge.className = `pane-status ${tone}`;
    $('native-connection').textContent = ({ idle: state.session ? 'PRONTO' : 'OFFLINE', live: 'AO VIVO', reconnecting: 'RECONECTANDO', polling: 'CONSULTANDO' })[state.connection] || 'OFFLINE';
    $('native-meta-model').textContent = state.session?.requested_model || selectedModel() || '—';
    $('native-meta-effort').textContent = $('native-effort').value ? `${$('native-effort').value} · próximo turno` : 'por turno';
    $('native-meta-session').textContent = state.session?.session_id || '—';
    $('native-meta-backend').textContent = state.session ? (isNative(state.session) ? state.session.backend : 'omniroute · legado') : selectedRuntime();
    const backend = state.session?.backend || selectedRuntime();
    const runtimeLabel = backendLabel({ backend });
    const nextTitle = `${runtimeLabel} · ${state.session?.session_id || fixed?.sessionId || 'sem sessão'}`;
    const titleChanged = pane.dataset.title !== nextTitle;
    pane.dataset.title = nextTitle;
    pane.setAttribute('aria-label', `Pane ${pane.dataset.title}`);
    const titleNode = pane.querySelector('.pane-identity strong');
    if (titleNode) titleNode.textContent = runtimeLabel;
    const icon = pane.querySelector('.pane-icon');
    if (icon) icon.textContent = backend === 'claude_native' ? 'CL' : 'CX';
    const runtimeTitle = pane.querySelector('.native-runtime-copy strong');
    if (runtimeTitle?.firstChild) runtimeTitle.firstChild.textContent = `${runtimeLabel} `;
    pane.querySelectorAll('.minimize, .expand').forEach(button => {
      button.setAttribute('aria-label', `${button.classList.contains('minimize') ? 'Minimizar' : 'Expandir'} ${runtimeLabel}`);
    });
    $('native-prompt').setAttribute('aria-label', `Tarefa para ${runtimeLabel}`);
    if (titleChanged) host.refreshPaneLabels?.();
    $('native-foot').textContent = state.session?.worktree_path || state.session?.branch || 'sem worktree ativo';
    $('native-foot').title = state.session?.worktree_path || state.session?.branch || '';
    $('native-runtime-model').textContent = state.session?.requested_model || 'Selecione uma sessão';
    $('native-runtime-worktree').textContent = state.session?.branch
      ? `worktree · ${state.session.branch}` : 'Worktree isolado · sem projeto';
    $('native-settings-status').textContent = state.session
      ? `${state.session.session_id} · ${selectedModel() || state.session.requested_model || 'modelo pendente'}` : 'sem sessão';
  };
  const render = () => {
    renderGate(); renderModels(); renderSessions(); renderLog(); renderLive(); renderApprovals(); renderChanges(); renderReview(); renderControls(); renderChrome();
    if (state.session && !fixed) host.setPaneAvailable?.('native', true);
  };

  // ---- loading ------------------------------------------------------------
  const closeStream = () => {
    if (state.stream) { state.stream.source.close(); state.stream = null; }
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  };
  const stopPolling = () => { clearTimeout(state.pollTimer); state.pollTimer = null; };
  const resetSession = () => {
    closeStream(); stopPolling();
    Object.assign(state, { session: null, turns: [], approvals: [], files: [], diff: null, changesError: '', selectedFile: null,
      review: null, preflight: null, history: [], reviewDecisionUncertain: false,
      activeTurnId: null, live: null, cursor: null, connection: 'idle' });
    state.seenApprovals = new Set();
    state.operationDetails = new Map();
    state.busy.clear();
    $('native-apply-confirm').checked = false;
    state.sessionNote = null; state.approvalPollFailed = false;
    setNote('native-error', '');
    setNote('native-approval-note', '');
  };
  const loadChanges = async generation => {
    if (!state.session) return;
    state.busy.add('changes'); renderControls();
    try {
      const [changed, diff] = await Promise.all([api(sessionPath('/changed-files')), api(sessionPath('/diff'))]);
      if (stale(generation)) return;
      state.files = changed.files || [];
      state.diff = diff;
      state.changesError = '';
      if (state.preflight) { state.preflight = null; $('native-apply-confirm').checked = false; }
    } catch (error) {
      if (stale(generation)) return;
      state.changesError = error.message;
      state.diff = null;
    } finally {
      if (!stale(generation)) { state.busy.delete('changes'); renderChanges(); renderReview(); renderControls(); }
    }
  };
  const loadApprovals = async generation => {
    if (!state.session) return;
    try {
      const data = await api(sessionPath('/approvals'));
      if (stale(generation)) return;
      state.approvals = data.approvals || [];
      const fresh = pendingApprovals().filter(item => !state.seenApprovals.has(item.approval_id));
      fresh.forEach(item => state.seenApprovals.add(item.approval_id));
      if (fresh.length) {
        $('native-settings').open = true;
        $('native-approvals-section').open = true;
        host.notify(`${backendLabel(state.session)} aguarda sua aprovação`);
      }
      if (state.approvalPollFailed) { state.approvalPollFailed = false; setNote('native-approval-note', ''); }
    } catch (error) {
      if (stale(generation)) return;
      state.approvalPollFailed = true;
      setNote('native-approval-note', `Falha ao consultar aprovações: ${error.message}`, true);
    }
    renderApprovals(); renderChrome();
  };
  const loadHistory = async generation => {
    if (!state.session) return;
    try {
      const data = await api(sessionPath('/integrations'));
      if (!stale(generation)) {
        state.history = data.integrations || [];
        const persisted = state.review
          ? state.history.find(row => row.review_id === state.review.review_id)
          : state.history[0];
        if (persisted) {
          if (state.review?.status !== persisted.status) { state.preflight = null; $('native-apply-confirm').checked = false; }
          state.review = persisted;
          if (persisted.status !== 'REVIEWED') state.reviewDecisionUncertain = false;
        }
        renderReview(); renderControls();
      }
    } catch (_error) { /* history is informational; the review controls stay explicit */ }
  };
  const loadTurns = async generation => {
    const data = await api(sessionPath('/turns'));
    if (stale(generation)) return null;
    state.turns = data.turns || [];
    return state.turns.at(-1) || null;
  };
  const finishTurn = async (generation, turnId) => {
    if (stale(generation) || state.activeTurnId !== turnId) return;
    closeStream(); stopPolling();
    state.activeTurnId = null; state.live = null; state.connection = 'idle';
    try { await loadTurns(generation); } catch (error) { setNote('native-error', `Falha ao consultar a tarefa: ${error.message}`, true); }
    await Promise.all([loadChanges(generation), loadApprovals(generation), loadHistory(generation)]);
    if (!stale(generation)) render();
  };
  const reconcile = async (generation, turnId) => {
    if (stale(generation) || state.activeTurnId !== turnId) return;
    try {
      const turn = await api(sessionPath(`/turns/${encodeURIComponent(turnId)}`));
      if (stale(generation) || state.activeTurnId !== turnId) return;
      if (TERMINAL.includes(turn.status)) { await finishTurn(generation, turnId); return; }
      await loadApprovals(generation);
    } catch (error) {
      if (!stale(generation)) setNote('native-error', `Falha ao consultar a tarefa: ${error.message}. O servidor continua com o registro dela.`, true);
    }
    schedulePoll(generation, turnId);
  };
  const schedulePoll = (generation, turnId) => {
    stopPolling();
    if (stale(generation) || state.activeTurnId !== turnId) return;
    state.pollTimer = setTimeout(() => { void reconcile(generation, turnId); }, POLL_MS);
  };

  const processEvent = (stream, raw) => {
    if (state.stream !== stream || stale(stream.generation)) return;
    if (raw.lastEventId) {
      if (stream.seen.has(raw.lastEventId)) return;
      stream.seen.add(raw.lastEventId);
      if (stream.seen.size > 1000) stream.seen.delete(stream.seen.values().next().value);
      state.cursor = { turnId: stream.turnId, id: raw.lastEventId };
    }
    let event;
    try { event = JSON.parse(raw.data); } catch (_error) { return; }
    if (!event || typeof event !== 'object') return;
    const type = event.type || event.method || '';
    const params = event.params || {};
    if (/requestApproval$/.test(type)) void loadApprovals(stream.generation);
    if (['nexus/replayGap', 'nexus/turnSnapshot', 'nexus/turnEnded', 'nexus/noTurn'].includes(type) || /turn\/(completed|failed|interrupted)$/.test(type)) {
      closeStream();
      if (type === 'nexus/replayGap') state.live = { status: 'RECONCILING', text: '', notice: 'Eventos ao vivo expiraram; consultando o registro persistido.' };
      renderLive();
      void reconcile(stream.generation, stream.turnId);
      return;
    }
    const live = state.live || { status: 'RUNNING', text: '' };
    if (/agentMessage.*delta|delta.*agentMessage/i.test(type)) {
      const delta = params.delta || params.textDelta;
      if (typeof delta === 'string') live.text = (live.text + delta).slice(-TEXT_LIMIT);
    } else if (/item\/completed$/.test(type) && params.item?.type === 'agentMessage' && typeof params.item.text === 'string') {
      live.text = params.item.text.slice(-TEXT_LIMIT);
    } else if (/item\/(started|updated|completed)$/.test(type) && params.item?.type && params.item.type !== 'agentMessage') {
      live.activity = `${params.item.type}${params.item.command ? `: ${params.item.command}` : ''}${params.item.status ? ` · ${params.item.status}` : ''}`;
    } else if (/turn\/started$/.test(type)) live.status = 'RUNNING';
    else return;
    state.live = live;
    renderLive();
  };
  const openStream = (generation, turnId) => {
    closeStream();
    if (typeof window.EventSource !== 'function') {
      state.connection = 'polling';
      setNote('native-error', 'Este navegador não suporta EventSource; o estado é consultado periodicamente.', true);
      schedulePoll(generation, turnId);
      renderChrome();
      return;
    }
    const resume = state.cursor?.turnId === turnId ? `&cursor=${encodeURIComponent(state.cursor.id)}` : '';
    const source = new window.EventSource(`${sessionPath('/events')}?turn_id=${encodeURIComponent(turnId)}${resume}`);
    const stream = { source, seen: new Set(), generation, turnId };
    state.stream = stream;
    state.connection = 'live';
    state.live = state.live || { status: 'RUNNING', text: '' };
    source.onmessage = raw => processEvent(stream, raw);
    source.onerror = () => {
      if (state.stream !== stream) return;
      state.connection = 'reconnecting';
      renderChrome();
      if (source.readyState === window.EventSource.CLOSED) {
        closeStream();
        state.reconnectTimer = setTimeout(() => {
          if (!stale(generation) && state.activeTurnId === turnId && !state.stream) openStream(generation, turnId);
        }, 1000);
      }
      schedulePoll(generation, turnId);
    };
    schedulePoll(generation, turnId);
    renderChrome();
  };
  const watchTurn = (generation, turnId) => {
    state.activeTurnId = turnId;
    openStream(generation, turnId);
    void loadApprovals(generation);
  };

  const selectSession = async sessionId => {
    state.generation++;
    const generation = state.generation;
    resetSession();
    if (!sessionId) { render(); return; }
    state.busy.add('session'); render();
    try {
      const session = await api(`/api/dev/sessions/${encodeURIComponent(sessionId)}`);
      if (stale(generation)) return;
      if (fixed && (session.session_id !== fixed.sessionId || session.backend !== fixed.backend || session.project_id !== fixed.projectId)) {
        throw new Error('Identidade ou backend da sessão divergente do pane persistido.');
      }
      state.session = session;
      if (state.models.some(model => model.id === session.requested_model)) $('native-model').value = session.requested_model;
      const latest = await loadTurns(generation);
      if (stale(generation)) return;
      if (turnIsActive(latest) && isNative(session)) watchTurn(generation, latest.turn_id);
      state.busy.delete('session');
      render();
      await Promise.all([loadChanges(generation), loadApprovals(generation), loadHistory(generation)]);
    } catch (error) {
      if (stale(generation)) return;
      state.session = null;
      setSessionNote(`Não foi possível abrir a sessão: ${error.message}`, true);
    } finally {
      if (!stale(generation)) { state.busy.delete('session'); render(); }
    }
  };
  const loadModelsFor = async (generation, backend) => {
    try {
      const data = await api(`/api/dev/models?backend=${backend}`);
      if (stale(generation)) return;
      state.models = (data.models || []).filter(model => model.selectable === true && typeof model.id === 'string');
      state.defaultModel = state.models.some(model => model.id === data.default_model) ? data.default_model : null;
      state.modelsError = data.catalog_error || '';
    } catch (error) {
      if (!stale(generation)) { state.models = []; state.defaultModel = null; state.modelsError = error.message; }
    }
  };
  const loadProject = async projectId => {
    state.generation++;
    const generation = state.generation;
    resetSession();
    Object.assign(state, { projectId, gate: null, integrationGate: null, gateError: '', models: [], modelsError: '', sessions: [], sessionsError: '' });
    render();
    if (!projectId) return;
    const settle = (promise, apply, fail) => promise.then(data => { if (!stale(generation)) apply(data); },
      error => { if (!stale(generation)) fail(error.message); });
    await Promise.all([
      settle(api('/api/dev/execution-status'), data => { state.gate = data.execution_enabled === true; }, message => { state.gate = false; state.gateError = message; }),
      settle(api('/api/dev/integration-status'), data => { state.integrationGate = data.integration_enabled === true; }, () => { state.integrationGate = false; }),
      loadModelsFor(generation, selectedRuntime()),
      settle(api(`/api/dev/sessions?project_id=${encodeURIComponent(projectId)}`), data => { state.sessions = data.sessions || []; },
        message => { state.sessionsError = message; }),
    ]);
    if (stale(generation)) return;
    render();
    if (fixed) {
      await selectSession(fixed.sessionId);
      if (state.session && fixed.effort) {
        const choice = $('native-effort');
        state.fixedEffortValid = [...choice.children].some(option => option.value === fixed.effort);
        if (state.fixedEffortValid) choice.value = fixed.effort;
        else setNote('native-error', 'Effort persistido não está mais no catálogo. Envio bloqueado.', true);
        renderControls(); renderChrome();
      }
    }
  };

  // ---- explicit actions ---------------------------------------------------
  const switchRuntime = async () => {
    if (!state.projectId) return;
    const generation = state.generation;
    state.models = []; state.modelsError = ''; renderModels();
    await loadModelsFor(generation, selectedRuntime());
    if (!stale(generation)) { renderModels(); renderControls(); }
  };
  const createSession = async () => {
    const model = selectedModel();
    const backend = selectedRuntime();
    if (!state.projectId || !model || state.busy.has('create')) return;
    const generation = state.generation;
    state.busy.add('create'); setSessionNote(`Criando sessão ${RUNTIME_LABELS[backend]}…`); renderControls();
    try {
      const session = await api('/api/dev/sessions', { method: 'POST', body: { project_id: state.projectId, backend, requested_model: model } });
      if (stale(generation)) return;
      state.sessions = [session, ...state.sessions.filter(item => item.session_id !== session.session_id)];
      state.busy.delete('create');
      await selectSession(session.session_id);
      setSessionNote(`Sessão ${session.session_id} criada em worktree isolado.`);
    } catch (error) {
      if (stale(generation)) return;
      setSessionNote(`Não foi possível criar a sessão: ${error.message}. Confira a lista antes de tentar novamente.`, true);
    } finally {
      state.busy.delete('create');
      if (!stale(generation)) { renderSessions(); renderControls(); }
    }
  };
  const sendTask = async () => {
    const session = state.session;
    const prompt = $('native-prompt').value.trim();
    if (!session || !isNative(session) || session.status !== 'ACTIVE' || state.gate !== true || state.activeTurnId ||
        !prompt || !selectedModel() || !state.fixedEffortValid ||
        (fixed && selectedModel() !== session.requested_model) || state.busy.has('send')) return;
    const generation = state.generation;
    const body = { prompt, model: selectedModel(), ...($('native-effort').value ? { effort: $('native-effort').value } : {}) };
    state.busy.add('send'); setNote('native-error', ''); renderControls();
    try {
      const turn = await api(sessionPath('/turns'), { method: 'POST', body });
      if (stale(generation)) return;
      if ($('native-prompt').value.trim() === prompt) $('native-prompt').value = '';
      state.turns = [...state.turns, turn];
      state.live = { status: 'RUNNING', text: '' };
      watchTurn(generation, turn.turn_id);
    } catch (error) {
      if (stale(generation)) return;
      // The draft is kept and the POST is never repeated automatically.
      setNote('native-error', `${error.message}. O resultado pode ser incerto; confira as tarefas da sessão antes de enviar de novo.`, true);
    } finally {
      state.busy.delete('send');
      if (!stale(generation)) render();
    }
  };
  const stopTask = async () => {
    if (!state.session || !state.activeTurnId || state.busy.has('stop')) return;
    const generation = state.generation;
    state.busy.add('stop'); renderControls();
    try {
      const result = await api(sessionPath(`/interrupt?turn_id=${encodeURIComponent(state.activeTurnId)}`), { method: 'POST' });
      setNote('native-error', `Interrupção: ${result.status || 'solicitada'}`, result.status === 'INTERRUPT_FAILED');
    } catch (error) {
      if (!stale(generation)) setNote('native-error', `Não foi possível interromper: ${error.message}`, true);
    } finally {
      state.busy.delete('stop');
      if (!stale(generation)) renderControls();
    }
  };
  async function loadOperationDetail(approvalId) {
    const key = `operation:${approvalId}`;
    if (!state.session || state.busy.has(key)) return;
    const generation = state.generation;
    state.busy.add(key); renderApprovals();
    try {
      const detail = await api(sessionPath(`/approvals/${encodeURIComponent(approvalId)}/operation`));
      if (!stale(generation)) state.operationDetails.set(approvalId, detail);
    } catch (error) {
      if (!stale(generation)) setNote('native-approval-note', `Não foi possível carregar a alteração: ${error.message}`, true);
    } finally {
      state.busy.delete(key);
      if (!stale(generation)) renderApprovals();
    }
  }
  async function decide(approvalId, decision, operationSha256) {
    const key = `approval:${approvalId}`;
    if (!state.session || state.busy.has(key)) return;
    const generation = state.generation;
    state.busy.add(key); renderApprovals();
    try {
      const body = operationSha256 ? { decision, operation_sha256: operationSha256 } : { decision };
      await api(sessionPath(`/approvals/${encodeURIComponent(approvalId)}`), { method: 'POST', body });
      if (!stale(generation)) setNote('native-approval-note', decision === 'accept' ? 'Aprovado uma vez.' : 'Recusado.');
    } catch (error) {
      if (!stale(generation)) setNote('native-approval-note', `Não foi possível registrar a decisão: ${error.message}`, true);
    } finally {
      state.busy.delete(key);
      state.operationDetails.delete(approvalId);
      await loadApprovals(generation);
    }
  }
  const createReview = async () => {
    if (!state.session || state.busy.has('review')) return;
    const generation = state.generation;
    state.busy.add('review'); setNote('native-review-note', 'Registrando o snapshot de revisão…'); renderControls();
    try {
      const review = await api(sessionPath('/reviews'), { method: 'POST', body: {} });
      if (stale(generation)) return;
      state.review = review; state.preflight = null; state.reviewDecisionUncertain = false;
      $('native-apply-confirm').checked = false;
      setNote('native-review-note', 'Snapshot registrado. Inspecione o diff e o destino antes de decidir.');
      await loadHistory(generation);
    } catch (error) {
      if (!stale(generation)) setNote('native-review-note', `Não foi possível registrar a revisão: ${error.message}`, true);
    } finally {
      state.busy.delete('review');
      if (!stale(generation)) { renderReview(); renderControls(); }
    }
  };
  const checkPreflight = async () => {
    if (!state.session || !state.review || state.review.status !== 'REVIEWED' || state.reviewDecisionUncertain ||
        state.activeTurnId || state.busy.has('preflight')) return;
    const generation = state.generation;
    state.busy.add('preflight'); renderControls();
    try {
      const result = await api(sessionPath(`/preflight?review_id=${encodeURIComponent(state.review.review_id)}`));
      if (!stale(generation)) {
        state.preflight = result;
        if (typeof result.gate_enabled === 'boolean') state.integrationGate = result.gate_enabled;
        $('native-apply-confirm').checked = false;
        setNote('native-review-note', '');
      }
    } catch (error) {
      if (!stale(generation)) { state.preflight = null; $('native-apply-confirm').checked = false; setNote('native-review-note', `Preflight falhou: ${error.message}`, true); }
    } finally {
      state.busy.delete('preflight');
      if (!stale(generation)) { renderReview(); renderControls(); }
    }
  };
  const applyReview = async () => {
    const review = state.review;
    if (!state.session || !isNative(state.session) || state.session.status !== 'ACTIVE' ||
        !review || review.status !== 'REVIEWED' || state.reviewDecisionUncertain || !reviewableSnapshot(review) ||
        state.integrationGate !== true || state.activeTurnId ||
        !state.preflight?.ready || state.preflight.review_id !== review.review_id ||
        !$('native-apply-confirm').checked || state.busy.has('apply')) return;
    const generation = state.generation;
    let submitted = false;
    state.busy.add('apply'); renderControls();
    try {
      // Read current server preflight immediately before the explicit Apply.
      // apply_review independently revalidates again under its project lock.
      const current = await api(sessionPath(`/preflight?review_id=${encodeURIComponent(review.review_id)}`));
      if (stale(generation)) return;
      state.preflight = current;
      if (typeof current.gate_enabled === 'boolean') state.integrationGate = current.gate_enabled;
      if (!current.ready || state.integrationGate !== true || current.review_id !== review.review_id) {
        $('native-apply-confirm').checked = false;
        setNote('native-review-note', 'Aplicação bloqueada pelo preflight atual.', true);
        return;
      }
      if (state.activeTurnId || state.review?.review_id !== review.review_id || !$('native-apply-confirm').checked) return;
      submitted = true;
      const result = await api(sessionPath('/apply'), { method: 'POST', body: { review_id: review.review_id, confirmed: true } });
      if (stale(generation)) return;
      state.review = { ...review, ...result };
      state.preflight = null; state.reviewDecisionUncertain = false; $('native-apply-confirm').checked = false;
      setNote('native-review-note', `Alterações aplicadas em ${result.branch || review.target_branch || 'destino'} · commit ${result.commit_sha || 'indisponível'}.`);
      await Promise.all([loadHistory(generation), loadChanges(generation)]);
    } catch (error) {
      if (stale(generation)) return;
      state.preflight = null; $('native-apply-confirm').checked = false;
      if (submitted) state.reviewDecisionUncertain = true;
      setNote('native-review-note', `${submitted ? 'Resultado do Apply incerto' : 'Aplicação bloqueada'}: ${error.message}. Consulte o histórico antes de qualquer nova decisão.`, true);
      if (submitted) await loadHistory(generation);
    } finally {
      state.busy.delete('apply');
      if (!stale(generation)) { renderReview(); renderControls(); }
    }
  };
  const rejectReview = async () => {
    const review = state.review;
    if (!state.session || !isNative(state.session) || state.session.status !== 'ACTIVE' ||
        !review || review.status !== 'REVIEWED' || state.reviewDecisionUncertain ||
        state.activeTurnId || state.busy.has('reject')) return;
    const generation = state.generation;
    state.busy.add('reject'); renderControls();
    try {
      const result = await api(sessionPath('/reject'), { method: 'POST', body: { review_id: review.review_id } });
      if (stale(generation)) return;
      state.review = { ...review, ...result };
      state.preflight = null; state.reviewDecisionUncertain = false; $('native-apply-confirm').checked = false;
      setNote('native-review-note', 'Revisão rejeitada. Nenhuma alteração foi aplicada.');
      await loadHistory(generation);
    } catch (error) {
      if (stale(generation)) return;
      state.preflight = null; $('native-apply-confirm').checked = false;
      state.reviewDecisionUncertain = true;
      setNote('native-review-note', `Resultado da rejeição incerto: ${error.message}. Consulte o histórico antes de tentar novamente.`, true);
      await loadHistory(generation);
    } finally {
      state.busy.delete('reject');
      if (!stale(generation)) { renderReview(); renderControls(); }
    }
  };

  $('native-session').addEventListener('change', event => { void selectSession(event.currentTarget.value); });
  $('native-model').addEventListener('change', () => { renderEfforts(); renderControls(); renderChrome(); });
  $('native-effort').addEventListener('change', () => { renderControls(); renderChrome(); });
  $('native-runtime')?.addEventListener('change', () => { void switchRuntime(); });
  $('native-create').addEventListener('click', () => { void createSession(); });
  $('native-composer').addEventListener('submit', event => { event.preventDefault(); void sendTask(); });
  $('native-prompt').addEventListener('input', renderControls);
  $('native-prompt').addEventListener('keydown', event => {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    $('native-composer').requestSubmit();
  });
  $('native-stop').addEventListener('click', () => { void stopTask(); });
  $('native-refresh').addEventListener('click', () => { void loadChanges(state.generation); void loadApprovals(state.generation); });
  $('native-review-create').addEventListener('click', () => { void createReview(); });
  $('native-review-preflight').addEventListener('click', () => { void checkPreflight(); });
  $('native-apply-confirm').addEventListener('change', renderControls);
  $('native-review-apply').addEventListener('click', () => { void applyReview(); });
  $('native-review-reject').addEventListener('click', () => { void rejectReview(); });

  if (!fixed) host.onProject(projectId => { void loadProject(projectId); });
  else {
    $('native-session-menu').hidden = true;
    $('native-new').hidden = true;
    $('native-model').disabled = true;
    $('native-effort').disabled = true;
    $('native-runtime').value = fixed.backend;
    void loadProject(fixed.projectId);
  }
  render();
  return {
    dispose: () => { state.generation++; closeStream(); stopPolling(); },
  };
  };
  window.NexusNativePane = Object.freeze({ mount });
  const systemPane = document.getElementById('native-pane');
  if (systemPane) mount(systemPane);
})();
