/* Nexus multi-pane agent workspace (P1).
 *
 * Maestro (centre): task -> auditable pane proposal (/api/workspace/plans).
 * Panes (grid): one persisted agent per pane, each bound to its own real
 * Development session and worktree. Missions run through the existing
 * mission runner; the exact model must be re-confirmed for every mission.
 * Nothing here applies, merges, pushes or deletes. All server/model text is
 * rendered with textContent only.
 */
(function (global) {
  "use strict";

  const ACTIVE = new Set(["PENDING", "RUNNING", "TESTING", "REPAIRING"]);
  const FAST_POLL_MS = 3000;
  const IDLE_POLL_MS = 15000;
  const STORAGE_KEY = "nexus.workspace.v1";

  const STATUS_LABELS = {
    PENDING: "Na fila", RUNNING: "Executando", TESTING: "Testando", REPAIRING: "Corrigindo",
    READY_FOR_REVIEW: "Aguardando revisão", FAILED: "Falhou", BLOCKED: "Bloqueado",
    CANCELLED: "Cancelado", OUTCOME_UNKNOWN: "Resultado desconhecido",
  };

  function costLabel(costStatus) {
    switch (costStatus) {
      case "free": return "gratuito";
      case "low_cost": return "baixo custo";
      case "standard": return "custo standard — preço não informado";
      case "premium": return "custo premium — preço não informado";
      default: return "custo desconhecido";
    }
  }

  function paneStatus(pane) {
    if (!pane.session) return { label: "Sessão ausente", tone: "bad" };
    if (pane.session.status !== "ACTIVE") return { label: `Sessão ${pane.session.status}`, tone: "bad" };
    const mission = pane.latest_mission;
    if (!mission) return { label: "Ocioso", tone: "" };
    const label = STATUS_LABELS[mission.status] || mission.status;
    if (ACTIVE.has(mission.status)) return { label, tone: "run" };
    if (mission.status === "READY_FOR_REVIEW") return { label, tone: "warn" };
    if (mission.status === "CANCELLED") return { label, tone: "" };
    return { label, tone: "bad" };
  }

  function density(panes) {
    if (!panes.length) return "empty";
    return panes.length === 1 ? "compact" : "multi";
  }

  function sortPanes(panes) {
    return panes.slice().sort((a, b) => (a.layout.position - b.layout.position) || String(a.created_at).localeCompare(String(b.created_at)));
  }

  /* Swap a pane with its neighbour; returns the PATCH list or [] at an edge. */
  function moveUpdates(panes, paneId, direction) {
    const ordered = sortPanes(panes);
    const index = ordered.findIndex((pane) => pane.pane_id === paneId);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= ordered.length) return [];
    const a = ordered[index];
    const b = ordered[target];
    const aPos = a.layout.position === b.layout.position ? target : b.layout.position;
    const bPos = a.layout.position === b.layout.position ? index : a.layout.position;
    return [
      { pane_id: a.pane_id, layout: { position: aPos } },
      { pane_id: b.pane_id, layout: { position: bPos } },
    ];
  }

  function missionPayload(pane, form) {
    const description = String(form.description || "").trim();
    if (!description) return { error: "Descreva a missão do agente." };
    if (!pane.model) return { error: "Escolha um modelo para este agente." };
    if (!form.testProfile) return { error: "Escolha um perfil de teste confiável." };
    if (!form.confirmed) return { error: `Confirme o uso de ${pane.model}.` };
    return {
      payload: { description, confirm_model: pane.model, test_profile: form.testProfile },
    };
  }

  function canStartMission(pane, state) {
    return Boolean(state.execution_enabled && pane.session && pane.session.status === "ACTIVE" && !pane.busy && pane.model);
  }

  function createWorkspace(env) {
    const doc = env.document;
    const fetchFn = env.fetch;
    const schedule = env.setTimeout || setTimeout;
    const cancel = env.clearTimeout || clearTimeout;
    const storage = env.storage || null;
    const $ = (id) => doc.getElementById(id);

    const state = {
      projectId: null, projects: [], workspace: null, models: [], catalogError: null,
      profiles: [], plans: [], selectedPaneId: null, timer: null, drafts: {}, openDetails: new Set(),
    };

    function readPrefs() {
      try { return JSON.parse((storage && storage.getItem(STORAGE_KEY)) || "{}") || {}; } catch (_error) { return {}; }
    }
    function writePrefs(patch) {
      try { if (storage) storage.setItem(STORAGE_KEY, JSON.stringify({ ...readPrefs(), ...patch })); } catch (_error) { /* optional */ }
    }

    async function api(path, options = {}) {
      const init = { method: options.method || "GET", headers: { Accept: "application/json" } };
      if (options.body !== undefined) {
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(options.body);
      }
      const response = await fetchFn(path, init);
      let data = null;
      try { data = await response.json(); } catch (_error) { data = null; }
      if (!response.ok) {
        const detail = data && data.detail;
        const message = detail && typeof detail === "object" ? `${detail.code}: ${detail.message}` : (typeof detail === "string" ? detail : `HTTP ${response.status}`);
        const error = new Error(message);
        error.code = detail && detail.code;
        throw error;
      }
      return data;
    }

    function el(tag, options = {}, ...children) {
      const node = doc.createElement(tag);
      if (options.className) node.className = options.className;
      if (options.text !== undefined) node.textContent = String(options.text);
      if (options.attrs) Object.entries(options.attrs).forEach(([key, value]) => node.setAttribute(key, value));
      if (options.dataset) Object.entries(options.dataset).forEach(([key, value]) => { node.dataset[key] = String(value); });
      children.filter(Boolean).forEach((child) => node.appendChild(child));
      return node;
    }

    function disclosure(key, summary) {
      const details = el("details", {}, el("summary", { text: summary }));
      if (state.openDetails.has(key)) details.open = true;
      details.addEventListener("toggle", () => {
        if (details.open) state.openDetails.add(key); else state.openDetails.delete(key);
      });
      return details;
    }

    function chip(text, tone) {
      const node = el("span", { className: "ws-chip", text });
      if (tone) node.dataset.tone = tone;
      return node;
    }

    function button(text, onClick, options = {}) {
      const node = el("button", { className: `ws-button ${options.className || ""}`.trim(), text, attrs: { type: "button" } });
      if (options.title) node.title = options.title;
      if (options.label) node.setAttribute("aria-label", options.label);
      node.disabled = Boolean(options.disabled);
      node.addEventListener("click", (event) => {
        if (event && event.stopPropagation) event.stopPropagation();
        return onClick(event);
      });
      return node;
    }

    function showError(message) {
      const node = $("ws-error");
      node.textContent = message || "";
      node.hidden = !message;
    }

    function modelOptions(select, selected, includeNone) {
      const options = [];
      if (includeNone) options.push(el("option", { text: "— escolher depois —", attrs: { value: "" } }));
      state.models.forEach((model) => {
        const option = el("option", { text: `${model.id} · ${costLabel(model.cost_status)}`, attrs: { value: model.id } });
        if (model.id === selected) option.setAttribute("selected", "selected");
        options.push(option);
      });
      select.replaceChildren(...options);
      select.value = selected || "";
    }

    function modelCost(modelId) {
      const row = state.models.find((model) => model.id === modelId);
      return row ? row.cost_status : null;
    }

    /* ── Rendering ── */

    function renderGates() {
      const ws = state.workspace;
      const nodes = [];
      if (ws) {
        nodes.push(chip(ws.execution_enabled ? "Execução habilitada" : "Execução desabilitada", ws.execution_enabled ? "ok" : "warn"));
        nodes.push(chip(ws.integration_enabled ? "Apply manual disponível" : "Apply desligado", ws.integration_enabled ? "warn" : "ok"));
        nodes.push(chip(`${ws.panes.length}/${ws.limits.max_open_panes} agentes`));
      }
      if (state.catalogError) nodes.push(chip(`Modelos: ${state.catalogError}`, "bad"));
      $("ws-gates").replaceChildren(...nodes);
    }

    function renderProposal(plan) {
      const rows = plan.panes.map((proposed, index) => {
        const label = `${proposed.title} · ${proposed.model || "modelo a escolher"}`;
        const row = el("div", { className: "ws-proposal-row" },
          el("span", { className: "ws-grow", text: label }),
          chip(proposed.model ? costLabel(proposed.cost_status) : "sem rota gratuita — escolha em Configurar", proposed.cost_authorization_required ? "warn" : "ok"),
          button("Criar", () => createFromPlan(plan, index), { className: "ws-button-small" }));
        row.title = `Critérios: ${proposed.signals.join(", ")}`;
        return row;
      });
      const meta = el("div", { className: "ws-msg-meta",
        text: `${plan.policy_version} · ${plan.complexity === "simple" ? "tarefa simples: um agente" : `${plan.panes.length} responsabilidades`}${plan.dropped_by_limit && plan.dropped_by_limit.length ? ` · limite ${plan.limit} (fora: ${plan.dropped_by_limit.join(", ")})` : ""}` });
      const all = plan.panes.length > 1 ? button("Criar todos", () => createAllFromPlan(plan), { className: "ws-button-small" }) : null;
      return el("div", { className: "ws-proposal" }, meta, ...rows, all);
    }

    function renderMaestro() {
      const items = [];
      state.plans.slice().reverse().forEach((plan) => {
        items.push(el("li", { className: "ws-msg ws-msg-user", text: plan.task_text }));
        items.push(el("li", { className: "ws-msg" },
          el("div", { text: "Proposta do Maestro" }),
          renderProposal(plan)));
      });
      $("ws-maestro-log").replaceChildren(...items);
    }

    function renderMission(pane) {
      const mission = pane.latest_mission;
      if (!mission) return el("p", { className: "ws-msg-meta", text: "Nenhuma missão ainda." });
      const section = el("div", { className: "ws-pane-section" },
        el("h3", { text: "Última missão" }),
        el("div", { className: "ws-mono", text: `${mission.mission_id} · ${mission.requested_model} · tentativa ${mission.attempt_count}` }));
      if (mission.tests.length) {
        const last = mission.tests[mission.tests.length - 1];
        section.appendChild(el("div", { text: `Testes: ${last.status}${last.exit_code === null || last.exit_code === undefined ? "" : ` (exit ${last.exit_code})`} · ${mission.tests.length} execução(ões)` }));
      }
      if (mission.error_code) section.appendChild(el("div", { className: "ws-error", text: `${mission.error_code}: ${mission.error_message || ""}` }));
      if (mission.changed_files.length) {
        section.appendChild(el("h3", { text: "Arquivos alterados" }));
        section.appendChild(el("ul", { className: "ws-pane-files" },
          ...mission.changed_files.map((file) => el("li", { text: `${file.status} ${file.path}` }))));
      }
      if (mission.status === "READY_FOR_REVIEW" && !mission.integration_applied) {
        const review = el("div", { className: "ws-pane-review" },
          el("span", { text: "Revisão humana necessária. Nada foi aplicado ao projeto. " }),
          el("a", { text: "Revisar e decidir no modo avançado", attrs: { href: "/nexus" } }));
        section.appendChild(review);
        section.appendChild(renderDiff(pane));
      }
      return section;
    }

    function renderDiff(pane) {
      const details = disclosure(`${pane.pane_id}:diff`, "Ver diff");
      const body = el("pre", { className: "ws-mono", text: "" });
      details.appendChild(body);
      const loadDiff = async () => {
        if (!details.open || body.dataset.loaded) return;
        body.textContent = "Carregando…";
        try {
          const diff = await api(`/api/dev/sessions/${encodeURIComponent(pane.session.session_id)}/diff`);
          body.textContent = diff.files.map((file) => file.patch || `${file.path}: ${file.reason || "sem patch"}`).join("\n") || "Sem alterações.";
          body.dataset.loaded = "1";
        } catch (error) {
          body.textContent = error.message;
        }
      };
      details.addEventListener("toggle", loadDiff);
      if (details.open) loadDiff();
      return details;
    }

    function renderMissionForm(pane) {
      const draft = state.drafts[pane.pane_id] || {};
      const description = el("textarea", { attrs: { maxlength: "100000", placeholder: "Missão para este agente", "aria-label": `Missão para ${pane.title}` } });
      description.value = draft.description || "";
      description.addEventListener("input", () => { state.drafts[pane.pane_id] = { ...state.drafts[pane.pane_id], description: description.value }; });

      const profile = el("select", { attrs: { "aria-label": "Perfil de teste" } },
        ...(state.profiles.length
          ? state.profiles.map((item) => el("option", { text: item.name || item.id, attrs: { value: item.id } }))
          : [el("option", { text: "Nenhum perfil de teste confiável para este projeto", attrs: { value: "" } })]));
      if (state.profiles.length) profile.value = draft.testProfile || state.profiles[0].id;
      profile.addEventListener("change", () => { state.drafts[pane.pane_id] = { ...state.drafts[pane.pane_id], testProfile: profile.value }; });

      const confirm = el("input", { attrs: { type: "checkbox" } });
      confirm.hidden = !pane.model;
      const confirmLabel = el("label", {}, confirm,
        el("span", { text: pane.model ? `Confirmo o uso de ${pane.model} (${costLabel(modelCost(pane.model))})` : "Escolha um modelo em Configurar" }));
      const allowed = canStartMission(pane, state.workspace || {});
      const start = button("Iniciar missão", async () => {
        const result = missionPayload(pane, { description: description.value, testProfile: profile.value, confirmed: confirm.checked });
        if (result.error) { showError(result.error); return; }
        start.disabled = true;
        try {
          await api(`/api/workspace/panes/${encodeURIComponent(pane.pane_id)}/missions`, { method: "POST", body: result.payload });
          delete state.drafts[pane.pane_id];
          showError("");
          await refresh({ force: true });
        } catch (error) {
          showError(error.message);
          start.disabled = false;
        }
      }, { className: "ws-button-primary", disabled: !allowed });
      if (!state.profiles.length) start.disabled = true;
      return el("div", { className: "ws-pane-form" }, description, profile, confirmLabel, start);
    }

    function renderConfig(pane) {
      const details = disclosure(`${pane.pane_id}:config`, "Configurar");
      const select = el("select", { attrs: { "aria-label": `Modelo de ${pane.title}` } });
      modelOptions(select, pane.model, !pane.model);
      select.addEventListener("change", () => patchPane(pane.pane_id, { model: select.value }));
      details.append(
        el("p", { className: "ws-msg-meta", text: "A troca de modelo vale apenas para missões futuras." }),
        select,
        el("div", { className: "ws-mono", text: `${pane.pane_id} · sessão ${pane.session ? pane.session.session_id : "—"}` }),
        el("div", { className: "ws-mono", text: pane.session ? `${pane.session.branch} · ${pane.session.worktree_path}` : "" }),
      );
      return details;
    }

    function renderHistory(pane) {
      const details = disclosure(`${pane.pane_id}:history`, "Histórico");
      const list = el("ol", { className: "ws-pane-events" });
      details.appendChild(list);
      const loadHistory = async () => {
        if (!details.open) return;
        try {
          const history = await api(`/api/workspace/panes/${encodeURIComponent(pane.pane_id)}/history`);
          list.replaceChildren(...history.events.map((event) => el("li", { text: `${event.created_at} · ${event.kind} · ${JSON.stringify(event.detail)}` })));
        } catch (error) {
          list.replaceChildren(el("li", { text: error.message }));
        }
      };
      details.addEventListener("toggle", loadHistory);
      if (details.open) loadHistory();
      return details;
    }

    function renderPane(pane, index, total) {
      const status = paneStatus(pane);
      const node = el("article", { className: "ws-pane", dataset: {
        paneId: pane.pane_id, selected: pane.pane_id === state.selectedPaneId,
        expanded: pane.layout.expanded, collapsed: pane.layout.collapsed,
      }, attrs: { "aria-label": pane.title } });
      const tools = el("div", { className: "ws-pane-tools" },
        button("←", () => move(pane.pane_id, -1), { className: "ws-button-small", label: "Mover para trás", disabled: index === 0 }),
        button("→", () => move(pane.pane_id, 1), { className: "ws-button-small", label: "Mover para frente", disabled: index === total - 1 }),
        button(pane.layout.collapsed ? "▸" : "▾", () => patchPane(pane.pane_id, { layout: { collapsed: !pane.layout.collapsed } }), { className: "ws-button-small", label: pane.layout.collapsed ? "Expandir conteúdo" : "Recolher" }),
        button(pane.layout.expanded ? "⤡" : "⤢", () => patchPane(pane.pane_id, { layout: { expanded: !pane.layout.expanded } }), { className: "ws-button-small", label: pane.layout.expanded ? "Tamanho normal" : "Largura total" }),
        button("×", () => patchPane(pane.pane_id, { layout: { hidden: true } }), { className: "ws-button-small", label: "Ocultar (a sessão é preservada)", title: "Ocultar: a sessão e o histórico são preservados" }));
      const head = el("header", { className: "ws-pane-head" },
        el("span", { className: "ws-pane-title", text: pane.title }), chip(status.label, status.tone), tools);
      head.addEventListener("click", () => select(pane.pane_id));
      const body = el("div", { className: "ws-pane-body" },
        el("div", { className: "ws-pane-meta" },
          chip(pane.role_label), chip(pane.model || "sem modelo", pane.model ? "" : "warn"),
          chip(pane.origin === "policy" ? "proposto pelo Maestro" : "manual")),
        renderMission(pane),
        renderMissionForm(pane),
        renderConfig(pane),
        renderHistory(pane));
      node.append(head, body);
      return node;
    }

    function renderGrid() {
      const panes = sortPanes(state.workspace ? state.workspace.panes : []);
      $("ws-main").dataset.density = density(panes);
      $("ws-grid").replaceChildren(...panes.map((pane, index) => renderPane(pane, index, panes.length)));
      $("ws-empty").hidden = panes.length > 0;
      const hidden = state.workspace ? state.workspace.hidden_panes : [];
      $("ws-hidden-count").textContent = String(hidden.length);
      $("ws-hidden-list").replaceChildren(...(hidden.length ? hidden.map((pane) => button(`Restaurar ${pane.title}`, () => patchPane(pane.pane_id, { layout: { hidden: false } }))) : [el("span", { className: "ws-msg-meta", text: "Nenhum agente oculto." })]));
    }

    function editingInGrid() {
      const active = doc.activeElement;
      const grid = $("ws-grid");
      return Boolean(active && grid && grid.contains && grid.contains(active) && ["TEXTAREA", "INPUT", "SELECT"].includes(active.tagName));
    }

    function render(options = {}) {
      renderGates();
      renderMaestro();
      if (options.force || !editingInGrid()) renderGrid();
      const ready = Boolean(state.projectId && state.workspace);
      $("ws-add-pane").disabled = !ready;
      $("ws-maestro-send").disabled = !ready;
    }

    /* ── Actions ── */

    function select(paneId) {
      state.selectedPaneId = paneId;
      writePrefs({ selected: { ...(readPrefs().selected || {}), [state.projectId]: paneId } });
      renderGrid();
    }

    async function patchPane(paneId, body) {
      try {
        await api(`/api/workspace/panes/${encodeURIComponent(paneId)}`, { method: "PATCH", body });
        showError("");
      } catch (error) {
        showError(error.message);
      }
      await refresh({ force: true });
    }

    async function move(paneId, direction) {
      const updates = moveUpdates(state.workspace.panes, paneId, direction);
      try {
        for (const update of updates) {
          await api(`/api/workspace/panes/${encodeURIComponent(update.pane_id)}`, { method: "PATCH", body: { layout: update.layout } });
        }
      } catch (error) {
        showError(error.message);
      }
      await refresh({ force: true });
    }

    async function createPane(body) {
      const pane = await api("/api/workspace/panes", { method: "POST", body: { project_id: state.projectId, ...body } });
      state.selectedPaneId = pane.pane_id;
      return pane;
    }

    async function createFromPlan(plan, index) {
      const proposed = plan.panes[index];
      try {
        await createPane({ role: proposed.role, title: proposed.title, model: proposed.model || null, plan_id: plan.plan_id });
        showError("");
      } catch (error) {
        showError(error.message);
      }
      await refresh({ force: true });
    }

    async function createAllFromPlan(plan) {
      for (let index = 0; index < plan.panes.length; index += 1) {
        try {
          await createPane({ role: plan.panes[index].role, title: plan.panes[index].title, model: plan.panes[index].model || null, plan_id: plan.plan_id });
        } catch (error) {
          showError(error.message);
          break;
        }
      }
      await refresh({ force: true });
    }

    async function submitTask(event) {
      if (event && event.preventDefault) event.preventDefault();
      const input = $("ws-maestro-input");
      const task = input.value.trim();
      if (!task || !state.projectId) return;
      $("ws-maestro-send").disabled = true;
      try {
        const plan = await api("/api/workspace/plans", { method: "POST", body: { project_id: state.projectId, task } });
        state.plans.unshift(plan);
        input.value = "";
        showError("");
      } catch (error) {
        showError(error.message);
      }
      render();
    }

    function openAddDialog() {
      const roles = (state.workspace && state.workspace.roles) || {};
      $("ws-add-role").replaceChildren(...Object.entries(roles).map(([id, label]) => el("option", { text: label, attrs: { value: id } })));
      $("ws-add-name").value = "";
      modelOptions($("ws-add-model"), null, true);
      const dialog = $("ws-add-dialog");
      if (dialog.showModal) dialog.showModal(); else dialog.setAttribute("open", "");
    }

    async function confirmAdd(event) {
      if (event && event.preventDefault) event.preventDefault();
      const dialog = $("ws-add-dialog");
      try {
        await createPane({ role: $("ws-add-role").value, title: $("ws-add-name").value || null, model: $("ws-add-model").value || null });
        showError("");
        if (dialog.close) dialog.close(); else dialog.removeAttribute("open");
      } catch (error) {
        showError(error.message);
      }
      await refresh({ force: true });
    }

    /* ── Data loading ── */

    function scheduleRefresh() {
      if (state.timer) cancel(state.timer);
      const busy = state.workspace && state.workspace.panes.some((pane) => pane.busy);
      state.timer = schedule(() => { refresh().catch(() => {}); }, busy ? FAST_POLL_MS : IDLE_POLL_MS);
    }

    async function refresh(options = {}) {
      if (!state.projectId) { render(options); return; }
      try {
        state.workspace = await api(`/api/workspace/state?project_id=${encodeURIComponent(state.projectId)}`);
        if (!state.workspace.panes.some((pane) => pane.pane_id === state.selectedPaneId)) {
          state.selectedPaneId = state.workspace.panes.length ? state.workspace.panes[0].pane_id : null;
        }
      } catch (error) {
        state.workspace = null;
        showError(error.message);
      }
      render(options);
      scheduleRefresh();
    }

    async function loadProject(projectId) {
      state.projectId = projectId || null;
      state.plans = [];
      state.profiles = [];
      writePrefs({ project: state.projectId });
      if (!state.projectId) { render(); return; }
      state.selectedPaneId = (readPrefs().selected || {})[state.projectId] || null;
      const [plans, profiles] = await Promise.all([
        api(`/api/workspace/plans?project_id=${encodeURIComponent(state.projectId)}`).catch((error) => { showError(error.message); return { plans: [] }; }),
        api(`/api/dev/test-profiles?project_id=${encodeURIComponent(state.projectId)}`).catch(() => ({ profiles: [] })),
      ]);
      state.plans = plans.plans || [];
      state.profiles = profiles.profiles || [];
      await refresh({ force: true });
    }

    async function init() {
      $("ws-maestro-form").addEventListener("submit", submitTask);
      $("ws-add-pane").addEventListener("click", openAddDialog);
      $("ws-add-form").addEventListener("submit", confirmAdd);
      $("ws-add-cancel").addEventListener("click", () => { const dialog = $("ws-add-dialog"); if (dialog.close) dialog.close(); else dialog.removeAttribute("open"); });
      $("ws-project-select").addEventListener("change", (event) => loadProject(event.target.value));

      const [projects, models] = await Promise.all([
        api("/api/projects").catch((error) => { showError(error.message); return { projects: [] }; }),
        api("/api/dev/models").catch((error) => ({ models: [], catalog_error: error.message })),
      ]);
      state.models = models.models || [];
      state.catalogError = models.catalog_error || null;
      state.projects = (projects.projects || []).filter((project) => project.enabled);
      const authorized = state.projects.filter((project) => project.dev_enabled);
      const options = [el("option", { text: authorized.length ? "Escolha um projeto" : "Nenhum projeto autorizado", attrs: { value: "" } })];
      state.projects.forEach((project) => {
        const option = el("option", { text: project.dev_enabled ? project.name : `${project.name} (não autorizado)`, attrs: { value: project.id } });
        if (!project.dev_enabled) option.disabled = true;
        options.push(option);
      });
      $("ws-project-select").replaceChildren(...options);
      const remembered = readPrefs().project;
      const initial = authorized.some((project) => project.id === remembered) ? remembered : (authorized.length === 1 ? authorized[0].id : "");
      $("ws-project-select").value = initial;
      await loadProject(initial);
    }

    return { init, refresh, state, api, submitTask, createFromPlan, patchPane, move, select };
  }

  const exported = { createWorkspace, paneStatus, density, sortPanes, moveUpdates, missionPayload, canStartMission, costLabel };
  global.NexusWorkspace = exported;
  if (typeof module !== "undefined" && module.exports) module.exports = exported;

  if (global.document && global.document.getElementById && global.document.getElementById("ws-main")) {
    let storage = null;
    try { storage = global.localStorage; } catch (_error) { storage = null; }
    createWorkspace({ document: global.document, fetch: global.fetch.bind(global), storage }).init();
  }
})(typeof window !== "undefined" ? window : globalThis);
