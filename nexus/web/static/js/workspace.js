/* Nexus agent workspace (P1 layout + P2A intelligent Maestro).
 *
 * Maestro (centre): a persistent conversation backed by a read-only Nexus
 * Chat session. Each answer carries a validated plan (or a clearly labelled
 * deterministic fallback). Approving a plan creates every planned agent in
 * one step, with the exact models and limits shown; it never starts work.
 * Panes (grid): one persisted agent per pane, each bound to its own real
 * Development session and worktree. Agents receive direct messages (real
 * Development turns) and missions (existing mission runner). Nothing here
 * applies, merges, pushes or deletes. All server/model text is rendered with
 * textContent only.
 */
(function (global) {
  "use strict";

  const ACTIVE = new Set(["PENDING", "RUNNING", "TESTING", "REPAIRING"]);
  const FAST_POLL_MS = 2500;
  const IDLE_POLL_MS = 15000;
  const STORAGE_KEY = "nexus.workspace.v1";
  const TIMEOUT_CHOICES = [5, 10, 15, 30, 60];

  const STATUS_LABELS = {
    PENDING: "Na fila", RUNNING: "Executando", TESTING: "Testando", REPAIRING: "Corrigindo",
    READY_FOR_REVIEW: "Aguardando revisão", FAILED: "Falhou", BLOCKED: "Bloqueado",
    CANCELLED: "Cancelado", OUTCOME_UNKNOWN: "Resultado desconhecido",
  };
  const PLAN_STATUS = {
    PROPOSED: "Proposto", SUPERSEDED: "Substituído por um plano mais novo", APPROVING: "Criando agentes…",
    APPROVED: "Aprovado — agentes criados", APPROVED_PARTIAL: "Aprovado parcialmente", APPROVAL_FAILED: "Falha ao criar agentes",
  };
  const EXECUTION_STATUS_LABELS = {
    AWAITING_EXECUTION: "Aguardando início", RUNNING: "Executando sequência",
    WAITING_DEPENDENCY: "Aguardando dependência", AWAITING_REVIEW: "Aguardando revisão humana",
    BLOCKED: "Bloqueado", FAILED: "Falhou", INTERRUPTED: "Interrompido", RESULT_UNCERTAIN: "Resultado incerto",
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

  function isFree(costStatus) { return costStatus === "free"; }

  function paneStatus(pane) {
    if (!pane.session) return { label: "Sessão ausente", tone: "bad" };
    if (pane.session.status !== "ACTIVE") return { label: `Sessão ${pane.session.status}`, tone: "bad" };
    const mission = pane.latest_mission;
    const talking = (pane.messages || []).some((message) => message.status === "PENDING" || message.status === "RUNNING");
    if (talking) return { label: "Respondendo", tone: "run" };
    if ((pane.dependencies || []).some((dependency) => !dependency.ready) && !mission) return { label: "Aguardando dependência", tone: "" };
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

  /* True when the approved plan already covers the next use of this pane. */
  function planAuthorized(pane) {
    return Boolean(pane.brief && pane.brief.authorization && pane.brief.authorization.applies);
  }

  function missionPayload(pane, form) {
    const description = String(form.description || "").trim();
    if (!description && !pane.brief) return { error: "Descreva a missão do agente." };
    if (!pane.model) return { error: "Escolha um modelo para este agente." };
    if (!form.testProfile) return { error: "Escolha um perfil de teste confiável." };
    const authorized = planAuthorized(pane);
    if (!authorized && !form.confirmed) return { error: `Confirme o uso de ${pane.model}.` };
    if (!authorized && !isFree(form.costStatus || pane.cost_status) && !form.confirmCost)
      return { error: `Confirme o custo de ${pane.model}.` };
    const payload = { test_profile: form.testProfile };
    if (description) payload.description = description;
    if (!authorized) payload.confirm_model = pane.model;
    if (!authorized && !isFree(form.costStatus || pane.cost_status)) payload.confirm_cost = true;
    return { payload };
  }

  function messagePayload(pane, form) {
    const text = String(form.text || "").trim();
    if (!text) return { error: "Escreva uma mensagem para o agente." };
    if (!pane.model) return { error: "Escolha um modelo para este agente." };
    if (planAuthorized(pane)) return { payload: { text } };
    if (!form.confirmed) return { error: `Confirme o uso de ${pane.model}.` };
    if (!isFree(form.costStatus || pane.cost_status) && !form.confirmCost)
      return { error: `Confirme o custo de ${pane.model}.` };
    return { payload: { text, confirm_model: pane.model,
      ...(!isFree(form.costStatus || pane.cost_status) ? { confirm_cost: true } : {}) } };
  }

  function canStartMission(pane, state) {
    const blocked = (pane.dependencies || []).some((dependency) => !dependency.ready);
    return Boolean(state.execution_enabled && pane.session && pane.session.status === "ACTIVE" && !pane.busy && pane.model && !blocked);
  }

  /* Build the approval request for a plan; every agent needs an explicit model. */
  function approvalPayload(plan, form, models) {
    const agentModels = {};
    for (const agent of plan.agents) {
      const model = form.models[agent.key];
      if (!model) return { error: `Escolha o modelo de ${agent.title}.` };
      agentModels[agent.key] = model;
    }
    const paid = Object.values(agentModels).filter((model) => {
      const row = models.find((item) => item.id === model);
      return !row || !isFree(row.cost_status);
    });
    if (paid.length && !form.confirmCosts) return { error: `Confirme o custo de: ${[...new Set(paid)].join(", ")}.` };
    return {
      payload: {
        agent_models: agentModels,
        max_turns_per_agent: Number(form.maxTurns),
        mission_timeout_seconds: Number(form.timeoutMinutes) * 60,
        confirm_costs: Boolean(paid.length && form.confirmCosts),
      },
      paid: [...new Set(paid)],
    };
  }

  function approvedExecutionLimits(plan) {
    const approval = plan.approval || {};
    const turns = approval.max_turns_per_agent;
    const seconds = approval.mission_timeout_seconds;
    if (plan.status !== "APPROVED" || !Number.isInteger(turns) || turns < 1 || turns > 6 ||
        !Number.isInteger(seconds) || seconds < 60 || seconds > 3600) {
      return { error: "Aprovação persistida incompleta. Revise a aprovação antes de executar a sequência." };
    }
    return { max_turns_per_agent: turns, mission_timeout_seconds: seconds };
  }

  function persistedCostConfirmed(plan) {
    const approval = plan.approval || {};
    const choices = Object.values(approval.agent_models || {});
    const paid = choices.filter((choice) => !isFree(choice.cost_status));
    const confirmed = new Set(approval.cost_confirmed_for || []);
    return plan.status === "APPROVED" && paid.length > 0 && paid.every((choice) => confirmed.has(choice.model));
  }

  function executionPayload(plan, form) {
    const limits = approvedExecutionLimits(plan);
    if (limits.error) return limits;
    const testProfiles = {};
    for (const agent of plan.agents) {
      const profile = (form.profiles || {})[agent.key];
      if (!profile) return { error: `Escolha o perfil de teste de ${agent.title}.` };
      testProfiles[agent.key] = profile;
    }
    return { payload: {
      agent_keys: plan.agents.map((agent) => agent.key),
      test_profiles: testProfiles,
      // The persisted approval is authoritative even if a local draft is
      // stale or has been edited. The server independently enforces equality.
      max_turns_per_agent: limits.max_turns_per_agent,
      mission_timeout_seconds: limits.mission_timeout_seconds,
      confirm_costs: Boolean(form.confirmCosts || persistedCostConfirmed(plan)),
    } };
  }

  function planSourceLabel(plan) {
    if (plan.source === "llm") return { text: "Plano do Maestro (IA)", tone: "ok" };
    if (plan.source === "deterministic-fallback") return { text: "Proposta determinística — fallback", tone: "warn" };
    return { text: "Proposta determinística (sem IA)", tone: "" };
  }

  function outcomeLabel(message) {
    switch (message.outcome) {
      case "conversation": return { text: "Somente conversa", tone: "" };
      case "code_changed": return { text: "Alterou código — verificar", tone: "warn" };
      case "failed": return { text: "Falhou", tone: "bad" };
      case "cancelled": return { text: "Cancelada", tone: "" };
      default: return { text: "Em andamento", tone: "run" };
    }
  }

  function createWorkspace(env) {
    const doc = env.document;
    const fetchFn = env.fetch;
    const schedule = env.setTimeout || setTimeout;
    const cancel = env.clearTimeout || clearTimeout;
    const storage = env.storage || null;
    const $ = (id) => doc.getElementById(id);

    const state = {
      projectId: null, projects: [], workspace: null, maestro: null, models: [], catalogError: null,
      profiles: [], selectedPaneId: null, timer: null, drafts: {}, approvals: {}, openDetails: new Set(),
      planningDraft: { model: "", confirmCost: false },
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

    function checkbox(label, checked, onChange) {
      const input = el("input", { attrs: { type: "checkbox" } });
      input.checked = Boolean(checked);
      input.addEventListener("change", () => onChange(input.checked));
      return { input, label: el("label", { className: "ws-check" }, input, el("span", { text: label })) };
    }

    function showError(message) {
      const node = $("ws-error");
      node.textContent = message || "";
      node.hidden = !message;
    }

    function modelOptions(select, selected, includeNone) {
      const options = [];
      if (includeNone) options.push(el("option", { text: "— escolher —", attrs: { value: "" } }));
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

    /* ── Top bar ── */

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

    /* ── Maestro ── */

    function renderPlanning() {
      const planning = state.maestro && state.maestro.planning;
      const host = $("ws-maestro-planning");
      if (!planning) { host.replaceChildren(); return; }
      const details = disclosure("maestro:planning", planning.authorized
        ? `IA: ${planning.model} · ${costLabel(planning.cost_status)} · somente leitura`
        : "IA do Maestro desligada — usando proposta determinística");
      const select = el("select", { attrs: { "aria-label": "Modelo do Maestro" } });
      modelOptions(select, state.planningDraft.model || planning.model || "", true);
      select.addEventListener("change", () => { state.planningDraft = { model: select.value, confirmCost: false }; renderPlanning(); });
      const chosen = select.value;
      const needsCost = chosen && !isFree(modelCost(chosen));
      const cost = checkbox(`Autorizo usar ${chosen || "este modelo"} (${costLabel(modelCost(chosen))}) apenas para planejar`,
        state.planningDraft.confirmCost, (value) => { state.planningDraft.confirmCost = value; });
      cost.label.hidden = !needsCost;
      const activate = button(planning.authorized ? "Trocar modelo" : "Ativar Maestro", () => authorizePlanning(select.value, cost.input.checked),
        { className: "ws-button-small ws-button-primary", disabled: !chosen });
      details.append(
        el("p", { className: "ws-msg-meta", text: "O Maestro lê o projeto sem escrever nada. Autorizar o planejamento não autoriza a execução de agentes." }),
        select, cost.label, activate);
      if (!planning.authorized && !state.models.length) details.appendChild(el("p", { className: "ws-msg-meta", text: "Nenhum modelo aprovado disponível agora." }));
      host.replaceChildren(details);
    }

    function renderAgentRow(plan, agent, draft, editable) {
      const deps = agent.depends_on.length
        ? `depois de ${agent.depends_on.map((key) => (plan.agents.find((item) => item.key === key) || { title: key }).title).join(", ")}`
        : (agent.independent ? "independente" : "em sequência");
      const head = el("div", { className: "ws-agent-head" },
        el("strong", { text: agent.title }), chip(agent.role), chip(deps));
      const body = el("div", { className: "ws-agent-task", text: agent.task });
      const row = el("div", { className: "ws-proposal-row ws-agent" }, head, body);
      if (agent.acceptance_criteria && agent.acceptance_criteria.length) {
        const criteria = disclosure(`${plan.plan_id}:${agent.key}:criteria`, "Critérios de aceite");
        criteria.appendChild(el("ul", { className: "ws-plan-criteria" }, ...agent.acceptance_criteria.map((item) => el("li", { text: item }))));
        row.appendChild(criteria);
      }
      if (editable) {
        const select = el("select", { attrs: { "aria-label": `Modelo de ${agent.title}` } });
        modelOptions(select, draft.models[agent.key] || "", true);
        select.addEventListener("change", () => { draft.models[agent.key] = select.value; draft.confirmCosts = false; renderMaestro(); });
        const recommended = agent.model ? `recomendado: ${agent.model}` : "sem recomendação gratuita";
        row.appendChild(el("div", { className: "ws-agent-model" }, select,
          chip(draft.models[agent.key] ? costLabel(modelCost(draft.models[agent.key])) : recommended,
            draft.models[agent.key] && isFree(modelCost(draft.models[agent.key])) ? "ok" : "warn")));
      } else {
        row.appendChild(el("div", { className: "ws-agent-model" }, chip(agent.model || "sem modelo"), chip(costLabel(agent.cost_status))));
      }
      return row;
    }

    function approvalDraft(plan) {
      if (!state.approvals[plan.plan_id]) {
        const models = {};
        plan.agents.forEach((agent) => { models[agent.key] = agent.model || ""; });
        state.approvals[plan.plan_id] = { models, profiles: {}, maxTurns: 1, timeoutMinutes: 15, confirmCosts: false };
      }
      const draft = state.approvals[plan.plan_id];
      if (plan.status === "APPROVED") {
        const limits = approvedExecutionLimits(plan);
        draft.maxTurns = limits.error ? null : limits.max_turns_per_agent;
        draft.timeoutMinutes = limits.error ? null : limits.mission_timeout_seconds / 60;
        draft.confirmCosts = Boolean(draft.confirmCosts || persistedCostConfirmed(plan));
      }
      return draft;
    }

    function renderPlan(plan) {
      const source = planSourceLabel(plan);
      const card = el("div", { className: "ws-plan", dataset: { source: plan.source, status: plan.status } });
      card.appendChild(el("div", { className: "ws-plan-head" },
        chip(source.text, source.tone), chip(PLAN_STATUS[plan.status] || plan.status, plan.status === "PROPOSED" ? "" : "ok"),
        plan.agents.length ? chip(plan.agents.length === 1 ? "1 agente" : `${plan.agents.length} agentes · em sequência`) : null));
      if (plan.fallback_reason) card.appendChild(el("p", { className: "ws-msg-meta", text: `Motivo do fallback: ${plan.fallback_reason}` }));
      if (!plan.agents.length) return card;
      const editable = plan.status === "PROPOSED" && Boolean(state.workspace);
      const draft = approvalDraft(plan);
      plan.agents.forEach((agent) => card.appendChild(renderAgentRow(plan, agent, draft, editable)));
      if (plan.status === "APPROVED") {
        const limits = approvedExecutionLimits(plan);
        const execution = plan.execution;
        if (execution) {
          card.appendChild(el("div", { className: "ws-pane-section" },
            el("h3", { text: EXECUTION_STATUS_LABELS[execution.status] || execution.status }),
            el("div", { className: "ws-msg-meta", text: `${execution.current_index}/${execution.steps.length} agentes processados` }),
            ...execution.steps.map((step) => el("div", { text: `${step.ordinal + 1}. ${step.agent_key}: ${step.status}${step.error_code ? ` · ${step.error_code}` : ""}` })),
            execution.error_message ? el("div", { className: "ws-error", text: execution.error_message }) : null,
            ["RUNNING", "WAITING_DEPENDENCY"].includes(execution.status)
              ? button("Interromper sequência", () => interruptExecution(plan), { className: "ws-button-secondary" }) : null));
          return card;
        }
        const profileControls = plan.agents.map((agent) => {
          const select = el("select", { attrs: { "aria-label": `Perfil de teste de ${agent.title}` } },
            ...state.profiles.map((profile) => el("option", { text: profile.name || profile.id, attrs: { value: profile.id } })));
          select.value = draft.profiles[agent.key] || (state.profiles[0] && state.profiles[0].id) || "";
          draft.profiles[agent.key] = select.value;
          select.addEventListener("change", () => { draft.profiles[agent.key] = select.value; });
          return el("label", { className: "ws-plan-limits" }, el("span", { text: `Teste · ${agent.title}` }), select);
        });
        card.append(
          el("p", { className: limits.error ? "ws-error" : "ws-msg-meta", text: limits.error || `Limites aprovados: ${limits.max_turns_per_agent} turnos por agente · ${limits.mission_timeout_seconds} segundos por missão` }),
          el("div", { className: "ws-plan-limits" }, ...profileControls),
          button("Autorizar e executar sequência", () => authorizeExecution(plan), { className: "ws-button-primary", disabled: !state.profiles.length || Boolean(limits.error) }));
        return card;
      }
      if (!editable) return card;

      const turns = el("select", { attrs: { "aria-label": "Turnos por agente" } },
        ...[1, 2, 3, 4, 5, 6].map((value) => el("option", { text: `${value} ${value === 1 ? "turno" : "turnos"} por agente`, attrs: { value: String(value) } })));
      turns.value = String(draft.maxTurns);
      turns.addEventListener("change", () => { draft.maxTurns = Number(turns.value); });
      const timeout = el("select", { attrs: { "aria-label": "Duração máxima por missão" } },
        ...TIMEOUT_CHOICES.map((value) => el("option", { text: `até ${value} min por missão`, attrs: { value: String(value) } })));
      timeout.value = String(draft.timeoutMinutes);
      timeout.addEventListener("change", () => { draft.timeoutMinutes = Number(timeout.value); });
      const preview = approvalPayload(plan, { ...draft, confirmCosts: true }, state.models);
      const paid = preview.paid || [];
      const cost = checkbox(`Autorizo os modelos não gratuitos: ${paid.join(", ")}`, draft.confirmCosts, (value) => { draft.confirmCosts = value; });
      cost.label.hidden = !paid.length;
      const approve = button(`Aprovar e criar ${plan.agents.length === 1 ? "o agente" : `${plan.agents.length} agentes`}`,
        () => approvePlan(plan), { className: "ws-button-primary" });
      card.append(
        el("div", { className: "ws-plan-limits" }, turns, timeout),
        cost.label,
        el("p", { className: "ws-msg-meta", text: "A aprovação cria os agentes e autoriza estes modelos dentro dos limites acima. Nenhuma missão começa sozinha; nada é aplicado ao projeto. Limites financeiros não são impostos pela infraestrutura." }),
        approve);
      return card;
    }

    async function authorizeExecution(plan) {
      const draft = approvalDraft(plan);
      const result = executionPayload(plan, draft);
      if (result.error) { showError(result.error); return; }
      try {
        await api(`/api/workspace/plans/${encodeURIComponent(plan.plan_id)}/execute`, { method: "POST", body: result.payload });
        showError("");
      } catch (error) { showError(error.message); }
      await refresh({ force: true });
    }

    async function interruptExecution(plan) {
      try { await api(`/api/workspace/plans/${encodeURIComponent(plan.plan_id)}/interrupt`, { method: "POST" }); showError(""); }
      catch (error) { showError(error.message); }
      await refresh({ force: true });
    }

    function renderMaestro() {
      const conversation = state.maestro;
      const items = [];
      (conversation ? conversation.messages : []).forEach((message) => {
        if (message.author === "user") {
          const item = el("li", { className: "ws-msg ws-msg-user", text: message.text });
          if (message.status === "FAILED") item.appendChild(el("div", { className: "ws-msg-meta", text: `Falha: ${message.error_code || "erro"}` }));
          items.push(item);
          return;
        }
        const item = el("li", { className: "ws-msg" }, el("div", { className: "ws-msg-text", text: message.text }));
        if (message.plan) item.appendChild(renderPlan(message.plan));
        items.push(item);
      });
      if (conversation && conversation.busy) items.push(el("li", { className: "ws-msg ws-msg-pending", text: "O Maestro está analisando o projeto…" }));
      $("ws-maestro-log").replaceChildren(...items);
      renderPlanning();
    }

    /* ── Panes ── */

    function renderMission(pane) {
      const mission = pane.latest_mission;
      if (!mission) return null;
      const section = el("div", { className: "ws-pane-section" }, el("h3", { text: "Última missão" }),
        el("div", { text: `${STATUS_LABELS[mission.status] || mission.status} · tentativa ${mission.attempt_count}` }));
      if (mission.tests.length) {
        const last = mission.tests[mission.tests.length - 1];
        section.appendChild(el("div", { text: `Testes: ${last.status}${last.exit_code === null || last.exit_code === undefined ? "" : ` (exit ${last.exit_code})`}` }));
      }
      if (mission.error_code) section.appendChild(el("div", { className: "ws-error", text: `${mission.error_code}: ${mission.error_message || ""}` }));
      if (mission.changed_files.length) {
        section.appendChild(el("ul", { className: "ws-pane-files" },
          ...mission.changed_files.map((file) => el("li", { text: `${file.status} ${file.path}` }))));
      }
      if (mission.status === "READY_FOR_REVIEW" && !mission.integration_applied) {
        section.appendChild(el("div", { className: "ws-pane-review" },
          el("span", { text: "Revisão humana necessária. Nada foi aplicado ao projeto. " }),
          el("a", { text: "Revisar no modo avançado", attrs: { href: "/nexus" } })));
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

    function renderBrief(pane) {
      if (!pane.brief) return null;
      const section = el("div", { className: "ws-pane-section" }, el("h3", { text: "Tarefa planejada" }),
        el("div", { className: "ws-agent-task", text: pane.brief.task }));
      if (pane.brief.acceptance_criteria.length) {
        const criteria = disclosure(`${pane.pane_id}:criteria`, "Critérios de aceite");
        criteria.appendChild(el("ul", { className: "ws-plan-criteria" }, ...pane.brief.acceptance_criteria.map((item) => el("li", { text: item }))));
        section.appendChild(criteria);
      }
      const waiting = (pane.dependencies || []).filter((dependency) => !dependency.ready);
      if (waiting.length) section.appendChild(el("p", { className: "ws-msg-meta", text: `Aguardando revisão de: ${waiting.map((item) => item.title).join(", ")}` }));
      const auth = pane.brief.authorization;
      section.appendChild(chip(auth.applies ? `Autorizado pelo plano · ${auth.turns_used}/${auth.max_turns} usados` : "Autorização do plano esgotada ou modelo alterado", auth.applies ? "ok" : "warn"));
      return section;
    }

    function renderChat(pane) {
      const section = el("div", { className: "ws-pane-section" }, el("h3", { text: "Conversa" }));
      const list = el("ol", { className: "ws-pane-chat" });
      (pane.messages || []).forEach((message) => {
        const outcome = outcomeLabel(message);
        list.appendChild(el("li", { className: "ws-chat-user", text: message.text }));
        const reply = el("li", { className: "ws-chat-agent" });
        reply.appendChild(chip(outcome.text, outcome.tone));
        if (message.response_text) reply.appendChild(el("div", { className: "ws-msg-text", text: message.response_text }));
        else if (message.error_code) reply.appendChild(el("div", { className: "ws-error", text: message.error_code }));
        list.appendChild(reply);
      });
      if (!list.children.length) list.appendChild(el("li", { className: "ws-msg-meta", text: "Sem mensagens ainda." }));
      const draft = state.drafts[pane.pane_id] || {};
      const input = el("textarea", { attrs: { maxlength: "100000", placeholder: `Mensagem para ${pane.title}`, "aria-label": `Mensagem para ${pane.title}` } });
      input.value = draft.message || "";
      input.addEventListener("input", () => { state.drafts[pane.pane_id] = { ...state.drafts[pane.pane_id], message: input.value }; });
      const authorized = planAuthorized(pane);
      const confirm = checkbox(pane.model ? `Confirmo o uso de ${pane.model}` : "Escolha um modelo em Configurar", false, () => {});
      confirm.label.hidden = authorized || !pane.model;
      const cost = checkbox(`Autorizo o custo de ${pane.model} (${costLabel(modelCost(pane.model))})`, false, () => {});
      cost.label.hidden = authorized || !pane.model || isFree(modelCost(pane.model));
      const disabled = !(state.workspace && state.workspace.execution_enabled) || pane.busy || !pane.model;
      const send = button("Enviar", async () => {
        const result = messagePayload(pane, { text: input.value, confirmed: confirm.input.checked,
          confirmCost: cost.input.checked, costStatus: modelCost(pane.model) });
        if (result.error) { showError(result.error); return; }
        send.disabled = true;
        try {
          await api(`/api/workspace/panes/${encodeURIComponent(pane.pane_id)}/messages`, { method: "POST", body: result.payload });
          state.drafts[pane.pane_id] = { ...state.drafts[pane.pane_id], message: "" };
          showError("");
          await refresh({ force: true });
        } catch (error) {
          showError(error.message);
          send.disabled = false;
        }
      }, { className: "ws-button-small", disabled });
      section.append(list, el("div", { className: "ws-pane-form" }, input, confirm.label, cost.label, send),
        el("p", { className: "ws-msg-meta", text: "Mensagens rodam no worktree isolado deste agente. Se alterarem código, a mudança fica marcada para verificação." }));
      return section;
    }

    function renderMissionForm(pane) {
      const draft = state.drafts[pane.pane_id] || {};
      const planned = Boolean(pane.brief);
      const details = disclosure(`${pane.pane_id}:mission`, planned ? "Executar tarefa planejada" : "Nova missão (com testes)");
      if (planned) details.open = details.open || !pane.latest_mission;
      const description = el("textarea", { attrs: { maxlength: "100000", placeholder: planned ? "Instruções extras (opcional)" : "Missão para este agente", "aria-label": `Missão para ${pane.title}` } });
      description.value = draft.description || "";
      description.addEventListener("input", () => { state.drafts[pane.pane_id] = { ...state.drafts[pane.pane_id], description: description.value }; });
      const profile = el("select", { attrs: { "aria-label": "Perfil de teste" } },
        ...(state.profiles.length
          ? state.profiles.map((item) => el("option", { text: item.name || item.id, attrs: { value: item.id } }))
          : [el("option", { text: "Nenhum perfil de teste confiável para este projeto", attrs: { value: "" } })]));
      if (state.profiles.length) profile.value = draft.testProfile || state.profiles[0].id;
      profile.addEventListener("change", () => { state.drafts[pane.pane_id] = { ...state.drafts[pane.pane_id], testProfile: profile.value }; });
      const authorized = planAuthorized(pane);
      const confirm = checkbox(pane.model ? `Confirmo o uso de ${pane.model}` : "Escolha um modelo em Configurar", false, () => {});
      confirm.label.hidden = authorized || !pane.model;
      const cost = checkbox(`Autorizo o custo de ${pane.model} (${costLabel(modelCost(pane.model))})`, false, () => {});
      cost.label.hidden = authorized || !pane.model || isFree(modelCost(pane.model));
      const start = button(planned ? "Executar tarefa" : "Iniciar missão", async () => {
        const result = missionPayload(pane, { description: description.value, testProfile: profile.value,
          confirmed: confirm.input.checked, confirmCost: cost.input.checked, costStatus: modelCost(pane.model) });
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
      }, { className: "ws-button-primary", disabled: !canStartMission(pane, state.workspace || {}) || !state.profiles.length });
      details.appendChild(el("div", { className: "ws-pane-form" }, description, profile, confirm.label, cost.label, start));
      return details;
    }

    function renderConfig(pane) {
      const details = disclosure(`${pane.pane_id}:config`, "Detalhes e configuração");
      const select = el("select", { attrs: { "aria-label": `Modelo de ${pane.title}` } });
      modelOptions(select, pane.model, !pane.model);
      select.addEventListener("change", () => patchPane(pane.pane_id, { model: select.value }));
      details.append(
        el("p", { className: "ws-msg-meta", text: "A troca de modelo vale apenas para turnos e missões futuras." }),
        select,
        el("div", { className: "ws-mono", text: `${pane.pane_id} · sessão ${pane.session ? pane.session.session_id : "—"}` }),
        el("div", { className: "ws-mono", text: pane.session ? `${pane.session.branch} · ${pane.session.worktree_path}` : "" }),
      );
      if (pane.latest_mission) {
        details.appendChild(el("div", { className: "ws-mono", text: `missão ${pane.latest_mission.mission_id} · ${pane.latest_mission.requested_model}` }));
      }
      details.appendChild(renderHistory(pane));
      return details;
    }

    function renderHistory(pane) {
      const details = disclosure(`${pane.pane_id}:history`, "Histórico técnico");
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
        expanded: pane.layout.expanded, collapsed: pane.layout.collapsed, origin: pane.origin,
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
          chip(pane.origin === "maestro" ? "criado pelo Maestro" : pane.origin === "policy" ? "proposta determinística" : "manual")),
        renderBrief(pane),
        renderMission(pane),
        renderChat(pane),
        renderMissionForm(pane),
        renderConfig(pane));
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

    function editingIn(id) {
      const active = doc.activeElement;
      const host = $(id);
      return Boolean(active && host && host.contains && host.contains(active) && ["TEXTAREA", "INPUT", "SELECT"].includes(active.tagName));
    }

    function render(options = {}) {
      renderGates();
      if (options.force || !(editingIn("ws-maestro-log") || editingIn("ws-maestro-planning"))) renderMaestro();
      if (options.force || !editingIn("ws-grid")) renderGrid();
      const ready = Boolean(state.projectId && state.workspace && state.maestro);
      $("ws-add-pane").disabled = !ready;
      $("ws-maestro-send").disabled = !ready || Boolean(state.maestro && state.maestro.busy);
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

    async function authorizePlanning(model, confirmCost) {
      try {
        state.maestro = await api("/api/workspace/maestro/authorize", { method: "POST", body: { project_id: state.projectId, model, confirm_cost: Boolean(confirmCost) } });
        state.planningDraft = { model: "", confirmCost: false };
        state.openDetails.delete("maestro:planning");
        showError("");
      } catch (error) {
        showError(error.message);
      }
      render({ force: true });
    }

    async function sendMaestro(event) {
      if (event && event.preventDefault) event.preventDefault();
      const input = $("ws-maestro-input");
      const text = input.value.trim();
      if (!text || !state.projectId) return;
      $("ws-maestro-send").disabled = true;
      try {
        state.maestro = await api("/api/workspace/maestro/messages", { method: "POST", body: { project_id: state.projectId, text } });
        input.value = "";
        showError("");
      } catch (error) {
        showError(error.message);
      }
      render({ force: true });
      scheduleRefresh();
    }

    async function approvePlan(plan) {
      const result = approvalPayload(plan, approvalDraft(plan), state.models);
      if (result.error) { showError(result.error); return; }
      try {
        const approved = await api(`/api/workspace/plans/${encodeURIComponent(plan.plan_id)}/approve`, { method: "POST", body: result.payload });
        if (approved.panes && approved.panes.length) state.selectedPaneId = approved.panes[0].pane_id;
        const errors = approved.plan && approved.plan.approval ? approved.plan.approval.errors : [];
        showError(errors && errors.length ? `Alguns agentes não foram criados: ${errors.map((item) => `${item.key}: ${item.code}`).join("; ")}` : "");
      } catch (error) {
        showError(error.message);
      }
      await refresh({ force: true });
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
        const pane = await api("/api/workspace/panes", { method: "POST", body: {
          project_id: state.projectId, role: $("ws-add-role").value, title: $("ws-add-name").value || null, model: $("ws-add-model").value || null,
        } });
        state.selectedPaneId = pane.pane_id;
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
      const panesBusy = state.workspace && state.workspace.panes.some((pane) => pane.busy || (pane.messages || []).some((message) => message.outcome === null));
      const busy = panesBusy || Boolean(state.maestro && state.maestro.busy) || Boolean(state.catalogError);
      state.timer = schedule(() => { refresh().catch(() => {}); }, busy ? FAST_POLL_MS : IDLE_POLL_MS);
    }

    async function loadModels() {
      const models = await api("/api/dev/models").catch((error) => ({ models: [], catalog_error: error.message }));
      state.models = models.models || [];
      state.catalogError = models.catalog_error || null;
    }

    async function refresh(options = {}) {
      if (!state.projectId) { render(options); return; }
      // Model discovery can fail transiently; keep retrying until it works so
      // plans never stay unapprovable because of one bad first load.
      if (!state.models.length || state.catalogError) await loadModels();
      const query = `project_id=${encodeURIComponent(state.projectId)}`;
      const [workspace, maestro] = await Promise.all([
        api(`/api/workspace/state?${query}`).catch((error) => { showError(error.message); return null; }),
        api(`/api/workspace/maestro?${query}`).catch((error) => { showError(error.message); return state.maestro; }),
      ]);
      state.workspace = workspace;
      state.maestro = maestro;
      if (workspace && !workspace.panes.some((pane) => pane.pane_id === state.selectedPaneId)) {
        state.selectedPaneId = workspace.panes.length ? workspace.panes[0].pane_id : null;
      }
      render(options);
      scheduleRefresh();
    }

    async function loadProject(projectId) {
      state.projectId = projectId || null;
      state.maestro = null;
      state.profiles = [];
      state.approvals = {};
      writePrefs({ project: state.projectId });
      if (!state.projectId) { render(); return; }
      state.selectedPaneId = (readPrefs().selected || {})[state.projectId] || null;
      const profiles = await api(`/api/dev/test-profiles?project_id=${encodeURIComponent(state.projectId)}`).catch(() => ({ profiles: [] }));
      state.profiles = profiles.profiles || [];
      await refresh({ force: true });
    }

    async function init() {
      $("ws-maestro-form").addEventListener("submit", sendMaestro);
      $("ws-maestro-input").addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendMaestro(); }
      });
      $("ws-add-pane").addEventListener("click", openAddDialog);
      $("ws-add-form").addEventListener("submit", confirmAdd);
      $("ws-add-cancel").addEventListener("click", () => { const dialog = $("ws-add-dialog"); if (dialog.close) dialog.close(); else dialog.removeAttribute("open"); });
      $("ws-project-select").addEventListener("change", (event) => loadProject(event.target.value));

      const [projects] = await Promise.all([
        api("/api/projects").catch((error) => { showError(error.message); return { projects: [] }; }),
        loadModels(),
      ]);
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

    return { init, refresh, state, api, sendMaestro, approvePlan, authorizeExecution, authorizePlanning, patchPane, move, select };
  }

  const exported = {
    createWorkspace, paneStatus, density, sortPanes, moveUpdates, missionPayload, messagePayload,
    canStartMission, costLabel, approvalPayload, approvedExecutionLimits, executionPayload, planSourceLabel, outcomeLabel, planAuthorized,
  };
  global.NexusWorkspace = exported;
  if (typeof module !== "undefined" && module.exports) module.exports = exported;

  if (global.document && global.document.getElementById && global.document.getElementById("ws-main")) {
    let storage = null;
    try { storage = global.localStorage; } catch (_error) { storage = null; }
    createWorkspace({ document: global.document, fetch: global.fetch.bind(global), storage }).init();
  }
})(typeof window !== "undefined" ? window : globalThis);
