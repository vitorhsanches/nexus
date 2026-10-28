(() => {
  const grid = document.getElementById('pane-grid');
  const paneNav = document.getElementById('pane-nav');
  const toast = document.getElementById('toast');
  const modelCatalogList = document.getElementById('model-catalog-list');
  const modelCatalogStatus = document.getElementById('model-catalog-status');
  const modelCatalogCount = document.getElementById('model-catalog-count');
  const modelCatalogProjectNote = document.getElementById('model-catalog-project-note');
  const modelCatalogRefreshButton = document.getElementById('model-catalog-refresh');
  const modelCatalogSearch = document.getElementById('model-filter-search');
  const modelCatalogProvider = document.getElementById('model-filter-provider');
  const modelCatalogCost = document.getElementById('model-filter-cost');
  const modelCatalogAvailability = document.getElementById('model-filter-availability');
  const modelCatalogQualification = document.getElementById('model-filter-qualification');
  const modelAdminTokenInput = document.getElementById('model-admin-token');
  const modelAdminUnlock = document.getElementById('model-admin-unlock');
  const modelAdminStatus = document.getElementById('model-admin-status');
  const costAuthModel = document.getElementById('cost-auth-model');
  const costAuthList = document.getElementById('cost-auth-list');
  const costAuthCreate = document.getElementById('cost-auth-create');
  const groupControls = document.getElementById('group-controls');
  const savedLayouts = new Map();
  const fiveLayouts = new Map();
  const preferredColumns = new Map();
  const gap = 6;
  const minimumWidth = 300;
  const minimumHeight = 210;
  const agentTemplate = grid.querySelector('[data-pane="agent-a"]').cloneNode(true);
  grid.querySelector('[data-pane="agent-a"]').remove();
  grid.querySelector('[data-pane="agent-b"]').remove();
  // Panes owned by the cockpit itself (not per-agent): never created or removed from agent data.
  const systemPanes = ['maestro', 'native', 'review'];
  const compactSystemPanes = grid.querySelector('[data-pane="maestro"]')?.dataset.workspaceInactive === 'true';
  const knownAgents = new Map();
  const projectListeners = new Set();
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
  let modelAdminToken = '';
  let modelCatalogRequest = 0;
  let aiSettings = null;
  let aiSettingsRequest = 0;
  let aiSettingsOpen = false;
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
  const allPanes = () => [...grid.querySelectorAll(':scope > .pane')];
  const panes = () => allPanes().filter(pane => pane.dataset.workspaceInactive !== 'true');
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
    pane.dataset.title = ({ maestro: 'Maestro', native: 'Codex Native', review: 'Revisão' })[pane.dataset.pane];
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
    const status = document.createElement('span');
    status.className = 'pane-nav-status';
    button.append(dot, label, status);
    return button;
  };

  // Mirrors each pane's own status badge (the single source of truth already
  // rendered by updateAgentPane/renderSystemPanes) into the sidebar row --
  // never a second, independently computed status.
  const updateSidebar = () => {
    const existing = new Map([...paneNav.children].map(button => [button.dataset.paneTarget, button]));
    panes().forEach(pane => {
      const button = existing.get(pane.dataset.pane) || makeNavButton(pane);
      button.classList.toggle('active', pane.classList.contains('selected'));
      button.classList.toggle('is-minimized', pane.classList.contains('minimized'));
      button.querySelector('.pane-nav-name').textContent = paneTitle(pane);
      const statusNode = button.querySelector('.pane-nav-status');
      if (statusNode) {
        const badge = pane.querySelector('.pane-status');
        statusNode.textContent = (badge?.lastChild?.textContent || '').trim();
        statusNode.classList.toggle('is-ready', Boolean(badge?.classList.contains('ready')));
        statusNode.classList.toggle('is-waiting', Boolean(badge?.classList.contains('waiting')));
      }
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
        divider.style.bottom = '0px';
      } else {
        const y = layout.rowPixels.slice(0, index + 1).reduce((a, b) => a + b, 0) + gap * index + gap / 2;
        divider.style.top = `${y}px`;
        divider.style.left = layout.featured ? `${layout.columnPixels[0] + gap}px` : '0px';
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
  const clearFivePositioning = () => {
    grid.style.removeProperty('display');
    allPanes().forEach(pane => ['position', 'left', 'top', 'width', 'height'].forEach(name => pane.style.removeProperty(name)));
  };
  const drawFivePaneLayout = visible => {
    const key = visible.map(pane => pane.dataset.pane).join(',');
    // Flowbyte-style cockpit: the primary terminal gets a little more width,
    // while the two lower panes remain balanced for side-by-side review.
    const saved = fiveLayouts.get(key) || { top: [.37, .69], bottom: .5, row: .56 };
    const width = grid.clientWidth;
    const height = grid.clientHeight;
    const topWidths = [saved.top[0] * width, (saved.top[1] - saved.top[0]) * width, (1 - saved.top[1]) * width];
    const bottomWidths = [saved.bottom * width, (1 - saved.bottom) * width];
    const topHeight = saved.row * height;
    const place = (pane, x, y, w, h) => {
      pane.style.position = 'absolute';
      pane.style.left = `${x}px`;
      pane.style.top = `${y}px`;
      pane.style.width = `${Math.max(0, w - gap)}px`;
      pane.style.height = `${Math.max(0, h - gap)}px`;
    };
    let x = 0;
    visible.slice(0, 3).forEach((pane, index) => { place(pane, x, 0, topWidths[index], topHeight); x += topWidths[index]; });
    x = 0;
    visible.slice(3).forEach((pane, index) => { place(pane, x, topHeight, bottomWidths[index], height - topHeight); x += bottomWidths[index]; });
    clearDividers();
    const divider = (axis, segment, index, position) => {
      const node = document.createElement('div');
      node.className = `grid-divider ${axis === 'row' ? 'horizontal' : 'vertical'}`;
      node.setAttribute('role', 'separator');
      node.setAttribute('tabindex', '0');
      node.setAttribute('aria-orientation', axis === 'row' ? 'horizontal' : 'vertical');
      node.setAttribute('aria-label', 'Redimensionar panes adjacentes');
      if (axis === 'row') { node.style.top = `${position}px`; node.style.left = '0'; }
      else { node.style.left = `${position}px`; node.style.top = segment === 'top' ? '0' : `${topHeight}px`; node.style.bottom = segment === 'top' ? `${height - topHeight}px` : '0'; }
      const change = value => {
        if (axis === 'row') saved.row = Math.max(minimumHeight / height, Math.min(1 - minimumHeight / height, value / height));
        else if (segment === 'bottom') saved.bottom = Math.max(minimumWidth / width, Math.min(1 - minimumWidth / width, value / width));
        else {
          const low = index === 0 ? minimumWidth / width : saved.top[0] + minimumWidth / width;
          const high = index === 0 ? saved.top[1] - minimumWidth / width : 1 - minimumWidth / width;
          saved.top[index] = Math.max(low, Math.min(high, value / width));
        }
        fiveLayouts.set(key, saved);
        drawFivePaneLayout(visible);
      };
      node.addEventListener('pointerdown', event => {
        if (event.button !== 0) return;
        event.preventDefault();
        const start = axis === 'row' ? event.clientY : event.clientX;
        const initial = position;
        const move = pointer => change(initial + (axis === 'row' ? pointer.clientY : pointer.clientX) - start);
        const finish = () => {
          window.removeEventListener('pointermove', move);
          window.removeEventListener('pointerup', finish);
        };
        window.addEventListener('pointermove', move);
        window.addEventListener('pointerup', finish);
      });
      node.addEventListener('keydown', event => {
        const delta = { ArrowLeft: -16, ArrowRight: 16, ArrowUp: -16, ArrowDown: 16 }[event.key];
        if (delta === undefined) return;
        event.preventDefault();
        change(position + delta);
      });
      grid.append(node);
    };
    divider('column', 'top', 0, topWidths[0]);
    divider('column', 'top', 1, topWidths[0] + topWidths[1]);
    divider('column', 'bottom', 0, bottomWidths[0]);
    divider('row', '', 0, topHeight);
  };
  const updateLayout = () => {
    const all = panes();
    clearFivePositioning();
    if (!all.length) {
      grid.dataset.visible = '0';
      grid.style.removeProperty('grid-template-columns');
      grid.style.removeProperty('grid-template-rows');
      groupControls.hidden = true;
      layout = null;
      clearDividers();
      document.getElementById('grid-layout-label').textContent = '▦  Sem panes abertas';
      return;
    }
    if (mobileLayout()) {
      all.forEach(pane => { pane.hidden = false; pane.classList.remove('featured'); });
      groupControls.hidden = true;
      grid.style.removeProperty('grid-template-columns');
      grid.style.removeProperty('grid-template-rows');
      grid.dataset.visible = String(all.length);
      document.getElementById('grid-layout-label').textContent = '▦  Empilhado';
      document.getElementById('toggle-layout').hidden = true;
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
    if (compactSystemPanes && visible.length === 5 && maxColumns >= 3 && maxRows === 2 && !grid.classList.contains('has-maximized')) {
      grid.style.display = 'block';
      grid.dataset.visible = '5';
      grid.dataset.columns = '3';
      grid.dataset.rows = '2';
      groupControls.hidden = groupCount === 1;
      document.getElementById('group-label').textContent = `Grupo ${groupIndex + 1}/${groupCount}`;
      document.getElementById('previous-group').disabled = groupIndex === 0;
      document.getElementById('next-group').disabled = groupIndex === groupCount - 1;
      document.getElementById('grid-layout-label').textContent = '▦  Grade 3 + 2';
      document.getElementById('toggle-layout').hidden = true;
      layout = null;
      drawFivePaneLayout(visible);
      return;
    }
    const firstColumnCount = Math.ceil(visible.length / maxRows);
    const lastColumnCount = Math.min(maxColumns, visible.length);
    const columnOptions = Array.from({ length: lastColumnCount - firstColumnCount + 1 }, (_, index) => firstColumnCount + index);
    const preferenceKey = `${groupIndex}:${visible.length}`;
    const automaticColumns = visible.length >= 3 && maxRows === 2 ? firstColumnCount : lastColumnCount;
    const columns = columnOptions.includes(preferredColumns.get(preferenceKey))
      ? preferredColumns.get(preferenceKey) : automaticColumns;
    const rows = Math.ceil(visible.length / columns);
    const featured = rows === 2 && ((visible.length === 3 && columns === 2) || (!compactSystemPanes && visible.length === 5 && columns === 3));
    all.forEach(pane => pane.classList.toggle('featured', featured && pane === visible[0]));
    grid.dataset.visible = String(visible.length);
    grid.dataset.columns = String(columns);
    grid.dataset.rows = String(rows);
    groupControls.hidden = groupCount === 1;
    document.getElementById('group-label').textContent = `Grupo ${groupIndex + 1}/${groupCount}`;
    document.getElementById('previous-group').disabled = groupIndex === 0;
    document.getElementById('next-group').disabled = groupIndex === groupCount - 1;
    document.getElementById('grid-layout-label').textContent = `▦  Grade ${columns} × ${rows}`;
    document.getElementById('toggle-layout').hidden = columnOptions.length < 2 || grid.classList.contains('has-maximized');
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
      key, columns, rows, featured, columnOptions, preferenceKey,
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
    if (!pane || pane.dataset.workspaceInactive === 'true') return;
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
  const setPaneAvailable = (kind, available) => {
    if (!compactSystemPanes) return;
    const pane = grid.querySelector(`[data-pane="${kind}"]`);
    if (!pane || !systemPanes.includes(kind)) return;
    const wasAvailable = pane.dataset.workspaceInactive !== 'true';
    if (wasAvailable === available) return;
    pane.dataset.workspaceInactive = available ? 'false' : 'true';
    if (!available && pane.classList.contains('selected')) {
      pane.classList.remove('selected');
      const next = panes()[0];
      if (next) next.classList.add('selected');
    }
    syncUI();
  };

  const addCloseControl = pane => {
    if (systemPanes.includes(pane.dataset.pane)) return;
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
    setPaneAvailable('maestro', true);
    selectPane(pane);
    pane.scrollIntoView({ behavior: 'smooth', block: 'center' });
    pane.focus({ preventScroll: true });
    notify('Pane Maestro selecionado');
  });
  document.getElementById('open-review').addEventListener('click', () => {
    setPaneAvailable('review', true);
    selectPane(grid.querySelector('[data-pane="review"]'));
  });
  document.getElementById('open-native-session').addEventListener('click', () => {
    setPaneAvailable('native', true);
    selectPane(grid.querySelector('[data-pane="native"]'));
  });
  document.getElementById('menu-toggle').addEventListener('click', event => {
    const open = document.getElementById('sidebar').classList.toggle('open');
    event.currentTarget.setAttribute('aria-expanded', String(open));
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    if (!aiSection.hidden) { aiSection.hidden = true; aiSettingsOpen = false; }
    document.getElementById('settings-advanced').open = false;
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
    fiveLayouts.clear();
    preferredColumns.clear();
    updateLayout();
    syncStreams();
    notify('Layout automático restaurado');
  });
  document.getElementById('toggle-layout').addEventListener('click', () => {
    if (!layout || layout.columnOptions.length < 2) return;
    const index = layout.columnOptions.indexOf(layout.columns);
    preferredColumns.set(layout.preferenceKey, layout.columnOptions[(index + 1) % layout.columnOptions.length]);
    updateLayout();
    syncStreams();
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
      headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(modelAdminToken && (options.method === 'POST' || options.admin) ? { Authorization: `Bearer ${modelAdminToken}` } : {}) },
      ...(options.body ? { body: JSON.stringify(options.body) } : {})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data?.detail?.message || `HTTP ${response.status}`);
    return data;
  };
  const costOf = model => modelCatalog.find(row => row.id === model)?.cost_status ||
    Object.values(aiSettings?.roles || {}).flatMap(role => role.models || []).find(row => row.id === model)?.cost_status || 'unknown';
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
    : value === 'not-discovered' ? 'Não anunciado atualmente pelo OmniRoute'
    : value === 'omniroute-unavailable' ? 'OmniRoute indisponível · seleção preservada'
    : (value || 'Indisponível');
  const modelManagementLabel = value => ({
    active: 'Ativo', inactive: 'Inativo', candidate: 'Candidato · não libera execução',
    not_qualified: 'Não qualificado', blocked: 'Bloqueado'
  })[value] || 'Não qualificado';
  const modelIsQualified = model => model.qualification_status === 'qualified' || model.approval_status === 'approved';
  const modelIsActive = model => model.active === undefined ? Boolean(model.can_execute) : Boolean(model.active);
  const filterModelCatalog = () => {
    const query = (modelCatalogSearch.value || '').trim().toLocaleLowerCase();
    const provider = modelCatalogProvider.value;
    const cost = modelCatalogCost.value;
    const availability = modelCatalogAvailability.value;
    const qualification = modelCatalogQualification.value;
    return discoveredModelCatalog.filter(model => {
      if (query && !`${model.id} ${model.name || ''}`.toLocaleLowerCase().includes(query)) return false;
      if (provider && model.provider !== provider) return false;
      if (cost && (model.cost_status || 'unknown') !== cost) return false;
      if (availability === 'available' && model.discovered === false) return false;
      if (availability === 'missing' && model.discovered !== false) return false;
      if (availability === 'unavailable' && model.discovered !== null) return false;
      const currentQualification = model.qualification_status || (model.approval_status === 'approved' ? 'qualified' : model.approval_status || 'not_qualified');
      if (qualification && currentQualification !== qualification) return false;
      return true;
    });
  };
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
    const providers = [...new Set(discoveredModelCatalog.map(model => model.provider || 'unknown'))].sort();
    const selectedProvider = modelCatalogProvider.value;
    modelCatalogProvider.replaceChildren(element('option', '', 'Todos os provedores'));
    modelCatalogProvider.children[0].value = '';
    providers.forEach(provider => {
      const option = element('option', '', provider); option.value = provider; modelCatalogProvider.append(option);
    });
    if (providers.includes(selectedProvider)) modelCatalogProvider.value = selectedProvider;
    const filteredModels = filterModelCatalog();
    if (!filteredModels.length) {
      const empty = modelCatalogError
        ? 'Não foi possível obter modelos do OmniRoute. Atualize o catálogo quando o serviço estiver disponível.'
        : discoveredModelCatalog.length ? 'Nenhum modelo corresponde aos filtros.' : 'Nenhum modelo foi anunciado pelo OmniRoute.';
      modelCatalogList.replaceChildren(element('p', 'model-catalog-empty', empty));
      return;
    }
    modelCatalogList.replaceChildren(...filteredModels.map(model => {
      const status = ['approved', 'not_qualified', 'blocked'].includes(model.approval_status)
        ? model.approval_status : 'not_qualified';
      const managementStatus = model.management_status || (modelIsActive(model) ? 'active' : modelIsQualified(model) ? 'inactive' : status === 'blocked' ? 'blocked' : model.candidate ? 'candidate' : 'not_qualified');
      const card = element('article', `model-catalog-card is-${status} is-${managementStatus}`);
      const heading = element('div', 'model-catalog-card-head');
      heading.append(element('strong', 'model-catalog-card-name', model.name || model.id || 'Modelo sem nome'),
        element('span', `model-catalog-badge is-${status}`, modelApprovalLabel(status)));
      const stateBadge = element('span', `model-catalog-badge is-${managementStatus}`, modelManagementLabel(managementStatus));
      heading.append(stateBadge);
      card.append(heading,
        element('p', 'model-catalog-meta', `${model.id || 'ID desconhecido'} · provedor ${model.provider || 'desconhecido'}`),
        element('p', 'model-catalog-meta', `${modelAvailabilityLabel(model.availability)} · custo ${modelCostLabel(model.cost_status)}`),
        element('p', 'model-catalog-reason', model.approval_reason || 'Sem qualificação para execução.'));
      const actions = element('div', 'model-catalog-card-actions');
      const blocked = status === 'blocked' || managementStatus === 'blocked';
      if (modelIsQualified(model) && !blocked) {
        const toggle = element('button', 'model-catalog-action', modelIsActive(model) ? 'Desativar' : 'Ativar');
        toggle.type = 'button';
        toggle.disabled = pending.has(`model:${model.id}`);
        toggle.addEventListener('click', async () => {
          const key = `model:${model.id}`; pending.add(key); renderModelCatalog();
          try {
            const data = await api('/api/dev/model-management/activation', { method: 'POST', body: { model_id: model.id, active: !modelIsActive(model) } });
            discoveredModelCatalog = Array.isArray(data.models) ? data.models : discoveredModelCatalog;
            modelCatalog = Array.isArray(data.approved_models) ? data.approved_models : modelCatalog;
            renderModelCatalog(); renderPlanningControls();
            knownAgents.forEach((_agent, paneId) => renderAgentControls(paneId));
            notify(modelIsActive(model) ? 'Modelo desativado para novas execuções' : 'Modelo ativo; autorização de custos permanece independente');
          } catch (error) { notify(`Falha ao alterar modelo: ${error.message}`); }
          finally { pending.delete(key); renderModelCatalog(); }
        });
        actions.append(toggle);
      } else if (!blocked && model.approval_status !== 'approved' && (model.discovered !== false || model.candidate)) {
        const candidate = element('button', 'model-catalog-action', model.candidate ? 'Remover candidato' : 'Marcar candidato');
        candidate.type = 'button'; candidate.disabled = pending.has(`candidate:${model.id}`);
        candidate.addEventListener('click', async () => {
          const key = `candidate:${model.id}`; pending.add(key); renderModelCatalog();
          try {
            const data = await api('/api/dev/model-management/candidate', { method: 'POST', body: { model_id: model.id, candidate: !model.candidate } });
            discoveredModelCatalog = Array.isArray(data.models) ? data.models : discoveredModelCatalog;
            renderModelCatalog();
            notify(model.candidate ? 'Candidato removido' : 'Candidato registrado; execução continua bloqueada');
          } catch (error) { notify(`Falha ao atualizar candidato: ${error.message}`); }
          finally { pending.delete(key); renderModelCatalog(); }
        });
        actions.append(candidate);
      }
      if (!blocked && model.discovered === true && !model.qualification?.status?.includes('QUALIFIED')) {
        const qualificationForm = element('div', 'model-qualification-form');
        qualificationForm.append(element('strong', '', 'Qualificação explícita · novo modelo ficará inativo'));
        const provider = element('input'); provider.value = model.provider || ''; provider.setAttribute('aria-label', 'Provedor confirmado');
        const capabilities = element('div', 'model-qualification-options');
        const selectedCapabilities = [];
        ['mechanical', 'standard-coding', 'advanced-coding', 'review', 'planning'].forEach(value => {
          const label = element('label'); const checkbox = element('input'); checkbox.type = 'checkbox'; checkbox.value = value;
          checkbox.addEventListener('change', () => checkbox.checked ? selectedCapabilities.push(value) : selectedCapabilities.splice(selectedCapabilities.indexOf(value), 1));
          label.append(checkbox, document.createTextNode(value)); capabilities.append(label);
        });
        const efforts = element('div', 'model-qualification-options');
        const selectedEfforts = [];
        (model.supported_efforts || []).forEach(value => {
          const label = element('label'); const checkbox = element('input'); checkbox.type = 'checkbox'; checkbox.value = value;
          checkbox.addEventListener('change', () => checkbox.checked ? selectedEfforts.push(value) : selectedEfforts.splice(selectedEfforts.indexOf(value), 1));
          label.append(checkbox, document.createTextNode(`Esforço ${value}`)); efforts.append(label);
        });
        const cost = element('select'); cost.setAttribute('aria-label', 'Classificação de custo explícita');
        [['unknown','Desconhecido'],['free','Grátis'],['low_cost','Baixo custo'],['standard','Padrão'],['premium','Premium']].forEach(([value,label]) => { const option=element('option','',label); option.value=value; cost.append(option); });
        const risk = element('select'); risk.setAttribute('aria-label', 'Risco máximo explícito');
        ['low','medium','high'].forEach(value => { const option=element('option','',`Risco máximo ${value}`); option.value=value; risk.append(option); });
        const qualifyButton = element('button', 'model-catalog-action', 'Validar metadados e qualificar');
        qualifyButton.type = 'button'; qualifyButton.disabled = !model.appserver_compatible || !model.supported_efforts?.length;
        qualifyButton.addEventListener('click', async () => {
          if (!modelAdminToken) { notify('Desbloqueie as alterações administrativas primeiro'); return; }
          const key = `qualification:${model.id}`; pending.add(key); renderModelCatalog();
          try {
            const data = await api('/api/dev/model-management/qualifications', { method: 'POST', body: { model_id: model.id, provider: provider.value.trim(), capabilities: selectedCapabilities, efforts: selectedEfforts, cost_class: cost.value, max_risk_level: risk.value } });
            discoveredModelCatalog = data.models || discoveredModelCatalog; modelCatalog = data.approved_models || modelCatalog;
            renderModelCatalog(); notify('Qualificação salva. O modelo permanece inativo até ativação explícita.');
          } catch (error) { notify(`Qualificação recusada: ${error.message}`); }
          finally { pending.delete(key); renderModelCatalog(); }
        });
        qualificationForm.append(provider, capabilities, efforts, cost, risk, qualifyButton,
          element('small', '', model.appserver_compatible ? 'Compatibilidade de protocolo e esforços será revalidada no backend; inferência real não foi testada.' : 'Bloqueado: Codex App-Server não confirmou este modelo e esforços.'));
        card.append(qualificationForm);
      }
      if (model.qualification?.status === 'QUALIFIED' && !blocked) {
        const revoke = element('button', 'model-catalog-action', 'Desqualificar'); revoke.type = 'button';
        revoke.addEventListener('click', async () => {
          if (!modelAdminToken) { notify('Desbloqueie as alterações administrativas primeiro'); return; }
          try { const data = await api('/api/dev/model-management/qualifications/revoke', { method: 'POST', body: { model_id: model.id } }); discoveredModelCatalog = data.models || discoveredModelCatalog; modelCatalog = data.approved_models || modelCatalog; renderModelCatalog(); notify('Qualificação revogada; ativações limpas.'); }
          catch (error) { notify(`Falha ao desqualificar: ${error.message}`); }
        });
        actions.append(revoke);
      }
      if (actions.children.length) card.append(actions);
      return card;
    }));
  };
  [modelCatalogSearch, modelCatalogProvider, modelCatalogCost, modelCatalogAvailability, modelCatalogQualification]
    .forEach(control => control.addEventListener('input', renderModelCatalog));
  [modelCatalogProvider, modelCatalogCost, modelCatalogAvailability, modelCatalogQualification]
    .forEach(control => control.addEventListener('change', renderModelCatalog));
  modelAdminUnlock.addEventListener('click', async () => {
    const token = modelAdminTokenInput.value.trim();
    try {
      const status = await api('/api/dev/model-management/admin-status');
      if (!status.admin_auth_configured || token.length < 32) throw new Error('Configure NEXUS_ADMIN_TOKEN com pelo menos 32 caracteres no processo do servidor.');
      modelAdminToken = token; modelAdminTokenInput.value = ''; modelAdminStatus.textContent = 'Alterações administrativas desbloqueadas nesta sessão do navegador'; renderModelCatalog();
    } catch (error) { modelAdminToken = ''; modelAdminStatus.textContent = 'Alterações administrativas bloqueadas'; notify(error.message); }
  });
  const renderCostAuthorizations = () => {
    const selected = costAuthModel.value;
    costAuthModel.replaceChildren(element('option', '', 'Selecione um modelo aprovado'));
    const payable = [...modelCatalog, ...discoveredModelCatalog.filter(model => model.cost_status !== 'free' && model.active && model.approval_status === 'approved' && model.discovered === true && !modelCatalog.some(approved => approved.id === model.id))];
    payable.filter(model => model.cost_status !== 'free').forEach(model => {
      const option = element('option', '', `${model.id} · ${model.cost_status || 'unknown'}`);
      option.value = model.id;
      costAuthModel.append(option);
    });
    if ([...costAuthModel.children].some(option => option.value === selected)) costAuthModel.value = selected;
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
  const aiSection = document.getElementById('settings-ai');
  const aiStatus = document.getElementById('ai-settings-status');
  const aiExecutors = document.getElementById('ai-executors');
  const aiRoles = document.getElementById('ai-roles');
  const aiRoleLabels = { maestro: 'Maestro · planejamento', development: 'Desenvolvimento', review: 'Revisão' };
  const renderAiSettings = () => {
    aiExecutors.replaceChildren();
    aiRoles.replaceChildren();
    if (!aiSettings) return;
    Object.entries(aiSettings.executors || {}).forEach(([key, provider]) => {
      if (!provider.connected) return;
      const card = element('article', `ai-executor ${provider.enabled ? 'is-connected' : 'is-disconnected'}`);
      card.append(element('strong', '', provider.name), element('span', 'ai-state', provider.enabled ? 'Ativo no Nexus' : 'Inativo no Nexus'), element('p', '', provider.detail));
      if (provider.native_cli) card.append(element('small', '', provider.native_cli));
      const toggle = element('button', '', provider.enabled ? 'Desativar no Nexus' : 'Ativar no Nexus');
      toggle.type = 'button';
      toggle.addEventListener('click', async () => {
        if (!modelAdminToken || !projectId) { notify('Autentique o operador para alterar provedores.'); return; }
        toggle.disabled = true;
        try {
          aiSettings = await api('/api/dev/ai-settings/providers', { method: 'POST', body: {
            project_id: projectId, provider: key, enabled: !provider.enabled
          } });
          renderAiSettings(); renderPlanningControls();
        } catch (error) { notify(error.message); toggle.disabled = false; }
      });
      card.append(toggle); aiExecutors.append(card);
    });
    if (!aiExecutors.childElementCount) aiExecutors.append(element('p', 'ai-role-note', 'Nenhum provedor OAuth conectado e habilitado no OmniRoute. Confira as conexões pelo OmniRoute.'));
    Object.entries(aiRoleLabels).forEach(([role, label]) => {
      const data = aiSettings.roles?.[role] || { models: [], selection: null, valid: false };
      const card = element('article', 'ai-role');
      card.append(element('h3', '', label));
      const executor = element('select'); executor.setAttribute('aria-label', `Provedor de ${label}`);
      const providers = Object.entries(aiSettings.executors || {}).filter(([, item]) => item.connected && item.enabled);
      executor.append(...providers.map(([key, item]) => { const option = element('option', '', item.name); option.value = key; return option; }));
      executor.value = data.valid && providers.some(([key]) => key === data.selection.provider)
        ? data.selection.provider : (providers[0]?.[0] || '');
      const model = element('select'); model.setAttribute('aria-label', `Modelo de ${label}`);
      const updateModels = () => {
        const selected = model.value;
        const empty = element('option', '', 'Selecione um modelo elegível'); empty.value = '';
        const rows = data.models.filter(row => row.provider === executor.value);
        model.replaceChildren(empty, ...rows.map(row => {
          const option = element('option', '', `${row.name} · ${row.cost_status}`);
          option.value = row.id; return option;
        }));
        model.value = rows.some(row => row.id === selected) ? selected :
          (data.valid && rows.some(row => row.id === data.selection.model_id) ? data.selection.model_id : '');
      };
      executor.addEventListener('change', updateModels); updateModels();
      const note = element('p', 'ai-role-note', data.selection
        ? (data.valid ? 'Preferência salva · revalidada antes de novas execuções.' : 'Preferência salva indisponível: verifique conexão, qualificação, ativação, capacidade ou custos.')
        : 'Sem preferência salva. O comportamento anterior permanece.');
      const save = element('button', '', 'Salvar'); save.type = 'button';
      const clear = element('button', '', 'Limpar'); clear.type = 'button';
      save.disabled = !providers.length || !data.models.length;
      clear.disabled = !data.selection;
      save.addEventListener('click', async () => {
        if (!projectId || !modelAdminToken || !model.value) { notify('Selecione um modelo e autentique o operador.'); return; }
        save.disabled = true;
        try {
          aiSettings = await api('/api/dev/ai-settings', { method: 'POST', body: { project_id: projectId, role, executor: executor.value, model_id: model.value } });
          renderAiSettings(); renderPlanningControls(); notify('Preferência de IA salva.');
        } catch (error) { note.textContent = error.message; save.disabled = false; }
      });
      clear.addEventListener('click', async () => {
        if (!projectId || !modelAdminToken) { notify('Autentique o operador para limpar a preferência.'); return; }
        clear.disabled = true;
        try {
          aiSettings = await api('/api/dev/ai-settings/clear', { method: 'POST', body: { project_id: projectId, role } });
          renderAiSettings(); renderPlanningControls(); notify('Preferência removida.');
        } catch (error) { note.textContent = error.message; clear.disabled = false; }
      });
      const fields = element('div', 'ai-role-fields'); fields.append(executor, model);
      const actions = element('div', 'ai-role-actions'); actions.append(save, clear);
      card.append(fields, actions, note); aiRoles.append(card);
    });
  };
  const refreshAiSettings = async (force = false) => {
    const request = ++aiSettingsRequest;
    if (!projectId) { aiSettings = null; aiStatus.textContent = 'Selecione um projeto autorizado.'; renderAiSettings(); return; }
    const selectedProject = projectId;
    aiStatus.textContent = 'Verificando conexões OAuth do OmniRoute e modelos elegíveis…';
    try {
      const data = await api(`/api/dev/ai-settings?project_id=${encodeURIComponent(selectedProject)}${force ? '&refresh=true' : ''}`);
      if (request !== aiSettingsRequest || selectedProject !== projectId) return;
      aiSettings = data;
      aiStatus.textContent = data.catalog_error ? `Catálogo indisponível: ${data.catalog_error}` : 'Preferências do projeto · modelos elegíveis por função';
      renderAiSettings(); renderPlanningControls();
    } catch (error) {
      if (request !== aiSettingsRequest || selectedProject !== projectId) return;
      aiSettings = null; aiStatus.textContent = `Configurações indisponíveis: ${error.message}`; renderAiSettings();
    }
  };
  const refreshProviderStatus = async () => {
    if (!projectId || !aiSettings) return;
    const selectedProject = projectId;
    try {
      const data = await api(`/api/dev/ai-settings/providers?project_id=${encodeURIComponent(selectedProject)}`);
      if (selectedProject !== projectId || !aiSettings) return;
      const old = aiSettings.executors;
      aiSettings.executors = data.executors;
      if (Object.keys(data.executors).some(key => data.executors[key].executable && !old?.[key]?.executable)) {
        void refreshAiSettings(true);
      } else {
        renderAiSettings(); renderPlanningControls();
        knownAgents.forEach((_agent, paneId) => renderAgentControls(paneId));
      }
    } catch (_error) {
      if (selectedProject !== projectId || !aiSettings) return;
      Object.values(aiSettings.executors || {}).forEach(provider => { provider.connected = false; provider.executable = false; });
      renderAiSettings(); renderPlanningControls();
      knownAgents.forEach((_agent, paneId) => renderAgentControls(paneId));
    }
  };
  document.getElementById('ai-admin-unlock').addEventListener('click', async () => {
    const token = document.getElementById('ai-admin-token').value.trim();
    if (token.length < 32) { notify('Informe o token administrativo configurado no servidor.'); return; }
    const previous = modelAdminToken; modelAdminToken = token;
    try {
      await api('/api/dev/ai-settings/admin-check', { admin: true });
      document.getElementById('ai-admin-token').value = '';
      document.getElementById('ai-admin-status').textContent = 'Operador autenticado nesta sessão do navegador';
    } catch (error) { modelAdminToken = previous; notify(error.message); }
  });
  document.getElementById('ai-settings-refresh').addEventListener('click', () => { void refreshAiSettings(true); });
  const openAiSettings = () => {
    aiSettingsOpen = true;
    aiSection.hidden = false;
    document.getElementById('settings-advanced').open = false;
    aiSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('menu-toggle').setAttribute('aria-expanded', 'false');
    void refreshAiSettings();
  };
  document.getElementById('settings-ai-button').addEventListener('click', openAiSettings);
  document.getElementById('settings-ai-link').addEventListener('click', openAiSettings);
  document.getElementById('close-ai-settings').addEventListener('click', () => { aiSection.hidden = true; aiSettingsOpen = false; });
  modelCatalogRefreshButton.addEventListener('click', () => { void refreshModelCatalog(true); if (aiSettingsOpen) void refreshAiSettings(true); });
  document.getElementById('settings-models-link').addEventListener('click', () => {
    aiSection.hidden = true;
    aiSettingsOpen = false;
    document.getElementById('settings-advanced').open = true;
    document.getElementById('settings-models').scrollIntoView({ behavior: 'smooth', block: 'start' });
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('menu-toggle').setAttribute('aria-expanded', 'false');
  });
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
  const planningEffort = element('select', 'cockpit-model-select');
  planningEffort.setAttribute('aria-label', 'Esforço desta mensagem ou autorização do Maestro');
  const planningCost = checkbox();
  const planningCostLabel = formField('Autorizo o custo deste modelo para planejamento', planningCost);
  const authorize = element('button', 'cockpit-authorize', 'Autorizar planejamento');
  authorize.type = 'button';
  maestroComposer.controls.append(planningSelect, planningEffort, planningCostLabel, authorize);
  planningSelect.addEventListener('change', () => { planningEffort.value = ''; planningCost.checked = false; renderPlanningControls(); });
  planningEffort.addEventListener('change', () => renderPlanningControls());
  const renderPlanningControls = () => {
    const planning = maestroState?.planning;
    const selected = planningSelect.value;
    const preferred = aiSettings?.roles?.maestro;
    const preferredModel = preferred?.valid ? preferred.selection.model_id : '';
    const aiRows = (preferred?.models || []).filter(row => aiSettings?.executors?.[row.provider]?.executable);
    const planningModels = [...aiRows, ...modelCatalog.filter(row => !row.id.startsWith('cc/') && !row.id.startsWith('cx/'))];
    const emptyOption = element('option', '', 'Modelo de planejamento…');
    emptyOption.value = '';
    planningSelect.replaceChildren(emptyOption, ...planningModels.map(row => {
      const option = element('option', '', `${row.name || row.id} · ${row.cost_status || 'unknown'}`);
      option.value = row.id;
      return option;
    }));
    const currentBase = aiRows.find(row => Object.values(row.routes_by_effort || {}).includes(planning?.model))?.id || planning?.model;
    planningSelect.value = planningModels.some(row => row.id === selected) ? selected :
      (preferredModel || (planningModels.some(row => row.id === currentBase) ? currentBase : ''));
    const selectedRow = planningModels.find(row => row.id === planningSelect.value);
    const previousEffort = planningEffort.value;
    const efforts = selectedRow?.efforts || selectedRow?.supported_efforts || [];
    const effortPrompt = element('option', '', 'Escolha o esforço desta execução'); effortPrompt.value = '';
    planningEffort.replaceChildren(effortPrompt, ...efforts.map(value => {
      const option = element('option', '', value); option.value = value; return option;
    }));
    planningEffort.value = efforts.includes(previousEffort) ? previousEffort : '';
    const exactModel = selectedRow?.routes_by_effort?.[planningEffort.value] || planningSelect.value;
    const planningGrant = activeCostAuthorizationFor(exactModel);
    planningCostLabel.hidden = true;
    planningCost.checked = false;
    authorize.textContent = planning?.authorized ? 'Trocar modelo' : 'Autorizar planejamento';
    authorize.disabled = pending.has('authorize') || !projectId || !planningSelect.value ||
      (selectedRow?.routes_by_effort && !planningEffort.value) ||
      (costOf(exactModel) !== 'free' && !planningGrant) ||
      (planning?.authorized && planning.model === exactModel);
    maestroComposer.note.textContent = planning?.authorized
      ? `IA autorizada: ${planning.model} · ${planning.cost_status || 'unknown'} · planejamento somente leitura${planningGrant ? ` · ${planningGrant.uses_remaining}/${planningGrant.max_uses} usos restantes` : ''}`
      : (planningSelect.value && costOf(exactModel) !== 'free' && !planningGrant
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
        project_id: selectedProject, model, ...(planningEffort.value ? { effort: planningEffort.value } : {}), confirm_cost: false
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
      body = { project_id: selectedProject, text, ...(planningEffort.value ? { effort: planningEffort.value } : {}) };
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
      body = { text, ...(composer.effort?.value ? { effort: composer.effort.value } : {}), ...(!authorized ? { confirm_model: agent.model } : {}),
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
        ...(controls.effort.value ? { effort: controls.effort.value } : {}),
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
    const route = modelCatalog.find(item => item.id === agent.model);
    const oauthProvider = agent.model?.startsWith('cc/') ? 'claude_code' : agent.model?.startsWith('cx/') ? 'codex' : null;
    const providerUnavailable = oauthProvider && aiSettings?.executors?.[oauthProvider]?.executable !== true;
    const supportedEfforts = oauthProvider
      ? (route?.supported_efforts || []).filter(value => route?.confirmed_efforts?.includes(value))
      : (route?.supported_efforts || []);
    const updateEffort = select => {
      const oldValue = select.dataset.modelId === agent.model ? select.value : '';
      const prompt = element('option', '', 'Esforço desta execução'); prompt.value = '';
      select.replaceChildren(prompt, ...supportedEfforts.map(value => {
        const option = element('option', '', value); option.value = value; return option;
      }));
      select.value = supportedEfforts.includes(oldValue) ? oldValue : '';
      select.dataset.modelId = agent.model || '';
    };
    updateEffort(composer.effort);
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
    updateEffort(mission.effort);
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
      agent.session?.status !== 'ACTIVE' || !agent.model || providerUnavailable ||
      (agent.model.startsWith('cc/') || agent.model.startsWith('cx/')) && !composer.effort.value;
    mission.start.disabled = pending.has(paneId) || !workspaceState?.execution_enabled || agent.busy || activeMission ||
      agent.session?.status !== 'ACTIVE' || !agent.model || !mission.profile.value || providerUnavailable ||
      (agent.model.startsWith('cc/') || agent.model.startsWith('cx/')) && !mission.effort.value ||
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
    [['BACKEND', agent.backend || agent.session?.backend || '—'], ['MODELO', agent.model || 'Não selecionado'],
      ['EFFORT', agent.brief?.authorization?.effort || '—'], ['SESSÃO', agent.session?.session_id || 'Ausente']].forEach(([label, value]) => {
      const field = element('span', '', `${label} `);
      field.append(element('b', '', value));
      meta.append(field);
    });
    pane.querySelector('.terminal-kicker').replaceChildren(element('span', '', '●'), document.createTextNode(' EVENTOS NEXUS / NÃO É SHELL '), element('span', 'right', status));
    const path = agent.session?.worktree_path || agent.session?.branch || 'Sem worktree ativo';
    pane.querySelector('.foot-path').textContent = path;
    pane.querySelector('.foot-path').title = path;
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
    const agentEffort = element('select', 'cockpit-model-select');
    agentEffort.setAttribute('aria-label', 'Esforço desta mensagem do agente');
    composer.effort = agentEffort;
    composer.controls.append(agentEffort);
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
    const missionEffort = element('select', 'cockpit-model-select');
    missionEffort.setAttribute('aria-label', 'Esforço desta missão');
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
    missionDetails.append(missionSummary, missionStatus, missionDescription, missionProfile, missionEffort, profileNote, missionActions, diff, missionError);
    composer.form.append(missionDetails);
    const missionControls = { composer, summary: missionSummary, status: missionStatus, description: missionDescription,
      profile: missionProfile, effort: missionEffort, profileNote, start: startMission, interrupt, inspect, diff, error: missionError };
    pane.missionControls = missionControls;
    startMission.addEventListener('click', () => { void submitMission(agent.pane_id, missionControls); });
    interrupt.addEventListener('click', () => { void interruptMission(agent.pane_id, missionControls); });
    inspect.addEventListener('click', () => { void inspectMission(missionControls); });
    missionProfile.addEventListener('change', () => renderAgentControls(agent.pane_id));
    missionEffort.addEventListener('change', () => renderAgentControls(agent.pane_id));
    agentEffort.addEventListener('change', () => renderAgentControls(agent.pane_id));
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
    const legacyItems = awaiting.length ? awaiting.map(agent => {
      const item = element('div', 'review-step');
      const details = element('div');
      details.append(element('strong', '', agent.title), element('small', '', agent.latest_mission.mission_id));
      item.append(element('span', 'step-number', '○'), details);
      return item;
    }) : [element('div', 'review-step', 'Nenhuma missão aguardando revisão.')];
    if (compactSystemPanes) {
      const summary = review.querySelector('#legacy-review-summary');
      if (summary) {
        summary.hidden = !awaiting.length;
        summary.replaceChildren(...(awaiting.length ? legacyItems : []));
      }
      if (awaiting.length) setPaneAvailable('review', true);
    } else {
      review.querySelector('.pane-status').lastChild.textContent = awaiting.length ? ' Aguardando' : ' Sem pendências';
      review.querySelector('.pane-meta').replaceChildren(element('span', '', `AGENTES  ${workspace.panes.length}`), element('span', '', `AGUARDANDO REVISÃO  ${awaiting.length}`));
      review.querySelector('.review-steps').replaceChildren(...legacyItems);
      review.querySelector('.pane-footer > span:last-child').textContent = 'INTEGRAÇÃO EXIGE APROVAÇÃO';
    }
    if (compactSystemPanes && (maestro?.busy || (maestro?.messages || []).length)) setPaneAvailable('maestro', true);
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
      panes().filter(pane => !systemPanes.includes(pane.dataset.pane) && !knownAgents.has(pane.dataset.pane)).forEach(pane => pane.remove());
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
      void refreshProviderStatus();
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
    aiSettings = null;
    aiSettingsRequest++;
    renderAiSettings();
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
    panes().filter(pane => !systemPanes.includes(pane.dataset.pane)).forEach(pane => pane.remove());
    renderProjects();
    syncUI();
    if (compactSystemPanes) systemPanes.forEach(kind => setPaneAvailable(kind, false));
    else selectPane(grid.querySelector('[data-pane="maestro"]'));
    projectListeners.forEach(listener => {
      try { listener(projectId); } catch (error) { notify(`Falha no painel do projeto: ${error.message}`); }
    });
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
      void refreshAiSettings();
    }
  };
  document.getElementById('project-toggle').addEventListener('click', () => {
    const nav = document.getElementById('project-nav');
    nav.hidden = !nav.hidden;
  });
  // Small surface for add-on panes (cockpit-native.js): the selected project and a toast.
  window.NexusCockpit = Object.freeze({
    projectId: () => projectId,
    notify,
    setPaneAvailable,
    onProject: listener => { projectListeners.add(listener); if (projectId) listener(projectId); },
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
