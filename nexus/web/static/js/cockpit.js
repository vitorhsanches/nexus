(() => {
  const grid = document.getElementById('pane-grid');
  const paneNav = document.getElementById('pane-nav');
  const toast = document.getElementById('toast');
  const modelCatalogList = document.getElementById('model-catalog-list');
  const modelCatalogStatus = document.getElementById('model-catalog-status');
  const modelCatalogCount = document.getElementById('model-catalog-count');
  const modelCatalogProjectNote = document.getElementById('model-catalog-project-note');
  const modelCatalogRefreshButton = document.getElementById('model-catalog-refresh');
  const costAuthModel = document.getElementById('cost-auth-model');
  const costAuthList = document.getElementById('cost-auth-list');
  const costAuthCreate = document.getElementById('cost-auth-create');
  const groupControls = document.getElementById('group-controls');
  const savedLayouts = new Map();
  const gap = 6;
  const minimumWidth = 300;
  const minimumHeight = 210;
  const agentTemplate = grid.querySelector('[data-pane="agent-a"]').cloneNode(true);
  grid.querySelector('[data-pane="agent-a"]').remove();
  grid.querySelector('[data-pane="agent-b"]').remove();
  const knownAgents = new Map();
  const closedAgents = new Set();
  const pending = new Set();
  const streams = new Map();
  const liveTurns = new Map();
  const settledStreams = new Map();
  const streamCursors = new Map();
  const reconnectHold = new Set();
  const interruptRequests = new Set();
  const maxStreams = 4;
  const maestroDrafts = new Map();
  let workspaceState = null;
  let maestroState = null;
  let modelCatalog = [];
  let discoveredModelCatalog = [];
  let modelCatalogError = '';
  let modelCatalogRequest = 0;
  let costAuthorizations = [];
  let testProfiles = [];
  let testProfilesLoading = false;
  let testProfilesError = '';
  let projectId = null;
  let projects = [];
  let projectsLoaded = false;
  let projectsError = '';
  let refreshTimer;
  let refreshGeneration = 0;
  let refreshRequest = 0;
  const panes = () => [...grid.querySelectorAll(':scope > .pane')];
  const twoDigits = number => String(number).padStart(2, '0');
  let toastTimer;
  let groupIndex = 0;
  let groupCapacity = 4;
  let layout = null;

  const notify = message => {
    toast.textContent = message;
    toast.classList.add('visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('visible'), 2600);
  };

  panes().forEach(pane => {
    pane.dataset.title = ({ maestro: 'Maestro', review: 'Revisão' })[pane.dataset.pane];
  });
  const paneTitle = pane => pane.dataset.title;

  const makeNavButton = pane => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'pane-nav-link';
    button.dataset.paneTarget = pane.dataset.pane;
    const dot = document.createElement('span');
    dot.className = 'pane-nav-dot';
    const label = document.createElement('span');
    label.className = 'pane-nav-name';
    label.textContent = paneTitle(pane);
    button.append(dot, label);
    return button;
  };

  const updateSidebar = () => {
    const existing = new Map([...paneNav.children].map(button => [button.dataset.paneTarget, button]));
    panes().forEach(pane => {
      const button = existing.get(pane.dataset.pane) || makeNavButton(pane);
      button.classList.toggle('active', pane.classList.contains('selected'));
      button.classList.toggle('is-minimized', pane.classList.contains('minimized'));
      button.querySelector('.pane-nav-name').textContent = paneTitle(pane);
      if (pane.classList.contains('selected')) button.setAttribute('aria-current', 'true');
      else button.removeAttribute('aria-current');
      paneNav.append(button);
      existing.delete(pane.dataset.pane);
    });
    existing.forEach(button => button.remove());
  };

  const mobileLayout = () => window.matchMedia('(max-width: 650px)').matches;
  const clearDividers = () => grid.querySelectorAll(':scope > .grid-divider').forEach(divider => divider.remove());
  const fitTracks = (fractions, total, minimum) => {
    if (total < fractions.length * minimum) return fractions.map(() => total / fractions.length);
    const tracks = fractions.map(fraction => fraction * total);
    for (let pass = 0; pass < tracks.length; pass++) {
      const small = tracks.map((size, index) => size < minimum ? index : -1).filter(index => index >= 0);
      if (!small.length) break;
      const deficit = small.reduce((sum, index) => sum + minimum - tracks[index], 0);
      small.forEach(index => { tracks[index] = minimum; });
      const donors = tracks.map((size, index) => !small.includes(index) && size > minimum ? index : -1).filter(index => index >= 0);
      const room = donors.reduce((sum, index) => sum + tracks[index] - minimum, 0);
      if (!room) break;
      donors.forEach(index => { tracks[index] -= deficit * (tracks[index] - minimum) / room; });
    }
    return tracks;
  };
  const positionDividers = () => {
    if (!layout) return;
    grid.querySelectorAll(':scope > .grid-divider').forEach(divider => {
      const index = Number(divider.dataset.index);
      if (divider.dataset.axis === 'columns') {
        const x = layout.columnPixels.slice(0, index + 1).reduce((a, b) => a + b, 0) + gap * index + gap / 2;
        divider.style.left = `${x}px`;
      } else {
        const y = layout.rowPixels.slice(0, index + 1).reduce((a, b) => a + b, 0) + gap * index + gap / 2;
        divider.style.top = `${y}px`;
      }
    });
  };
  const applyTracks = () => {
    if (!layout) return;
    grid.style.gridTemplateColumns = layout.columnPixels.map(value => `${value}px`).join(' ');
    grid.style.gridTemplateRows = layout.rowPixels.map(value => `${value}px`).join(' ');
    positionDividers();
  };
  const rememberTracks = () => {
    if (!layout) return;
    const columnTotal = layout.columnPixels.reduce((a, b) => a + b, 0);
    const rowTotal = layout.rowPixels.reduce((a, b) => a + b, 0);
    savedLayouts.set(layout.key, {
      columns: layout.columnPixels.map(value => value / columnTotal),
      rows: layout.rowPixels.map(value => value / rowTotal)
    });
  };
  const resizePair = (axis, index, desiredFirst) => {
    const tracks = axis === 'columns' ? layout.columnPixels : layout.rowPixels;
    const minimum = axis === 'columns' ? minimumWidth : minimumHeight;
    const pairTotal = tracks[index] + tracks[index + 1];
    const next = Math.max(minimum, Math.min(pairTotal - minimum, desiredFirst));
    tracks[index] = next;
    tracks[index + 1] = pairTotal - next;
    applyTracks();
  };
  const makeDivider = (axis, index) => {
    const divider = document.createElement('div');
    divider.className = `grid-divider ${axis === 'columns' ? 'vertical' : 'horizontal'}`;
    divider.dataset.axis = axis;
    divider.dataset.index = String(index);
    divider.setAttribute('role', 'separator');
    divider.setAttribute('tabindex', '0');
    divider.setAttribute('aria-orientation', axis === 'columns' ? 'vertical' : 'horizontal');
    divider.setAttribute('aria-label', axis === 'columns' ? 'Redimensionar colunas vizinhas' : 'Redimensionar linhas vizinhas');
    divider.addEventListener('pointerdown', event => {
      if (event.button !== 0 || !layout) return;
      event.preventDefault();
      divider.setPointerCapture(event.pointerId);
      grid.classList.add('is-resizing');
      const tracks = axis === 'columns' ? layout.columnPixels : layout.rowPixels;
      const first = tracks[index];
      const start = axis === 'columns' ? event.clientX : event.clientY;
      const move = pointer => resizePair(axis, index, first + (axis === 'columns' ? pointer.clientX : pointer.clientY) - start);
      const finish = () => {
        divider.removeEventListener('pointermove', move);
        divider.removeEventListener('pointerup', finish);
        divider.removeEventListener('pointercancel', finish);
        grid.classList.remove('is-resizing');
        rememberTracks();
      };
      divider.addEventListener('pointermove', move);
      divider.addEventListener('pointerup', finish);
      divider.addEventListener('pointercancel', finish);
    });
    divider.addEventListener('keydown', event => {
      const direction = axis === 'columns' ? { ArrowLeft: -16, ArrowRight: 16 } : { ArrowUp: -16, ArrowDown: 16 };
      if (!(event.key in direction) || !layout) return;
      event.preventDefault();
      const tracks = axis === 'columns' ? layout.columnPixels : layout.rowPixels;
      resizePair(axis, index, tracks[index] + direction[event.key]);
      rememberTracks();
    });
    grid.append(divider);
  };
  const drawDividers = () => {
    clearDividers();
    if (!layout || mobileLayout() || grid.classList.contains('has-maximized')) return;
    for (let index = 0; index < layout.columns - 1; index++) makeDivider('columns', index);
    for (let index = 0; index < layout.rows - 1; index++) makeDivider('rows', index);
    positionDividers();
  };
  const updateLayout = () => {
    const all = panes();
    if (mobileLayout()) {
      all.forEach(pane => { pane.hidden = false; });
      groupControls.hidden = true;
      grid.style.removeProperty('grid-template-columns');
      grid.style.removeProperty('grid-template-rows');
      grid.dataset.visible = String(all.length);
      document.getElementById('grid-layout-label').textContent = '▦  Empilhado';
      layout = null;
      clearDividers();
      return;
    }
    const maxColumns = Math.max(1, Math.min(4, Math.floor((grid.clientWidth + gap) / (minimumWidth + gap))));
    const maxRows = grid.clientHeight >= 2 * minimumHeight + gap ? 2 : 1;
    groupCapacity = maxColumns * maxRows;
    const groupCount = Math.max(1, Math.ceil(all.length / groupCapacity));
    groupIndex = Math.max(0, Math.min(groupIndex, groupCount - 1));
    const visible = all.slice(groupIndex * groupCapacity, (groupIndex + 1) * groupCapacity);
    const visibleSet = new Set(visible);
    all.forEach(pane => { pane.hidden = !visibleSet.has(pane); });
    const columns = visible.length === 4 && maxRows === 2 && maxColumns >= 2 ? 2 : Math.min(maxColumns, visible.length);
    const rows = Math.ceil(visible.length / columns);
    grid.dataset.visible = String(visible.length);
    grid.dataset.columns = String(columns);
    grid.dataset.rows = String(rows);
    groupControls.hidden = groupCount === 1;
    document.getElementById('group-label').textContent = `Grupo ${groupIndex + 1}/${groupCount}`;
    document.getElementById('previous-group').disabled = groupIndex === 0;
    document.getElementById('next-group').disabled = groupIndex === groupCount - 1;
    document.getElementById('grid-layout-label').textContent = `▦  Grade ${columns} × ${rows}`;
    if (grid.classList.contains('has-maximized')) {
      grid.style.gridTemplateColumns = 'minmax(0, 1fr)';
      grid.style.gridTemplateRows = 'minmax(0, 1fr)';
      layout = null;
      clearDividers();
      return;
    }
    const key = `${groupIndex}:${columns}x${rows}:${visible.map(pane => pane.dataset.pane).join(',')}`;
    const stored = savedLayouts.get(key);
    const columnFractions = stored?.columns || Array(columns).fill(1 / columns);
    const rowFractions = stored?.rows || Array(rows).fill(1 / rows);
    layout = {
      key, columns, rows,
      columnPixels: fitTracks(columnFractions, grid.clientWidth - gap * (columns - 1), minimumWidth),
      rowPixels: fitTracks(rowFractions, grid.clientHeight - gap * (rows - 1), minimumHeight)
    };
    applyTracks();
    drawDividers();
  };

  const syncUI = () => {
    const current = panes();
    const count = current.length;
    grid.dataset.count = String(count);
    document.getElementById('pane-count').textContent = twoDigits(count);
    document.getElementById('sidebar-pane-count').textContent = twoDigits(count);
    const stats = document.querySelectorAll('.overview-stat strong');
    stats[0].textContent = twoDigits(count);
    stats[1].textContent = twoDigits(knownAgents.size);
    document.getElementById('footer-pane-count').textContent = `${count} PANES · ${[...knownAgents.values()].filter(agent => agent.session).length} SESSÕES`;
    current.forEach(pane => {
      const close = pane.querySelector('.close');
      if (close) close.disabled = count === 1;
    });
    updateSidebar();
    updateLayout();
    syncStreams();
  };

  const selectPane = pane => {
    if (grid.classList.contains('has-maximized') && !pane.classList.contains('maximized')) {
      panes().forEach(item => {
        item.classList.remove('maximized');
        item.querySelector('.expand').textContent = '⤢';
        item.querySelector('.expand').setAttribute('aria-label', `Expandir ${paneTitle(item)}`);
      });
      grid.classList.remove('has-maximized');
      updateLayout();
    }
    if (!mobileLayout() && pane.hidden) {
      groupIndex = Math.floor(panes().indexOf(pane) / groupCapacity);
      updateLayout();
    }
    panes().forEach(item => item.classList.toggle('selected', item === pane));
    updateSidebar();
    syncStreams();
  };

  const closePane = pane => {
    const current = panes();
    if (current.length === 1 || pane.dataset.pane === 'maestro') return;
    const index = current.indexOf(pane);
    const wasSelected = pane.classList.contains('selected');
    const wasMaximized = pane.classList.contains('maximized');
    pane.remove();
    if (wasMaximized) grid.classList.remove('has-maximized');
    syncUI();
    if (wasSelected) selectPane(panes()[Math.min(index, panes().length - 1)]);
    closedAgents.add(pane.dataset.pane);
    notify(`${paneTitle(pane)} oculto nesta prévia`);
  };

  const addCloseControl = pane => {
    if (pane.dataset.pane === 'maestro' || pane.dataset.pane === 'review') return;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'pane-tool close';
    button.textContent = '×';
    button.title = 'Fechar pane';
    button.setAttribute('aria-label', `Fechar ${paneTitle(pane)}`);
    pane.querySelector('.pane-tools').append(button);
  };
  panes().forEach(addCloseControl);

  document.getElementById('add-pane').addEventListener('click', () => {
    const agent = [...knownAgents.values()].find(item => !panes().some(pane => pane.dataset.pane === item.pane_id));
    if (!agent) { notify('Todos os agentes registrados já estão visíveis'); return; }
    const pane = createAgentPane(agent);
    closedAgents.delete(agent.pane_id);
    syncUI();
    selectPane(pane);
    pane.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    notify(`${agent.title} exibido`);
  });

  grid.addEventListener('click', event => {
    const pane = event.target.closest('.pane');
    if (!pane) return;
    const button = event.target.closest('button');
    if (!button) { selectPane(pane); return; }
    if (button.classList.contains('close')) { closePane(pane); return; }
    if (button.classList.contains('expand')) {
      const expanding = !pane.classList.contains('maximized');
      panes().forEach(item => {
        item.classList.remove('maximized');
        item.querySelector('.expand').textContent = '⤢';
        item.querySelector('.expand').setAttribute('aria-label', `Expandir ${paneTitle(item)}`);
      });
      pane.classList.toggle('maximized', expanding);
      grid.classList.toggle('has-maximized', expanding);
      button.textContent = expanding ? '⤡' : '⤢';
      button.setAttribute('aria-label', `${expanding ? 'Restaurar' : 'Expandir'} ${paneTitle(pane)}`);
      if (expanding) {
        pane.classList.remove('minimized');
        pane.querySelector('.minimize').textContent = '−';
        pane.querySelector('.minimize').setAttribute('aria-label', `Minimizar ${paneTitle(pane)}`);
      }
      selectPane(pane);
      syncUI();
      return;
    }
    if (button.classList.contains('minimize')) {
      const minimized = pane.classList.toggle('minimized');
      button.textContent = minimized ? '+' : '−';
      button.setAttribute('aria-label', `${minimized ? 'Restaurar' : 'Minimizar'} ${paneTitle(pane)}`);
      if (minimized && pane.classList.contains('maximized')) {
        pane.classList.remove('maximized');
        grid.classList.remove('has-maximized');
        pane.querySelector('.expand').textContent = '⤢';
      }
      syncUI();
    }
  });

  grid.addEventListener('keydown', event => {
    if (!event.target.classList.contains('pane') || !['Enter', ' '].includes(event.key)) return;
    event.preventDefault();
    selectPane(event.target);
  });
  paneNav.addEventListener('click', event => {
    const button = event.target.closest('[data-pane-target]');
    if (!button) return;
    const pane = panes().find(item => item.dataset.pane === button.dataset.paneTarget);
    if (!pane) return;
    selectPane(pane);
    pane.scrollIntoView({ behavior: 'smooth', block: 'center' });
    pane.focus({ preventScroll: true });
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('menu-toggle').setAttribute('aria-expanded', 'false');
  });
  document.getElementById('focus-maestro').addEventListener('click', () => {
    const pane = grid.querySelector('[data-pane="maestro"]');
    selectPane(pane);
    pane.scrollIntoView({ behavior: 'smooth', block: 'center' });
    pane.focus({ preventScroll: true });
    notify('Pane Maestro selecionado');
  });
  document.getElementById('menu-toggle').addEventListener('click', event => {
    const open = document.getElementById('sidebar').classList.toggle('open');
    event.currentTarget.setAttribute('aria-expanded', String(open));
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    panes().forEach(pane => {
      pane.classList.remove('maximized');
      pane.querySelector('.expand').textContent = '⤢';
      pane.querySelector('.expand').setAttribute('aria-label', `Expandir ${paneTitle(pane)}`);
    });
    grid.classList.remove('has-maximized');
    updateLayout();
    syncStreams();
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('menu-toggle').setAttribute('aria-expanded', 'false');
  });
  document.getElementById('previous-group').addEventListener('click', () => {
    if (groupIndex === 0) return;
    groupIndex--;
    grid.classList.remove('has-maximized');
    panes().forEach(pane => {
      pane.classList.remove('maximized');
      pane.querySelector('.expand').textContent = '⤢';
      pane.querySelector('.expand').setAttribute('aria-label', `Expandir ${paneTitle(pane)}`);
    });
    updateLayout();
    selectPane(panes()[groupIndex * groupCapacity]);
  });
  document.getElementById('next-group').addEventListener('click', () => {
    groupIndex++;
    grid.classList.remove('has-maximized');
    panes().forEach(pane => {
      pane.classList.remove('maximized');
      pane.querySelector('.expand').textContent = '⤢';
      pane.querySelector('.expand').setAttribute('aria-label', `Expandir ${paneTitle(pane)}`);
    });
    updateLayout();
    selectPane(panes()[groupIndex * groupCapacity]);
  });
  document.getElementById('restore-layout').addEventListener('click', () => {
    savedLayouts.clear();
    updateLayout();
    syncStreams();
    notify('Layout automático restaurado');
  });
  let resizeFrame = 0;
  const scheduleLayout = () => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => {
      updateLayout();
      syncStreams();
    });
  };
  window.addEventListener('resize', scheduleLayout);
  new ResizeObserver(scheduleLayout).observe(grid);

  const element = (tag, className, value) => {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (value !== undefined) item.textContent = String(value);
    return item;
  };
  const api = async (path, options = {}) => {
    const response = await fetch(path, {
      method: options.method || 'GET',
      headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}) },
      ...(options.body ? { body: JSON.stringify(options.body) } : {})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data?.detail?.message || `HTTP ${response.status}`);
    return data;
  };
  const costOf = model => modelCatalog.find(row => row.id === model)?.cost_status || 'unknown';
  const activeCostAuthorizationFor = model => costAuthorizations.find(auth => auth.model === model &&
    auth.status === 'ACTIVE' && Number(auth.uses_remaining) > 0);
  const agentAuthorizationApplies = agent => Boolean(agent.brief?.authorization?.applies ||
    (agent.model && costOf(agent.model) !== 'free' && activeCostAuthorizationFor(agent.model)));
  const modelCostLabel = value => ({
    free: 'Grátis', low_cost: 'Baixo custo', standard: 'Padrão', premium: 'Premium', unknown: 'Desconhecido'
  })[value] || 'Desconhecido';
  const modelApprovalLabel = value => ({
    approved: 'Aprovado para execução', not_qualified: 'Não qualificado', blocked: 'Bloqueado'
  })[value] || 'Não qualificado';
  const modelAvailabilityLabel = value => value === 'omniroute-listed-unverified'
    ? 'Anunciado pelo OmniRoute · inferência não verificada'
    : (value || 'Indisponível');
  const renderModelCatalog = () => {
    modelCatalogCount.textContent = String(discoveredModelCatalog.length);
    modelCatalogProjectNote.textContent = projectsError
      ? `Não foi possível verificar projetos autorizados: ${projectsError}. Planejamento e execução ficam bloqueados até validar o projeto.`
      : (projects.length
        ? 'A seleção e a execução continuam limitadas às rotas aprovadas exibidas nos controles existentes.'
        : (projectsLoaded
          ? 'Nenhum projeto autorizado para Development Mode. Os modelos ficam visíveis, mas planejamento e execução permanecem bloqueados.'
          : 'Verificando projetos autorizados; planejamento e execução permanecem bloqueados até a confirmação.'));
    modelCatalogStatus.textContent = modelCatalogError
      ? `Catálogo indisponível: ${modelCatalogError}`
      : `${discoveredModelCatalog.length} modelo(s) anunciado(s) pelo OmniRoute; descoberta não comprova inferência.`;
    modelCatalogStatus.classList.toggle('is-error', Boolean(modelCatalogError));
    if (!discoveredModelCatalog.length) {
      const empty = modelCatalogError
        ? 'Não foi possível obter modelos do OmniRoute. Atualize o catálogo quando o serviço estiver disponível.'
        : 'Nenhum modelo foi anunciado pelo OmniRoute.';
      modelCatalogList.replaceChildren(element('p', 'model-catalog-empty', empty));
      return;
    }
    modelCatalogList.replaceChildren(...discoveredModelCatalog.map(model => {
      const status = ['approved', 'not_qualified', 'blocked'].includes(model.approval_status)
        ? model.approval_status : 'not_qualified';
      const card = element('article', `model-catalog-card is-${status}`);
      const heading = element('div', 'model-catalog-card-head');
      heading.append(element('strong', 'model-catalog-card-name', model.name || model.id || 'Modelo sem nome'),
        element('span', `model-catalog-badge is-${status}`, modelApprovalLabel(status)));
      card.append(heading,
        element('p', 'model-catalog-meta', `${model.id || 'ID desconhecido'} · provedor ${model.provider || 'desconhecido'}`),
        element('p', 'model-catalog-meta', `${modelAvailabilityLabel(model.availability)} · custo ${modelCostLabel(model.cost_status)}`),
        element('p', 'model-catalog-reason', model.approval_reason || 'Sem qualificação para execução.'));
      return card;
    }));
  };
  const renderCostAuthorizations = () => {
    const selected = costAuthModel.value;
    costAuthModel.replaceChildren(element('option', '', 'Selecione um modelo aprovado'));
    modelCatalog.filter(model => model.cost_status !== 'free').forEach(model => {
      const option = element('option', '', `${model.id} · ${model.cost_status || 'unknown'}`);
      option.value = model.id;
      costAuthModel.append(option);
    });
    if (costAuthModel.children.some(option => option.value === selected)) costAuthModel.value = selected;
    costAuthCreate.disabled = !projectId || !costAuthModel.value || !document.getElementById('cost-auth-confirm').checked || pending.has('cost-auth');
    if (!projectId) {
      costAuthList.textContent = 'Selecione um projeto autorizado para consultar autorizações.';
      return;
    }
    if (!costAuthorizations.length) {
      costAuthList.textContent = 'Nenhuma autorização persistente registrada para este projeto.';
      return;
    }
    costAuthList.replaceChildren(...costAuthorizations.map(auth => {
      const item = element('div', 'cost-auth-item');
      item.append(element('span', 'cost-auth-title', `${auth.model} · ${auth.cost_status} · ${auth.status}`),
        element('span', 'cost-auth-meta', `${auth.uses_remaining}/${auth.max_uses} usos restantes · ${auth.active_executions}/${auth.max_concurrent} simultâneos · expira ${new Date(auth.expires_at).toLocaleString()}`));
      if (auth.status === 'ACTIVE') {
        const revokeButton = element('button', 'cost-auth-revoke', 'Revogar');
        revokeButton.type = 'button';
        revokeButton.addEventListener('click', async () => {
          try {
            await api(`/api/workspace/cost-authorizations/${encodeURIComponent(auth.authorization_id)}?project_id=${encodeURIComponent(projectId)}`, { method: 'DELETE' });
            await refresh();
          } catch (error) { costAuthList.append(element('p', 'model-catalog-status is-error', error.message)); }
        });
        item.append(revokeButton);
      }
      return item;
    }));
  };
  costAuthModel.addEventListener('change', renderCostAuthorizations);
  document.getElementById('cost-auth-confirm').addEventListener('change', renderCostAuthorizations);
  costAuthCreate.addEventListener('click', async () => {
    if (costAuthCreate.disabled || !projectId) return;
    const confirmed = document.getElementById('cost-auth-confirm');
    pending.add('cost-auth'); renderCostAuthorizations();
    try {
      await api('/api/workspace/cost-authorizations', { method: 'POST', body: {
        project_id: projectId, model: costAuthModel.value, confirm_cost: true,
        max_uses: Number(document.getElementById('cost-auth-uses').value),
        validity_seconds: Number(document.getElementById('cost-auth-days').value) * 86400,
        max_concurrent: Number(document.getElementById('cost-auth-concurrency').value)
      } });
      confirmed.checked = false;
      await refresh();
    } catch (error) { costAuthList.textContent = `Falha ao salvar: ${error.message}`; }
    finally { pending.delete('cost-auth'); renderCostAuthorizations(); }
  });
  const refreshModelCatalog = async (force = false) => {
    const request = ++modelCatalogRequest;
    modelCatalogRefreshButton.disabled = true;
    modelCatalogRefreshButton.textContent = 'Atualizando…';
    modelCatalogStatus.textContent = 'Consultando a lista de modelos do OmniRoute…';
    try {
      const data = await api(`/api/dev/model-catalog${force ? '?refresh=true' : ''}`);
      if (request !== modelCatalogRequest) return;
      discoveredModelCatalog = Array.isArray(data.models) ? data.models : [];
      modelCatalog = Array.isArray(data.approved_models) ? data.approved_models : [];
      modelCatalogError = data.catalog_error || '';
      renderModelCatalog();
      renderPlanningControls();
      knownAgents.forEach((_agent, paneId) => renderAgentControls(paneId));
    } catch (error) {
      if (request !== modelCatalogRequest) return;
      discoveredModelCatalog = [];
      modelCatalog = [];
      modelCatalogError = `${error.message}. Nenhuma rota permanece selecionável.`;
      renderModelCatalog();
      renderPlanningControls();
      knownAgents.forEach((_agent, paneId) => renderAgentControls(paneId));
    } finally {
      if (request === modelCatalogRequest) {
        modelCatalogRefreshButton.disabled = false;
        modelCatalogRefreshButton.textContent = 'Atualizar catálogo';
      }
    }
  };
  modelCatalogRefreshButton.addEventListener('click', () => { void refreshModelCatalog(true); });
  const formField = (label, input) => {
    const field = element('label', 'cockpit-confirm');
    field.append(input, element('span', '', label));
    return field;
  };
  const checkbox = () => {
    const input = element('input');
    input.type = 'checkbox';
    return input;
  };
  const makeComposer = (pane, kind) => {
    const form = element('form', 'cockpit-composer');
    const note = element('div', 'cockpit-compose-note');
    const controls = element('div', 'cockpit-compose-controls');
    const input = element('textarea', 'cockpit-input');
    input.maxLength = 100000;
    input.rows = 2;
    input.placeholder = kind === 'maestro' ? 'Mensagem para o Maestro' : 'Mensagem para o agente';
    input.setAttribute('aria-label', input.placeholder);
    const send = element('button', 'cockpit-send', 'Enviar ↵');
    send.type = 'submit';
    const error = element('div', 'cockpit-compose-error');
    error.setAttribute('role', 'status');
    const footer = pane.querySelector('.pane-footer');
    form.append(note, controls, input, send, error);
    pane.append(form, footer);
    input.addEventListener('keydown', event => {
      if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
      event.preventDefault();
      form.requestSubmit();
    });
    return { form, note, controls, input, send, error };
  };
  const maestroPane = grid.querySelector('[data-pane="maestro"]');
  const maestroComposer = makeComposer(maestroPane, 'maestro');
  const planningSelect = element('select', 'cockpit-model-select');
  planningSelect.setAttribute('aria-label', 'Modelo de planejamento do Maestro');
  const planningCost = checkbox();
  const planningCostLabel = formField('Autorizo o custo deste modelo para planejamento', planningCost);
  const authorize = element('button', 'cockpit-authorize', 'Autorizar planejamento');
  authorize.type = 'button';
  maestroComposer.controls.append(planningSelect, planningCostLabel, authorize);
  planningSelect.addEventListener('change', () => { planningCost.checked = false; renderPlanningControls(); });
  const renderPlanningControls = () => {
    const planning = maestroState?.planning;
    const selected = planningSelect.value;
    const emptyOption = element('option', '', 'Modelo de planejamento…');
    emptyOption.value = '';
    planningSelect.replaceChildren(emptyOption, ...modelCatalog.map(row => {
      const option = element('option', '', `${row.id} · ${row.cost_status || 'unknown'}`);
      option.value = row.id;
      return option;
    }));
    planningSelect.value = modelCatalog.some(row => row.id === selected) ? selected :
      (modelCatalog.some(row => row.id === planning?.model) ? planning.model : '');
    const planningGrant = activeCostAuthorizationFor(planningSelect.value);
    planningCostLabel.hidden = true;
    planningCost.checked = false;
    authorize.textContent = planning?.authorized ? 'Trocar modelo' : 'Autorizar planejamento';
    authorize.disabled = pending.has('authorize') || !projectId || !planningSelect.value ||
      (costOf(planningSelect.value) !== 'free' && !planningGrant) ||
      (planning?.authorized && planning.model === planningSelect.value);
    maestroComposer.note.textContent = planning?.authorized
      ? `IA autorizada: ${planning.model} · ${planning.cost_status || 'unknown'} · planejamento somente leitura${planningGrant ? ` · ${planningGrant.uses_remaining}/${planningGrant.max_uses} usos restantes` : ''}`
      : (planningSelect.value && costOf(planningSelect.value) !== 'free' && !planningGrant
        ? 'Crie uma autorização persistente com limites no Catálogo de modelos antes de habilitar este modelo no Maestro.'
        : 'Planejamento determinístico. Autorize um modelo para usar IA; execução de agentes exige aprovação separada.');
    maestroComposer.send.disabled = pending.has('maestro') || pending.has('authorize') || !projectId || Boolean(maestroState?.busy);
  };
  planningCost.addEventListener('change', renderPlanningControls);
  authorize.addEventListener('click', async () => {
    if (pending.has('authorize') || !projectId || !planningSelect.value) return;
    const selectedProject = projectId;
    const model = planningSelect.value;
    pending.add('authorize');
    renderPlanningControls();
    try {
      await api('/api/workspace/maestro/authorize', { method: 'POST', body: {
        project_id: selectedProject, model, confirm_cost: false
      } });
      maestroComposer.error.textContent = '';
    } catch (error) {
      maestroComposer.error.textContent = `${error.message}. Confira o estado atualizado antes de tentar novamente.`;
    } finally {
      pending.delete('authorize');
      await refresh();
      renderPlanningControls();
    }
  });
  const submitMessage = async (kind, composer, paneId) => {
    const key = kind === 'maestro' ? 'maestro' : paneId;
    const text = composer.input.value.trim();
    if (!text || pending.has(key) || !projectId) return;
    const selectedProject = projectId;
    let path, body;
    if (kind === 'maestro') {
      if (maestroState?.busy || pending.has('authorize')) return;
      path = '/api/workspace/maestro/messages';
      body = { project_id: selectedProject, text };
    } else {
      const agent = knownAgents.get(paneId);
      if (!agent || !workspaceState?.execution_enabled || agent.busy ||
          (agent.messages || []).some(message => ['PENDING', 'RUNNING'].includes(message.status)) ||
          agent.session?.status !== 'ACTIVE') return;
      if (!agent.model) { composer.error.textContent = 'Selecione um modelo no Workspace antes de enviar.'; return; }
      const authorized = agentAuthorizationApplies(agent);
      const confirmed = composer.controls.querySelector('.cockpit-model-confirm input');
      const costConfirmed = composer.controls.querySelector('.cockpit-cost-confirm input');
      if (!authorized && !confirmed?.checked) { composer.error.textContent = `Confirme o uso de ${agent.model}.`; return; }
      if (!authorized && costOf(agent.model) !== 'free' && !costConfirmed?.checked) {
        composer.error.textContent = `Confirme o custo de ${agent.model}.`; return;
      }
      path = `/api/workspace/panes/${encodeURIComponent(paneId)}/messages`;
      body = { text, ...(!authorized ? { confirm_model: agent.model } : {}),
        ...(!authorized && costOf(agent.model) !== 'free' ? { confirm_cost: true } : {}) };
    }
    pending.add(key);
    composer.send.disabled = true;
    composer.error.textContent = '';
    let accepted = false;
    try {
      await api(path, { method: 'POST', body });
      accepted = true;
      if (kind === 'maestro' && maestroDrafts.get(selectedProject)?.trim() === text) maestroDrafts.set(selectedProject, '');
      if (selectedProject === projectId && composer.input.value.trim() === text) composer.input.value = '';
      if (kind === 'agent') {
        composer.controls.querySelector('.cockpit-model-confirm input').checked = false;
        composer.controls.querySelector('.cockpit-cost-confirm input').checked = false;
      }
    } catch (error) {
      composer.error.textContent = `${error.message}. Resultado pode ser incerto; confira o histórico antes de reenviar.`;
    } finally {
      // A failed response may follow a committed POST. Reconcile only by GET.
      await refresh();
      pending.delete(key);
      if (kind === 'maestro') renderPlanningControls();
      else renderAgentControls(paneId);
      if (accepted) composer.error.textContent = '';
    }
  };
  const submitMission = async (paneId, controls) => {
    const agent = knownAgents.get(paneId);
    const description = controls.description.value.trim();
    const profile = controls.profile.value;
    if (!agent || (!description && !agent.brief) || !profile || pending.has(paneId) || !projectId) return;
    if (!workspaceState?.execution_enabled || agent.busy || agent.session?.status !== 'ACTIVE') return;
    if ((agent.dependencies || []).some(dependency => !dependency.ready)) {
      controls.error.textContent = 'Aguarde a dependência e o handoff antes de iniciar esta missão.';
      return;
    }
    const authorized = agentAuthorizationApplies(agent);
    const confirmModel = controls.composer.controls.querySelector('.cockpit-model-confirm input');
    const confirmCost = controls.composer.controls.querySelector('.cockpit-cost-confirm input');
    if (!authorized && !confirmModel?.checked) {
      controls.error.textContent = `Confirme o uso de ${agent.model || 'um modelo'} antes da missão.`;
      return;
    }
    if (!authorized && costOf(agent.model) !== 'free' && !confirmCost?.checked) {
      controls.error.textContent = `Confirme o custo de ${agent.model} antes da missão.`;
      return;
    }
    pending.add(paneId);
    controls.start.disabled = true;
    controls.error.textContent = '';
    try {
      await api(`/api/workspace/panes/${encodeURIComponent(paneId)}/missions`, { method: 'POST', body: {
        ...(description ? { description } : {}), test_profile: profile,
        ...(!authorized ? { confirm_model: agent.model } : {}),
        ...(!authorized && costOf(agent.model) !== 'free' ? { confirm_cost: true } : {})
      } });
      if (controls.description.value.trim() === description) controls.description.value = '';
      confirmModel.checked = false;
      confirmCost.checked = false;
    } catch (error) {
      controls.error.textContent = `${error.message}. O resultado pode ser incerto; confira a missão antes de tentar novamente.`;
    } finally {
      await refresh();
      pending.delete(paneId);
      renderAgentControls(paneId);
    }
  };
  const interruptMission = async (paneId, controls) => {
    const agent = knownAgents.get(paneId);
    const missionId = agent?.latest_mission?.mission_id;
    const sessionId = agent?.session?.session_id;
    if (!missionId || !sessionId || pending.has(`interrupt:${paneId}`) || interruptRequests.has(missionId)) return;
    const key = `interrupt:${paneId}`;
    pending.add(key);
    controls.interrupt.disabled = true;
    controls.error.textContent = '';
    try {
      await api(`/api/dev/sessions/${encodeURIComponent(sessionId)}/missions/${encodeURIComponent(missionId)}/interrupt`, { method: 'POST' });
      interruptRequests.add(missionId);
    } catch (error) {
      controls.error.textContent = `${error.message}. Confira o estado atualizado antes de repetir a solicitação.`;
    } finally {
      await refresh();
      pending.delete(key);
      renderAgentControls(paneId);
    }
  };
  const inspectMission = async controls => {
    const sessionId = controls.inspect.dataset.sessionId;
    const missionId = controls.inspect.dataset.missionId;
    if (!sessionId || !missionId) return;
    controls.inspect.disabled = true;
    controls.diff.hidden = false;
    controls.diff.textContent = 'Consultando o estado atual do worktree da sessão…';
    try {
      const diff = await api(`/api/dev/sessions/${encodeURIComponent(sessionId)}/diff`);
      if (controls.inspect.dataset.sessionId !== sessionId || controls.inspect.dataset.missionId !== missionId) return;
      const baseSha = diff.base_sha || 'base desconhecida';
      const headSha = diff.head_sha || 'HEAD desconhecido';
      const patches = (diff.files || []).map(file => file.patch || `${file.path}: ${file.reason || 'sem patch'}`).join('\n\n') || 'Sem alterações no worktree.';
      controls.diff.textContent = `Estado atual do worktree desta sessão contra a base persistida (${baseSha} → ${headSha}) · consultado às ${new Date().toLocaleTimeString()}\n\n${patches}`;
      controls.inspect.textContent = 'Atualizar diff';
    } catch (error) {
      if (controls.inspect.dataset.sessionId !== sessionId || controls.inspect.dataset.missionId !== missionId) return;
      controls.diff.textContent = `Falha ao carregar diff: ${error.message}`;
      controls.inspect.textContent = 'Tentar novamente';
    } finally {
      controls.inspect.disabled = false;
    }
  };
  maestroComposer.form.addEventListener('submit', event => {
    event.preventDefault();
    void submitMessage('maestro', maestroComposer);
  });
  const renderAgentControls = paneId => {
    const pane = panes().find(item => item.dataset.pane === paneId);
    const agent = knownAgents.get(paneId);
    if (!pane || !agent) return;
    const composer = pane.composer;
    const planAuthorized = Boolean(agent.brief?.authorization?.applies);
    const costGrant = activeCostAuthorizationFor(agent.model);
    const authorized = planAuthorized || Boolean(agent.model && costOf(agent.model) !== 'free' && costGrant);
    const modelConfirm = composer.controls.querySelector('.cockpit-model-confirm');
    const costConfirm = composer.controls.querySelector('.cockpit-cost-confirm');
    if (pane.dataset.confirmedModel !== (agent.model || '')) {
      modelConfirm.querySelector('input').checked = false;
      costConfirm.querySelector('input').checked = false;
      pane.dataset.confirmedModel = agent.model || '';
    }
    modelConfirm.hidden = authorized || !agent.model;
    costConfirm.hidden = authorized || !agent.model || costOf(agent.model) === 'free';
    modelConfirm.querySelector('span').textContent = `Confirmo o uso de ${agent.model || 'um modelo'}`;
    costConfirm.querySelector('span').textContent = `Autorizo o custo de ${agent.model || 'um modelo'} (${costOf(agent.model)})`;
    const mission = pane.missionControls;
    const previousProfile = mission.profile.value;
    const profilePrompt = testProfilesLoading ? 'Carregando perfis confiáveis…' :
      (testProfilesError ? 'Perfis indisponíveis — execução bloqueada' :
        (testProfiles.length ? 'Selecione perfil verificado…' : 'Nenhum perfil confiável'));
    mission.profile.replaceChildren(
      element('option', '', profilePrompt),
      ...testProfiles.map(profile => {
        const option = element('option', '', `${profile.name} · ${profile.id} · ${profile.timeout_seconds}s`);
        option.value = profile.id;
        return option;
      })
    );
    mission.profile.value = testProfiles.some(profile => profile.id === previousProfile) ? previousProfile : '';
    mission.profileNote.textContent = testProfilesError ? `Perfis de teste indisponíveis: ${testProfilesError}` : '';
    composer.note.textContent = planAuthorized
      ? `Modelo ${agent.model} autorizado pelo plano vigente`
      : (costGrant && authorized
        ? `Autorização ativa: ${agent.model} · ${costGrant.cost_status} · ${costGrant.uses_remaining}/${costGrant.max_uses} usos restantes · ${costGrant.active_executions}/${costGrant.max_concurrent} simultâneos`
        : `Turno no worktree isolado · ${agent.model || 'modelo não selecionado'} · ${costOf(agent.model)}`);
    const activeMission = ['PENDING', 'RUNNING', 'TESTING', 'REPAIRING'].includes(agent.latest_mission?.status);
    const latestMission = agent.latest_mission;
    if (latestMission && !activeMission) interruptRequests.delete(latestMission.mission_id);
    mission.summary.textContent = latestMission
      ? `Missão ${latestMission.mission_id} · ${latestMission.status}`
      : (agent.brief ? 'Executar tarefa planejada' : 'Criar missão com testes');
    mission.status.hidden = !latestMission;
    mission.status.replaceChildren();
    if (latestMission) {
      mission.status.append(element('strong', '', `${latestMission.status} · tentativa ${latestMission.attempt_count || 0}`));
      if (latestMission.tests?.length) {
        const lastTest = latestMission.tests.at(-1);
        mission.status.append(element('span', '', `Testes: ${lastTest.status}${lastTest.exit_code === null || lastTest.exit_code === undefined ? '' : ` (exit ${lastTest.exit_code})`}`));
      }
      if (latestMission.changed_files?.length) mission.status.append(element('span', '', `${latestMission.changed_files.length} arquivo(s) alterado(s) no worktree isolado`));
      if (latestMission.error_code) mission.status.append(element('span', 'cockpit-error-text', `${latestMission.error_code}: ${latestMission.error_message || ''}`));
      if (!activeMission) mission.status.append(element('span', 'cockpit-mission-review-note', 'Revisão somente leitura; nenhuma alteração será aplicada ou integrada aqui.'));
    }
    composer.send.disabled = pending.has(paneId) || !workspaceState?.execution_enabled || agent.busy || activeMission ||
      (agent.messages || []).some(message => ['PENDING', 'RUNNING'].includes(message.status)) ||
      agent.session?.status !== 'ACTIVE' || !agent.model;
    mission.start.disabled = pending.has(paneId) || !workspaceState?.execution_enabled || agent.busy || activeMission ||
      agent.session?.status !== 'ACTIVE' || !agent.model || !mission.profile.value ||
      (!mission.description.value.trim() && !agent.brief) || (agent.dependencies || []).some(dependency => !dependency.ready);
    mission.interrupt.hidden = !activeMission;
    const interruptRequested = Boolean(latestMission?.interrupt_requested) || interruptRequests.has(latestMission?.mission_id);
    mission.interrupt.disabled = pending.has(`interrupt:${paneId}`) || interruptRequested;
    mission.interrupt.textContent = interruptRequested ? 'Interrupção solicitada' : 'Interromper missão';
    mission.inspect.hidden = !agent.latest_mission || activeMission;
    mission.inspect.dataset.sessionId = agent.session?.session_id || '';
    mission.inspect.dataset.missionId = agent.latest_mission?.mission_id || '';
    const currentSessionId = agent.session?.session_id || '';
    const currentMissionId = agent.latest_mission?.mission_id || '';
    if (mission.diff.dataset.sessionId !== currentSessionId || mission.diff.dataset.missionId !== currentMissionId) {
      mission.diff.dataset.sessionId = currentSessionId;
      mission.diff.dataset.missionId = currentMissionId;
      mission.diff.hidden = true;
      mission.diff.textContent = '';
      mission.inspect.textContent = 'Revisar diff atual';
    }
  };
  const statusOf = agent => {
    if (!agent.session || agent.session.status !== 'ACTIVE') return agent.session?.status || 'Sessão ausente';
    if ((agent.messages || []).some(message => ['PENDING', 'RUNNING'].includes(message.status))) return 'Respondendo';
    if ((agent.dependencies || []).some(dependency => !dependency.ready) && !agent.latest_mission) return 'Aguardando handoff';
    return agent.latest_mission?.status || 'Ocioso';
  };
  const updateAgentPane = (pane, agent, history) => {
    pane.dataset.title = agent.title || agent.pane_id;
    pane.setAttribute('aria-label', `Pane ${pane.dataset.title}`);
    pane.querySelector('.pane-icon').textContent = (agent.title || 'A').slice(0, 1).toUpperCase();
    pane.querySelector('.pane-identity strong').textContent = pane.dataset.title;
    pane.querySelector('.pane-identity div > span').textContent = agent.role_label || agent.role || 'AGENTE';
    const status = statusOf(agent);
    const badge = pane.querySelector('.pane-status');
    badge.lastChild.textContent = ` ${status}`;
    badge.className = `pane-status ${['FAILED', 'OUTCOME_UNKNOWN', 'RESULT_UNCERTAIN'].includes(status) ? 'waiting' : 'idle'}`;
    const meta = pane.querySelector('.pane-meta');
    meta.replaceChildren();
    [['MODELO', agent.model || 'Não selecionado'], ['MISSÃO', agent.latest_mission?.mission_id || 'Nenhuma'], ['SESSÃO', agent.session?.session_id || 'Ausente']].forEach(([label, value]) => {
      const field = element('span', '', `${label} `);
      field.append(element('b', '', value));
      meta.append(field);
    });
    pane.querySelector('.terminal-kicker').replaceChildren(element('span', '', '●'), document.createTextNode(' EVENTOS NEXUS / NÃO É SHELL '), element('span', 'right', status));
    pane.querySelector('.foot-path').textContent = agent.session?.branch || 'Sem worktree ativo';
    pane.querySelector('.pane-footer > span:last-child').textContent = 'MENSAGENS NEXUS · SEM SHELL';
    const body = pane.querySelector('.terminal-body');
    const signature = JSON.stringify([agent.latest_mission, agent.messages, agent.dependencies, history?.events]);
    if (body.dataset.signature === signature) return;
    const oldScroll = body.scrollTop;
    const eventList = element('div', 'cockpit-event-list');
    const mission = agent.latest_mission;
    if (mission) {
      eventList.append(element('div', 'cockpit-event-heading', `MISSÃO ${mission.mission_id} · ${mission.status}`));
      if (mission.tests?.length) eventList.append(element('div', 'cockpit-event', `Testes: ${mission.tests.at(-1).status}`));
      if (mission.error_code) eventList.append(element('div', 'cockpit-event', `Erro: ${mission.error_code} · ${mission.error_message || ''}`));
      (mission.changed_files || []).slice(0, 8).forEach(file => eventList.append(element('div', 'cockpit-event', `${file.status} ${file.path}`)));
    }
    (history?.events || []).slice(-12).forEach(event => eventList.append(element('div', 'cockpit-event', `${event.created_at || ''}  ${event.kind}`)));
    const conversation = element('div', 'cockpit-conversation');
    (agent.messages || []).forEach(message => {
      const item = element('div', 'cockpit-message');
      item.append(element('strong', '', `Você · ${message.created_at || ''}`), element('p', '', message.text));
      const response = element('div', 'cockpit-response');
      response.append(element('strong', '', `Agente · ${message.status || 'PENDING'} · ${message.outcome || 'aguardando'}`));
      if (message.response_text) response.append(element('p', '', message.response_text));
      if (message.error_code) response.append(element('p', 'cockpit-error-text', `${message.error_code}: ${message.error_message || ''}`));
      item.append(response);
      conversation.append(item);
    });
    if (!eventList.children.length) eventList.append(element('div', 'cockpit-event', 'Nenhuma atividade registrada para este agente.'));
    body.replaceChildren(pane.querySelector('.terminal-kicker'), conversation, eventList);
    body.dataset.signature = signature;
    body.scrollTop = oldScroll;
    pane.querySelector('.minimize').setAttribute('aria-label', `Minimizar ${pane.dataset.title}`);
    pane.querySelector('.expand').setAttribute('aria-label', `Expandir ${pane.dataset.title}`);
    const close = pane.querySelector('.close');
    if (close) close.setAttribute('aria-label', `Fechar ${pane.dataset.title}`);
  };
  const createAgentPane = agent => {
    const pane = agentTemplate.cloneNode(true);
    pane.hidden = false;
    pane.dataset.pane = agent.pane_id;
    pane.classList.remove('selected', 'minimized', 'maximized');
    addCloseControl(pane);
    const composer = makeComposer(pane, 'agent');
    pane.composer = composer;
    const missionDetails = element('details', 'cockpit-mission-details');
    const missionSummary = element('summary', '', 'Criar missão com testes');
    const missionDescription = element('textarea', 'cockpit-input cockpit-mission-input');
    missionDescription.rows = 2;
    missionDescription.maxLength = 100000;
    missionDescription.placeholder = agent.brief ? 'Instruções extras (opcional)' : 'Objetivo da missão';
    missionDescription.setAttribute('aria-label', 'Objetivo da missão');
    const missionProfile = element('select', 'cockpit-model-select cockpit-mission-profile');
    missionProfile.setAttribute('aria-label', 'Perfil confiável de testes');
    const profilePrompt = element('option', '', 'Carregando perfis confiáveis…');
    profilePrompt.value = '';
    missionProfile.append(profilePrompt);
    const profileNote = element('div', 'cockpit-mission-note');
    profileNote.setAttribute('role', 'status');
    const missionStatus = element('div', 'cockpit-mission-status');
    missionStatus.setAttribute('role', 'status');
    missionStatus.hidden = true;
    const missionActions = element('div', 'cockpit-mission-actions');
    const startMission = element('button', 'cockpit-send cockpit-mission-start', 'Iniciar missão');
    startMission.type = 'button';
    const interrupt = element('button', 'cockpit-send cockpit-mission-interrupt', 'Interromper missão');
    interrupt.type = 'button';
    interrupt.hidden = true;
    const inspect = element('button', 'cockpit-send cockpit-mission-inspect', 'Revisar diff');
    inspect.type = 'button';
    inspect.hidden = true;
    const diff = element('pre', 'cockpit-mission-diff');
    diff.hidden = true;
    const missionError = element('div', 'cockpit-compose-error cockpit-mission-error');
    missionError.setAttribute('role', 'status');
    missionActions.append(startMission, interrupt, inspect);
    missionDetails.append(missionSummary, missionStatus, missionDescription, missionProfile, profileNote, missionActions, diff, missionError);
    composer.form.append(missionDetails);
    const missionControls = { composer, summary: missionSummary, status: missionStatus, description: missionDescription,
      profile: missionProfile, profileNote, start: startMission, interrupt, inspect, diff, error: missionError };
    pane.missionControls = missionControls;
    startMission.addEventListener('click', () => { void submitMission(agent.pane_id, missionControls); });
    interrupt.addEventListener('click', () => { void interruptMission(agent.pane_id, missionControls); });
    inspect.addEventListener('click', () => { void inspectMission(missionControls); });
    missionProfile.addEventListener('change', () => renderAgentControls(agent.pane_id));
    missionDescription.addEventListener('input', () => renderAgentControls(agent.pane_id));
    const modelInput = checkbox();
    const costInput = checkbox();
    const modelLabel = formField('', modelInput);
    const costLabel = formField('', costInput);
    modelLabel.classList.add('cockpit-model-confirm');
    costLabel.classList.add('cockpit-cost-confirm');
    composer.controls.append(modelLabel, costLabel);
    composer.form.addEventListener('submit', event => {
      event.preventDefault();
      void submitMessage('agent', composer, pane.dataset.pane);
    });
    grid.append(pane);
    updateAgentPane(pane, agent);
    renderAgentControls(agent.pane_id);
    return pane;
  };
  const renderProjects = () => {
    const nav = document.getElementById('project-nav');
    nav.replaceChildren(...projects.map(project => {
      const button = element('button', `mission-link${project.id === projectId ? ' active' : ''}`);
      button.type = 'button';
      button.append(element('span', 'mission-indicator'), element('span', '', project.name));
      button.addEventListener('click', () => { nav.hidden = true; selectProject(project.id); });
      return button;
    }));
    document.getElementById('project-count').textContent = twoDigits(projects.length);
    const selected = projects.find(project => project.id === projectId);
    document.getElementById('project-name').textContent = selected?.name || 'Selecione um projeto';
    document.getElementById('project-detail').textContent = selected ? 'Projeto autorizado' : 'Nenhum projeto selecionado';
    document.getElementById('workspace-id').textContent = selected?.id || '—';
    renderModelCatalog();
  };
  const renderMissions = agents => {
    const missions = agents.filter(agent => agent.latest_mission);
    document.getElementById('mission-count').textContent = twoDigits(missions.length);
    const nav = document.getElementById('mission-nav');
    nav.replaceChildren(...missions.map(agent => {
      const button = element('button', 'mission-link');
      button.type = 'button';
      const details = element('span');
      details.append(element('strong', '', agent.title), element('small', '', agent.latest_mission.status));
      button.append(element('span', 'mission-indicator'), details);
      button.addEventListener('click', () => {
        let pane = panes().find(item => item.dataset.pane === agent.pane_id);
        if (!pane) { pane = createAgentPane(agent); closedAgents.delete(agent.pane_id); syncUI(); }
        selectPane(pane);
        pane.focus({ preventScroll: true });
      });
      return button;
    }));
  };
  const renderSystemPanes = (workspace, maestro) => {
    maestroPane.querySelector('.pane-status').lastChild.textContent = maestro?.busy ? ' Planejando' : ' Disponível';
    maestroPane.querySelector('.pane-meta').replaceChildren(element('span', '', `MODELO  ${maestro?.planning?.model || 'Não autorizado'}`), element('span', '', `SESSÃO  ${maestro?.conversation_id || '—'}`));
    const body = maestroPane.querySelector('.maestro-body');
    const signature = JSON.stringify(maestro?.messages || []);
    if (body.dataset.signature !== signature) {
      const oldScroll = body.scrollTop;
      const conversation = element('div', 'cockpit-conversation');
      (maestro?.messages || []).forEach(message => {
        const item = element('div', `cockpit-message ${message.author === 'user' ? 'cockpit-user' : 'cockpit-maestro'}`);
        item.append(element('strong', '', `${message.author === 'user' ? 'Você' : 'Maestro'} · ${message.status || ''}`),
          element('p', '', message.text || ''));
        if (message.error_code) item.append(element('p', 'cockpit-error-text', `${message.error_code}: ${message.error_message || ''}`));
        if (message.plan) item.append(element('small', '', `Plano ${message.plan.plan_id || ''} · ${message.plan.source || 'origem desconhecida'}${message.plan.fallback_reason ? ` · ${message.plan.fallback_reason}` : ''}`));
        conversation.append(item);
      });
      if (!conversation.children.length) conversation.append(element('div', 'cockpit-event', 'Nenhuma mensagem registrada para este projeto.'));
      body.replaceChildren(maestroPane.querySelector('.terminal-kicker'), conversation);
      body.dataset.signature = signature;
      body.scrollTop = oldScroll;
    }
    maestroPane.querySelector('.pane-footer > span:last-child').textContent = 'PLANEJAMENTO · SEM EXECUÇÃO AUTOMÁTICA';
    renderPlanningControls();
    const review = grid.querySelector('[data-pane="review"]');
    const awaiting = workspace.panes.filter(agent => agent.latest_mission?.status === 'READY_FOR_REVIEW');
    review.querySelector('.pane-status').lastChild.textContent = awaiting.length ? ' Aguardando' : ' Sem pendências';
    review.querySelector('.pane-meta').replaceChildren(element('span', '', `AGENTES  ${workspace.panes.length}`), element('span', '', `AGUARDANDO REVISÃO  ${awaiting.length}`));
    review.querySelector('.review-steps').replaceChildren(...(awaiting.length ? awaiting.map(agent => {
      const item = element('div', 'review-step');
      const details = element('div');
      details.append(element('strong', '', agent.title), element('small', '', agent.latest_mission.mission_id));
      item.append(element('span', 'step-number', '○'), details);
      return item;
    }) : [element('div', 'review-step', 'Nenhuma missão aguardando revisão.')])) ;
    review.querySelector('.pane-footer > span:last-child').textContent = 'INTEGRAÇÃO EXIGE APROVAÇÃO';
  };
  const streamBody = key => key === 'maestro'
    ? maestroPane.querySelector('.maestro-body')
    : panes().find(pane => pane.dataset.pane === key)?.querySelector('.terminal-body');
  const renderLive = key => {
    const body = streamBody(key);
    if (!body) return;
    const live = liveTurns.get(key);
    let node = body.querySelector('.cockpit-live');
    if (!live || (!live.text && !live.activity && !live.notice)) {
      if (node) node.remove();
      return;
    }
    if (!node) { node = element('div', 'cockpit-live'); body.append(node); }
    node.replaceChildren(element('strong', '', `AO VIVO · ${live.status || 'RUNNING'}`));
    if (live.text) node.append(element('pre', 'cockpit-live-text', live.text));
    if (live.activity) node.append(element('div', 'cockpit-event', live.activity));
    if (live.notice) node.append(element('div', 'cockpit-error-text', live.notice));
  };
  const stopStream = key => {
    const stream = streams.get(key);
    if (stream) { stream.source.close(); streams.delete(key); }
  };
  const stopAllStreams = () => {
    [...streams.keys()].forEach(stopStream);
    liveTurns.clear();
    settledStreams.clear();
    streamCursors.clear();
    reconnectHold.clear();
  };
  const activeSpecs = () => {
    if (!projectId || typeof window.EventSource !== 'function') return [];
    const specs = [];
    const add = (key, kind, sessionId, turnId) => {
      const pane = panes().find(item => item.dataset.pane === key);
      if (!pane || pane.hidden || pane.classList.contains('minimized') ||
          (grid.classList.contains('has-maximized') && !pane.classList.contains('maximized')) ||
          !sessionId || !turnId) return;
      specs.push({ key, kind, sessionId, turnId, project: projectId, selected: pane.classList.contains('selected') });
    };
    if (maestroState?.busy) add('maestro', 'chat', maestroState.chat_session_id, maestroState.active_turn_id);
    knownAgents.forEach(agent => add(agent.pane_id, 'dev', agent.session?.session_id, agent.active_turn_id));
    specs.sort((a, b) => Number(b.selected) - Number(a.selected));
    return specs.slice(0, maxStreams);
  };
  const reconcileStream = stream => {
    if (stream.project === projectId) void refresh();
  };
  const processStreamEvent = (stream, raw) => {
    if (streams.get(stream.key) !== stream || stream.project !== projectId) return;
    const eventId = raw.lastEventId;
    if (eventId) {
      if (stream.seen.has(eventId)) return;
      const cursor = streamCursors.get(stream.key);
      if (!cursor || cursor.turnId !== stream.turnId) streamCursors.set(stream.key, { turnId: stream.turnId, id: eventId });
      else cursor.id = eventId;
      stream.seen.add(eventId);
      if (stream.seen.size > 1000) stream.seen.delete(stream.seen.values().next().value);
    }
    let event;
    try { event = JSON.parse(raw.data); } catch (_error) { return; }
    if (!event || typeof event !== 'object') return;
    const type = event.type || event.method || '';
    if (type === 'nexus/replayGap' || type === 'nexus/turnSnapshot' ||
        type === 'nexus/turnEnded' || type === 'nexus/noTurn' || /turn\/(completed|failed|interrupted)$/.test(type)) {
      stopStream(stream.key);
      settledStreams.set(stream.key, stream.turnId);
      if (type === 'nexus/replayGap') {
        liveTurns.set(stream.key, { turnId: stream.turnId, status: 'RECONCILING',
          notice: 'Eventos ao vivo expiraram; consultando o registro persistido.' });
        renderLive(stream.key);
      } else if (type === 'nexus/turnSnapshot') {
        const turn = event.turn || event.params?.turn || {};
        liveTurns.set(stream.key, { turnId: stream.turnId, status: turn.status || 'RECONCILING',
          text: turn.response_text || '', notice: turn.error_message || '' });
        renderLive(stream.key);
      }
      reconcileStream(stream);
      return;
    }
    const live = liveTurns.get(stream.key) || { turnId: stream.turnId, status: 'RUNNING', text: '' };
    if (live.turnId !== stream.turnId) return;
    const params = event.params || {};
    if (/agentMessage.*delta|delta.*agentMessage/i.test(type)) {
      const delta = params.delta || params.textDelta;
      if (typeof delta === 'string') live.text = (live.text + delta).slice(-20000);
    } else if (/item\/completed$/.test(type) && params.item?.type === 'agentMessage' && typeof params.item.text === 'string') {
      live.text = params.item.text.slice(-20000);
    } else if (/item\/(started|updated|completed)$/.test(type) && params.item?.type && params.item.type !== 'agentMessage') {
      live.activity = `Evento ${params.item.type} · ${type}`;
    } else if (/turn\/started$/.test(type)) live.status = 'RUNNING';
    else return;
    liveTurns.set(stream.key, live);
    renderLive(stream.key);
  };
  const startStream = spec => {
    const cursor = streamCursors.get(spec.key);
    const resumeCursor = cursor?.turnId === spec.turnId ? `&cursor=${encodeURIComponent(cursor.id)}` : '';
    const url = `/api/${spec.kind}/sessions/${encodeURIComponent(spec.sessionId)}/events?turn_id=${encodeURIComponent(spec.turnId)}${resumeCursor}`;
    const source = new window.EventSource(url);
    const stream = { ...spec, source, seen: new Set() };
    streams.set(spec.key, stream);
    const previous = liveTurns.get(spec.key);
    liveTurns.set(spec.key, previous?.turnId === spec.turnId
      ? { ...previous, status: 'RUNNING' } : { turnId: spec.turnId, status: 'RUNNING', text: '' });
    source.onmessage = raw => processStreamEvent(stream, raw);
    source.onerror = () => {
      if (streams.get(spec.key) !== stream) return;
      const live = liveTurns.get(spec.key);
      if (live) { live.status = 'RECONNECTING'; renderLive(spec.key); }
      // EventSource reconnects using Last-Event-ID. Polling continues as fallback.
      if (source.readyState === window.EventSource.CLOSED) {
        stopStream(spec.key);
        reconnectHold.add(spec.key);
        setTimeout(() => {
          reconnectHold.delete(spec.key);
          if (spec.project === projectId) syncStreams();
        }, 1000);
      }
      reconcileStream(stream);
    };
  };
  const syncStreams = () => {
    const wanted = activeSpecs();
    for (const [key, turnId] of settledStreams) {
      if (!wanted.some(spec => spec.key === key && spec.turnId === turnId)) settledStreams.delete(key);
    }
    for (const [key, stream] of streams) {
      if (!wanted.some(spec => spec.key === key && spec.project === stream.project &&
          spec.sessionId === stream.sessionId && spec.turnId === stream.turnId)) stopStream(key);
    }
    for (const spec of wanted) {
      const current = streams.get(spec.key);
      if (streamCursors.get(spec.key)?.turnId !== spec.turnId) streamCursors.delete(spec.key);
      if (!current && !reconnectHold.has(spec.key) && settledStreams.get(spec.key) !== spec.turnId) startStream(spec);
      renderLive(spec.key);
    }
    for (const [key, live] of liveTurns) {
      const spec = wanted.find(item => item.key === key);
      if (!spec || live.turnId !== spec.turnId) {
        liveTurns.delete(key);
        renderLive(key);
      }
    }
  };
  const refresh = async () => {
    if (!projectId) return;
    clearTimeout(refreshTimer);
    const generation = refreshGeneration;
    const request = ++refreshRequest;
    try {
      const query = encodeURIComponent(projectId);
      const [workspace, maestro, authData] = await Promise.all([
        api(`/api/workspace/state?project_id=${query}`),
        api(`/api/workspace/maestro?project_id=${query}`),
        api(`/api/workspace/cost-authorizations?project_id=${query}`)
      ]);
      if (generation !== refreshGeneration || request !== refreshRequest) return;
      workspaceState = workspace;
      maestroState = maestro;
      costAuthorizations = authData.authorizations || [];
      renderCostAuthorizations();
      renderPlanningControls();
      const agents = (workspace.panes || []).slice().sort((a, b) => (a.layout?.position || 0) - (b.layout?.position || 0));
      knownAgents.clear();
      agents.forEach(agent => knownAgents.set(agent.pane_id, agent));
      panes().filter(pane => !['maestro', 'review'].includes(pane.dataset.pane) && !knownAgents.has(pane.dataset.pane)).forEach(pane => pane.remove());
      if (!panes().some(pane => pane.classList.contains('selected'))) selectPane(grid.querySelector('[data-pane="maestro"]'));
      const histories = await Promise.all(agents.map(agent => api(`/api/workspace/panes/${encodeURIComponent(agent.pane_id)}/history`).catch(() => null)));
      if (generation !== refreshGeneration || request !== refreshRequest) return;
      agents.forEach((agent, index) => {
        let pane = panes().find(item => item.dataset.pane === agent.pane_id);
        if (!pane && !closedAgents.has(agent.pane_id)) pane = createAgentPane(agent);
        if (pane) { updateAgentPane(pane, agent, histories[index]); renderAgentControls(agent.pane_id); }
      });
      renderMissions(agents);
      renderSystemPanes(workspace, maestro);
      document.getElementById('execution-count').textContent = twoDigits(agents.reduce((total, agent) => total + (agent.mission_count || 0), 0));
      document.getElementById('data-status').textContent = 'Dados do Nexus · atualização automática';
      syncUI();
    } catch (error) {
      if (generation === refreshGeneration && request === refreshRequest) document.getElementById('data-status').textContent = `Falha ao carregar: ${error.message}`;
    } finally {
      if (generation === refreshGeneration && request === refreshRequest) refreshTimer = setTimeout(refresh, 15000);
    }
  };
  const selectProject = id => {
    stopAllStreams();
    if (projectId) maestroDrafts.set(projectId, maestroComposer.input.value);
    projectId = id;
    maestroComposer.input.value = maestroDrafts.get(id) || '';
    maestroComposer.error.textContent = '';
    planningSelect.value = '';
    planningCost.checked = false;
    refreshGeneration++;
    clearTimeout(refreshTimer);
    closedAgents.clear();
    knownAgents.clear();
    interruptRequests.clear();
    testProfiles = [];
    testProfilesLoading = Boolean(projectId);
    testProfilesError = '';
    workspaceState = null;
    maestroState = null;
    panes().filter(pane => !['maestro', 'review'].includes(pane.dataset.pane)).forEach(pane => pane.remove());
    renderProjects();
    syncUI();
    selectPane(grid.querySelector('[data-pane="maestro"]'));
    if (projectId) {
      const selectedProject = projectId;
      api(`/api/dev/test-profiles?project_id=${encodeURIComponent(selectedProject)}`)
        .then(data => {
          if (selectedProject !== projectId) return;
          testProfiles = data.profiles || [];
          testProfilesLoading = false;
          knownAgents.forEach((_agent, paneId) => renderAgentControls(paneId));
        })
        .catch(error => {
          if (selectedProject !== projectId) return;
          testProfilesLoading = false;
          testProfilesError = error.message;
          knownAgents.forEach((_agent, paneId) => renderAgentControls(paneId));
          document.getElementById('data-status').textContent = `Perfis de teste indisponíveis: ${error.message}`;
        });
      refresh();
    }
  };
  document.getElementById('project-toggle').addEventListener('click', () => {
    const nav = document.getElementById('project-nav');
    nav.hidden = !nav.hidden;
  });
  syncUI();
  void refreshModelCatalog();
  api('/api/projects').then(data => {
    projects = (data.projects || []).filter(project => project.enabled && project.dev_enabled);
    projectsLoaded = true;
    projectsError = '';
    renderProjects();
    if (projects.length) selectProject(projects[0].id);
    else document.getElementById('data-status').textContent = 'Nenhum projeto autorizado para Development Mode';
  }).catch(error => {
    projectsLoaded = true;
    projectsError = error.message;
    renderModelCatalog();
    document.getElementById('data-status').textContent = `Falha ao carregar projetos: ${error.message}`;
  });
})();
