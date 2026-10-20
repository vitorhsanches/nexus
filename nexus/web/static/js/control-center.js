/* Nexus Control Center — read-only operational overview and safe navigation. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var state = {
    programs: [],
    runs: [],
    agents: [],
    routing: null,
    featuredProgram: null,
    programDetail: null,
    featuredRun: null,
    checkpoints: [],
    recoveryCheckpoints: [],
    availability: { programs: false, runs: false, agents: false },
    follow: true,
    cleared: false,
  };

  function fetchJSON(url) {
    return fetch(url).then(function (response) {
      if (!response.ok) throw new Error(url + " returned " + response.status);
      return response.json();
    });
  }

  function text(id, value) {
    var node = $(id);
    if (node) node.textContent = value === null || value === undefined ? "—" : String(value);
  }

  function node(tag, className, value) {
    var item = document.createElement(tag);
    if (className) item.className = className;
    if (value !== undefined && value !== null) item.textContent = String(value);
    return item;
  }

  function normalized(value) { return String(value || "").trim().toUpperCase(); }

  function parseTime(value) {
    if (!value) return 0;
    var source = String(value);
    if (!/[Zz]$/.test(source) && !/[+-]\d{2}:?\d{2}$/.test(source)) {
      source = source.replace(" ", "T") + "Z";
    }
    var parsed = new Date(source).getTime();
    return isNaN(parsed) ? 0 : parsed;
  }

  function clock(value) {
    var time = parseTime(value);
    if (!time) return "--:--:--";
    return new Date(time).toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  }

  var TERMINAL_PROGRAM_STATUSES = ["COMPLETED", "BLOCKED", "FAILED"];

  function programActivityTime(program) {
    return parseTime(program.updated_at) || parseTime(program.created_at);
  }

  function newestProgram(programs) {
    var active = programs.filter(function (program) {
      return TERMINAL_PROGRAM_STATUSES.indexOf(normalized(program.status)) === -1;
    });
    var pool = active.length ? active : programs;
    return pool.reduce(function (best, program) {
      if (!best) return program;
      return programActivityTime(program) > programActivityTime(best) ? program : best;
    }, null);
  }

  function mergeRunCheckpoints(entries) {
    var merged = [];
    entries.forEach(function (entry) {
      merged = merged.concat(entry.checkpoints || []);
    });
    return merged.sort(function (a, b) {
      return parseTime(a.created_at) - parseTime(b.created_at);
    });
  }

  function newestRun(runs) {
    var active = ["RUNNING", "ROUTING", "REVIEWING", "CREATED"];
    for (var i = 0; i < active.length; i += 1) {
      var match = runs.find(function (run) {
        return normalized(run.display_status || run.status) === active[i];
      });
      if (match) return match;
    }
    return runs[0] || null;
  }

  // F2.4B: loadOverview()/loadFeaturedDetails()/loadRouting() used to poll
  // unconditionally every 6-20s regardless of which view was active --
  // including a burst of up to 40 PARALLEL checkpoint fetches
  // (loadFeaturedDetails below). A live smoke test found that running this
  // flood continuously while Development Mode held one browser EventSource
  // connection open led to that SSE stream silently dying (net::ERR_ABORTED)
  // and never reconnecting; a bounded server-side reproduction (direct,
  // non-browser concurrent load against the same endpoints) completed
  // cleanly, isolating the cause to the browser's own connection-queue
  // behavior rather than a backend/thread-pool problem. Pausing this
  // polling while Overview/the Run-or-Program detail view isn't the one
  // on screen removes the flood in the one case that actually matters
  // (Development Mode's long-lived SSE connection) without removing any
  // dashboard functionality: Overview still refreshes immediately the
  // moment the user returns to it (see chat.js/dev.js's exit()).
  function overviewPollingSuspended() {
    var view = document.body.dataset.view;
    return view === "chat" || view === "dev";
  }

  function loadOverview() {
    if (overviewPollingSuspended()) return;
    var requests = [
      ["programs", fetchJSON("/api/programs")],
      ["runs", fetchJSON("/api/operational/runs")],
      ["agents", fetchJSON("/api/operational/agents")],
    ].map(function (entry) {
      return entry[1].then(
        function (payload) { return { key: entry[0], ok: true, payload: payload }; },
        function () { return { key: entry[0], ok: false, payload: {} }; }
      );
    });

    Promise.all(requests).then(function (results) {
      results.forEach(function (result) {
        state.availability[result.key] = result.ok;
        if (result.key === "programs") state.programs = result.ok ? (result.payload.programs || []) : [];
        if (result.key === "runs") state.runs = result.ok ? (result.payload.runs || []) : [];
        if (result.key === "agents") state.agents = result.ok ? (result.payload.agents || []) : [];
      });
      state.featuredProgram = newestProgram(state.programs);
      state.featuredRun = newestRun(state.runs);
      renderKpis();
      loadFeaturedDetails();
    });
  }

  // Runs up to `limit` of `items`' own async jobs at once instead of firing
  // all of them in one Promise.all -- a browser caps concurrent HTTP/1.1
  // connections per origin (Chrome: 6), so an unbounded burst (this
  // endpoint used to fire up to 40 at once) queues behind, and can starve,
  // any OTHER connection to the same origin, including Development Mode's
  // own long-lived SSE stream (see F2.4B's overviewPollingSuspended()
  // above for the full story). Preserves the exact same results in the
  // exact same order as Promise.all(items.map(job)) -- only the fan-out
  // width changes, not the data.
  function mapWithConcurrency(items, limit, job) {
    var results = new Array(items.length);
    var nextIndex = 0;
    function runNext() {
      var index = nextIndex++;
      if (index >= items.length) return Promise.resolve();
      return job(items[index], index).then(function (value) {
        results[index] = value;
        return runNext();
      });
    }
    var workers = [];
    for (var i = 0; i < Math.min(limit, items.length); i += 1) workers.push(runNext());
    return Promise.all(workers).then(function () { return results; });
  }

  function loadFeaturedDetails() {
    var programRequest = state.featuredProgram
      ? fetchJSON("/api/programs/" + encodeURIComponent(state.featuredProgram.program_id))
      : Promise.resolve({ program: null });
    // Prefer the newest run that actually has durable events. A newly-created
    // empty run must not hide useful recent execution history from the anchor.
    var runCandidates = state.runs.slice(0, 40);
    var checkpointRequest = runCandidates.length
      ? mapWithConcurrency(runCandidates, 6, function (run) {
          return fetchJSON("/api/runs/" + encodeURIComponent(run.id) + "/checkpoints")
            .then(function (payload) { return { run: run, checkpoints: payload.checkpoints || [] }; })
            .catch(function () { return { run: run, checkpoints: [] }; });
        }).then(function (results) {
          var featured = results.find(function (entry) { return entry.checkpoints.length; }) ||
            results[0] || { run: null, checkpoints: [] };
          return { run: featured.run, checkpoints: featured.checkpoints, all: mergeRunCheckpoints(results) };
        })
      : Promise.resolve({ run: null, checkpoints: [], all: [] });

    Promise.all([programRequest, checkpointRequest]).then(function (results) {
      state.programDetail = results[0].program || null;
      state.featuredRun = results[1].run || state.featuredRun;
      state.checkpoints = results[1].checkpoints || [];
      state.recoveryCheckpoints = results[1].all || [];
      renderProgram();
      renderTerminal();
      renderRecovery();
      renderActivity();
    }).catch(function () {
      state.programDetail = null;
      state.checkpoints = [];
      state.recoveryCheckpoints = [];
      renderProgram();
      renderTerminal();
      renderRecovery();
      renderActivity();
    });
  }

  function loadRouting() {
    if (overviewPollingSuspended()) return;
    fetchJSON("/api/routing/status").then(function (payload) {
      state.routing = payload;
      renderRuntime();
      renderRouting();
      gateActions();
      renderKpis();
    }).catch(function () {
      state.routing = null;
      renderRuntime();
      renderRouting();
      gateActions();
      renderKpis();
    });
  }

  function renderKpis() {
    var activePrograms = state.programs.filter(function (p) {
      return normalized(p.status) === "RUNNING";
    }).length;
    var runningTasks = state.agents.filter(function (a) {
      return a.role === "Worker" && normalized(a.status) === "RUNNING";
    }).length;
    var waitingReview = state.agents.filter(function (a) {
      var status = normalized(a.status);
      return a.role === "ManagerReview" && ["QUEUED", "WAITING", "REVIEWING"].indexOf(status) !== -1;
    }).length;
    var blockedPrograms = state.programs.filter(function (p) {
      return normalized(p.status) === "BLOCKED";
    }).length;
    var blockedRuns = state.runs.filter(function (r) {
      return normalized(r.display_status || r.status) === "BLOCKED";
    }).length;
    var providerNames = {};
    if (state.routing) {
      (state.routing.routes || []).forEach(function (route) {
        if (route.callable) providerNames[route.provider] = true;
      });
    }
    text("kpi-programs", state.availability.programs ? activePrograms : "UNKNOWN");
    text("kpi-tasks", state.availability.agents ? runningTasks : "UNKNOWN");
    text("kpi-review", state.availability.agents ? waitingReview : "UNKNOWN");
    text("kpi-blocked", state.availability.programs && state.availability.runs
      ? blockedPrograms + blockedRuns
      : "UNKNOWN");
    text("kpi-providers", state.routing ? Object.keys(providerNames).length : "UNKNOWN");
  }

  function renderProgram() {
    var summary = state.featuredProgram;
    var detail = state.programDetail;
    var openButton = $("overview-open-program");
    var phaseContainer = $("overview-program-phases");
    phaseContainer.innerHTML = "";
    if (!state.availability.programs) {
      text("overview-program-name", "Program data unavailable");
      text("overview-program-meta", "The Programs endpoint could not be reached.");
      $("overview-program-progress").style.width = "0%";
      phaseContainer.appendChild(node("div", "ops-empty", "Program state is unknown."));
      openButton.disabled = true;
      openButton.removeAttribute("data-program-id");
      return;
    }
    if (!summary || !detail) {
      text("overview-program-name", "No active program");
      text("overview-program-meta", "No durable Program state is available.");
      $("overview-program-progress").style.width = "0%";
      phaseContainer.appendChild(node("div", "ops-empty", "No phase data."));
      openButton.disabled = true;
      openButton.removeAttribute("data-program-id");
      return;
    }

    text("overview-program-name", summary.name || summary.program_id);
    text("overview-program-meta",
      summary.program_id + "  ·  " + normalized(summary.status) + "  ·  " +
      (summary.project_id || "NO PROJECT") + "  ·  " +
      (summary.current_phase ? "PHASE " + summary.current_phase + "/" + summary.phase_count : "NO ACTIVE PHASE"));
    openButton.disabled = false;
    openButton.setAttribute("data-program-id", summary.program_id);

    var phases = (detail.phases || []).slice();
    if (detail.final) phases.push({
      index: phases.length + 1,
      title: detail.final.title || "Final verification",
      status: detail.final.status,
      attempts: detail.final.attempts,
    });
    var completed = phases.filter(function (phase) {
      return ["DONE", "COMPLETED", "PASSED"].indexOf(normalized(phase.status)) !== -1;
    }).length;
    $("overview-program-progress").style.width = (phases.length ? Math.round(completed / phases.length * 100) : 0) + "%";

    if (!phases.length) {
      phaseContainer.appendChild(node("div", "ops-empty", "No phase data."));
      return;
    }
    phases.forEach(function (phase) {
      var status = normalized(phase.status) || "PENDING";
      var step = node("div", "phase-step " + status.toLowerCase());
      step.appendChild(node("div", "phase-index", String(phase.index || "—").padStart(2, "0")));
      step.appendChild(node("div", "phase-title", phase.title || "Untitled phase"));
      step.appendChild(node("div", "phase-status", status + " · " + (phase.attempts || 0) + " ATT"));
      phaseContainer.appendChild(step);
    });
  }

  function stateKind(value) {
    var status = normalized(value);
    if (["ONLINE", "HEALTHY", "AVAILABLE", "YES"].indexOf(status) !== -1) return "good";
    if (["AUTH REQUIRED", "UNKNOWN", "CONFIGURED", "DEGRADED"].indexOf(status) !== -1) return "warn";
    return "bad";
  }

  function runtimeValue(id, value) {
    text(id, value);
    var item = $(id);
    if (item) item.setAttribute("data-state", stateKind(value));
    // The run view's System block mirrors the same values (data-mirror), so
    // runtime state is fetched once and shown in both places.
    document.querySelectorAll('[data-mirror="' + id + '"]').forEach(function (mirror) {
      mirror.textContent = value === null || value === undefined ? "—" : String(value);
      mirror.setAttribute("data-state", stateKind(value));
    });
  }

  function renderRuntime() {
    var runtime = state.routing && state.routing.runtime;
    var badge = $("overview-runtime-badge");
    if (!runtime) {
      runtimeValue("runtime-omniroute", "UNKNOWN");
      runtimeValue("runtime-credentials", "UNKNOWN");
      runtimeValue("runtime-providers", "UNKNOWN");
      runtimeValue("runtime-routes", "UNKNOWN");
      badge.textContent = "RUNTIME UNKNOWN";
      badge.className = "ops-badge neutral";
      return;
    }
    runtimeValue("runtime-omniroute", runtime.status);
    runtimeValue("runtime-credentials", runtime.credential_store);
    runtimeValue("runtime-providers", runtime.providers);
    runtimeValue("runtime-routes", state.routing.summary.callable_routes);
    badge.textContent = "RUNTIME " + runtime.status;
    badge.className = "ops-badge " + (runtime.status === "ONLINE" ? "online" : runtime.status === "DEGRADED" ? "degraded" : "offline");
  }

  function cssToken(value) {
    return String(value || "unknown").toLowerCase().replace(/[^a-z0-9]+/g, "-");
  }

  function renderRouting() {
    var body = $("routing-rows");
    body.innerHTML = "";
    if (!state.routing) {
      var unavailable = node("tr");
      var cell = node("td", "ops-empty", "Routing telemetry unavailable.");
      cell.colSpan = 4;
      unavailable.appendChild(cell);
      body.appendChild(unavailable);
      text("routing-count", "UNKNOWN");
      return;
    }
    var routes = state.routing.routes || [];
    text("routing-count", state.routing.summary.callable_routes + " CALLABLE");
    text("routing-policy", (state.routing.policy || []).join(" → "));
    routes.forEach(function (route) {
      var row = node("tr");
      var identity = node("td");
      identity.appendChild(node("span", "route-provider", route.provider + " · " + route.effort + " · " + route.cost_class));
      var model = node("span", "route-model", route.model);
      model.title = route.model;
      identity.appendChild(model);
      row.appendChild(identity);
      row.appendChild(node("td", "route-capability", (route.capabilities || []).join(", ") || "—"));
      row.appendChild(node("td", "route-state " + cssToken(route.health), route.health));
      row.appendChild(node("td", "route-state " + cssToken(route.eligibility), route.eligibility));
      body.appendChild(row);
    });
    if (!routes.length) {
      var emptyRow = node("tr");
      var emptyCell = node("td", "ops-empty", "No approved routes exposed.");
      emptyCell.colSpan = 4;
      emptyRow.appendChild(emptyCell);
      body.appendChild(emptyRow);
    }
  }

  var EVENT_LABELS = {
    MISSION_TRIAGE: "classify_request",
    PLAN_APPROVED: "plan.approved",
    EXECUTION_START: "route_selected",
    WORKER_ATTEMPT: "worker.attempt",
    REVIEW: "review.status",
    LIFECYCLE: "lifecycle",
    SCOUT_STAGE_STARTED: "scout.start",
    SCOUT_STAGE_COMPLETED: "scout.complete",
    INTEGRATION_STAGE_CREATED: "integration.create",
    INTEGRATION_ROUND_START: "integration.round",
    INTEGRATION_ROUND_COMPLETED: "integration.complete",
    INTEGRATION_WORKER_COMMITTED: "worker.commit",
    INTEGRATION_WORKER_APPLIED: "worker.apply",
    INTEGRATION_FINALIZE_START: "integration.finalize",
    INTEGRATION_COMPLETED: "integration.completed",
    RUN_TELEMETRY_SUMMARY: "telemetry.summary",
    LIFECYCLE_ORPHAN_DETECTED: "recovery.orphaned",
    RESUME_START: "recovery.resume",
    RUN_TERMINAL: "run.terminal",
  };

  function eventDetail(checkpoint) {
    var payload = checkpoint.payload || {};
    var fields = [];
    ["status", "verdict", "decision", "model", "provider", "failure_class", "reason", "route_class"].forEach(function (key) {
      if (payload[key] !== undefined && payload[key] !== null && payload[key] !== "") {
        fields.push(key + "=" + String(payload[key]).slice(0, 120));
      }
    });
    if (checkpoint.worker_ordinal !== null && checkpoint.worker_ordinal !== undefined) {
      fields.unshift("worker=" + checkpoint.worker_ordinal);
    }
    return fields.join(" · ") || "sequence=" + checkpoint.sequence;
  }

  function eventLevel(checkpoint) {
    var content = normalized([
      checkpoint.boundary,
      (checkpoint.payload || {}).status,
      (checkpoint.payload || {}).verdict,
      (checkpoint.payload || {}).decision,
      (checkpoint.payload || {}).failure_class,
    ].join(" "));
    if (/FAILED|BLOCKED|EXHAUSTED|ERROR/.test(content)) return "error";
    if (/RETRY|FALLBACK|REVIEW|ORPHAN|RESUME|WAIT/.test(content)) return "warn";
    if (/COMPLETED|PASS|SUCCESS/.test(content)) return "success";
    return "info";
  }

  function renderTerminal() {
    var output = $("terminal-output");
    output.innerHTML = "";
    text("terminal-run-id", state.featuredRun ? state.featuredRun.id : "NO RUN SELECTED");
    text("terminal-event-count", state.checkpoints.length + " EVENTS");
    if (state.cleared) {
      output.appendChild(node("div", "terminal-empty", "View cleared. Persisted events are unchanged."));
      return;
    }
    if (!state.checkpoints.length) {
      output.appendChild(node("div", "terminal-empty", "No persisted execution events."));
      return;
    }
    state.checkpoints.slice(-120).forEach(function (checkpoint) {
      var level = eventLevel(checkpoint);
      var line = node("div", "terminal-line");
      line.appendChild(node("span", "terminal-time", clock(checkpoint.created_at)));
      line.appendChild(node("span", "terminal-level " + (level === "info" || level === "success" ? "" : level), level.toUpperCase()));
      line.appendChild(node("span", "terminal-event", EVENT_LABELS[checkpoint.boundary] || String(checkpoint.boundary || "event").toLowerCase().replace(/_/g, ".")));
      line.appendChild(node("span", "terminal-detail", eventDetail(checkpoint)));
      output.appendChild(line);
    });
    if (state.follow) output.scrollTop = output.scrollHeight;
  }

  function isRecovery(checkpoint) {
    var payload = checkpoint.payload || {};
    var signal = normalized([checkpoint.boundary, payload.decision, payload.verdict, payload.status, payload.failure_class].join(" "));
    return /FAIL|BLOCK|RETRY|FALLBACK|ORPHAN|RESUME|RECOVER|RATE.LIMIT/.test(signal);
  }

  function appendSignal(container, item) {
    var row = node("div", "signal-item " + item.level);
    row.appendChild(node("span", "signal-time", clock(item.at)));
    var content = node("span", "signal-title");
    content.appendChild(node("strong", "", item.title));
    content.appendChild(document.createTextNode(item.detail || ""));
    row.appendChild(content);
    container.appendChild(row);
  }

  function renderRecovery() {
    var container = $("recovery-list");
    container.innerHTML = "";
    var items = state.recoveryCheckpoints.filter(isRecovery).slice(-12).map(function (checkpoint) {
      return {
        at: checkpoint.created_at,
        title: EVENT_LABELS[checkpoint.boundary] || checkpoint.boundary,
        detail: eventDetail(checkpoint),
        level: eventLevel(checkpoint),
      };
    });
    if (state.programDetail) {
      (state.programDetail.events || []).filter(function (event) {
        return /FAIL|BLOCK|RETRY|FALLBACK|RESUME|RECOVER/.test(normalized(event.kind));
      }).slice(-12).forEach(function (event) {
        var detail = event.failure_class || event.reason || event.error || state.programDetail.program_id;
        items.push({
          at: event.at,
          title: String(event.kind || "program.signal").toLowerCase().replace(/_/g, "."),
          detail: String(detail).slice(0, 180),
          level: /FAIL|BLOCK|ERROR/.test(normalized(event.kind)) ? "error" : "warn",
        });
      });
      (state.programDetail.phases || []).filter(function (phase) {
        return !!phase.last_failure_class;
      }).forEach(function (phase) {
        items.push({
          at: state.programDetail.updated_at,
          title: "phase." + phase.index + ".failure",
          detail: phase.last_failure_class + (phase.last_error ? " · " + String(phase.last_error).slice(0, 140) : ""),
          level: "error",
        });
      });
    }
    items.sort(function (a, b) { return parseTime(b.at) - parseTime(a.at); });
    items = items.slice(0, 12);
    if (!items.length) {
      container.appendChild(node("div", "ops-empty", "No failure or recovery signals recorded."));
      return;
    }
    items.forEach(function (item) { appendSignal(container, item); });
  }

  function renderActivity() {
    var container = $("activity-list");
    container.innerHTML = "";
    var items = state.recoveryCheckpoints.slice(-10).map(function (checkpoint) {
      return {
        at: checkpoint.created_at,
        title: EVENT_LABELS[checkpoint.boundary] || checkpoint.boundary,
        detail: eventDetail(checkpoint),
        level: eventLevel(checkpoint),
      };
    });
    if (state.programDetail) {
      (state.programDetail.events || []).slice(-8).forEach(function (event) {
        items.push({
          at: event.at,
          title: String(event.kind || "program.event").toLowerCase().replace(/_/g, "."),
          detail: state.programDetail.program_id,
          level: /FAIL|BLOCK|ERROR/.test(normalized(event.kind)) ? "error" : "info",
        });
      });
    }
    items.sort(function (a, b) { return parseTime(b.at) - parseTime(a.at); });
    if (!items.length) {
      container.appendChild(node("div", "ops-empty", "No recent activity recorded."));
      return;
    }
    items.slice(0, 12).forEach(function (item) { appendSignal(container, item); });
  }

  function gateActions() {
    var runButton = $("run-btn");
    var reason = $("run-disabled-reason");
    var available = !!(state.routing && state.routing.summary && state.routing.summary.actions_available);
    runButton.dataset.runtimeDisabled = available ? "false" : "true";
    runButton.disabled = !available;
    reason.textContent = available ? "" : "Disabled: no callable provider route is currently observed.";
  }

  function showOverview(targetId) {
    if (window.NexusRun && window.NexusRun.showOverview) window.NexusRun.showOverview();
    document.querySelectorAll(".nav-item").forEach(function (item) {
      item.classList.toggle("active", item.getAttribute("data-target") === (targetId || "overview-dashboard"));
    });
    var target = $(targetId || "overview-dashboard");
    if (target) target.scrollIntoView({ block: "start" });
  }

  function runCommand(raw) {
    var command = String(raw || "").trim();
    var lower = command.toLowerCase();
    var feedback = $("command-feedback");
    if (!command || lower === "overview") {
      showOverview("overview-dashboard");
      feedback.textContent = "Overview opened";
      return;
    }
    if (lower === "models" || lower === "routing" || lower === "models & routing") {
      showOverview("overview-routing");
      feedback.textContent = "Routing opened";
      return;
    }
    if (lower === "recovery") {
      showOverview("overview-recovery");
      feedback.textContent = "Recovery opened";
      return;
    }
    if (lower === "projects") {
      showOverview("sidebar-projects");
      feedback.textContent = "Projects focused";
      return;
    }
    var program = command.match(/^open\s+program\s+(.+)$/i);
    if (program && window.NexusProgram) {
      window.NexusProgram.selectProgram(program[1].trim());
      feedback.textContent = "Program detail opened";
      return;
    }
    var execution = command.match(/^(?:view|open)\s+execution\s+(.+)$/i);
    if (execution && window.NexusRun) {
      window.NexusRun.openRun(execution[1].trim());
      feedback.textContent = "Execution detail opened";
      return;
    }
    feedback.textContent = "Navigation command not recognized";
  }

  document.querySelectorAll(".nav-item").forEach(function (item) {
    item.addEventListener("click", function () {
      var target = item.getAttribute("data-target");
      if (target === "chat-view" && window.NexusChat) {
        if (window.NexusDev) window.NexusDev.exit();
        window.NexusChat.enter();
        return;
      }
      if (target === "dev-view" && window.NexusDev) {
        if (window.NexusChat) window.NexusChat.exit();
        window.NexusDev.enter();
        return;
      }
      if (window.NexusChat) window.NexusChat.exit();
      if (window.NexusDev) window.NexusDev.exit();
      showOverview(target);
      // Resume Overview's own polling with an immediate refresh rather
      // than waiting up to 6-20s for the next interval tick -- it was
      // paused (see overviewPollingSuspended()) the whole time Chat/Dev
      // was open, so returning here is exactly the case that must not
      // show stale data.
      if (!overviewPollingSuspended()) { loadOverview(); loadRouting(); }
    });
  });

  $("overview-open-program").addEventListener("click", function () {
    var id = $("overview-open-program").getAttribute("data-program-id");
    if (id && window.NexusProgram) window.NexusProgram.selectProgram(id);
  });

  $("terminal-follow").addEventListener("click", function () {
    state.follow = !state.follow;
    if (state.follow && state.cleared) {
      state.cleared = false;
      renderTerminal();
    }
    $("terminal-follow").classList.toggle("active", state.follow);
    $("terminal-follow").setAttribute("aria-pressed", state.follow ? "true" : "false");
    if (state.follow) $("terminal-output").scrollTop = $("terminal-output").scrollHeight;
  });

  $("terminal-clear").addEventListener("click", function () {
    state.cleared = true;
    renderTerminal();
  });

  $("command-palette").addEventListener("submit", function (event) {
    event.preventDefault();
    runCommand($("nexus-command-input").value);
  });

  window.NexusControlCenter = { refresh: loadOverview, showOverview: showOverview };
  loadOverview();
  loadRouting();
  setInterval(loadOverview, 6000);
  setInterval(loadRouting, 20000);
})();
