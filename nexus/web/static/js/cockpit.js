(() => {
  const grid = document.getElementById('pane-grid');
  const paneNav = document.getElementById('pane-nav');
  const toast = document.getElementById('toast');
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
  let projectId = null;
  let projects = [];
  let refreshTimer;
  let refreshGeneration = 0;
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
    notify('Layout automático restaurado');
  });
  let resizeFrame = 0;
  const scheduleLayout = () => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(updateLayout);
  };
  window.addEventListener('resize', scheduleLayout);
  new ResizeObserver(scheduleLayout).observe(grid);

  const element = (tag, className, value) => {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (value !== undefined) item.textContent = String(value);
    return item;
  };
  const api = async path => {
    const response = await fetch(path, { headers: { Accept: 'application/json' } });
    const data = await response.json();
    if (!response.ok) throw new Error(data?.detail?.message || `HTTP ${response.status}`);
    return data;
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
    pane.querySelector('.pane-footer > span:last-child').textContent = 'LEITURA · SEM CLI';
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
    if (!eventList.children.length) eventList.append(element('div', 'cockpit-event', 'Nenhuma atividade registrada para este agente.'));
    body.replaceChildren(pane.querySelector('.terminal-kicker'), eventList);
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
    grid.append(pane);
    updateAgentPane(pane, agent);
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
    const maestroPane = grid.querySelector('[data-pane="maestro"]');
    maestroPane.querySelector('.pane-status').lastChild.textContent = maestro?.busy ? ' Planejando' : ' Leitura';
    maestroPane.querySelector('.pane-meta').replaceChildren(element('span', '', `MODELO  ${maestro?.planning?.model || 'Não autorizado'}`), element('span', '', `SESSÃO  ${maestro?.conversation_id || '—'}`));
    const lastMessage = (maestro?.messages || []).at(-1);
    maestroPane.querySelector('.maestro-copy h3').textContent = lastMessage ? 'Última atividade do Maestro' : 'Maestro sem mensagens';
    maestroPane.querySelector('.maestro-copy p').textContent = lastMessage ? `${lastMessage.author} · ${lastMessage.status} · ${lastMessage.text || ''}` : 'Nenhuma atividade registrada para o projeto selecionado.';
    maestroPane.querySelector('.pane-footer > span:last-child').textContent = 'EVENTOS NEXUS · SEM CLI';
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
  const refresh = async () => {
    if (!projectId) return;
    const generation = refreshGeneration;
    try {
      const query = encodeURIComponent(projectId);
      const [workspace, maestro] = await Promise.all([
        api(`/api/workspace/state?project_id=${query}`),
        api(`/api/workspace/maestro?project_id=${query}`)
      ]);
      if (generation !== refreshGeneration) return;
      const agents = (workspace.panes || []).slice().sort((a, b) => (a.layout?.position || 0) - (b.layout?.position || 0));
      knownAgents.clear();
      agents.forEach(agent => knownAgents.set(agent.pane_id, agent));
      panes().filter(pane => !['maestro', 'review'].includes(pane.dataset.pane) && !knownAgents.has(pane.dataset.pane)).forEach(pane => pane.remove());
      if (!panes().some(pane => pane.classList.contains('selected'))) selectPane(grid.querySelector('[data-pane="maestro"]'));
      const histories = await Promise.all(agents.map(agent => api(`/api/workspace/panes/${encodeURIComponent(agent.pane_id)}/history`).catch(() => null)));
      if (generation !== refreshGeneration) return;
      agents.forEach((agent, index) => {
        let pane = panes().find(item => item.dataset.pane === agent.pane_id);
        if (!pane && !closedAgents.has(agent.pane_id)) pane = createAgentPane(agent);
        if (pane) updateAgentPane(pane, agent, histories[index]);
      });
      renderMissions(agents);
      renderSystemPanes(workspace, maestro);
      document.getElementById('execution-count').textContent = twoDigits(agents.reduce((total, agent) => total + (agent.mission_count || 0), 0));
      document.getElementById('data-status').textContent = 'Dados do Nexus · atualização automática';
      syncUI();
    } catch (error) {
      if (generation === refreshGeneration) document.getElementById('data-status').textContent = `Falha ao carregar: ${error.message}`;
    } finally {
      if (generation === refreshGeneration) refreshTimer = setTimeout(refresh, 15000);
    }
  };
  const selectProject = id => {
    projectId = id;
    refreshGeneration++;
    clearTimeout(refreshTimer);
    closedAgents.clear();
    knownAgents.clear();
    panes().filter(pane => !['maestro', 'review'].includes(pane.dataset.pane)).forEach(pane => pane.remove());
    renderProjects();
    syncUI();
    selectPane(grid.querySelector('[data-pane="maestro"]'));
    if (projectId) refresh();
  };
  document.getElementById('project-toggle').addEventListener('click', () => {
    const nav = document.getElementById('project-nav');
    nav.hidden = !nav.hidden;
  });
  syncUI();
  api('/api/projects').then(data => {
    projects = (data.projects || []).filter(project => project.enabled && project.dev_enabled);
    renderProjects();
    if (projects.length) selectProject(projects[0].id);
    else document.getElementById('data-status').textContent = 'Nenhum projeto autorizado para Development Mode';
  }).catch(error => { document.getElementById('data-status').textContent = `Falha ao carregar projetos: ${error.message}`; });
})();
