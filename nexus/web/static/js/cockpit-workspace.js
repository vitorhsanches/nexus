// Presentation only: existing modules remain the owners of plans, turns and approvals.
(() => {
  const host = window.NexusCockpit;
  if (!host) return;
  const tabs = document.getElementById('workspace-tabs');
  const paneNav = document.getElementById('pane-nav');
  const grid = document.getElementById('pane-grid');
  const drawer = document.getElementById('native-plan-authoring');
  const backdrop = document.getElementById('workspace-drawer-backdrop');
  const openButton = document.getElementById('open-native-plan');
  const closeButton = document.getElementById('close-native-plan');
  const planSelect = document.getElementById('review-plan-select');
  const planNav = document.getElementById('manual-plan-nav');
  let previousFocus = null;
  let scheduled = false;
  let planRequest = 0;
  let planTimer = null;

  const closeDrawer = () => {
    if (drawer.hidden) return;
    drawer.hidden = true;
    backdrop.hidden = true;
    document.body.classList.remove('drawer-open');
    previousFocus?.focus?.({ preventScroll: true });
  };
  const openDrawer = () => {
    previousFocus = document.activeElement;
    drawer.hidden = false;
    backdrop.hidden = false;
    document.body.classList.add('drawer-open');
    closeButton.focus();
  };
  openButton.addEventListener('click', openDrawer);
  closeButton.addEventListener('click', closeDrawer);
  backdrop.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !drawer.hidden) { event.stopPropagation(); closeDrawer(); }
  }, true);
  window.addEventListener('nexus:plan-created', closeDrawer);

  const makeTab = (label, active, onClick) => {
    const tab = document.createElement('button');
    tab.type = 'button';
    tab.className = 'workspace-tab';
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', String(active));
    tab.title = label;
    const dot = document.createElement('span');
    dot.className = 'workspace-tab-dot';
    const name = document.createElement('span');
    name.textContent = label;
    tab.append(dot, name);
    tab.addEventListener('click', onClick);
    return tab;
  };
  const renderTabs = () => {
    const selected = grid.querySelector(':scope > .pane.selected:not([data-workspace-inactive="true"])');
    const projectName = document.getElementById('project-name').textContent.trim();
    const entries = [makeTab(projectName || 'Workspace', !selected, () => document.getElementById('project-toggle').click())];
    paneNav.querySelectorAll('[data-pane-target]').forEach(button => {
      const kind = button.dataset.paneTarget;
      const label = kind === 'review' && planSelect.value
        ? `Plano ${planSelect.value}` : (button.querySelector('.pane-nav-name')?.textContent?.trim() || kind);
      entries.push(makeTab(label, selected?.dataset.pane === kind, () => button.click()));
    });
    tabs.replaceChildren(...entries);
  };
  const scheduleTabs = () => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; renderTabs(); });
  };
  new MutationObserver(scheduleTabs).observe(paneNav, { childList: true, subtree: true, attributes: true, characterData: true });
  new MutationObserver(scheduleTabs).observe(planSelect, { childList: true, subtree: true });
  planSelect.addEventListener('change', scheduleTabs);
  const refreshGates = async () => {
    const target = document.getElementById('footer-operation-status');
    try {
      const [execution, integration] = await Promise.all([
        fetch('/api/dev/execution-status').then(response => response.json()),
        fetch('/api/dev/integration-status').then(response => response.json()),
      ]);
      target.textContent = `Execução ${execution.execution_enabled ? 'habilitada' : 'desabilitada'} · Integração ${integration.integration_enabled ? 'habilitada' : 'desabilitada'}`;
    } catch (_error) { target.textContent = 'Gates indisponíveis · confira o servidor'; }
  };
  const refreshPlans = async () => {
    const projectId = host.projectId();
    const request = ++planRequest;
    if (!projectId) { planNav.replaceChildren(); document.getElementById('manual-plan-count').textContent = '00'; return; }
    try {
      const response = await fetch(`/api/workspace/plans?project_id=${encodeURIComponent(projectId)}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (request !== planRequest || projectId !== host.projectId()) return;
      const plans = Array.isArray(data.plans) ? data.plans : [];
      const entries = plans.map(plan => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'mission-link';
        button.title = plan.task_text || plan.plan_id;
        const dot = document.createElement('span'); dot.className = 'mission-indicator';
        const labels = document.createElement('span');
        const title = document.createElement('strong'); title.textContent = plan.task_text || plan.plan_id;
        const status = document.createElement('small'); status.textContent = `${plan.plan_id} · ${plan.status}`;
        labels.append(title, status); button.append(dot, labels);
        button.addEventListener('click', async () => {
          host.setPaneAvailable('review', true);
          document.getElementById('open-review').click();
          await window.NexusPlanMonitor?.refresh?.(projectId, plan.plan_id);
          scheduleTabs();
        });
        return button;
      });
      planNav.replaceChildren(...entries);
      document.getElementById('manual-plan-count').textContent = String(plans.length).padStart(2, '0');
    } catch (_error) {
      if (request === planRequest && projectId === host.projectId()) {
        planNav.replaceChildren();
        const note = document.createElement('span'); note.className = 'nav-error'; note.textContent = 'Planos indisponíveis';
        planNav.append(note);
      }
    }
  };
  host.onProject(() => {
    closeDrawer(); scheduleTabs();
    clearInterval(planTimer);
    void refreshPlans();
    planTimer = setInterval(() => { if (document.visibilityState !== 'hidden') void refreshPlans(); }, 15000);
  });
  window.addEventListener('nexus:plan-created', () => { void refreshPlans(); });
  window.addEventListener('nexus:plan-updated', () => { void refreshPlans(); });
  void refreshGates();
  scheduleTabs();
})();
