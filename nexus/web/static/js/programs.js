/* Nexus – Program Control Center (Phase UI-1.1)
   Read-only observability over the Program layer (nexus build/run).
   Polling-based, same style as nexus.js. Never guesses state from text:
   every rendered field comes straight from /api/programs{,/{id}}.

   PROGRAM VIEW vs RUN VIEW: exactly one is active at a time (coordinated
   through window.NexusMode / window.NexusRun, defined in nexus.js, which
   loads first). Selecting a Program always clears any open Run from the
   center workspace and the sidebar's "active" highlight, and vice versa
   -- see setViewMode()/deselectProgram() and nexus.js's openRun()/
   deselectRun(). */

(function () {
  "use strict";

  var state = {
    programs: [],
    selectedProgramId: null,
    userSelected: false,   // true once the user has explicitly picked something
    pollTimer: null,
  };

  var $ = function (id) { return document.getElementById(id); };

  var dom = {
    sidebarPrograms: $("sidebar-programs"),
    cpEmpty:         $("cp-empty"),
    cpBody:          $("cp-body"),
    waitingBanner:   $("cp-waiting-banner"),
    waitingText:     $("cp-waiting-text"),
    programId:       $("cp-program-id"),
    programName:     $("cp-program-name"),
    status:          $("cp-status"),
    phaseOf:         $("cp-phase-of"),
    autonomy:        $("cp-autonomy"),
    duration:        $("cp-duration"),
    phases:          $("cp-phases"),
    acceptance:      $("cp-acceptance"),
    failure:         $("cp-failure"),
    routing:         $("cp-routing"),
    telemetry:       $("cp-telemetry"),
    agents:          $("cp-agents"),
    files:           $("cp-files"),
    // Center workspace (PROGRAM VIEW)
    wsEmpty:         $("ws-empty"),
    wsProgram:       $("ws-program"),
    wsProgramId:     $("ws-program-id"),
    wsProgramStatus: $("ws-program-status"),
    wsProgramName:   $("ws-program-name"),
    wsProgramTiming: $("ws-program-timing"),
    wsProgramPhase:  $("ws-program-phase"),
    programPhaseGroups: $("program-phase-groups"),
  };

  function fetch_json(url) {
    return fetch(url).then(function (r) { return r.json(); });
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function parseUTC(ts) {
    if (!ts) return null;
    var s = String(ts).trim();
    if (!/[Zz]$/.test(s) && !/[+-]\d{2}:\d{2}$/.test(s) && !/[+-]\d{4}$/.test(s)) {
      s = s.replace(" ", "T") + "Z";
    }
    var d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
  }

  function elapsed(start, end) {
    var s = parseUTC(start);
    if (!s) return "UNKNOWN";
    var e = end ? parseUTC(end) : new Date();
    var ms = e - s;
    if (ms < 0) return "UNKNOWN";
    var sec = Math.round(ms / 1000);
    if (sec < 60) return sec + "s";
    var min = Math.floor(sec / 60);
    if (min < 60) return min + "m " + (sec % 60) + "s";
    return Math.floor(min / 60) + "h " + (min % 60) + "m";
  }

  var TERMINAL_STATUSES = ["COMPLETED", "BLOCKED"];

  function isTerminal(status) {
    return TERMINAL_STATUSES.indexOf(status) !== -1;
  }

  function statusClass(s) {
    return "s-" + String(s || "unknown").toLowerCase();
  }

  /* ── Sidebar: Programs list ── */

  function loadPrograms() {
    fetch_json("/api/programs").then(function (data) {
      state.programs = data.programs || [];
      renderSidebar();

      var running = state.programs.filter(function (p) { return p.status === "RUNNING"; });
      // Section 6: only a single, unambiguous RUNNING Program is eligible
      // to claim initial focus; two at once is genuinely ambiguous, so
      // neither wins and the generic Run fallback decides instead.
      window.NexusMode.runningProgramId = running.length === 1 ? running[0].program_id : null;
      window.NexusMode.programsReady = true;
      if (window.NexusMode.tryDecideInitialFocus) window.NexusMode.tryDecideInitialFocus();

      // A Program already selected keeps being polled even if it drops
      // out of a later list refresh's "current" slot -- nothing here
      // re-selects on its own once the user (or the initial-focus
      // decision) has made a choice.
    }).catch(function () {});
  }

  function renderSidebar() {
    dom.sidebarPrograms.innerHTML = "";
    if (!state.programs.length) {
      dom.sidebarPrograms.appendChild(el("div", "empty-state", "No Programs yet."));
      return;
    }
    state.programs.forEach(function (p) {
      // Only highlight while PROGRAM VIEW is actually showing this one.
      var active = window.NexusMode.mode === "program" && state.selectedProgramId === p.program_id;
      var item = el("div", "sidebar-program-item" + (active ? " active" : ""));

      var top = el("div", "sidebar-program-top");
      top.appendChild(el("span", "sidebar-program-id", p.name || p.project_id || p.program_id));
      var badge = el("span", "sidebar-run-status " + statusClass(p.status), p.status || "UNKNOWN");
      top.appendChild(badge);
      item.appendChild(top);

      var subText = p.status === "COMPLETED" ? "COMPLETED"
        : p.current_phase ? "Phase " + p.current_phase + " / " + p.phase_count
        : (p.status || "UNKNOWN");
      item.appendChild(el("div", "sidebar-program-sub", subText));

      if (p.waiting_for_user) {
        item.appendChild(el("div", "sidebar-program-waiting", "⚠ " + (p.waiting_reason || "APPROVAL REQUIRED")));
      }

      item.title = p.program_id;
      item.addEventListener("click", function () {
        state.userSelected = true;
        selectProgram(p.program_id);
      });
      dom.sidebarPrograms.appendChild(item);
    });
  }

  /* ── Selecting a Program (exclusive with RUN VIEW) ── */

  function selectProgram(programId) {
    if (window.NexusRun && window.NexusRun.deselectRun) window.NexusRun.deselectRun();
    setViewMode("program");
    document.body.setAttribute("data-view", "detail");
    document.querySelectorAll(".nav-item").forEach(function (item) {
      item.classList.remove("active");
    });

    state.selectedProgramId = programId;
    stopPolling();
    dom.cpEmpty.classList.add("hidden");
    dom.cpBody.classList.add("visible");
    if (dom.wsEmpty) dom.wsEmpty.style.display = "none";
    if (dom.wsProgram) dom.wsProgram.classList.add("visible");

    pollOnce();
    state.pollTimer = setInterval(pollOnce, 4000);
    renderSidebar();
  }

  // Called by nexus.js's openRun() so a Program left open from before
  // never keeps showing in the right panel alongside an unrelated Run.
  function deselectProgram() {
    state.selectedProgramId = null;
    stopPolling();
    dom.cpBody.classList.remove("visible");
    dom.cpEmpty.classList.remove("hidden");
    if (dom.wsProgram) dom.wsProgram.classList.remove("visible");
    renderSidebar();
  }

  function setViewMode(mode) {
    window.NexusMode.mode = mode;
    var badge = document.getElementById("view-mode-badge");
    if (badge) {
      badge.textContent = mode === "program" ? "PROGRAM VIEW" : mode === "run" ? "RUN VIEW" : "OVERVIEW";
      badge.className = "view-mode-badge vm-" + (mode || "none");
    }
  }

  function stopPolling() {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  }

  function pollOnce() {
    var programId = state.selectedProgramId;
    if (!programId) return;
    fetch_json("/api/programs/" + programId).then(function (data) {
      var program = data.program;
      if (!program || state.selectedProgramId !== programId) return;
      renderProgram(program);
      if (isTerminal(program.status)) stopPolling();
    }).catch(function () {
      stopPolling();
    });
  }

  /* ── Rendering ── */

  function renderProgram(p) {
    renderOverview(p);
    renderWaitingBanner(p);
    renderCenterWorkspace(p);
    renderPhases(p);
    renderAcceptance(p.acceptance_summary, p.final);
    renderFailure(p);
    renderRouting(p.routing_timeline);
    renderTelemetry(p.telemetry);
    renderAgents(p.active_agents);
    renderFiles(p.files_changed_by_stage, p.phases);
  }

  function renderOverview(p) {
    dom.programId.textContent = p.program_id;
    dom.programName.textContent = p.name || p.project_id || "—";
    dom.status.textContent = p.status || "UNKNOWN";
    dom.status.className = "cp-status-badge st-" + String(p.status || "unknown").toLowerCase();

    var total = (p.phases || []).length + 1; // + final
    if (p.current_phase) {
      dom.phaseOf.textContent = "Phase " + p.current_phase + " / " + total;
    } else if (p.status === "COMPLETED") {
      dom.phaseOf.textContent = "All " + total + " phases done";
    } else {
      dom.phaseOf.textContent = "—";
    }

    dom.autonomy.textContent = (p.task_contract && p.task_contract.authority_resolution &&
      p.task_contract.authority_resolution.autonomy_profile) || "DEFAULT";
    dom.duration.textContent = elapsed(p.created_at, isTerminal(p.status) ? p.updated_at : null);
  }

  function renderWaitingBanner(p) {
    if (p.waiting_for_user) {
      dom.waitingBanner.style.display = "flex";
      dom.waitingText.textContent = (p.waiting_reason || "APPROVAL REQUIRED") + " — Nexus needs a decision";
    } else {
      dom.waitingBanner.style.display = "none";
    }
  }

  /* ── Center workspace: the Program's live "AI team" ──
     Reuses nexus.js's exact agent-card renderer (window.NexusRun.
     renderPhaseGroups) against the Program's current-phase agents, so a
     Program shows the same real Manager/Worker/Reviewer cards a Run does
     -- never a fabricated persistent roster. */
  function renderCenterWorkspace(p) {
    if (!dom.wsProgram) return;
    dom.wsProgramId.textContent = p.program_id;
    dom.wsProgramName.textContent = p.name || p.project_id || "—";
    var status = p.status || "UNKNOWN";
    dom.wsProgramStatus.textContent = p.waiting_for_user ? "WAITING FOR USER" : status;
    dom.wsProgramStatus.className = "run-hdr-status " + (
      p.waiting_for_user ? "ast-recoverable" :
      status === "COMPLETED" ? "ast-completed" :
      status === "BLOCKED" ? "ast-failed" :
      status === "RUNNING" ? "ast-running" : "ast-created"
    );
    dom.wsProgramPhase.textContent = p.current_phase
      ? "Phase " + p.current_phase + ": " + (currentStageTitle(p) || "")
      : (status === "COMPLETED" ? "All phases done" : "—");
    dom.wsProgramTiming.textContent = "⏱ " + elapsed(p.created_at, isTerminal(status) ? p.updated_at : null);

    if (window.NexusRun && window.NexusRun.renderPhaseGroups) {
      window.NexusRun.renderPhaseGroups(p.active_agents, dom.programPhaseGroups);
    }
    if (!p.active_agents || !p.active_agents.length) {
      dom.programPhaseGroups.innerHTML =
        '<div class="empty-state"><div class="empty-icon">&#9675;</div>' +
        (isTerminal(status) ? "No agents were active in the final recorded phase." : "No agents active yet.") +
        "</div>";
    }
  }

  function currentStageTitle(p) {
    if (p.current_stage_kind === "final") return p.final && p.final.title;
    var phase = (p.phases || []).find(function (ph) { return ph.index === p.current_phase; });
    return phase && phase.title;
  }

  var PHASE_ICON = { DONE: "✓", RUNNING: "●", BLOCKED: "✕", FAILED: "✕", PENDING: "○" };
  var PHASE_ICON_CLASS = { DONE: "done", RUNNING: "running", BLOCKED: "blocked", FAILED: "failed", PENDING: "pending" };

  function renderPhases(p) {
    dom.phases.innerHTML = "";
    (p.phases || []).forEach(function (phase) {
      dom.phases.appendChild(renderPhaseRow(
        phase.title, phase.status,
        p.current_stage_kind === "phase" && p.current_phase === phase.index,
        phase.run_ids
      ));
    });
    if (p.final) {
      dom.phases.appendChild(renderPhaseRow(
        p.final.title || "Final verification", p.final.status,
        p.current_stage_kind === "final", p.final.run_ids
      ));
    }
  }

  function renderPhaseRow(title, status, isCurrent, runIds) {
    status = status || "PENDING";
    var row = el("div", "cp-phase-row" + (isCurrent ? " current" : ""));
    row.appendChild(el("span", "cp-phase-icon " + (PHASE_ICON_CLASS[status] || "pending"),
      PHASE_ICON[status] || "○"));
    var titleEl = el("span", "cp-phase-title", title);
    titleEl.title = title; // full name on hover -- never destroys the layout
    row.appendChild(titleEl);
    row.appendChild(el("span", "cp-phase-state", status));
    if (runIds && runIds.length && window.NexusRun && window.NexusRun.openRun) {
      var lastRun = runIds[runIds.length - 1];
      var chip = el("span", "cp-phase-run-chip", "↗");
      chip.title = "Open " + lastRun;
      chip.addEventListener("click", function (ev) {
        ev.stopPropagation();
        state.userSelected = true;
        window.NexusRun.openRun(lastRun);
      });
      row.appendChild(chip);
    }
    return row;
  }

  function renderAcceptance(summary, final) {
    dom.acceptance.innerHTML = "";
    if (!summary || !summary.acceptance_total) {
      dom.acceptance.appendChild(el("div", "cp-empty-inline", "No acceptance criteria evaluated yet."));
      return;
    }
    var head = el("div", "cp-acc-headline");
    head.innerHTML = "<b>" + summary.acceptance_satisfied + "</b> / " + summary.acceptance_total + " satisfied";
    dom.acceptance.appendChild(head);

    var totals = el("div", "cp-acc-totals");
    totals.innerHTML =
      "<span><b>" + summary.acceptance_satisfied + "</b> satisfied</span>" +
      "<span><b>" + summary.acceptance_unsatisfied + "</b> unsatisfied</span>" +
      "<span><b>" + summary.acceptance_unverified + "</b> unverified</span>";
    dom.acceptance.appendChild(totals);

    var bar = el("div", "cp-acc-bar");
    var t = summary.acceptance_total || 1;
    [["satisfied", summary.acceptance_satisfied], ["unsatisfied", summary.acceptance_unsatisfied],
     ["unverified", summary.acceptance_unverified]].forEach(function (pair) {
      if (pair[1] > 0) {
        var seg = el("div", "cp-acc-bar-seg " + pair[0]);
        seg.style.width = (100 * pair[1] / t) + "%";
        bar.appendChild(seg);
      }
    });
    dom.acceptance.appendChild(bar);

    var criteria = (final && final.acceptance) || [];
    criteria.slice(0, 12).forEach(function (c) {
      var row = el("div", "cp-acc-item");
      var mark = c.status === "SATISFIED" ? "✓" : c.status === "UNSATISFIED" ? "✕" : "○";
      var markClass = c.status ? c.status.toLowerCase() : "pending";
      row.appendChild(el("span", "cp-acc-mark " + markClass, mark));
      var desc = el("span", "cp-acc-desc", c.description || c.id || "—");
      desc.title = c.description || "";
      row.appendChild(desc);
      dom.acceptance.appendChild(row);
    });
  }

  // Section 10: failure and the recovery Nexus already took (or will take)
  // are two separate lines -- a fixed, documented mapping of the real
  // failure_classification.py taxonomy (nexus/failure_classification.py),
  // never inferred from free text.
  var FAILURE_ACTION = {
    TRANSIENT_PROVIDER:       "RETRY → FALLBACK",
    ROUTE_UNAVAILABLE:        "BLOCKED",
    CAPABILITY_INSUFFICIENT:  "MODEL EXCLUSION / ESCALATION",
    VERIFICATION_FAILED:      "CORRECTIVE MISSION",
    REQUIREMENT_UNSATISFIED:  "CORRECTIVE MISSION",
    ENVIRONMENT_FAILURE:      "BLOCKED",
    AUTHORITY_REQUIRED:       "WAITING FOR APPROVAL",
    POLICY_BLOCKED:           "BLOCKED",
    UNSUPPORTED_CAPABILITY:   "BLOCKED",
    UNKNOWN_FAILURE:          "UNKNOWN",
  };

  function renderFailure(p) {
    dom.failure.innerHTML = "";
    var rows = [];
    (p.phases || []).forEach(function (phase) {
      if (phase.last_failure_class) {
        rows.push({ label: "Phase " + phase.index, failure_class: phase.last_failure_class, detail: phase.last_error });
      }
    });
    if (p.final && p.final.last_failure_class) {
      rows.push({ label: "Final", failure_class: p.final.last_failure_class, detail: p.final.last_error });
    }
    if (!rows.length) {
      dom.failure.appendChild(el("div", "cp-empty-inline", "No failures recorded."));
      return;
    }
    rows.forEach(function (r) {
      var row = el("div", "cp-failure-row");

      var failLine = el("div", "cp-failure-line");
      failLine.appendChild(el("span", "cp-failure-k", "FAILURE"));
      failLine.appendChild(el("span", "cp-failure-class", r.failure_class));
      row.appendChild(failLine);

      var action = FAILURE_ACTION[r.failure_class] || "UNKNOWN";
      var actionLine = el("div", "cp-failure-line");
      actionLine.appendChild(el("span", "cp-failure-k", "ACTION"));
      var actionCls = "cp-failure-action" + (action === "BLOCKED" ? " blocked" : action === "WAITING FOR APPROVAL" ? " waiting" : "");
      actionLine.appendChild(el("span", actionCls, action));
      row.appendChild(actionLine);

      row.appendChild(el("div", "cp-failure-scope", r.label));

      if (r.detail) {
        var details = el("details", "cp-failure-details");
        details.appendChild(el("summary", "", "raw detail"));
        var detail = String(r.detail);
        details.appendChild(el("div", "cp-failure-detail", detail.length > 400 ? detail.slice(0, 400) + "…" : detail));
        row.appendChild(details);
      }
      dom.failure.appendChild(row);
    });
  }

  var ROUTING_LABEL = {
    INITIAL_ROUTE: "INITIAL ROUTE",
    RETRY_SAME_TIER: "RETRY (same tier)",
    RETRY: "RETRY",
    FALLBACK_ROUTE_ATTEMPT: "FALLBACK",
    FALLBACK_EXHAUSTED: "FALLBACK EXHAUSTED",
    ESCALATE: "ESCALATION",
    BLOCKED: "BLOCKED",
  };

  function renderRouting(events) {
    dom.routing.innerHTML = "";
    if (!events || !events.length) {
      dom.routing.appendChild(el("div", "cp-empty-inline", "No routing decisions yet."));
      return;
    }
    events.slice(-20).forEach(function (e) {
      var row = el("div", "cp-routing-item");
      row.appendChild(el("span", "cp-routing-kind " + e.kind, ROUTING_LABEL[e.kind] || e.kind));
      var modelText = e.model || "UNKNOWN";
      if (e.worker_ordinal !== null && e.worker_ordinal !== undefined) {
        modelText = "W" + e.worker_ordinal + " · " + modelText;
      }
      var modelEl = el("span", "cp-routing-model", modelText);
      modelEl.title = modelText;
      row.appendChild(modelEl);
      dom.routing.appendChild(row);
    });
  }

  function renderTelemetry(t) {
    dom.telemetry.innerHTML = "";
    if (!t) {
      dom.telemetry.appendChild(el("div", "cp-empty-inline", "UNKNOWN"));
      return;
    }
    var grid = el("div", "cp-tel-grid");
    function row(k, v) {
      grid.appendChild(el("span", "cp-tel-k", k));
      grid.appendChild(el("span", "cp-tel-v", v));
    }
    row("LLM calls", t.llm_calls || 0);
    row("Tokens", t.total_tokens === null || t.total_tokens === undefined ? "UNKNOWN" : t.total_tokens);
    row("Retries", t.retry_count || 0);
    row("Fallbacks", t.fallback_count || 0);
    row("Escalations", t.escalation_count || 0);
    row("Models", (t.models_used || []).length);
    dom.telemetry.appendChild(grid);
    if (!t.llm_calls) {
      dom.telemetry.appendChild(el("div", "cp-empty-inline",
        "Telemetry is only visible for runs launched from this server process."));
    }
  }

  var ROLE_LABEL = { Manager: "MGR", Worker: "WORKER", ManagerReview: "REVIEWER", Scout: "SCOUT" };
  var STATUS_COLOR = {
    COMPLETED: "var(--green)", RUNNING: "var(--accent-hl)", FAILED: "var(--red)",
    BLOCKED: "var(--red)", CREATED: "var(--text-dim)",
  };
  // Human-readable label for route_class -- the Manager's planned capability
  // requirement. Unlike model/provider/effort (always the Adaptive Router's
  // actual decision by the time an agent row exists), route_class is never
  // overwritten, so it is the one truthful "planned requirement" to show
  // alongside the actual execution identity.
  var ROUTE_CLASS_LABEL = {
    "mechanical": "Mechanical",
    "standard-coding": "Standard Coding",
    "complex-coding": "Advanced Coding",
    "review-critical": "Review",
    "security-critical": "Security Review",
  };

  // Cache of fetched full-result payloads, keyed by agent id, so toggling
  // a report panel open/closed repeatedly never re-fetches.
  var fullResultCache = {};

  function renderAgents(agents) {
    dom.agents.innerHTML = "";
    if (!agents || !agents.length) {
      dom.agents.appendChild(el("div", "cp-empty-inline", "No agents active for the current phase."));
      return;
    }
    agents.forEach(function (a) {
      if (a.route_class) {
        var plannedLine = el(
          "div",
          "cp-agent-planned",
          "PLANNED: " + (ROUTE_CLASS_LABEL[a.route_class] || a.route_class)
        );
        dom.agents.appendChild(plannedLine);
      }

      var row = el("div", "cp-agent-row");
      row.appendChild(el("span", "cp-agent-role", ROLE_LABEL[a.role] || (a.role || "?").toUpperCase()));
      var modelEl = el("span", "cp-agent-model", a.model || "UNKNOWN");
      modelEl.title = a.route_class
        ? "Actual execution (not the planned " + a.route_class + " requirement): " + (a.model || "")
        : (a.model || "");
      row.appendChild(modelEl);
      var status = el("span", "cp-agent-status", (a.status || "?").toUpperCase());
      status.style.color = STATUS_COLOR[a.status] || "var(--text-dim)";
      row.appendChild(status);

      if (a.has_full_result) {
        var reportBtn = el("button", "cp-agent-report-btn", "FULL REPORT");
        reportBtn.type = "button";
        row.appendChild(reportBtn);
        dom.agents.appendChild(row);

        var panel = el("div", "cp-agent-report-panel");
        panel.style.display = "none";
        dom.agents.appendChild(panel);

        reportBtn.addEventListener("click", function () {
          toggleAgentReport(a.id, panel, reportBtn);
        });
      } else {
        dom.agents.appendChild(row);
      }
    });
  }

  function toggleAgentReport(agentId, panel, button) {
    var isOpen = panel.style.display !== "none";
    if (isOpen) {
      panel.style.display = "none";
      button.classList.remove("open");
      return;
    }

    panel.style.display = "block";
    button.classList.add("open");

    if (fullResultCache[agentId]) {
      renderReportPanel(panel, fullResultCache[agentId]);
      return;
    }

    panel.textContent = "Loading full report…";
    fetch_json("/api/operational/agents/" + encodeURIComponent(agentId) + "/result")
      .then(function (payload) {
        var result = payload && payload.result;
        if (!result || !result.has_full_result) {
          panel.textContent = "No full report preserved for this agent (historical run, or agent produced no complete result).";
          return;
        }
        fullResultCache[agentId] = result;
        renderReportPanel(panel, result);
      })
      .catch(function () {
        panel.textContent = "Failed to load full report.";
      });
  }

  function renderReportPanel(panel, result) {
    panel.textContent = "";
    var pre = el("pre", "cp-agent-report-text", result.full_result || "");
    panel.appendChild(pre);
    if (result.full_result_truncated) {
      panel.appendChild(el(
        "div",
        "cp-empty-inline",
        "Report exceeded the safety bound and was truncated."
      ));
    }
  }

  function renderFiles(filesByStage, phases) {
    dom.files.innerHTML = "";
    var any = false;
    (filesByStage || []).forEach(function (entry, idx) {
      if (!entry || !entry.available || !entry.files || !entry.files.length) return;
      any = true;
      var stage = el("div", "cp-file-stage");
      var title = (phases && phases[idx] && phases[idx].title) || ("Phase " + (idx + 1));
      stage.appendChild(el("div", "cp-file-stage-title", title));
      entry.files.slice(0, 8).forEach(function (f) {
        stage.appendChild(el("div", "cp-file-name", f));
      });
      if (entry.truncated) stage.appendChild(el("div", "cp-empty-inline", "…more not shown"));
      dom.files.appendChild(stage);
    });
    if (!any) dom.files.appendChild(el("div", "cp-empty-inline", "No committed file activity yet."));
  }

  /* ── Public bridge for nexus.js (Run → Program mode coordination) ── */
  window.NexusProgram = {
    selectProgram: function (id) { selectProgram(id); },
    deselectProgram: deselectProgram,
  };

  /* ── Bootstrap ── */
  loadPrograms();
  setInterval(loadPrograms, 6000);

})();
