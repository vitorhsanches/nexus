/* Nexus – Execution Control Center
   Polling-based live interface. No build tools required. */

(function () {
  "use strict";

  /* ── State ── */
  var state = {
    projects:        [],
    runs:            [],
    selectedProject: null,
    selectedRun:     null,
    activeJobId:     null,
    pollTimer:       null,
    nexusStatus:     "idle",
    lastRunId:       null,
    jobStartedAt:    null,
    agentPollTimer:  null,
    hasAutoOpened:   false,
    openRunId:       null,   // tracks which run is currently displayed
    agentOutputOpenId: null, // agent id shown in the output modal, if open
  };

  /* ── View-mode coordinator, shared with programs.js ──
     Exactly one of RUN VIEW / PROGRAM VIEW is ever active, so the center
     workspace and the right control panel can never show two unrelated
     execution contexts at once. Created here (nexus.js loads first);
     programs.js reads/extends the same object. */
  window.NexusMode = window.NexusMode || {
    mode: "overview",      // "overview" | "run" | "program"
    runsReady: false,
    programsReady: false,
    runningProgramId: null,
    decided: false,
  };

  /* ── DOM refs ── */
  var $ = function (id) { return document.getElementById(id); };

  var dom = {
    projectList:        $("sidebar-projects"),
    runList:            $("sidebar-runs"),
    topProject:         $("top-project"),
    topRun:             $("top-run"),
    nexusStatusEl:      $("nexus-status"),
    nexusStatusTxt:     $("nexus-status-text"),
    projectSelect:      $("project-select"),
    missionInput:       $("mission-input"),
    managerModelSelect: $("manager-model-select"),
    managerModelFilter: $("manager-model-filter"),
    managerModelReason: $("manager-model-reason"),
    prepareDepsCheck:   $("prepare-dependencies-check"),
    runBtn:             $("run-btn"),
    runMsg:             $("run-msg"),
    wsEmpty:            $("ws-empty"),
    wsRun:              $("ws-run"),
    wsProgram:          $("ws-program"),
    viewModeBadge:      $("view-mode-badge"),
    wsRunId:            $("ws-run-id"),
    wsRunStatus:        $("ws-run-status"),
    wsMission:          $("ws-run-mission"),
    wsRunManager:       $("ws-run-manager"),
    wsTiming:           $("ws-run-timing"),
    lcManager:          $("lc-manager"),
    lcScouts:           $("lc-scouts"),
    lcWorkers:          $("lc-workers"),
    lcReview:           $("lc-review"),
    lcIntegration:      $("lc-integration"),
    phaseGroups:        $("phase-groups"),
    missionProgress:    $("mission-progress"),
    reviewContent:      $("review-content"),
    integrationContent: $("integration-content"),
    resultContent:      $("result-content"),
    timeline:           $("timeline"),
  };

  /* ── Utilities ── */

  function fetch_json(url, opts) {
    return fetch(url, opts).then(function (r) { return r.json(); });
  }

  // parseUTC: treat bare "YYYY-MM-DD HH:MM:SS" timestamps as UTC (no local-time shift)
  function parseUTC(ts) {
    if (!ts) return null;
    var s = String(ts).trim();
    // If no timezone indicator (Z, z, or ±HH:MM / ±HHMM), treat as UTC
    if (!/[Zz]$/.test(s) && !/[+-]\d{2}:\d{2}$/.test(s) && !/[+-]\d{4}$/.test(s)) {
      s = s.replace(' ', 'T') + 'Z';
    }
    var d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
  }

  function elapsed(start, end) {
    if (!start) return null;
    var s = parseUTC(start);
    var e = end ? parseUTC(end) : new Date();
    if (!s) return null;
    var ms = e - s;
    if (ms < 0) return null;
    var sec = Math.round(ms / 1000);
    if (sec < 60) return sec + "s";
    return Math.floor(sec / 60) + "m " + (sec % 60) + "s";
  }

  function fmtTime(iso) {
    if (!iso) return "";
    try {
      var d = new Date(iso);
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    } catch (e) { return iso; }
  }

  function statusClass(s) {
    return "s-" + String(s || "created").toLowerCase();
  }

  function runDisplayStatus(run) {
    return String((run && (run.display_status || run.status)) || "UNKNOWN").toUpperCase();
  }

  function agentStatusClass(s) {
    return "ast-" + String(s || "created").toLowerCase();
  }

  function roleClass(r) {
    return "role-" + String(r || "").toLowerCase().replace(/[^a-z]/g, "");
  }

  // Shared by agent cards, the output modal, and Mission Progress so the
  // same agent is never labeled two different ways in the same view.
  function agentRoleLabel(a) {
    return a.role === "ManagerReview" ? "REVIEWER" :
      a.role === "Worker" && a.worker_ordinal !== null && a.worker_ordinal !== undefined
        ? "WORKER #" + a.worker_ordinal
        : (a.role || "AGENT").toUpperCase();
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  /* ── Nexus status indicator ── */
  function setNexusStatus(s) {
    state.nexusStatus = s;
    dom.nexusStatusEl.className = "nexus-status " + s;
    dom.nexusStatusTxt.textContent = s === "idle" ? "IDLE" : s === "active" ? "RUNNING" : "ERROR";
  }

  /* ── Load projects ── */
  function loadProjects() {
    fetch_json("/api/projects").then(function (data) {
      state.projects = data.projects || [];
      renderProjectSidebar();
      renderProjectSelect();
      if (!state.selectedProject && state.projects.length > 0) {
        var enabled = state.projects.filter(function (p) { return p.enabled; });
        if (enabled.length > 0) selectProject(enabled[0]);
      }
    }).catch(function () {});
  }

  /* ── Manager model selector (New Mission composer) ── */
  function loadManagerModels() {
    fetch_json("/api/manager/models").then(function (data) {
      state.managerModels = data || { auto: null, selectable: [], ineligible: [] };
      renderManagerModelSelect();
    }).catch(function () {});
  }

  function renderManagerModelSelect() {
    var sel = dom.managerModelSelect;
    if (!sel) return;
    var data = state.managerModels || { auto: null, selectable: [], ineligible: [] };
    var prev = sel.value;
    var filter = (dom.managerModelFilter && dom.managerModelFilter.value || "").trim().toLowerCase();

    sel.innerHTML = "";
    var autoLabel = (data.auto && data.auto.display_name) || "Auto — Nexus default";
    if (!filter || autoLabel.toLowerCase().indexOf(filter) !== -1) {
      var autoOpt = document.createElement("option");
      autoOpt.value = "";
      autoOpt.textContent = autoLabel;
      sel.appendChild(autoOpt);
    }

    (data.selectable || []).forEach(function (m) {
      var hay = (m.model_id + " " + m.display_name).toLowerCase();
      if (filter && hay.indexOf(filter) === -1) return;
      var opt = document.createElement("option");
      opt.value = m.model_id;
      opt.textContent = m.display_name + "  (" + m.model_id + ")";
      opt.title = m.reason || "";
      opt.dataset.reason = m.reason || "";
      sel.appendChild(opt);
    });

    (data.ineligible || []).forEach(function (m) {
      var hay = (m.model_id + " " + m.display_name).toLowerCase();
      if (filter && hay.indexOf(filter) === -1) return;
      var opt = document.createElement("option");
      opt.value = m.model_id;
      opt.textContent = "⚠ " + m.display_name + "  (unavailable)";
      opt.disabled = true;
      opt.title = m.reason || "";
      opt.dataset.reason = m.reason || "";
      sel.appendChild(opt);
    });

    // Keep the previous selection when it still has a matching option.
    var stillPresent = Array.prototype.some.call(sel.options, function (o) { return o.value === prev; });
    sel.value = stillPresent ? prev : "";
    updateManagerModelReason();
  }

  function updateManagerModelReason() {
    if (!dom.managerModelReason || !dom.managerModelSelect) return;
    var opt = dom.managerModelSelect.selectedOptions && dom.managerModelSelect.selectedOptions[0];
    dom.managerModelReason.textContent = (opt && opt.dataset && opt.dataset.reason) || "";
  }

  function renderProjectSidebar() {
    dom.projectList.innerHTML = "";
    if (!state.projects.length) {
      dom.projectList.appendChild(el("div", "empty-state", "No projects registered."));
      return;
    }
    state.projects.forEach(function (p) {
      if (!p.enabled) return;
      var active = state.selectedProject && state.selectedProject.id === p.id;
      var item = el("div", "sidebar-item" + (active ? " active" : ""));
      item.appendChild(el("span", "item-dot"));
      item.appendChild(el("span", "", p.name || p.id));
      item.addEventListener("click", function () { selectProject(p); });
      dom.projectList.appendChild(item);
    });
  }

  function renderProjectSelect() {
    var prev = dom.projectSelect.value;
    dom.projectSelect.innerHTML = '<option value="">— Select project —</option>';
    state.projects.forEach(function (p) {
      if (!p.enabled) return;
      var opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.name || p.id;
      dom.projectSelect.appendChild(opt);
    });
    if (prev) dom.projectSelect.value = prev;
  }

  function selectProject(p) {
    state.selectedProject = p;
    dom.topProject.textContent = p.name || p.id;
    dom.topProject.className = "topbar-value highlight";
    if (dom.projectSelect.value !== p.id) dom.projectSelect.value = p.id;
    renderProjectSidebar();
  }

  /* ── Load runs ── */
  function loadRuns() {
    fetch_json("/api/operational/runs").then(function (data) {
      state.runs = data.runs || [];
      renderRunSidebar();
      window.NexusMode.runsReady = true;
      tryDecideInitialFocus();
    }).catch(function () {});
  }

  // Prefer an active/running run, then the most recent one. Extracted so
  // both the shared initial-focus decider and the periodic fallback use
  // the exact same rule.
  function autoOpenBestRun() {
    if (state.hasAutoOpened || !state.runs.length) return;
    state.hasAutoOpened = true;
    var active = null;
    for (var i = 0; i < state.runs.length; i++) {
      var s = runDisplayStatus(state.runs[i]);
      if (s === "RUNNING" || s === "ROUTING" || s === "CREATED") {
        active = state.runs[i];
        break;
      }
    }
    var target = active || state.runs[0];
    if (target) openRun(target.id);
  }

  // Both loaders (runs here, Programs in programs.js) call this once they
  // have fetched at least once; it fires exactly once, deciding the SAME
  // single initial focus that section 6 requires -- a Program that is
  // genuinely RUNNING wins over the generic "most recent run" fallback,
  // so the two panes are never showing two independent contexts by
  // accident on first load.
  function tryDecideInitialFocus() {
    var m = window.NexusMode;
    if (m.decided || !m.runsReady || !m.programsReady) return;
    m.decided = true;
    // Phase 2 makes Overview the stable initial information architecture.
    // Runs and Programs remain one click away and still mutually exclusive.
    state.hasAutoOpened = true;
    showOverview();
  }

  function renderRunSidebar() {
    dom.runList.innerHTML = "";
    var runs = state.runs.slice(0, 15);
    if (!runs.length) {
      dom.runList.appendChild(el("div", "empty-state", "No runs yet."));
      return;
    }
    runs.forEach(function (r) {
      // Only highlight a Run as selected while RUN VIEW is actually active
      // -- a Program selection must never leave a stale Run looking active.
      var active = window.NexusMode.mode === "run" && state.openRunId === r.id;
      var item = el("div", "sidebar-item" + (active ? " active" : ""));
      item.appendChild(el("span", "item-dot"));
      item.appendChild(el("span", "", r.id));
      var displayStatus = runDisplayStatus(r);
      var badge = el("span", "sidebar-run-status " + statusClass(displayStatus), displayStatus);
      item.appendChild(badge);
      item.addEventListener("click", function () { openRun(r.id); });
      dom.runList.appendChild(item);
    });
  }

  /* ── View mode ── */
  function setViewMode(mode) {
    window.NexusMode.mode = mode;
    if (dom.viewModeBadge) {
      dom.viewModeBadge.textContent = mode === "run" ? "RUN VIEW" :
        mode === "program" ? "PROGRAM VIEW" : "OVERVIEW";
      dom.viewModeBadge.className = "view-mode-badge vm-" + (mode || "none");
    }
  }

  // Called by programs.js when a Program is selected, so a Run left open
  // from before never keeps showing in the center workspace alongside an
  // unrelated Program in the right panel.
  function deselectRun() {
    closeAgentOutputModal();
    currentActivityContext = { checkpoints: [], failure: null };
    state.openRunId = null;
    stopRunPolling();
    dom.wsRun.classList.remove("visible");
    dom.topRun.textContent = "—";
    dom.topRun.className = "topbar-value";
    renderRunSidebar();
  }

  function showOverview() {
    deselectRun();
    if (window.NexusProgram && window.NexusProgram.deselectProgram) {
      window.NexusProgram.deselectProgram();
    }
    setViewMode("overview");
    document.body.setAttribute("data-view", "overview");
    if (dom.wsEmpty) dom.wsEmpty.style.display = "none";
  }

  /* ── Open a run ── */
  function openRun(runId) {
    closeAgentOutputModal();   // never leave a stale agent's output open across a run switch
    currentActivityContext = { checkpoints: [], failure: null };   // cleared until this run's own poll lands
    if (window.NexusProgram && window.NexusProgram.deselectProgram) {
      window.NexusProgram.deselectProgram();
    }
    setViewMode("run");
    document.body.setAttribute("data-view", "detail");
    document.querySelectorAll(".nav-item").forEach(function (item) {
      item.classList.remove("active");
    });

    state.selectedRun = null;
    state.openRunId = runId;     // set early so sidebar can highlight
    stopRunPolling();
    dom.topRun.textContent = runId;
    dom.topRun.className = "topbar-value highlight";

    // Show run view, hide empty state and any Program workspace
    dom.wsEmpty.style.display = "none";
    if (dom.wsProgram) dom.wsProgram.classList.remove("visible");
    dom.wsRun.classList.add("visible");

    dom.wsRunId.textContent = runId;
    dom.wsRunStatus.textContent = "Loading…";
    dom.wsRun.setAttribute("data-phase", "loading");
    dom.wsRunStatus.className = "run-hdr-status ast-created";
    dom.wsMission.textContent = "";
    if (dom.wsRunManager) { dom.wsRunManager.textContent = "—"; dom.wsRunManager.removeAttribute("data-mode"); }
    dom.wsTiming.textContent = "";
    dom.phaseGroups.innerHTML = "";
    if (dom.missionProgress) dom.missionProgress.innerHTML = "";
    dom.timeline.innerHTML = "";
    dom.reviewContent.innerHTML = '<div class="empty-state"><div class="empty-icon">&#9675;</div>No reviews yet.</div>';
    dom.integrationContent.innerHTML = '<div class="empty-state"><div class="empty-icon">&#9675;</div>No integration events yet.</div>';
    dom.resultContent.innerHTML = '<div class="empty-state"><div class="empty-icon">&#9675;</div>Awaiting completion.</div>';
    resetLifecycle();
    startRunPolling(runId);
    renderRunSidebar();
  }

  /* ── Lifecycle stages ── */
  function resetLifecycle() {
    ["lc-manager", "lc-scouts", "lc-workers", "lc-review", "lc-integration"].forEach(function (id) {
      var e = $(id);
      if (e) e.className = "lifecycle-stage waiting";
    });
  }

  function updateLifecycle(checkpoints, agents, runStatus) {
    var boundaries = (checkpoints || []).map(function (c) { return c.boundary; });

    function hasBoundary(b) { return boundaries.indexOf(b) !== -1; }
    function hasRole(role) {
      return (agents || []).some(function (a) { return a.role === role; });
    }

    var terminated = hasBoundary("RUN_TERMINAL");
    var interrupted = runStatus === "RECOVERABLE";

    // PLANNING (Manager)
    if (dom.lcManager) {
      if (hasBoundary("PLAN_APPROVED")) dom.lcManager.className = "lifecycle-stage done";
      else if (hasRole("Manager")) dom.lcManager.className = "lifecycle-stage active";
      else dom.lcManager.className = "lifecycle-stage waiting";
    }

    // SCOUTING (Scout)
    if (dom.lcScouts) {
      if (hasRole("Scout") && hasBoundary("PLAN_APPROVED")) {
        var scoutsDone = (agents || []).filter(function (a) { return a.role === "Scout"; })
                                       .every(function (a) { return a.status === "COMPLETED"; });
        dom.lcScouts.className = "lifecycle-stage " + (scoutsDone ? "done" : "active");
      } else if (hasBoundary("EXECUTION_START")) {
        dom.lcScouts.className = "lifecycle-stage done";
      } else {
        dom.lcScouts.className = "lifecycle-stage waiting";
      }
    }

    // EXECUTION (Workers)
    if (dom.lcWorkers) {
      var workerAgents = (agents || []).filter(function (a) { return a.role === "Worker"; });
      if (workerAgents.length > 0) {
        var allDone = workerAgents.every(function (a) { return a.status === "COMPLETED"; });
        var anyFailed = workerAgents.some(function (a) { return a.status === "FAILED" || a.status === "BLOCKED"; });
        if (anyFailed && (terminated || interrupted)) dom.lcWorkers.className = "lifecycle-stage failed";
        else if (allDone) dom.lcWorkers.className = "lifecycle-stage done";
        else dom.lcWorkers.className = "lifecycle-stage active";
      } else {
        dom.lcWorkers.className = "lifecycle-stage waiting";
      }
    }

    // REVIEW (ManagerReview)
    if (dom.lcReview) {
      var reviewAgents = (agents || []).filter(function (a) { return a.role === "ManagerReview"; });
      if (reviewAgents.length > 0) {
        var allReviewDone = reviewAgents.every(function (a) { return a.status === "COMPLETED"; });
        dom.lcReview.className = "lifecycle-stage " + (allReviewDone ? "done" : "active");
      } else if (hasBoundary("REVIEW")) {
        dom.lcReview.className = "lifecycle-stage active";
      } else {
        dom.lcReview.className = "lifecycle-stage waiting";
      }
    }

    // DONE
    if (dom.lcIntegration) {
      if (runStatus === "COMPLETED") dom.lcIntegration.className = "lifecycle-stage done";
      else if (terminated || interrupted) dom.lcIntegration.className = "lifecycle-stage failed";
      else if (hasBoundary("REVIEW")) dom.lcIntegration.className = "lifecycle-stage active";
      else dom.lcIntegration.className = "lifecycle-stage waiting";
    }
  }

  /* ── Run header update ── */
  function updateRunHeader(run, checkpoints, agents) {
    var runId = run.id || "—";
    dom.wsRunId.textContent = runId;

    var status = runDisplayStatus(run);
    dom.wsRunStatus.textContent = status;
    var smap = {
      COMPLETED: "ast-completed", FAILED: "ast-failed", BLOCKED: "ast-failed",
      RUNNING: "ast-running", ROUTING: "ast-running", CREATED: "ast-created",
      RECOVERABLE: "ast-recoverable",
    };
    dom.wsRunStatus.className = "run-hdr-status " + (smap[status] || "ast-created");
    // The phase decides which element dominates the run view (see CSS).
    dom.wsRun.setAttribute("data-phase", phaseOf(status));
    var projectNode = document.getElementById("ws-run-project");
    if (projectNode) projectNode.textContent = run.project_name || run.project_id || "—";

    // Mission text from intent
    var intent = run.intent || "";
    dom.wsMission.textContent = intent.length > 90 ? intent.slice(0, 90) + "…" : intent;

    // Manager selection: mode is decided at Run creation and never changes
    // for an already-running Run, even if the composer's selector changes
    // afterward (a later selection only affects the *next* mission).
    if (dom.wsRunManager) {
      var mgrMode = run.manager_mode || "auto";
      if (mgrMode === "manual" && run.manager_requested_model) {
        var mgrAgent = (agents || []).filter(function (a) { return a.role === "Manager"; })[0];
        if (mgrAgent) {
          dom.wsRunManager.textContent = "Manager: manual · " + run.manager_requested_model;
        } else {
          var isTerminal = ["COMPLETED", "FAILED", "BLOCKED", "CANCELLED"].indexOf(
            String(run.status || "").toUpperCase()
          ) !== -1;
          // No Manager agent recorded. While the Run is still in flight this
          // may just be pending (DELEGATED strategy, Manager not dispatched
          // yet); once terminal with still no Manager agent, it means this
          // mission resolved to the DIRECT strategy (no Manager invoked at
          // all) -- never claim the requested model executed either way.
          dom.wsRunManager.textContent = isTerminal
            ? "Manager: manual (requested " + run.manager_requested_model + ", not invoked)"
            : "Manager: manual · " + run.manager_requested_model + " (pending)";
        }
        dom.wsRunManager.setAttribute("data-mode", "manual");
      } else {
        dom.wsRunManager.textContent = "Manager: auto";
        dom.wsRunManager.setAttribute("data-mode", "auto");
      }
    }

    // Timing: find RUN_TERMINAL checkpoint for end time
    var termCp = null;
    (checkpoints || []).forEach(function (cp) {
      if (cp.boundary === "RUN_TERMINAL") termCp = cp;
    });
    var endTime = termCp ? termCp.created_at : null;
    if (!endTime && status === "RECOVERABLE") {
      (checkpoints || []).forEach(function (cp) {
        if (cp.boundary === "LIFECYCLE_ORPHAN_DETECTED") endTime = cp.created_at;
      });
    }
    var dur = elapsed(run.created_at, endTime);
    dom.wsTiming.textContent = dur ? "⏱ " + dur : "";
  }

  /* ── Phase groups ── */
  var PHASE_DEFS = [
    { key: "planning",  label: "Planning",  labelColor: "var(--purple)",    roles: ["Manager"] },
    { key: "scouting",  label: "Scouting",  labelColor: "var(--cyan)",      roles: ["Scout"] },
    { key: "execution", label: "Execution", labelColor: "var(--accent-hl)", roles: ["Worker"] },
    { key: "review",    label: "Review",    labelColor: "var(--orange)",    roles: ["ManagerReview"] },
  ];

  // ``container`` defaults to the Run view's own grid; programs.js passes
  // its own center-workspace container to render the exact same agent
  // cards for a Program's currently active agents -- one implementation,
  // two contexts, no duplicated card markup.
  function renderPhaseGroups(agents, container) {
    container = container || dom.phaseGroups;
    container.innerHTML = "";
    if (!agents || !agents.length) return;

    PHASE_DEFS.forEach(function (phase) {
      var groupAgents = agents.filter(function (a) { return phase.roles.indexOf(a.role) !== -1; });
      if (!groupAgents.length) return;

      // Sort within group
      groupAgents.sort(function (a, b) { return (a.worker_ordinal || 0) - (b.worker_ordinal || 0); });

      var group = el("div", "phase-group");

      // Header
      var hdr = el("div", "phase-group-header");
      var lbl = el("span", "phase-group-label", phase.label);
      lbl.style.color = phase.labelColor;
      hdr.appendChild(lbl);
      hdr.appendChild(el("span", "phase-group-count",
        groupAgents.length + " agent" + (groupAgents.length !== 1 ? "s" : "")));
      group.appendChild(hdr);

      // Agent cards grid
      var grid = el("div", "phase-agents");
      groupAgents.forEach(function (a) { grid.appendChild(renderAgentCard(a, agents)); });
      group.appendChild(grid);

      container.appendChild(group);
    });
  }

  // Collapsed Agents disclosure: counts only. The full cards stay behind the
  // disclosure, so the default view shows health at a glance.
  // Collapsed Agents disclosure and System rows: counts only. A ✓ means every
  // member completed; a ✕ means at least one failed or was blocked.
  function updateAgentSummary(agents, checkpoints) {
    var node = document.getElementById("agent-disclosure-summary");
    var sysWorkers = document.getElementById("sys-workers");
    var sysReviews = document.getElementById("sys-reviews");
    var list = agents || [];
    var workers = list.filter(function (a) { return a.role === "Worker"; });
    var reviewers = list.filter(function (a) { return a.role === "ManagerReview"; });

    // A review counts as failed when its lifecycle verdict is not PASS, even
    // though the reviewer agent itself completed.
    var nonPassVerdicts = (checkpoints || []).filter(function (cp) {
      return cp.boundary === "LIFECYCLE" && cp.payload && cp.payload.verdict &&
        cp.payload.verdict !== "PASS";
    }).length;

    function group(members, extraFailed) {
      var done = countStatus(members, "COMPLETED");
      var failed = countStatus(members, "FAILED") + countStatus(members, "BLOCKED") + (extraFailed || 0);
      var allDone = members.length > 0 && done === members.length;
      var mark = failed ? " ✕" : (allDone ? " ✓" : "");
      var state = !members.length ? "idle" : (failed ? "bad" : (allDone ? "good" : "active"));
      return { text: done + "/" + members.length + mark, state: state };
    }

    var w = group(workers, 0);
    var r = group(reviewers, nonPassVerdicts);
    if (node) {
      node.textContent = list.length ? "Workers " + w.text + " · Reviews " + r.text : "no agents";
      var overall = !list.length ? "idle"
        : (w.state === "bad" || r.state === "bad") ? "bad"
        : (w.state === "good" && r.state === "good") ? "good" : "active";
      node.setAttribute("data-state", overall);
    }
    if (sysWorkers) { sysWorkers.textContent = w.text; sysWorkers.setAttribute("data-state", w.state); }
    if (sysReviews) { sysReviews.textContent = r.text; sysReviews.setAttribute("data-state", r.state); }
  }

  // Mission Progress: one compact row per real agent -- never a placeholder
  // for a role that has no agent yet. Workers/Reviewers/etc. are grouped and
  // ordered the same way the Agent Panes grid orders them, so the two stay
  // readable as the same story. Review rows show the resolved verdict (see
  // resolveReviewVerdict), never a bare "COMPLETED" read as a pass.
  var MISSION_PROGRESS_ROLE_ORDER = ["Manager", "Scout", "Worker", "ManagerReview"];

  function renderMissionProgress(agents, checkpoints) {
    var node = dom.missionProgress;
    if (!node) return;
    node.innerHTML = "";

    var list = agents || [];
    if (!list.length) return;

    var reviewCpByReviewerId = buildReviewCpByReviewerId(checkpoints);

    var ordered = [];
    MISSION_PROGRESS_ROLE_ORDER.forEach(function (role) {
      var members = list.filter(function (a) { return a.role === role; });
      members.sort(function (a, b) { return (a.worker_ordinal || 0) - (b.worker_ordinal || 0); });
      ordered = ordered.concat(members);
    });
    // A role outside the known set still gets a row -- never silently dropped.
    list.forEach(function (a) {
      if (MISSION_PROGRESS_ROLE_ORDER.indexOf(a.role) === -1) ordered.push(a);
    });

    ordered.forEach(function (a) {
      var row = el("div", "mp-row");
      row.appendChild(el("span", "mp-role", agentRoleLabel(a)));

      if (a.role === "ManagerReview") {
        var resolved = resolveReviewVerdict(a, agents, reviewCpByReviewerId);
        row.appendChild(el("span", "review-verdict " + resolved.cls, resolved.text));
      } else {
        row.appendChild(el("span", "agent-status " + agentStatusClass(a.status),
          (a.status || "WAITING").toUpperCase()));
      }

      node.appendChild(row);
    });
  }

  // One row per model this run has used. Roles and state come from the agents;
  // the full routing table is not reproduced here.
  function renderCurrentModels(agents) {
    var list = document.getElementById("current-models");
    if (!list) return;
    list.innerHTML = "";
    var byModel = {};
    (agents || []).forEach(function (a) {
      if (!a.model) return;
      var m = byModel[a.model] || (byModel[a.model] = { roles: {}, running: 0, failed: 0, done: 0 });
      var role = a.role === "ManagerReview" ? "Reviewer" : a.role === "Worker" ? "Worker" : a.role;
      m.roles[role] = (m.roles[role] || 0) + 1;
      var st = String(a.status || "").toUpperCase();
      if (st === "RUNNING") m.running++;
      else if (st === "FAILED" || st === "BLOCKED") m.failed++;
      else if (st === "COMPLETED") m.done++;
    });
    var models = Object.keys(byModel);
    if (!models.length) {
      list.appendChild(el("li", "model-empty", "No models in this run yet."));
      return;
    }
    models.forEach(function (id) {
      var m = byModel[id];
      var row = el("li", "model-row");
      row.appendChild(el("span", "model-id", id));
      row.appendChild(el("span", "model-roles", Object.keys(m.roles).map(function (role) {
        return role + " ×" + m.roles[role];
      }).join(" · ")));
      var state = m.running ? ["ACTIVE", "active"]
        : (m.failed && !m.done ? ["FAILED", "bad"] : ["DONE", "good"]);
      row.appendChild(el("span", "model-state " + state[1], state[0]));
      list.appendChild(row);
    });
  }

  function renderAgentCard(a, allAgents) {
    var status = (a.status || "created").toLowerCase();
    var card = el("div", "agent-card " + status);

    // Role + status row
    var roleBar = el("div", "agent-role-bar");
    roleBar.appendChild(el("span", "agent-role " + roleClass(a.role), agentRoleLabel(a)));
    roleBar.appendChild(el("span", "agent-status " + agentStatusClass(a.status),
      (a.status || "WAITING").toUpperCase()));
    card.appendChild(roleBar);

    // Agent ID
    card.appendChild(el("div", "agent-id", a.id || "—"));

    // Meta grid
    var meta = el("div", "agent-meta");
    function kv(k, v, mono) {
      var row = el("div", "meta-kv");
      row.appendChild(el("span", "meta-k", k));
      row.appendChild(el("span", "meta-v" + (mono ? " mono" : ""), v || "—"));
      meta.appendChild(row);
    }
    kv("model", a.model, true);
    kv("provider", a.provider);
    kv("effort", a.effort ? a.effort.toUpperCase() : "—");
    if (a.route_class) kv("route", a.route_class, true);
    if (a.role === "Manager" && state.selectedRun) {
      var mgrMode = state.selectedRun.manager_mode || "auto";
      var requested = state.selectedRun.manager_requested_model;
      if (mgrMode === "manual" && requested) {
        kv("selection", a.model === requested ? "manual ✓ matches" : ("manual — requested " + requested), true);
      } else {
        kv("selection", "auto", false);
      }
    }
    card.appendChild(meta);

    // Worktree: only ever the real path Nexus recorded, or an honest
    // "unavailable" (a completed/cleaned-up worktree is not fabricated).
    if (a.role === "Worker") {
      var wtRow = el("div", "agent-worktree");
      wtRow.appendChild(el("span", "meta-k", "worktree"));
      var wtVal = a.worktree ? a.worktree.split(/[\\/]/).pop() : null;
      wtRow.appendChild(el("span", "meta-v mono" + (wtVal ? "" : " dim"), wtVal || "UNAVAILABLE"));
      card.appendChild(wtRow);
    }

    // Duration
    if (a.started_at) {
      var dur = elapsed(a.started_at, a.finished_at);
      if (dur) card.appendChild(el("div", "agent-duration", "⏱ " + dur));
    }

    // Worker dependency: show parent only if parent is another Worker
    if (a.parent_agent_id && allAgents) {
      var parent = null;
      for (var i = 0; i < allAgents.length; i++) {
        if (allAgents[i].id === a.parent_agent_id) { parent = allAgents[i]; break; }
      }
      if (parent && parent.role === "Worker") {
        var ord = parent.worker_ordinal;
        var hasOrdinal = (typeof ord === 'number' && isFinite(ord));
        if (a.role === "ManagerReview") {
          // Reviewer depends on a worker – show "Reviewing: Worker [#N]"
          var deps = el("div", "agent-deps");
          deps.innerHTML = "Reviewing: <span>Worker" + (hasOrdinal ? " #" + ord : "") + "</span>";
          card.appendChild(deps);
        } else if (hasOrdinal) {
          // Worker→Worker DAG dependency; only display when ordinal is a real number
          var deps = el("div", "agent-deps");
          deps.innerHTML = "depends on: <span>Worker #" + ord + "</span>";
          card.appendChild(deps);
        }
        // If ordinal is undefined/null and role is not ManagerReview: render nothing
      }
    }

    // Execution events: only the checkpoints/fields genuinely attributable
    // to this exact agent id (see renderAgentActivity) -- never inferred
    // from worker_ordinal or lifecycle status alone.
    var activity = renderAgentActivity(a, currentActivityContext.checkpoints, currentActivityContext.failure);
    if (activity) card.appendChild(activity);

    // Output: the bounded `result` preview the agents API already returns
    // (no extra round trip), filterable in place. When the registry also
    // holds a complete `full_result`, an EXPAND action lazy-loads it into
    // the output modal -- never fetched just to render the pane.
    card.appendChild(renderAgentOutput(a));

    return card;
  }

  // Set once per poll cycle (pollRunOnce) right before renderPhaseGroups, and
  // cleared on deselectRun()/openRun() so a stale Run's checkpoints can never
  // leak into Program View's agent cards, which call renderPhaseGroups with
  // its own container but no checkpoint/failure context of its own.
  var currentActivityContext = { checkpoints: [], failure: null };

  var AGENT_ACTIVITY_LIMIT = 8;

  // Builds this one agent's event list from data already fetched for the
  // poll cycle -- no new request. Attribution is always by the checkpoint
  // payload's own agent id field (manager_id / worker_id / reviewer_id),
  // exactly as resolveReviewVerdict's reviewer_id fix requires -- never by
  // worker_ordinal, which is not a per-agent identifier (it is shared by
  // every attempt of the same worker slot).
  function buildAgentActivity(agent, checkpoints, failure) {
    var events = [];

    if (agent.started_at) {
      events.push({ time: agent.started_at, text: "Started", cls: "neutral" });
    }

    (checkpoints || []).forEach(function (cp) {
      var p = cp.payload || {};
      var entry = null;

      if (cp.boundary === "PLAN_APPROVED" && p.manager_id === agent.id) {
        entry = { text: "Plan approved", cls: "ok" };
      } else if (cp.boundary === "WORKER_ATTEMPT" && p.worker_id === agent.id) {
        var attemptStatus = String(p.status || "").toUpperCase();
        entry = {
          text: "Attempt " + (p.attempt != null ? p.attempt : "?") + " " + attemptStatus.toLowerCase(),
          cls: attemptStatus === "COMPLETED" ? "ok" : (attemptStatus === "FAILED" || attemptStatus === "BLOCKED") ? "bad" : "neutral",
        };
      } else if (cp.boundary === "REVIEW" && p.reviewer_id === agent.id) {
        entry = { text: "Review completed — " + (p.verdict || "NOT RECORDED"), cls: p.verdict === "PASS" ? "ok" : p.verdict ? "bad" : "neutral" };
      } else if (cp.boundary === "REVIEW" && p.worker_id === agent.id) {
        entry = { text: "Reviewed — " + (p.verdict || "NOT RECORDED"), cls: p.verdict === "PASS" ? "ok" : p.verdict ? "bad" : "neutral" };
      }

      if (entry) {
        entry.time = cp.created_at;
        events.push(entry);
      }
    });

    // The run's structured failure (nexus.failure_contract) already
    // sanitizes and bounds its message server-side, and carries the exact
    // agent it belongs to -- safe to show verbatim, and the only free-text
    // field this function ever displays.
    if (failure && failure.agent_id === agent.id && failure.message) {
      events.push({ time: null, text: "Failure: " + failure.message, cls: "bad" });
    }

    if (agent.finished_at) {
      var finalStatus = String(agent.status || "").toUpperCase();
      events.push({
        time: agent.finished_at,
        text: "Finished — " + (agent.status || "WAITING").toUpperCase(),
        cls: finalStatus === "COMPLETED" ? "ok" : (finalStatus === "FAILED" || finalStatus === "BLOCKED") ? "bad" : "neutral",
      });
    }

    // Bounded history per pane: keep only the most recent entries.
    if (events.length > AGENT_ACTIVITY_LIMIT) {
      events = events.slice(events.length - AGENT_ACTIVITY_LIMIT);
    }
    return events;
  }

  function renderAgentActivity(agent, checkpoints, failure) {
    var events = buildAgentActivity(agent, checkpoints, failure);
    if (!events.length) return null;

    var wrap = el("div", "agent-activity");
    wrap.appendChild(el("div", "agent-activity-label", "EXECUTION EVENTS"));

    var list = el("div", "agent-activity-list");
    events.forEach(function (ev) {
      var row = el("div", "agent-activity-row");
      row.appendChild(el("span", "agent-activity-time", ev.time ? fmtTime(ev.time) : "—"));
      row.appendChild(el("span", "agent-activity-text " + ev.cls, ev.text));
      list.appendChild(row);
    });
    wrap.appendChild(list);
    return wrap;
  }

  var RUNNING_LIKE_STATUSES = ["CREATED", "ROUTING", "RUNNING", "REVIEWING"];

  function renderAgentOutput(a) {
    if (!a.result) {
      var isRunningLike = RUNNING_LIKE_STATUSES.indexOf(String(a.status || "").toUpperCase()) !== -1;
      return el("div", "agent-output-empty",
        isRunningLike ? "No output yet — agent is still running." : "No output recorded for this agent.");
    }

    var wrap = el("div", "agent-output-wrap");

    var bar = el("div", "agent-output-bar");
    bar.appendChild(el("span", "agent-output-label", "PREVIEW"));
    if (a.has_full_result) {
      var expandBtn = el("button", "agent-output-expand", "EXPAND OUTPUT");
      expandBtn.type = "button";
      expandBtn.addEventListener("click", function () { openAgentOutputModal(a); });
      bar.appendChild(expandBtn);
    }
    wrap.appendChild(bar);

    var search = document.createElement("input");
    search.type = "search";
    search.className = "agent-output-search";
    search.placeholder = "filter output…";
    search.setAttribute("aria-label", "Filter agent output");

    var pre = el("div", "agent-output");
    String(a.result).split("\n").forEach(function (line) {
      pre.appendChild(el("div", "agent-output-line", line));
    });

    search.addEventListener("input", function () {
      var q = search.value.toLowerCase();
      pre.querySelectorAll(".agent-output-line").forEach(function (lineEl) {
        lineEl.style.display = (!q || lineEl.textContent.toLowerCase().indexOf(q) !== -1) ? "" : "none";
      });
    });

    wrap.appendChild(search);
    wrap.appendChild(pre);
    return wrap;
  }

  /* ── Agent output modal: focused, complete-output view ──
     Lives outside #phase-groups so the 3s run poll (which rebuilds that
     container from scratch every cycle) never touches it while open. Built
     once and reused; per-agent full results are cached by id so repeated
     opens of the same agent never re-fetch, and a failed fetch clears its
     cache entry so the next click retries. */
  var agentOutputModal = null;
  var agentResultCache = {};

  function ensureAgentOutputModal() {
    if (agentOutputModal) return agentOutputModal;

    var root = document.createElement("div");
    root.className = "agent-output-modal";
    root.hidden = true;
    root.innerHTML =
      '<div class="agent-output-modal-backdrop"></div>' +
      '<div class="agent-output-modal-panel" role="dialog" aria-modal="true" aria-label="Agent output">' +
        '<div class="agent-output-modal-header">' +
          '<div class="agent-output-modal-identity">' +
            '<span class="agent-role" id="aom-role"></span>' +
            '<span class="agent-status" id="aom-status"></span>' +
          '</div>' +
          '<button type="button" class="agent-output-modal-close" aria-label="Close output">&times;</button>' +
        '</div>' +
        '<div class="agent-output-modal-meta">' +
          '<span class="mono" id="aom-id"></span>' +
          '<span class="mono" id="aom-model"></span>' +
          '<span id="aom-provider"></span>' +
        '</div>' +
        '<input type="search" class="agent-output-search" id="aom-search" placeholder="filter output…" aria-label="Filter full output">' +
        '<div class="agent-output-modal-body" id="aom-body"></div>' +
      '</div>';
    document.body.appendChild(root);

    root.querySelector(".agent-output-modal-backdrop").addEventListener("click", closeAgentOutputModal);
    root.querySelector(".agent-output-modal-close").addEventListener("click", closeAgentOutputModal);

    agentOutputModal = root;
    return root;
  }

  function loadAgentFullResult(agentId) {
    if (!agentResultCache[agentId]) {
      agentResultCache[agentId] = fetch_json("/api/operational/agents/" + agentId + "/result")
        .then(function (data) {
          if (!data || !data.result) throw new Error((data && data.detail) || "No result data");
          return data.result;
        })
        .catch(function (err) {
          delete agentResultCache[agentId];   // allow a retry on the next open
          throw err;
        });
    }
    return agentResultCache[agentId];
  }

  function openAgentOutputModal(a) {
    var modal = ensureAgentOutputModal();
    modal.hidden = false;
    document.addEventListener("keydown", onAgentOutputModalKeydown);

    var roleEl = modal.querySelector("#aom-role");
    roleEl.textContent = agentRoleLabel(a);
    roleEl.className = "agent-role " + roleClass(a.role);
    var statusEl = modal.querySelector("#aom-status");
    statusEl.textContent = (a.status || "WAITING").toUpperCase();
    statusEl.className = "agent-status " + agentStatusClass(a.status);
    modal.querySelector("#aom-id").textContent = a.id || "—";
    modal.querySelector("#aom-model").textContent = a.model || "—";
    modal.querySelector("#aom-provider").textContent = a.provider || "—";

    var searchEl = modal.querySelector("#aom-search");
    searchEl.value = "";
    searchEl.oninput = null;

    var body = modal.querySelector("#aom-body");
    body.innerHTML = "";
    body.appendChild(el("div", "agent-output-loading", "Loading full output…"));

    state.agentOutputOpenId = a.id;
    loadAgentFullResult(a.id).then(function (data) {
      if (state.agentOutputOpenId !== a.id) return;   // closed/switched before it resolved
      renderAgentOutputModalBody(data);
    }).catch(function (err) {
      if (state.agentOutputOpenId !== a.id) return;
      body.innerHTML = "";
      body.appendChild(el("div", "agent-output-error",
        "Failed to load full output: " + (err && err.message ? err.message : "unknown error")));
    });
  }

  function renderAgentOutputModalBody(data) {
    var modal = agentOutputModal;
    var body = modal.querySelector("#aom-body");
    body.innerHTML = "";

    var text = data.has_full_result ? data.full_result : data.preview;
    if (!text) {
      body.appendChild(el("div", "agent-output-empty", "No output recorded for this agent."));
      return;
    }

    if (data.full_result_truncated) {
      body.appendChild(el("div", "agent-output-note",
        "This output exceeded the safety limit and was truncated when it was saved; it is not the complete original text."));
    } else if (!data.has_full_result) {
      body.appendChild(el("div", "agent-output-note",
        "No full report was persisted for this agent; showing the bounded preview only."));
    }

    // A single <pre> regardless of size -- never one DOM node per line --
    // so even a multi-megabyte result stays cheap to render.
    var pre = document.createElement("pre");
    pre.className = "agent-output-full";
    pre.textContent = text;
    body.appendChild(pre);

    var searchTimer = null;
    modal.querySelector("#aom-search").oninput = function (e) {
      var q = e.target.value;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(function () {
        pre.textContent = !q ? text : text.split("\n").filter(function (line) {
          return line.toLowerCase().indexOf(q.toLowerCase()) !== -1;
        }).join("\n");
      }, 150);
    };
  }

  function closeAgentOutputModal() {
    if (!agentOutputModal || agentOutputModal.hidden) return;
    agentOutputModal.hidden = true;
    state.agentOutputOpenId = null;
    document.removeEventListener("keydown", onAgentOutputModalKeydown);
  }

  function onAgentOutputModalKeydown(e) {
    if (e.key === "Escape") closeAgentOutputModal();
  }

  /* ── Review panel ── */
  // reviewer agent id → REVIEW checkpoint payload. Keyed by `reviewer_id`
  // (the orchestrator stamps this on every REVIEW checkpoint it writes,
  // alongside the authoritative `verdict` -- see progressive.py) because
  // `worker_ordinal` is a checkpoint-only field: agent records returned by
  // /api/operational/agents never carry it, so keying on it here always
  // misses. Attempt-safe for free: each retry gets its own reviewer agent
  // and its own checkpoint, so there is no cross-attempt collision.
  function buildReviewCpByReviewerId(checkpoints) {
    var map = {};
    (checkpoints || []).forEach(function (cp) {
      if (cp.boundary === "REVIEW" && cp.payload && cp.payload.reviewer_id) {
        map[cp.payload.reviewer_id] = cp.payload;
      }
    });
    return map;
  }

  // The REVIEW checkpoint's persisted `verdict` is the only authoritative
  // answer to "did this review pass" -- it is the same value the execution
  // engine itself branches on to gate integration (progressive.py). A
  // reviewer agent reaching lifecycle status COMPLETED only means the
  // reviewer *process* finished; it says nothing about the verdict it
  // reached (a completed review can still be BLOCKED or RETRY), so that
  // status is never used to infer PASS -- or any other verdict. Absent a
  // checkpoint, the honest answer is "not recorded," not an assumed pass.
  function resolveReviewVerdict(rev, agents, reviewCpByReviewerId) {
    var parent = findAgent(agents, rev.parent_agent_id);
    var cp = reviewCpByReviewerId[rev.id];
    var verdict = cp ? (cp.verdict || null) : null;

    return {
      workerOrd: parent ? parent.worker_ordinal : null,
      verdict: verdict,
      cls: verdict === "PASS" ? "pass" : verdict ? "fail" : "pending",
      text: verdict || "NOT RECORDED",
    };
  }

  function renderReviewPanel(agents, checkpoints) {
    var reviewers = (agents || []).filter(function (a) { return a.role === "ManagerReview"; });
    if (!reviewers.length) {
      dom.reviewContent.innerHTML = '<div class="empty-state"><div class="empty-icon">&#9675;</div>No reviews yet.</div>';
      return;
    }

    var reviewCpByReviewerId = buildReviewCpByReviewerId(checkpoints);

    // Sort reviewers by parent worker ordinal
    reviewers.sort(function (a, b) {
      var wa = findAgent(agents, a.parent_agent_id);
      var wb = findAgent(agents, b.parent_agent_id);
      return ((wa && wa.worker_ordinal) || 0) - ((wb && wb.worker_ordinal) || 0);
    });

    dom.reviewContent.innerHTML = "";
    reviewers.forEach(function (rev) {
      var row = el("div", "review-row");

      var resolved = resolveReviewVerdict(rev, agents, reviewCpByReviewerId);
      var workerLabel = (resolved.workerOrd !== null && resolved.workerOrd !== undefined)
        ? "Worker #" + resolved.workerOrd
        : (rev.parent_agent_id ? rev.parent_agent_id.slice(-8) : "Worker");

      row.appendChild(el("span", "review-worker", workerLabel));
      row.appendChild(el("span", "review-arrow", "→"));
      row.appendChild(el("span", "review-verdict " + resolved.cls, resolved.text));

      dom.reviewContent.appendChild(row);
    });
  }

  function findAgent(agents, id) {
    if (!id || !agents) return null;
    for (var i = 0; i < agents.length; i++) {
      if (agents[i].id === id) return agents[i];
    }
    return null;
  }

  /* ── Integration panel ── */
  var INTG_LABELS = {
    "integration_started": "Integration started",
    "integration_complete": "Integration complete",
    "integration_failed": "Integration failed",
    "branch_created": "Branch created",
    "pr_created": "PR created",
    "tests_passed": "Tests passed",
    "tests_failed": "Tests failed",
  };

  function renderIntegrationPanel(checkpoints) {
    var intgCps = (checkpoints || []).filter(function (cp) {
      return cp.boundary === "LIFECYCLE" || cp.boundary === "RUN_TERMINAL";
    });
    if (!intgCps.length) {
      dom.integrationContent.innerHTML = '<div class="empty-state"><div class="empty-icon">&#9675;</div>No integration events yet.</div>';
      return;
    }
    dom.integrationContent.innerHTML = "";
    intgCps.forEach(function (cp) {
      var row = el("div", "intg-row");
      var label;
      if (cp.boundary === "RUN_TERMINAL") {
        var s = (cp.payload && cp.payload.status) ? cp.payload.status : "terminal";
        label = "Run " + s.toLowerCase();
      } else if (cp.payload && cp.payload.event) {
        label = INTG_LABELS[cp.payload.event] || cp.payload.event;
      } else {
        label = "Lifecycle checkpoint";
      }
      row.appendChild(el("span", "intg-label", label));
      if (cp.created_at) row.appendChild(el("span", "intg-time", fmtTime(cp.created_at)));
      dom.integrationContent.appendChild(row);
    });
  }

  /* ── Result panel ──
     Terminal runs get a first-class Result. Every fact is read from persisted
     run, checkpoint or agent records. Missing evidence is shown as "not
     recorded", never inferred. */
  var PHASE_OF = {
    COMPLETED: "completed", FAILED: "failed", BLOCKED: "blocked",
    RECOVERABLE: "recoverable", RUNNING: "running", ROUTING: "running",
    REVIEWING: "running", CREATED: "running",
  };

  function phaseOf(status) { return PHASE_OF[status] || "idle"; }

  function fact(label, value, mono) {
    var row = el("div", "result-fact");
    row.appendChild(el("span", "result-fact-k", label));
    var missing = !value;
    row.appendChild(el("span", "result-fact-v" + (mono ? " mono" : "") + (missing ? " dim" : ""),
      missing ? "not recorded" : value));
    return row;
  }

  function lastCheckpoint(checkpoints, predicate) {
    var found = null;
    (checkpoints || []).forEach(function (cp) { if (predicate(cp)) found = cp; });
    return found;
  }

  function countStatus(list, status) {
    return list.filter(function (a) { return String(a.status || "").toUpperCase() === status; }).length;
  }

  function actionButton(label, onClick) {
    var b = el("button", "text-action", label);
    b.type = "button";
    b.addEventListener("click", onClick);
    return b;
  }

  function showTrace() {
    dom.timeline.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderCompletedResult(run, agents, checkpoints, wrap) {
    var workers = (agents || []).filter(function (a) { return a.role === "Worker"; });
    var reviewers = (agents || []).filter(function (a) { return a.role === "ManagerReview"; });
    var passed = (checkpoints || []).filter(function (cp) {
      return cp.boundary === "LIFECYCLE" && cp.payload && cp.payload.verdict === "PASS";
    }).length;

    var head = el("div", "result-head");
    head.appendChild(el("span", "result-icon ok", "✓"));
    head.appendChild(el("span", "result-title ok", "MISSION COMPLETED"));
    wrap.appendChild(head);

    var facts = el("div", "result-facts");
    facts.appendChild(fact("Project", run.project_name || run.project_id));
    facts.appendChild(fact("Run", run.id, true));
    facts.appendChild(fact("Duration", elapsed(run.started_at, run.finished_at), true));
    facts.appendChild(fact("Workers", countStatus(workers, "COMPLETED") + "/" + workers.length + " completed", true));
    facts.appendChild(fact("Reviews", passed + "/" + reviewers.length + " passed", true));
    wrap.appendChild(facts);

    // Full text: the run result, else each Worker's output.
    var full = run.result || "";
    if (!full) {
      full = (agents || []).filter(function (a) { return a.role === "Worker" && a.result; })
        .map(function (a, i) { return "## Output (Worker " + (i + 1) + ")\n\n" + a.result; })
        .join("\n\n");
    }
    if (!full) {
      wrap.appendChild(el("div", "result-summary", "No final result was persisted for this run."));
      return;
    }
    var concise = full.split(/\n\s*\n/).filter(function (part) { return part.trim(); })[0] || full;
    concise = concise.trim();
    if (concise.length > 480) concise = concise.slice(0, 480).trimEnd() + "…";
    wrap.appendChild(el("div", "result-section-label", "Summary"));
    wrap.appendChild(el("div", "result-summary", concise));

    var details = el("details", "result-full");
    details.appendChild(el("summary", "", "Full report"));
    details.appendChild(el("pre", "result-output", full));

    var actions = el("div", "result-actions");
    actions.appendChild(actionButton("FULL REPORT", function () {
      details.open = true;
      details.scrollIntoView({ behavior: "smooth", block: "start" });
    }));
    actions.appendChild(actionButton("EXECUTION DETAILS", showTrace));
    wrap.appendChild(actions);
    wrap.appendChild(details);
  }

  // A FAILED or BLOCKED run shows the structured failure the API supplies.
  // The client does not infer root causes. Fields the run does not have are
  // shown as "not recorded"; optional locators are left out when absent.
  function renderFailedResult(run, agents, checkpoints, wrap, status) {
    var f = run.failure || {};

    var head = el("div", "result-head");
    head.appendChild(el("span", "result-icon fail", "✕"));
    head.appendChild(el("span", "result-title fail", "MISSION " + status));
    wrap.appendChild(head);

    var facts = el("div", "result-facts");
    facts.appendChild(fact("Stage", f.stage));
    facts.appendChild(fact("Reason", f.code || run.status_detail, true));
    facts.appendChild(fact("Class", f.failure_class, true));
    facts.appendChild(fact("Category", f.category));
    facts.appendChild(fact("Agent", f.agent_id, true));
    if (f.agent_role) facts.appendChild(fact("Role", f.agent_role, true));
    facts.appendChild(fact("Model", f.model, true));
    facts.appendChild(fact("Provider", f.provider, true));
    if (f.worktree) facts.appendChild(fact("Worktree", String(f.worktree).split(/[\/]/).pop(), true));
    if (f.worker_ordinal !== undefined && f.worker_ordinal !== null) {
      facts.appendChild(fact("Worker", "#" + f.worker_ordinal, true));
    }
    wrap.appendChild(facts);

    wrap.appendChild(el("div", "result-section-label", "Root error"));
    if (f.message) {
      wrap.appendChild(el("pre", "result-output result-excerpt", f.message));
    } else {
      wrap.appendChild(el("div", "result-summary dim", "No root error text was recorded for this run."));
    }

    if (f.recovery_hint) {
      wrap.appendChild(el("div", "result-section-label", "Recovery"));
      wrap.appendChild(el("div", "result-summary", f.recovery_hint));
    }

    if (f.source === "derived") {
      wrap.appendChild(el("div", "result-summary dim",
        "Derived from persisted checkpoints; this run was recorded before structured failures."));
    }

    var actions = el("div", "result-actions");
    actions.appendChild(actionButton("VIEW DETAILS", showTrace));
    actions.appendChild(actionButton("RECOVERY", function () {
      var recovery = document.querySelector('[data-target="overview-recovery"]');
      if (recovery) recovery.click();
    }));
    wrap.appendChild(actions);
  }

  function renderResultPanel(run, agents, checkpoints) {
    var status = runDisplayStatus(run);
    dom.resultContent.innerHTML = "";
    var wrap = el("div", "result-wrap");

    if (status === "COMPLETED") {
      renderCompletedResult(run, agents, checkpoints, wrap);
    } else if (status === "FAILED" || status === "BLOCKED") {
      renderFailedResult(run, agents, checkpoints, wrap, status);
    } else if (status === "RECOVERABLE") {
      wrap.appendChild(el("span", "result-icon pending", "↻"));
      var txt = el("div");
      txt.appendChild(el("div", "result-text", "Execution interrupted; run is available for recovery."));
      if (run.status_detail) txt.appendChild(el("div", "result-summary", run.status_detail));
      wrap.appendChild(txt);
    } else if (status === "RUNNING" || status === "ROUTING") {
      wrap.appendChild(el("span", "result-icon pending", "◇"));
      wrap.appendChild(el("span", "result-text", "Mission in progress…"));
    } else {
      dom.resultContent.innerHTML = '<div class="empty-state"><div class="empty-icon">&#9675;</div>Awaiting completion.</div>';
      return;
    }
    dom.resultContent.appendChild(wrap);
  }

  /* ── Timeline ── */
  var BOUNDARY_LABEL = {
    MISSION_TRIAGE:  "Mission created",
    PLAN_APPROVED:   "Plan approved",
    EXECUTION_START: "Execution started",
    WORKER_ATTEMPT:  "Worker attempt",
    REVIEW:          "Review",
    LIFECYCLE:       "Lifecycle event",
    RUN_TERMINAL:    "Run completed",
  };

  var BOUNDARY_COLOR = {
    MISSION_TRIAGE:  "",
    PLAN_APPROVED:   "boundary-plan",
    EXECUTION_START: "boundary-exec",
    WORKER_ATTEMPT:  "boundary-exec",
    REVIEW:          "boundary-review",
    LIFECYCLE:       "boundary-lifecycle",
    RUN_TERMINAL:    "boundary-terminal",
  };

  function renderTimeline(checkpoints) {
    dom.timeline.innerHTML = "";
    if (!checkpoints || !checkpoints.length) {
      var e = el("div", "empty-state");
      e.innerHTML = '<div class="empty-icon">&#9711;</div>No checkpoints yet.';
      dom.timeline.appendChild(e);
      return;
    }
    checkpoints.forEach(function (cp) {
      dom.timeline.appendChild(renderCheckpoint(cp));
    });
  }

  // Display-only lanes for the trace filters. Nothing here changes data.
  var LANE_OF = {
    MISSION_TRIAGE: "manager", PLAN_APPROVED: "manager", EXECUTION_START: "manager",
    WORKER_ATTEMPT: "workers",
    REVIEW: "review",
  };

  function laneOf(cp) {
    if (LANE_OF[cp.boundary]) return LANE_OF[cp.boundary];
    if (cp.boundary === "LIFECYCLE" && cp.payload && cp.payload.verdict) return "review";
    return "system";
  }

  function renderCheckpoint(cp) {
    var item = el("div", "tl-item");
    item.setAttribute("data-lane", laneOf(cp));
    var bc = BOUNDARY_COLOR[cp.boundary] || "";
    var termOk = cp.payload && (cp.payload.status === "COMPLETED");
    if (bc === "boundary-terminal") bc += termOk ? " ok" : " fail";
    var dot = el("div", "tl-dot " + bc, cp.sequence);
    item.appendChild(dot);

    var body = el("div", "tl-body");
    var header = el("div", "tl-header");

    // Human-friendly label
    var label = BOUNDARY_LABEL[cp.boundary] || cp.boundary;
    if (cp.boundary === "WORKER_ATTEMPT" && cp.worker_ordinal !== null && cp.worker_ordinal !== undefined) {
      label = "Worker #" + cp.worker_ordinal + " attempt";
    } else if (cp.boundary === "LIFECYCLE" && cp.payload && cp.payload.event) {
      label = INTG_LABELS[cp.payload.event] || cp.payload.event;
    } else if (cp.boundary === "RUN_TERMINAL" && cp.payload && cp.payload.status) {
      label = "Run " + cp.payload.status.toLowerCase();
    }

    header.appendChild(el("span", "tl-boundary", label));
    if (cp.created_at) header.appendChild(el("span", "tl-time", fmtTime(cp.created_at)));
    if (cp.worker_ordinal !== null && cp.worker_ordinal !== undefined &&
        cp.boundary !== "WORKER_ATTEMPT") {
      header.appendChild(el("span", "tl-worker", "worker #" + cp.worker_ordinal));
    }
    body.appendChild(header);

    // Payload summary (skip verbose fields)
    var payload = cp.payload || {};
    var keys = Object.keys(payload).filter(function (k) {
      return k !== "repository_evidence" && k !== "approved_plan";
    });
    if (keys.length > 0) {
      var pl = el("div", "tl-payload");
      keys.slice(0, 5).forEach(function (k) {
        var v = payload[k];
        var vs = typeof v === "object" ? JSON.stringify(v) : String(v);
        if (vs.length > 80) vs = vs.slice(0, 80) + "…";
        var row = document.createElement("span");
        var isGood = (k === "status" && v === "COMPLETED") || (k === "verdict" && v === "PASS");
        var isBad  = (k === "status" && (v === "FAILED" || v === "BLOCKED")) ||
                     (k === "verdict" && v && v !== "PASS");
        row.innerHTML = '<span class="key">' + k + ':</span> ' +
          '<span class="val' + (isGood ? " ok" : isBad ? " fail" : "") + '">' + vs + '</span>  ';
        pl.appendChild(row);
      });
      body.appendChild(pl);
    }

    item.appendChild(body);
    return item;
  }

  /* ── Run polling ── */
  function startRunPolling(runId) {
    stopRunPolling();
    pollRunOnce(runId);
    state.agentPollTimer = setInterval(function () { pollRunOnce(runId); }, 3000);
  }

  function stopRunPolling() {
    if (state.agentPollTimer) { clearInterval(state.agentPollTimer); state.agentPollTimer = null; }
  }

  function pollRunOnce(runId) {
    Promise.all([
      fetch_json("/api/operational/runs/" + runId),
      fetch_json("/api/operational/agents?run_id=" + runId),
      fetch_json("/api/runs/" + runId + "/checkpoints"),
    ]).then(function (results) {
      var runData     = results[0].run || {};
      var agents      = results[1].agents || [];
      var checkpoints = results[2].checkpoints || [];

      state.selectedRun = runData;

      // Set before renderPhaseGroups so each agent card's activity section
      // reads this poll's own checkpoints/failure -- not an API call of
      // its own, and never a leftover from a previously viewed Run.
      currentActivityContext = { checkpoints: checkpoints, failure: runData.failure || null };

      updateRunHeader(runData, checkpoints, agents);
      renderPhaseGroups(agents);
      renderMissionProgress(agents, checkpoints);
      updateAgentSummary(agents, checkpoints);
      renderCurrentModels(agents);
      renderTimeline(checkpoints);
      renderReviewPanel(agents, checkpoints);
      renderIntegrationPanel(checkpoints);
      renderResultPanel(runData, agents, checkpoints);
      var displayStatus = runDisplayStatus(runData);
      updateLifecycle(checkpoints, agents, displayStatus);

      var terminal = ["COMPLETED", "FAILED", "BLOCKED", "CANCELLED"];
      if (terminal.indexOf(displayStatus) !== -1 || displayStatus === "RECOVERABLE") {
        stopRunPolling();
        setNexusStatus("idle");
        enableRunBtn();   // BUG1 fix: re-enable RUN button when run reaches terminal state
        // Refresh sidebar without re-auto-opening
        fetch_json("/api/operational/runs").then(function (data) {
          state.runs = data.runs || [];
          renderRunSidebar();
        }).catch(function () {});
      } else {
        renderRunSidebar();
      }
    }).catch(function (err) {
      console.error('[Nexus] pollRunOnce error for run ' + runId + ':', err);
      // Only show error if this run is still the displayed one
      if (state.openRunId === runId) {
        stopRunPolling();
        dom.wsRunStatus.textContent = "Error";
        dom.wsRunStatus.className = "run-hdr-status ast-failed";
        dom.phaseGroups.innerHTML = '<div class="empty-state" style="color:var(--red);padding:24px">Unable to load run details. Check server logs.</div>';
      }
    });
  }

  /* ── Job polling ── */
  function startJobPolling(jobId) {
    state.activeJobId = jobId;
    state.jobStartedAt = Date.now();
    state.pollTimer = setInterval(function () { pollJobOnce(jobId); }, 2000);
    pollJobOnce(jobId);
  }

  function stopJobPolling() {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  }

  function pollJobOnce(jobId) {
    fetch_json("/api/go/jobs/" + jobId).then(function (data) {
      var job = data.job || {};
      var runId = job.run_id;

      if (runId && runId !== state.lastRunId) {
        state.lastRunId = runId;
        stopJobPolling();
        dom.runMsg.textContent = "Run launched: " + runId;
        dom.runMsg.className = "msg-line";
        openRun(runId);
        // Refresh run list without triggering another auto-open
        fetch_json("/api/operational/runs").then(function (d) {
          state.runs = d.runs || [];
          renderRunSidebar();
        }).catch(function () {});
      }

      if (!runId && (job.status === "running" || !job.status)) {
        var waitSec = state.jobStartedAt ? Math.round((Date.now() - state.jobStartedAt) / 1000) : 0;
        dom.runMsg.textContent = "Dispatched \u2013 waiting for run\u2026 (" + waitSec + "s)";
        dom.runMsg.className = "msg-line";
      }

      if (job.status === "failed" && !runId) {
        stopJobPolling();
        setNexusStatus("error");
        enableRunBtn();
        dom.runMsg.textContent = "Error: " + (job.error_code || "") + " – " + (job.error || "Unknown error");
        dom.runMsg.className = "msg-line msg-error";
      }

      if (job.status === "completed" && !runId) {
        stopJobPolling();
        setNexusStatus("idle");
        enableRunBtn();
        fetch_json("/api/operational/runs").then(function (d) {
          state.runs = d.runs || [];
          renderRunSidebar();
        }).catch(function () {});
      }
    }).catch(function () {});
  }

  /* ── Mission submit ── */
  function disableRunBtn() {
    dom.runBtn.disabled = true;
    dom.runBtn.className = "run-btn running";
    dom.runBtn.textContent = "◈ RUNNING…";
  }

  function enableRunBtn() {
    if (dom.runBtn.dataset.runtimeDisabled === "true") {
      dom.runBtn.disabled = true;
      dom.runBtn.className = "run-btn";
      dom.runBtn.textContent = "▶ RUN MISSION";
      return;
    }
    dom.runBtn.disabled = false;
    dom.runBtn.className = "run-btn";
    dom.runBtn.textContent = "▶ RUN MISSION";
  }

  function submitMission() {
    if (dom.runBtn.dataset.runtimeDisabled === "true") {
      dom.runMsg.textContent = "Unavailable: no callable provider route.";
      dom.runMsg.className = "msg-line msg-error";
      return;
    }
    var mission = dom.missionInput.value.trim();
    if (!mission) {
      dom.runMsg.textContent = "Enter a mission description first.";
      dom.runMsg.className = "msg-line msg-error";
      return;
    }

    var projectId = dom.projectSelect.value || null;
    if (!projectId && state.selectedProject) projectId = state.selectedProject.id;

    var managerModel = (dom.managerModelSelect && dom.managerModelSelect.value) || null;
    var prepareDependencies = !!(dom.prepareDepsCheck && dom.prepareDepsCheck.checked);

    disableRunBtn();
    setNexusStatus("active");
    dom.runMsg.textContent = "Submitting mission…";
    dom.runMsg.className = "msg-line";

    fetch("/api/go", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mission: mission,
        project_id: projectId,
        manager_model: managerModel,
        prepare_dependencies: prepareDependencies,
      }),
    })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.job_id) {
        dom.runMsg.textContent = "Dispatched – waiting for run…";
        startJobPolling(data.job_id);
      } else {
        throw new Error(data.detail || "No job_id returned");
      }
    })
    .catch(function (err) {
      setNexusStatus("error");
      enableRunBtn();
      dom.runMsg.textContent = "Failed: " + err.message;
      dom.runMsg.className = "msg-line msg-error";
    });
  }

  /* ── Event wiring ── */
  dom.projectSelect.addEventListener("change", function () {
    var id = dom.projectSelect.value;
    if (!id) return;
    var p = state.projects.find(function (p) { return p.id === id; });
    if (p) selectProject(p);
  });

  dom.runBtn.addEventListener("click", submitMission);
  dom.missionInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submitMission();
  });
  if (dom.managerModelFilter) {
    dom.managerModelFilter.addEventListener("input", renderManagerModelSelect);
  }
  if (dom.managerModelSelect) {
    dom.managerModelSelect.addEventListener("change", updateManagerModelReason);
  }

  /* ── Public bridge for programs.js (Program ⇄ Run cross-link / mode) ── */
  window.NexusRun = {
    openRun: openRun,
    deselectRun: deselectRun,
    showOverview: showOverview,
    renderPhaseGroups: renderPhaseGroups,
  };
  window.NexusMode.tryDecideInitialFocus = tryDecideInitialFocus;

  /* ── Trace filters (display only) ── */
  (function bindTraceFilters() {
    var bar = document.querySelector(".trace-filters");
    if (!bar) return;
    bar.addEventListener("click", function (event) {
      var button = event.target.closest("[data-trace-filter]");
      if (!button) return;
      dom.timeline.setAttribute("data-filter", button.getAttribute("data-trace-filter"));
      bar.querySelectorAll("[data-trace-filter]").forEach(function (b) {
        var on = b === button;
        b.classList.toggle("active", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
    });
  })();

  /* ── Bootstrap ── */
  loadProjects();
  loadManagerModels();
  loadRuns();
  setInterval(function () {
    fetch_json("/api/operational/runs").then(function (d) {
      state.runs = d.runs || [];
      renderRunSidebar();
      autoOpenBestRun(); // no-op once state.hasAutoOpened is true
    }).catch(function () {});
  }, 5000);

})();
