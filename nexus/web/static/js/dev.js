/* Nexus Development Mode Cockpit client (F2.2 Phase 4).
 *
 * A separate area from Chat -- its own nav item, its own view, its own
 * API surface (/api/dev/...). Never touches chat.js's DOM or state.
 * Write-capable execution is only ever offered if the backend's own
 * feature gate (nexus/dev/feature_gate.py) reports itself enabled;
 * otherwise the Run control stays visibly disabled with the reason
 * shown, never hidden or silently clickable.
 */
(function () {
  "use strict";

  var API = "/api";
  var $ = function (id) { return document.getElementById(id); };

  // Fallback reconciliation poll (F2.4B): how often to re-check the
  // backend's own turn status while the SSE stream is reporting itself
  // disconnected, and for how long before giving up and leaving
  // "Connection lost" on screen rather than polling forever. Never used
  // to start or retry a turn -- strictly read-only GETs of already
  // backend-owned state.
  var RECONCILE_POLL_INTERVAL_MS = 4000;
  var RECONCILE_POLL_MAX_ATTEMPTS = 60; // ~4 minutes, comfortably past the backend's own turn timeout

  var state = {
    previousMode: null, previousView: null, previousNavId: null, previousFocus: null,
    allProjects: [], projects: [], sessions: [], session: null,
    modelOptions: [], defaultModel: null, testProfiles: [],
    selectedModel: null, selectedEffort: null,
    newSessionBackend: "codex_native", modelsBackend: null, approvals: [],
    executionEnabled: false, executionStatusChecked: false,
    review: null, selectedReviewPath: null, reviewLoading: false,
    integrationEnabled: false, integrationReview: null, integrationPreflight: null, integrationBusy: false,
    integrationReviewOutdated: false, integrationReviewUncertain: false, integrationHistoryIds: [],
    activeTurnId: null, source: null, sending: false,
    mission: null, missionPollTimer: null,
    activityStartedAt: null, elapsedTimer: null,
    // Connection state is deliberately tracked separately from execution
    // state (Running/Completed/Failed/...): a dropped SSE stream must
    // never be visually presented as a Failed turn -- only a real
    // backend-confirmed status may do that. One of "idle" (no stream
    // expected right now), "connected", "reconnecting" (EventSource is
    // retrying on its own) or "lost" (gave up; the fallback poll below is
    // now the only thing still watching this turn).
    connectionState: "idle",
    reconcilePoll: { timer: null, attempts: 0, turnId: null },
  };

  function setViewBadge(mode) {
    var badge = $("view-mode-badge");
    if (!badge) return;
    badge.textContent = mode === "dev" ? "DEVELOPMENT" : mode === "chat" ? "CHAT" : mode === "program" ? "PROGRAM VIEW" : mode === "run" ? "RUN VIEW" : "OVERVIEW";
    badge.className = "view-mode-badge vm-" + (mode || "none");
  }

  function setActiveNavigation(targetId) {
    document.querySelectorAll(".nav-item").forEach(function (item) {
      var active = item.id === targetId;
      item.classList.toggle("active", active);
      if (active) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current");
    });
  }

  function request(path, options) {
    return fetch(API + path, options).then(function (response) {
      return response.text().then(function (body) {
        var payload = null;
        try { payload = body ? JSON.parse(body) : null; } catch (_error) { payload = null; }
        if (!response.ok) {
          var detail = payload && payload.detail;
          var message = detail && typeof detail === "object" ? detail.message : detail;
          var error = new Error(typeof message === "string" ? message : "Request failed (HTTP " + response.status + ").");
          error.status = response.status;
          error.code = detail && typeof detail === "object" ? detail.code : "HTTP_ERROR";
          throw error;
        }
        return payload;
      });
    });
  }

  // Dev turn prompts/results flow through the same AppServerClient as
  // Chat and can echo back whatever a user pasted; apply the identical,
  // conservative key=value / Bearer / PEM / token redaction chat.js uses
  // before anything from a turn is ever shown. chat.js does not export
  // its own redactor, so this is the same pattern, kept in sync by hand
  // rather than coupled to chat.js's internals.
  var SENSITIVE_KEY_PATTERN = "password|passwd|pwd|credentials?|authorization|" +
    "access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|bearer[_-]?token|" +
    "session[_-]?token|csrf[_-]?token|oauth[_-]?token|token|" +
    "client[_-]?secret|secret[_-]?key|secret|api[_-]?key|api[_-]?secret|private[_-]?key";
  var KEY_VALUE_PATTERN = new RegExp(
    "(^|[^A-Za-z0-9_-])(" + SENSITIVE_KEY_PATTERN + ")(?![A-Za-z0-9_-])" +
    "([\"']?\\s*[:=]\\s*[\"']?)([^\"'\\s,;}]+)",
    "gi"
  );

  function redacted(value) {
    var str = String(value == null ? "" : value);
    str = str.replace(/\b(Bearer\s+)[A-Za-z0-9._~+\/-]+=*/gi, "$1[redacted]");
    str = str.replace(KEY_VALUE_PATTERN, function (full, lead, key, separator, val) {
      if (/^bearer$/i.test(val) || /^\[redacted[^\]]*\]$/i.test(val)) return full;
      return lead + key + separator + "[redacted]";
    });
    str = str.replace(/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g, "[redacted private key]");
    str = str.replace(/\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b/g, "[redacted token]");
    return str;
  }

  function showStatus(message, kind) {
    var status = $("dev-status-text");
    var banner = $("dev-backend-status");
    if (status) status.textContent = redacted(message);
    if (banner) banner.dataset.state = kind || "info";
  }

  function enter() {
    if (document.body.dataset.view !== "dev") {
      state.previousView = document.body.dataset.view || "overview";
      state.previousMode = (window.NexusMode && window.NexusMode.mode) || "overview";
      var activeNav = document.querySelector(".nav-item.active");
      state.previousNavId = activeNav ? activeNav.id : null;
      state.previousFocus = document.activeElement || null;
    }
    document.body.dataset.view = "dev";
    document.body.setAttribute("data-view", "dev");
    setViewBadge("dev");
    setActiveNavigation("nav-dev");
    var devReturn = $("dev-return");
    if (devReturn) devReturn.hidden = state.previousMode === "overview";
    var devView = $("ws-dev");
    if (devView) devView.setAttribute("aria-hidden", "false");
    var gatePromise = state.executionStatusChecked ? Promise.resolve(state.executionEnabled) : refreshExecutionStatus();
    var integrationPromise = loadIntegrationGate();
    var projectsPromise = state.projects.length ? Promise.resolve() : loadProjects();
    return Promise.all([gatePromise, integrationPromise, projectsPromise]).then(function () {});
  }

  function exit() {
    if (document.body.dataset.view !== "dev") return;
    // Close the listener on navigation (F2.4B requirement): re-entering
    // and reselecting the session reconciles against the real backend
    // status and reconnects if a turn is still active -- exactly the
    // same GET-based reconciliation a page refresh already relies on, so
    // nothing about the turn's own state is lost by not keeping a
    // background tab's SSE connection open indefinitely.
    closeStream();
    var previousView = state.previousView || "overview";
    var previousMode = state.previousMode || "overview";
    document.body.dataset.view = previousView;
    document.body.setAttribute("data-view", previousView);
    if (window.NexusMode) window.NexusMode.mode = previousMode;
    setViewBadge(previousMode);
    setActiveNavigation(state.previousNavId);
    var devView = $("ws-dev");
    if (devView) devView.setAttribute("aria-hidden", "true");
    var devReturn = $("dev-return");
    if (devReturn) devReturn.hidden = true;
    if (state.previousFocus && typeof state.previousFocus.focus === "function") state.previousFocus.focus();
    state.previousView = state.previousMode = state.previousNavId = state.previousFocus = null;
  }

  // -- Execution feature gate -------------------------------------------

  function refreshExecutionStatus() {
    return request("/dev/execution-status").then(function (payload) {
      state.executionEnabled = Boolean(payload && payload.execution_enabled);
      state.executionStatusChecked = true;
      renderExecutionGateBanner();
      setComposerState();
      updateMissionControls();
      return state.executionEnabled;
    }).catch(function (error) {
      // Unknown gate state is treated exactly like "disabled" for the
      // purpose of the Run control -- never optimistically enabled.
      state.executionEnabled = false;
      state.executionStatusChecked = true;
      renderExecutionGateBanner("Could not confirm the server's execution gate: " + error.message);
      setComposerState();
      updateMissionControls();
      return false;
    });
  }

  function renderExecutionGateBanner(errorText) {
    var banner = $("dev-gate-banner");
    if (!banner) return;
    if (errorText) {
      banner.hidden = false;
      banner.className = "dev-gate-banner dev-gate-error";
      banner.textContent = redacted(errorText);
      return;
    }
    if (state.executionEnabled) {
      banner.hidden = false;
      banner.className = "dev-gate-banner dev-gate-enabled";
      banner.textContent = "Write-capable execution is enabled on this server.";
    } else {
      banner.hidden = false;
      banner.className = "dev-gate-banner dev-gate-disabled";
      banner.textContent = "Write-capable execution is disabled on this server (NEXUS_DEV_EXECUTION_ENABLED is not set). " +
        "Sessions and worktrees can still be created and inspected, but no turn can run until an operator explicitly enables it.";
    }
  }

  // -- Projects -----------------------------------------------------------

  function populateProjects(projects) {
    // Only projects explicitly authorized for Development Mode -- never
    // every enabled project, which would imply Dev Mode is available
    // anywhere Chat is.
    state.allProjects = projects || [];
    state.projects = (projects || []).filter(function (project) {
      return project.enabled && (project.dev_enabled === true || project.dev_enabled === 1);
    });
    var select = $("dev-project-select");
    if (!select) return;
    select.replaceChildren();
    var prompt = document.createElement("option"); prompt.value = "";
    prompt.textContent = state.projects.length ? "Select a Development-Mode project…" : "No project is authorized for Development Mode";
    select.appendChild(prompt);
    state.projects.forEach(function (project) {
      var option = document.createElement("option"); option.value = project.id; option.textContent = project.name;
      select.appendChild(option);
    });
    select.disabled = state.projects.length === 0;
    var note = $("dev-project-note");
    if (note) {
      note.textContent = state.projects.length
        ? ""
        : "No project is authorized. Run .venv\\Scripts\\python -m nexus.dev.operator authorize <project-id> locally, then restart or refresh.";
    }
  }

  function loadProjects() {
    showStatus("Loading registered projects…", "loading");
    return request("/projects").then(function (payload) {
      populateProjects(payload && payload.projects);
      return loadSessions();
    }).then(function () {
      return loadModels(state.newSessionBackend);
    }).catch(function (error) {
      showStatus("Could not load Development Mode data: " + error.message, "error");
    });
  }

  // Each Development session runs on the backend it was created with; the
  // model list always comes from that same backend (never mixed).
  function loadModels(backend) {
    return request("/dev/models?backend=" + encodeURIComponent(backend)).then(function (models) {
      state.modelsBackend = backend;
      state.modelOptions = (models.models || []).filter(function (model) { return model.selectable === true && typeof model.id === "string"; });
      state.defaultModel = models.default_model;
      state.selectedModel = state.modelOptions.some(function (item) { return item.id === models.default_model; }) ? models.default_model : null;
      state.selectedEffort = null;
      populateModelSelect();
      populateEffortSelect();
      updateMissionControls();
      if (models.catalog_error) showStatus(models.catalog_error, "error");
      else showStatus("Ready.", "ready");
      return models;
    });
  }

  function populateModelSelect() {
    var select = $("dev-model-select"); if (!select) return;
    var search = $("dev-model-search"); var query = search ? search.value.trim().toLowerCase() : "";
    var previous = state.selectedModel;
    var matches = state.modelOptions.filter(function (model) {
      return !query || (model.id + " " + model.name).toLowerCase().includes(query);
    });
    var visible = matches.slice(0, 50);
    select.replaceChildren();
    var auto = document.createElement("option"); auto.value = ""; auto.textContent = "Select an approved model…";
    select.appendChild(auto);
    visible.forEach(function (model) {
      var option = document.createElement("option"); option.value = model.id;
      option.textContent = model.name + " · " + model.id + " · cost: " + (model.cost_status || "unknown");
      select.appendChild(option);
    });
    var resultCount = $("dev-model-result-count");
    if (resultCount) resultCount.textContent = matches.length > visible.length
      ? "Showing " + visible.length + " of " + matches.length + " routes."
      : matches.length + (matches.length === 1 ? " route" : " routes");
    if (previous && visible.some(function (model) { return model.id === previous; })) select.value = previous;
    else select.value = "";
  }

  function populateEffortSelect() {
    var select = $("dev-effort-select"); if (!select) return;
    var model = state.modelOptions.find(function (item) { return item.id === state.selectedModel; });
    select.replaceChildren();
    var options = model && Array.isArray(model.supported_efforts) ? model.supported_efforts : [];
    var defaultOption = document.createElement("option"); defaultOption.value = "";
    defaultOption.textContent = options.length ? "Model default" : "Not available for this model";
    select.appendChild(defaultOption);
    options.forEach(function (effort) {
      var option = document.createElement("option"); option.value = effort; option.textContent = effort; select.appendChild(option);
    });
    select.disabled = options.length === 0;
    state.selectedEffort = null; select.value = "";
  }

  // -- Sessions -------------------------------------------------------------

  function sessionStatusLabel(session) {
    return (session && session.status) || "UNKNOWN";
  }

  function renderSessions() {
    var container = $("dev-session-list");
    if (!container) return;
    container.replaceChildren();
    if (!state.sessions.length) {
      var empty = document.createElement("p"); empty.className = "dev-session-empty";
      empty.textContent = "No Development sessions for this project yet.";
      container.appendChild(empty);
      return;
    }
    state.sessions.forEach(function (session) {
      var button = document.createElement("button");
      button.type = "button"; button.className = "dev-session-item";
      var selected = state.session && state.session.session_id === session.session_id;
      button.classList.toggle("selected", selected);
      button.setAttribute("aria-pressed", selected ? "true" : "false");
      button.textContent = session.session_id + " · " + sessionStatusLabel(session);
      button.addEventListener("click", function () { selectSession(session.session_id); });
      container.appendChild(button);
    });
  }

  function loadSessions() {
    var projectId = $("dev-project-select") ? $("dev-project-select").value : "";
    return request("/dev/sessions" + (projectId ? "?project_id=" + encodeURIComponent(projectId) : "")).then(function (payload) {
      state.sessions = (payload && payload.sessions) || [];
      renderSessions();
      return state.sessions;
    }).catch(function (error) {
      showStatus("Could not load Development sessions: " + error.message, "error");
    });
  }

  function loadTestProfiles(projectId) {
    state.testProfiles = [];
    var select = $("dev-mission-test-profile");
    if (select) {
      select.replaceChildren();
      var loading = document.createElement("option"); loading.value = ""; loading.textContent = projectId ? "Loading trusted profiles…" : "Select a project first";
      select.appendChild(loading);
    }
    if (!projectId) { updateMissionControls(); return Promise.resolve(); }
    return request("/dev/test-profiles?project_id=" + encodeURIComponent(projectId)).then(function (payload) {
      state.testProfiles = (payload && payload.profiles) || [];
      if (select) {
        select.replaceChildren();
        var prompt = document.createElement("option"); prompt.value = "";
        prompt.textContent = state.testProfiles.length ? "Select a trusted profile…" : "No trusted profile configured for this project";
        select.appendChild(prompt);
        state.testProfiles.forEach(function (profile) {
          var option = document.createElement("option"); option.value = profile.id;
          option.textContent = profile.name + " · " + profile.id + " · " + profile.timeout_seconds + "s";
          select.appendChild(option);
        });
      }
      updateMissionControls();
    }).catch(function (error) {
      showStatus("Could not load trusted test profiles: " + error.message, "error");
      updateMissionControls();
    });
  }

  function chooseProject(projectId) {
    state.session = null;
    renderMission(null);
    resetTurnView();
    var createButton = $("dev-create-session");
    if (createButton) createButton.disabled = !projectId;
    loadTestProfiles(projectId);
    loadSessions();
  }

  function createSession() {
    var projectSelect = $("dev-project-select");
    var projectId = projectSelect ? projectSelect.value : "";
    if (!projectId) return;
    var button = $("dev-create-session"); if (button) button.disabled = true;
    showStatus("Creating a Development session…", "loading");
    var body = { project_id: projectId };
    if (state.newSessionBackend === "codex_native" || state.newSessionBackend === "claude_native") {
      body.backend = state.newSessionBackend;
      if (state.modelsBackend === state.newSessionBackend && state.selectedModel) body.requested_model = state.selectedModel;
    }
    request("/dev/sessions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (session) {
        state.sessions.unshift(session);
        renderSessions();
        selectSession(session.session_id);
        showStatus("Development session created.", "ready");
      })
      .catch(function (error) { showStatus("Could not create a Development session: " + error.message, "error"); })
      .finally(function () { if (button) button.disabled = !projectId; });
  }

  function selectSession(sessionId) {
    renderMission(null);
    if (!state.session || state.session.session_id !== sessionId) {
      state.review = null; state.selectedReviewPath = null;
      var summary = $("dev-review-summary"); if (summary) summary.textContent = "Loading read-only diff review…";
      var reviewFiles = $("dev-review-files"), reviewPatch = $("dev-review-patch");
      if (reviewFiles) reviewFiles.replaceChildren();
      if (reviewPatch) reviewPatch.replaceChildren();
      var changed = $("dev-review-changed"); if (changed) changed.hidden = true;
      state.integrationReviewOutdated = false;
      state.integrationReviewUncertain = false;
      state.integrationHistoryIds = [];
      updateReviewControls();
    }
    closeStream();
    request("/dev/sessions/" + encodeURIComponent(sessionId)).then(function (session) {
      state.session = session;
      var sessionBackend = session.backend || "omniroute";
      if (state.modelsBackend !== sessionBackend) loadModels(sessionBackend).catch(function () {});
      state.integrationReview = null; state.integrationPreflight = null;
      state.integrationReviewOutdated = false; state.integrationReviewUncertain = false;
      integrationMessage("Loading persisted review history…", "");
      var confirmation = $("dev-apply-confirm"); if (confirmation) confirmation.checked = false;
      renderSessions();
      renderSessionDetail();
      renderIntegrationSummary();
      updateIntegrationControls();
      return request("/dev/sessions/" + encodeURIComponent(sessionId) + "/turns");
    }).then(function (payload) {
      var turns = (payload && payload.turns) || [];
      var latest = turns.length ? turns[turns.length - 1] : null;
      renderTurnResult(latest);
      if (latest && (latest.status === "PENDING" || latest.status === "RUNNING")) {
        connectEvents(latest.turn_id);
      }
      loadChangedFiles();
      loadReview();
      loadIntegrationHistory();
      loadAutonomousMissions();
      loadApprovals();
      setComposerState();
    }).catch(function (error) { showStatus("Could not load this Development session: " + error.message, "error"); });
  }

  function missionIsActive(mission) {
    return Boolean(mission && ["PENDING", "RUNNING", "TESTING", "REPAIRING"].indexOf(mission.status) !== -1);
  }

  function updateMissionControls() {
    var hasSession = Boolean(state.session && state.session.status === "ACTIVE");
    var active = missionIsActive(state.mission);
    var description = $("dev-mission-description");
    var profile = $("dev-mission-test-profile");
    var start = $("dev-mission-start");
    var stop = $("dev-mission-stop");
    if (description) description.disabled = !hasSession || active || !state.executionEnabled;
    if (profile) profile.disabled = !hasSession || active || !state.executionEnabled;
    var profileSelected = Boolean(profile && profile.value && state.testProfiles.some(function (item) { return item.id === profile.value; }));
    if (start) start.disabled = !hasSession || active || !state.executionEnabled || !state.selectedModel || !profileSelected;
    if (stop) stop.disabled = !active;
  }

  function renderMission(mission) {
    state.mission = mission || null;
    var output = $("dev-mission-progress");
    if (output) {
      if (!mission) output.textContent = state.session ? "No autonomous mission has run in this session." : "Select an active Development session.";
      else {
        var lines = [
          mission.mission_id + " · " + mission.status,
          "Mission: " + mission.description,
          "Model: " + mission.requested_model,
          "Attempts: " + mission.attempt_count + " / 2",
          "Test profile: " + mission.test_profile,
          "Tests: " + ((mission.tests || []).map(function (test) {
            return "#" + test.attempt_number + " " + test.status + (test.exit_code == null ? "" : " (exit " + test.exit_code + ")");
          }).join(" · ") || "not started")
        ];
        if (mission.error_code) lines.push("Error: " + mission.error_code + " · " + (mission.error_message || ""));
        var failedTests = (mission.tests || []).filter(function (test) { return test.status !== "PASSED" && (test.stderr || test.stdout); });
        failedTests.forEach(function (test) {
          var evidence = String(test.stderr || test.stdout || "");
          if (evidence.length > 1200) evidence = evidence.slice(0, 1200) + "\n[output shortened in UI]";
          lines.push("Test #" + test.attempt_number + " evidence:\n" + evidence);
        });
        var changedFiles = mission.result && mission.result.changed_files;
        if (Array.isArray(changedFiles)) lines.push("Changed files: " + (changedFiles.map(function (file) { return file.path; }).join(", ") || "none"));
        if (mission.status === "READY_FOR_REVIEW") lines.push("Ready for human review. Review the diff below; integration was not applied.");
        output.textContent = redacted(lines.join("\n"));
      }
    }
    updateMissionControls();
    if (missionIsActive(mission)) scheduleMissionPoll();
    else if (state.missionPollTimer) { clearTimeout(state.missionPollTimer); state.missionPollTimer = null; }
  }

  function loadAutonomousMissions() {
    if (!state.session) { renderMission(null); return Promise.resolve(); }
    return request("/dev/sessions/" + encodeURIComponent(state.session.session_id) + "/missions")
      .then(function (payload) { renderMission(payload && payload.missions && payload.missions.length ? payload.missions[0] : null); })
      .catch(function (error) { renderMission(null); showStatus("Could not load autonomous missions: " + error.message, "error"); });
  }

  function scheduleMissionPoll() {
    if (state.missionPollTimer) clearTimeout(state.missionPollTimer);
    state.missionPollTimer = setTimeout(function () {
      if (!state.session || !state.mission) return;
      request("/dev/sessions/" + encodeURIComponent(state.session.session_id) + "/missions/" + encodeURIComponent(state.mission.mission_id))
        .then(function (mission) {
          var wasActive = missionIsActive(state.mission);
          renderMission(mission);
          loadApprovals();
          if (wasActive && !missionIsActive(mission)) { loadChangedFiles(); loadReview(); }
        })
        .catch(function (error) { showStatus("Could not refresh autonomous mission: " + error.message, "error"); });
    }, 1000);
  }

  function startAutonomousMission() {
    if (!state.session || !state.selectedModel || missionIsActive(state.mission)) return;
    var description = $("dev-mission-description");
    var profile = $("dev-mission-test-profile");
    var value = description ? description.value.trim() : "";
    if (!value) { showStatus("Describe the autonomous mission before starting it.", "error"); return; }
    if (!profile || !profile.value) { showStatus("Select a trusted test profile before starting.", "error"); return; }
    request("/dev/sessions/" + encodeURIComponent(state.session.session_id) + "/missions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ description: value, model: state.selectedModel, allowed_models: [state.selectedModel], test_profile: profile.value })
    }).then(function (mission) {
      renderMission(mission); showStatus("Autonomous mission started.", "loading");
    }).catch(function (error) { showStatus("Could not start autonomous mission: " + error.message, "error"); });
  }

  function interruptAutonomousMission() {
    if (!state.session || !state.mission || !missionIsActive(state.mission)) return;
    request("/dev/sessions/" + encodeURIComponent(state.session.session_id) + "/missions/" + encodeURIComponent(state.mission.mission_id) + "/interrupt", { method: "POST" })
      .then(function () { showStatus("Mission interruption requested.", "loading"); scheduleMissionPoll(); })
      .catch(function (error) { showStatus("Could not interrupt mission: " + error.message, "error"); });
  }

  function renderSessionDetail() {
    var detail = $("dev-session-detail");
    if (!detail || !state.session) return;
    detail.innerHTML = "";
    var project = state.allProjects.find(function (item) { return item.id === state.session.project_id; });
    var authorized = Boolean(project && project.enabled && (project.dev_enabled === true || project.dev_enabled === 1));
    var rows = [
      ["Session", state.session.session_id],
      ["Backend", state.session.backend === "codex_native" ? "Codex Native" : state.session.backend === "claude_native" ? "Claude Native" : "OmniRoute"],
      ["Status", state.session.status],
      ["Project authorization", authorized ? "Authorized (dev_enabled)" : "NOT authorized — revoked or disabled"],
      ["Branch", state.session.branch],
      ["Base commit", state.session.base_sha],
      ["Worktree", state.session.worktree_path],
    ];
    rows.forEach(function (pair) {
      var row = document.createElement("p");
      if (pair[0] === "Project authorization") row.className = authorized ? "dev-auth-ok" : "dev-auth-revoked";
      var strong = document.createElement("strong"); strong.textContent = pair[0] + ": ";
      row.appendChild(strong);
      row.appendChild(document.createTextNode(redacted(pair[1] || "—")));
      detail.appendChild(row);
    });
  }

  // -- Changed files (read-only Git status summary, F2.4A) ------------------

  function renderChangedFiles(files) {
    var container = $("dev-changed-files");
    if (!container) return;
    container.replaceChildren();
    if (!files || !files.length) {
      var empty = document.createElement("p"); empty.className = "dev-changed-files-empty";
      empty.textContent = "No file changes recorded in this worktree yet.";
      container.appendChild(empty);
      return;
    }
    var list = document.createElement("ul"); list.className = "dev-changed-files-list";
    files.forEach(function (file) {
      var item = document.createElement("li");
      item.className = "dev-changed-file dev-changed-file-" + (file.status || "changed");
      var badge = document.createElement("span"); badge.className = "dev-changed-file-badge"; badge.textContent = file.status || "changed";
      item.appendChild(badge);
      item.appendChild(document.createTextNode(" " + redacted(file.path)));
      list.appendChild(item);
    });
    container.appendChild(list);
  }

  function loadChangedFiles() {
    if (!state.session) return;
    request("/dev/sessions/" + encodeURIComponent(state.session.session_id) + "/changed-files")
      .then(function (payload) { renderChangedFiles((payload && payload.files) || []); })
      .catch(function () { renderChangedFiles([]); });
  }

  function renderReview(payload) {
    var filesBox = $("dev-review-files"), patchBox = $("dev-review-patch");
    if (!filesBox || !patchBox) return;
    var files = (payload && payload.files) || [];
    var previousId = state.review && state.review.review_id;
    var changed = Boolean(previousId && payload.review_id && previousId !== payload.review_id);
    state.review = payload;
    reconcilePersistedReviewWithDiff();
    var changedBox = $("dev-review-changed"); if (changedBox) changedBox.hidden = !changed;
    var summary = $("dev-review-summary");
    if (summary) summary.textContent = files.length + " file(s) · +" + (payload.additions || 0) + " / −" + (payload.deletions || 0) + (payload.truncated ? " · review truncated by safety limits" : "");
    filesBox.replaceChildren(); patchBox.replaceChildren();
    if (!files.length) {
      var empty = document.createElement("p"); empty.className = "dev-review-empty"; empty.textContent = "No changes from the saved base commit."; filesBox.appendChild(empty);
      var none = document.createElement("p"); none.className = "dev-review-empty"; none.textContent = "Nothing to display."; patchBox.appendChild(none);
      state.selectedReviewPath = null; return;
    }
    var selected = files.some(function (file) { return file.path === state.selectedReviewPath; }) ? state.selectedReviewPath : files[0].path;
    files.forEach(function (file) {
      var button = document.createElement("button"); button.type = "button";
      button.className = "dev-review-file" + (file.path === selected ? " selected" : "");
      button.setAttribute("aria-pressed", file.path === selected ? "true" : "false");
      var status = document.createElement("span"); status.className = "dev-review-file-status"; status.textContent = file.status;
      button.appendChild(status); button.appendChild(document.createTextNode(file.path));
      button.addEventListener("click", function () { state.selectedReviewPath = file.path; renderReview(state.review); });
      filesBox.appendChild(button);
    });
    state.selectedReviewPath = selected;
    var file = files.find(function (entry) { return entry.path === selected; });
    if (file.old_path) { var rename = document.createElement("p"); rename.className = "dev-review-empty"; rename.textContent = "Renamed from " + file.old_path; patchBox.appendChild(rename); }
    if (!file.patch) {
      var message = document.createElement("p"); message.className = "dev-review-empty";
      message.textContent = file.reason || (file.binary ? "Binary file; content omitted." : "No textual diff available."); patchBox.appendChild(message); return;
    }
    var pre = document.createElement("pre"), oldLine = 0, newLine = 0;
    file.patch.split("\n").forEach(function (line) {
      var hunk = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (hunk) { oldLine = Number(hunk[1]); newLine = Number(hunk[2]); }
      var row = document.createElement("span"); row.className = "dev-review-line";
      var content = line, number = "      ";
      if (line.startsWith("+") && !line.startsWith("+++")) { row.className += " dev-review-line-add"; number = "      " + newLine++; }
      else if (line.startsWith("-") && !line.startsWith("---")) { row.className += " dev-review-line-del"; number = "" + oldLine++ + "      "; }
      else if (line.startsWith(" ")) { number = ("      " + oldLine++).slice(-6) + " " + newLine++; }
      else if (line.startsWith("@@") || line.startsWith("diff ") || line.startsWith("index ") || line.startsWith("---") || line.startsWith("+++")) row.className += " dev-review-line-meta";
      row.textContent = number + " " + line; pre.appendChild(row);
    });
    patchBox.appendChild(pre);
  }

  function loadReview() {
    if (!state.session) {
      var noSession = $("dev-review-summary"); if (noSession) noSession.textContent = "No session selected. Select a Development session to load its diff.";
      return Promise.resolve();
    }
    if (state.reviewLoading) return Promise.resolve();
    var sessionId = state.session.session_id;
    var previousId = state.review && state.review.review_id;
    state.reviewLoading = true; updateReviewControls();
    var summary = $("dev-review-summary"); if (summary) summary.textContent = "Refreshing the read-only worktree diff…";
    return request("/dev/sessions/" + encodeURIComponent(sessionId) + "/diff")
      .then(function (payload) {
        if (!state.session || state.session.session_id !== sessionId) return;
        payload = payload || { files: [] };
        renderReview(payload);
        var files = payload.files || [];
        if (summary) {
          if (!files.length) summary.textContent = "No changes to review. Diff refreshed against the saved base commit.";
          else if (previousId && previousId === payload.review_id) summary.textContent = "Review refreshed. No new changes since the previous refresh · " + files.length + " file(s).";
          else if (previousId) summary.textContent = "Review refreshed. Worktree changes detected since the previous refresh · " + files.length + " file(s).";
          else summary.textContent = "Diff loaded from the selected worktree · " + files.length + " file(s).";
          if (state.integrationReviewOutdated) summary.textContent += " The persisted review is outdated; create a new review snapshot.";
        }
      })
      .catch(function (error) {
        if (!state.session || state.session.session_id !== sessionId) return;
        var summary = $("dev-review-summary"); if (summary) summary.textContent = "Could not load read-only diff review: " + error.message;
        // Keep the last successful review visible on transient failures.
      }).finally(function () { state.reviewLoading = false; updateReviewControls(); });
  }

  function reconcilePersistedReviewWithDiff() {
    var persistedDiff = state.integrationReview && state.integrationReview.snapshot && state.integrationReview.snapshot.diff;
    if (!state.integrationReview || !persistedDiff || !state.review || !persistedDiff.review_id || !state.review.review_id) return;
    var outdated = persistedDiff.review_id !== state.review.review_id;
    if (outdated && !state.integrationReviewOutdated) {
      state.integrationReviewOutdated = true;
      state.integrationPreflight = null;
      integrationMessage("Review outdated: the worktree diff changed after this persisted snapshot. Create a new review before checking readiness.", "error");
      renderIntegrationSummary();
    }
    updateIntegrationControls();
  }

  function updateReviewControls() {
    var hasSession = Boolean(state.session);
    var refresh = $("dev-review-refresh"); if (refresh) { refresh.disabled = !hasSession || state.reviewLoading; refresh.title = !hasSession ? "Select a Development session first." : state.reviewLoading ? "Refreshing the worktree diff…" : "Reload the read-only worktree diff."; }
    var create = $("dev-create-review"); if (create) { create.disabled = !hasSession || state.integrationBusy || state.integrationReviewUncertain; create.title = !hasSession ? "Select a Development session first." : state.integrationReviewUncertain ? "Review creation outcome is being reconciled against persisted history." : "Save a review snapshot for the current worktree content."; }
  }

  function loadIntegrationGate() {
    return request("/dev/integration-status").then(function (payload) {
      state.integrationEnabled = Boolean(payload && payload.integration_enabled);
      var gate = $("dev-integration-gate");
      if (gate) { gate.className = "dev-integration-gate " + (state.integrationEnabled ? "on" : "off"); gate.textContent = state.integrationEnabled ? "Human-approved integration gate: ON" : "Human-approved integration gate: OFF (Apply disabled)"; }
      updateIntegrationControls();
      return state.integrationEnabled;
    }).catch(function () {
      state.integrationEnabled = false;
      var gate = $("dev-integration-gate"); if (gate) { gate.className = "dev-integration-gate off"; gate.textContent = "Integration gate status unavailable; Apply remains disabled."; }
      updateIntegrationControls(); return false;
    });
  }

  function integrationMessage(message, kind) {
    var element = $("dev-integration-message"); if (!element) return;
    element.className = "dev-integration-message" + (kind ? " " + kind : "");
    element.textContent = redacted(message || "");
  }

  function updateIntegrationControls() {
    var hasSession = Boolean(state.session), hasReview = Boolean(state.integrationReview);
    var reviewActionable = hasReview && state.integrationReview.status === "REVIEWED" && !state.integrationReviewOutdated;
    updateReviewControls();
    var preflight = $("dev-integration-preflight"); if (preflight) preflight.disabled = !reviewActionable || state.integrationBusy;
    var reject = $("dev-reject-review"); if (reject) reject.disabled = !reviewActionable || state.integrationBusy;
    var confirm = $("dev-apply-confirm");
    var confirmed = Boolean(confirm && confirm.checked);
    if (confirm) confirm.disabled = !reviewActionable || !state.integrationEnabled || !state.integrationPreflight || !state.integrationPreflight.ready || state.integrationBusy;
    var apply = $("dev-apply-review"); if (apply) apply.disabled = !reviewActionable || !state.integrationEnabled || !state.integrationPreflight || !state.integrationPreflight.ready || !confirmed || state.integrationBusy;
  }

  function renderIntegrationSummary() {
    var box = $("dev-integration-summary"); if (!box) return;
    var review = state.integrationReview;
    if (!review) {
      box.textContent = state.session ? "No persisted review selected. Create a review snapshot before checking readiness." : "No session selected. Select a Development session before reviewing integration.";
      updateIntegrationControls(); return;
    }
    var snapshot = review.snapshot || review;
    var diff = snapshot.diff || {};
    var project = state.allProjects.find(function (item) { return item.id === review.project_id; });
    var preflight = state.integrationPreflight;
    var consequence = !state.integrationEnabled ? "Integration disabled by policy; Apply is unavailable."
      : state.integrationReviewOutdated ? "Review outdated; create a new persisted snapshot before preflight."
        : preflight && !preflight.ready ? "Preflight blocked; Apply is unavailable until every check passes."
          : preflight && preflight.ready ? "Apply creates one candidate commit and fast-forwards the destination branch."
            : "Apply remains disabled until a fresh review passes preflight.";
    box.textContent = "Project: " + ((project && project.name) || review.project_id || "unknown") + " · Branch: " + (review.target_branch || "unknown") +
      " · Files: " + ((diff.files && diff.files.length) || 0) + " · +" + (diff.additions || 0) + " / −" + (diff.deletions || 0) +
      " · Base: " + (review.base_sha || "unknown") + " · Review ID: " + review.review_id +
      " · Preflight: " + (preflight ? (preflight.ready ? "READY" : "BLOCKED") : "not checked") + " · " + consequence;
    updateIntegrationControls();
  }

  function createIntegrationReview() {
    if (!state.session) { integrationMessage("No session selected. Select a Development session before creating a review.", "error"); return; }
    if (state.integrationBusy || state.integrationReviewUncertain) return;
    var sessionId = state.session.session_id;
    var knownIds = state.integrationHistoryIds.slice();
    state.integrationBusy = true; updateIntegrationControls(); integrationMessage("Creating a content-bound review snapshot…", "");
    request("/dev/sessions/" + encodeURIComponent(sessionId) + "/reviews", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
      .then(function (review) {
        if (!state.session || state.session.session_id !== sessionId) return;
        state.integrationReview = review; state.integrationPreflight = null; state.integrationReviewOutdated = false;
        renderIntegrationSummary(); integrationMessage("Review created and persisted · Review ID: " + review.review_id + ". Check readiness before approving Apply.", "ready");
        return loadIntegrationHistory();
      })
      .catch(function (error) {
        if (error.status >= 500 || !error.status) {
          state.integrationReviewUncertain = true; updateIntegrationControls();
          integrationMessage("Review creation response was ambiguous: " + error.message + ". Checking persisted history; do not submit again until reconciliation finishes.", "error");
          return loadIntegrationHistory({ uncertainIds: knownIds });
        }
        integrationMessage("Could not create review snapshot: " + error.message, "error");
      })
      .finally(function () { state.integrationBusy = false; updateIntegrationControls(); });
  }

  function checkIntegrationPreflight() {
    if (!state.session || !state.integrationReview || state.integrationBusy) return;
    state.integrationBusy = true; state.integrationPreflight = null; updateIntegrationControls();
    request("/dev/sessions/" + encodeURIComponent(state.session.session_id) + "/preflight?review_id=" + encodeURIComponent(state.integrationReview.review_id))
      .then(function (payload) {
        state.integrationPreflight = payload;
        renderIntegrationSummary();
        var issues = payload.issues || [];
        integrationMessage(payload.ready ? "Preflight READY. Confirm the exact reviewed snapshot to enable Apply." : "Preflight blocked: " + (issues.map(function (item) { return item.message; }).join(" ") || "the server did not provide a blocking reason."), payload.ready ? "ready" : "error");
      }).catch(function (error) { integrationMessage("Preflight failed: " + error.message, "error"); })
      .finally(function () { state.integrationBusy = false; updateIntegrationControls(); });
  }

  function applyIntegrationReview() {
    if (!state.session || !state.integrationReview || !state.integrationPreflight || !state.integrationPreflight.ready || !$("dev-apply-confirm").checked || state.integrationBusy) return;
    state.integrationBusy = true; updateIntegrationControls(); integrationMessage("Applying the approved snapshot…", "");
    request("/dev/sessions/" + encodeURIComponent(state.session.session_id) + "/apply", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ review_id: state.integrationReview.review_id, confirmed: true }) })
      .then(function (result) {
        state.integrationReview.status = result.status; state.integrationPreflight = null;
        $("dev-apply-confirm").checked = false; renderIntegrationSummary();
        integrationMessage("Integrated as commit " + result.commit_sha + " on " + result.branch + ".", "ready");
        loadReview(); return loadIntegrationHistory();
      }).catch(function (error) {
        // A failed response can leave the persisted outcome in FAILED or
        // APPLY_UNCERTAIN. Clear readiness immediately so a stale checked
        // preflight can never leave Apply available while history reconciles.
        state.integrationPreflight = null; renderIntegrationSummary();
        integrationMessage("Apply failed: " + error.message + " Do not retry until preflight/history is checked.", "error");
        return loadIntegrationHistory();
      })
      .finally(function () { state.integrationBusy = false; updateIntegrationControls(); });
  }

  function rejectIntegrationReview() {
    if (!state.session || !state.integrationReview || state.integrationBusy) return;
    state.integrationBusy = true; updateIntegrationControls();
    request("/dev/sessions/" + encodeURIComponent(state.session.session_id) + "/reject", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ review_id: state.integrationReview.review_id }) })
      .then(function (result) { state.integrationReview.status = result.status; renderIntegrationSummary(); integrationMessage("Proposal rejected. The Development worktree remains available.", "ready"); return loadIntegrationHistory(); })
      .catch(function (error) { integrationMessage("Could not reject proposal: " + error.message, "error"); })
      .finally(function () { state.integrationBusy = false; updateIntegrationControls(); });
  }

  function loadIntegrationHistory(options) {
    if (!state.session) return Promise.resolve();
    options = options || {};
    var sessionId = state.session.session_id;
    return request("/dev/sessions/" + encodeURIComponent(sessionId) + "/integrations").then(function (payload) {
      if (!state.session || state.session.session_id !== sessionId) return;
      var container = $("dev-integration-history-list"); if (!container) return;
      container.replaceChildren();
      var rows = (payload && payload.integrations) || [];
      var previousIds = options.uncertainIds || state.integrationHistoryIds;
      state.integrationHistoryIds = rows.map(function (row) { return row.review_id; });
      if (!rows.length) {
        container.textContent = "No integration history.";
        if (options.uncertainIds) {
          state.integrationReviewUncertain = false;
          integrationMessage("No new persisted review was found after the ambiguous response. No retry was sent; you may create a review manually when ready.", "error");
        } else if (!state.integrationReview) integrationMessage("No persisted review exists for this session yet.", "");
        updateIntegrationControls();
        return;
      }
      rows.forEach(function (row) {
        var line = document.createElement("div"); line.className = "dev-integration-history-row";
        line.textContent = row.status + " · review " + row.review_id + (row.result_sha ? " · commit " + row.result_sha : "") + (row.error_message ? " · " + row.error_message : "");
        container.appendChild(line);
      });
      var persisted = state.integrationReview && rows.find(function (row) { return row.review_id === state.integrationReview.review_id; });
      if (persisted) {
        state.integrationReview = persisted;
        if (persisted.status !== "REVIEWED") state.integrationPreflight = null;
        renderIntegrationSummary();
      } else if (!state.integrationReview) {
        state.integrationReview = rows[0]; state.integrationPreflight = null; renderIntegrationSummary();
      }
      if (state.integrationReview && state.review && state.integrationReview.snapshot && state.integrationReview.snapshot.diff) reconcilePersistedReviewWithDiff();
      if (options.uncertainIds) {
        var discovered = rows.find(function (row) { return previousIds.indexOf(row.review_id) < 0; });
        state.integrationReviewUncertain = false;
        if (discovered) {
          state.integrationReview = discovered; state.integrationPreflight = null; state.integrationReviewOutdated = false;
          renderIntegrationSummary(); integrationMessage("Review creation response was ambiguous, but a persisted review was recovered from history · Review ID: " + discovered.review_id + ".", "ready");
        } else {
          integrationMessage("No new persisted review was found after the ambiguous response. No retry was sent; you may create a review manually when ready.", "error");
        }
      }
    }).catch(function (error) {
      if (options.uncertainIds) {
        state.integrationReviewUncertain = true;
        integrationMessage("Could not reconcile review creation with persisted history: " + error.message + ". Review creation stays blocked to prevent a blind duplicate.", "error");
      } else {
        integrationMessage("Could not load persisted review history: " + error.message, "error");
      }
      updateIntegrationControls();
    });
  }

  // -- Turn execution ---------------------------------------------------

  function setComposerState() {
    var promptInput = $("dev-prompt-input");
    var runButton = $("dev-run-button");
    var stopButton = $("dev-stop-button");
    var ready = Boolean(state.session) && state.session.status === "ACTIVE";
    if (promptInput) promptInput.disabled = !ready || state.sending;
    if (runButton) {
      var canRun = ready && state.executionEnabled && !state.sending && !state.activeTurnId;
      runButton.disabled = !canRun;
      runButton.title = !state.session ? "Select or create a Development session first."
        : !state.executionEnabled ? "Execution is disabled on this server (feature gate off)."
        : state.activeTurnId ? "A turn is already running."
        : "Run this prompt in the session's isolated worktree.";
    }
    if (stopButton) stopButton.disabled = !state.activeTurnId;
  }

  function resetTurnView() {
    closeStream();
    state.activeTurnId = null;
    var detail = $("dev-session-detail"); if (detail) detail.innerHTML = "";
    var result = $("dev-turn-result"); if (result) result.textContent = "No turn run yet.";
    var activity = $("dev-activity-events"); if (activity) activity.replaceChildren();
    renderChangedFiles([]);
    setExecutionState("idle", null);
    renderSessions();
  }

  var EXECUTION_STATE_LABELS = { running: "Running", completed: "Completed", failed: "Failed", interrupted: "Interrupted", idle: "Idle" };

  function normalizeExecutionState(raw) {
    var text = String(raw || "").toLowerCase();
    if (/fail/.test(text)) return "failed";
    if (/cancel|interrupt/.test(text)) return "interrupted";
    if (/complet/.test(text)) return "completed";
    if (/pending|running|started/.test(text)) return "running";
    return null;
  }

  function stopElapsedTicker() {
    if (state.elapsedTimer) { clearInterval(state.elapsedTimer); state.elapsedTimer = null; }
  }

  function renderElapsed() {
    var node = $("dev-activity-elapsed");
    if (!node) return;
    if (!state.activityStartedAt) { node.textContent = "Elapsed: —"; return; }
    var seconds = Math.max(0, Math.round((Date.now() - state.activityStartedAt) / 1000));
    node.textContent = "Elapsed: " + seconds + "s";
  }

  function startElapsedTicker() {
    stopElapsedTicker();
    renderElapsed();
    state.elapsedTimer = setInterval(renderElapsed, 1000);
  }

  function logActivityEvent(detail) {
    if (!detail) return;
    var list = $("dev-activity-events");
    if (!list) return;
    var item = document.createElement("li");
    item.textContent = redacted(detail);
    list.appendChild(item);
    while (list.children.length > 30) list.removeChild(list.children[0]);
  }

  // -- Human approvals (P4.0B) ------------------------------------------------

  function loadApprovals() {
    if (!state.session) return Promise.resolve();
    var sessionId = state.session.session_id;
    return request("/dev/sessions/" + encodeURIComponent(sessionId) + "/approvals").then(function (payload) {
      if (!state.session || state.session.session_id !== sessionId) return;
      state.approvals = (payload && payload.approvals) || [];
      renderApprovals();
    }).catch(function () {});
  }

  function renderApprovals() {
    var list = $("dev-approvals");
    if (!list) return;
    list.replaceChildren();
    var shown = state.approvals.filter(function (item) { return item.status === "PENDING"; })
      .concat(state.approvals.filter(function (item) { return item.status !== "PENDING"; }).slice(0, 5));
    if (!shown.length) {
      var empty = document.createElement("li"); empty.textContent = "No approval requests.";
      list.appendChild(empty);
      return;
    }
    shown.forEach(function (approval) {
      var row = document.createElement("li");
      row.className = "dev-approval dev-approval-" + String(approval.status || "").toLowerCase();
      var summary = approval.summary || {};
      var text = document.createElement("span");
      text.textContent = redacted(
        (approval.kind === "file_change" ? "File change" : "Command") + " · " + approval.status +
        (summary.command ? " · " + summary.command : "") +
        (summary.grantRoot ? " · write access to " + summary.grantRoot : "") +
        (summary.reason ? " · reason: " + summary.reason : "")
      );
      row.appendChild(text);
      if (summary.input) {
        // The exact operation that an approval allows -- nothing else can run.
        var exact = document.createElement("pre");
        exact.className = "dev-approval-input";
        exact.textContent = redacted((summary.tool ? summary.tool + " " : "") + summary.input);
        row.appendChild(exact);
      }
      if (approval.status === "PENDING") {
        [["accept", "Approve once"], ["decline", "Decline"]].forEach(function (pair) {
          var button = document.createElement("button");
          button.type = "button"; button.className = "chat-secondary-button";
          button.textContent = pair[1];
          button.addEventListener("click", function () { decideApproval(approval.approval_id, pair[0]); });
          row.appendChild(button);
        });
      }
      list.appendChild(row);
    });
  }

  function decideApproval(approvalId, decision) {
    if (!state.session) return;
    request("/dev/sessions/" + encodeURIComponent(state.session.session_id) + "/approvals/" + encodeURIComponent(approvalId), {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision: decision }),
    }).then(function () {
      logActivityEvent((decision === "accept" ? "Approved" : "Declined") + " " + approvalId);
      return loadApprovals();
    }).catch(function (error) {
      showStatus("Could not record the decision: " + error.message, "error");
      loadApprovals();
    });
  }

  function setExecutionState(rawState, detail) {
    var key = normalizeExecutionState(rawState) || "idle";
    var node = $("dev-activity-status");
    if (node) {
      node.textContent = EXECUTION_STATE_LABELS[key];
      node.className = "dev-activity-status dev-activity-status-" + key;
    }
    if (key === "running") {
      state.activityStartedAt = state.activityStartedAt || Date.now();
      startElapsedTicker();
    } else {
      stopElapsedTicker();
      if (key === "idle") state.activityStartedAt = null;
      renderElapsed();
    }
    if (detail) logActivityEvent(detail);
    setComposerState();
  }

  function renderTurnResult(turn) {
    var result = $("dev-turn-result");
    if (!result) return;
    if (!turn) { result.textContent = "No turn run yet."; setExecutionState("idle", null); return; }
    if (turn.response_text) result.textContent = redacted(turn.response_text);
    else if (turn.error_message) result.textContent = (turn.error_code || "ERROR") + ": " + redacted(turn.error_message);
    else result.textContent = "Turn status: " + turn.status;
    setExecutionState(turn.status, null);
  }

  function runTurn() {
    if (!state.session) return;
    var promptInput = $("dev-prompt-input");
    var prompt = promptInput ? promptInput.value.trim() : "";
    if (!prompt) { showStatus("Enter a task before running.", "error"); return; }
    if (!state.executionEnabled) { showStatus("Execution is disabled on this server.", "error"); return; }
    state.sending = true; setComposerState();
    var body = { prompt: prompt };
    if (state.selectedModel) body.model = state.selectedModel;
    showStatus("Starting a Development turn…", "loading");
    request("/dev/sessions/" + encodeURIComponent(state.session.session_id) + "/turns", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }).then(function (turn) {
      state.sending = false;
      connectEvents(turn.turn_id);
      setComposerState();
    }).catch(function (error) {
      state.sending = false;
      showStatus("Could not start the turn: " + error.message, "error");
      setComposerState();
    });
  }

  function interruptTurn() {
    if (!state.session || !state.activeTurnId) return;
    var turnId = state.activeTurnId;
    var button = $("dev-stop-button"); if (button) button.disabled = true;
    request("/dev/sessions/" + encodeURIComponent(state.session.session_id) + "/interrupt?turn_id=" + encodeURIComponent(turnId), { method: "POST" })
      .then(function (result) {
        showStatus("Interrupt status: " + result.status, result.status === "INTERRUPT_FAILED" ? "error" : "loading");
      }).catch(function (error) { showStatus("Could not interrupt turn: " + error.message, "error"); setComposerState(); });
  }

  // -- Connection state (F2.4B) --------------------------------------------
  //
  // Deliberately separate from execution state (dev-activity-status /
  // setExecutionState above): the backend is the only authority on
  // whether a turn is Running/Completed/Failed/Interrupted, so a dropped
  // browser connection must never repaint that badge. This renders a
  // second, independent indicator for what the UI itself currently knows
  // about its own link to the backend.
  var CONNECTION_STATE_LABELS = { connected: "Live", reconnecting: "Reconnecting…", lost: "Connection lost", idle: "" };

  function setConnectionState(key) {
    state.connectionState = key;
    var node = $("dev-connection-status");
    if (!node) return;
    node.textContent = CONNECTION_STATE_LABELS[key] || "";
    node.className = "dev-connection-status dev-connection-status-" + key;
    node.hidden = key === "idle";
  }

  function stopReconcilePoll() {
    if (state.reconcilePoll.timer) { clearTimeout(state.reconcilePoll.timer); state.reconcilePoll.timer = null; }
    state.reconcilePoll.attempts = 0;
    state.reconcilePoll.turnId = null;
  }

  // Bounded, read-only fallback for exactly the failure mode a live smoke
  // test actually hit: the EventSource's own auto-reconnect can itself
  // get starved (e.g. by an unrelated burst of same-origin requests
  // saturating the browser's connection pool -- see
  // control-center.js's overviewPollingSuspended()) and never recover on
  // its own. This polls the same durable turn-detail endpoint a page
  // refresh already used to reconcile, so a user is never required to
  // manually refresh to see a turn's real, backend-confirmed outcome.
  // Never starts or retries a turn -- GET only.
  function startReconcilePoll(turnId) {
    if (state.reconcilePoll.timer && state.reconcilePoll.turnId === turnId) return; // already polling this turn
    stopReconcilePoll();
    state.reconcilePoll.turnId = turnId;
    function tick() {
      state.reconcilePoll.attempts += 1;
      if (!state.session || state.activeTurnId !== turnId) { stopReconcilePoll(); return; }
      request("/dev/sessions/" + encodeURIComponent(state.session.session_id) + "/turns/" + encodeURIComponent(turnId))
        .then(function (turn) {
          if (state.activeTurnId !== turnId) { stopReconcilePoll(); return; }
          var normalized = normalizeExecutionState(turn.status);
          if (normalized && normalized !== "running") {
            // Backend-confirmed terminal state found through the fallback
            // path -- this is exactly the recovery F2.4B exists to
            // guarantee without a manual refresh.
            stopReconcilePoll();
            closeStream();
            renderTurnResult(turn);
            loadChangedFiles();
            loadReview();
            setComposerState();
            return;
          }
          scheduleNext();
        })
        .catch(function () { scheduleNext(); });
    }
    function scheduleNext() {
      if (state.reconcilePoll.attempts >= RECONCILE_POLL_MAX_ATTEMPTS) {
        stopReconcilePoll();
        showStatus("Could not confirm this turn's status after repeated attempts. It may still be running on the server; try again shortly.", "error");
        return;
      }
      state.reconcilePoll.timer = setTimeout(tick, RECONCILE_POLL_INTERVAL_MS);
    }
    tick();
  }

  function reconcileAfterTerminal(turnId) {
    stopReconcilePoll();
    if (!state.session) return;
    request("/dev/sessions/" + encodeURIComponent(state.session.session_id) + "/turns/" + encodeURIComponent(turnId))
      .then(function (turn) {
        if (state.activeTurnId === turnId) state.activeTurnId = null;
        renderTurnResult(turn);
        loadChangedFiles();
        loadReview();
        setComposerState();
      }).catch(function (error) { showStatus("Could not reconcile turn status: " + error.message, "error"); });
  }

  function processEvent(rawEvent, turnId) {
    // Any message at all, including a malformed one, proves the stream is
    // genuinely alive right now -- clear "reconnecting"/"lost" and stand
    // down the fallback poll before even trying to parse it.
    setConnectionState("connected");
    stopReconcilePoll();
    var event;
    try { event = JSON.parse(rawEvent.data); } catch (_error) { return; }
    if (!event || typeof event !== "object") return;
    var type = event.type || event.method || "";
    if (type === "nexus/turnSnapshot") {
      var snapshot = event.turn || {};
      renderTurnResult(snapshot);
      closeStream(); reconcileAfterTerminal(turnId); return;
    }
    if (type === "nexus/noTurn") { setExecutionState("idle", "Backend reports no active turn."); closeStream(); return; }
    if (type === "nexus/replayGap") {
      closeStream();
      showStatus("Some live events expired; reconciling with the backend record.", "loading");
      reconcileAfterTerminal(turnId);
      return;
    }
    if (type === "nexus/turnEnded") { closeStream(); reconcileAfterTerminal(turnId); loadApprovals(); return; }
    if (/requestApproval$/.test(type)) { logActivityEvent("Approval requested — see Approvals."); loadApprovals(); return; }
    if (/turn\/started$/.test(type)) setExecutionState("running", "Turn started.");
    if (/turn\/(failed|interrupted|completed)$/.test(type)) {
      closeStream(); reconcileAfterTerminal(turnId); return;
    }
    if (/item\/(started|updated|completed)$/.test(type)) {
      var item = (event.params && event.params.item) || {};
      if (item.type && item.type !== "agentMessage" && item.type !== "userMessage") {
        var finished = /item\/completed$/.test(type) && item.type === "commandExecution";
        var output = finished && typeof item.aggregatedOutput === "string" ? item.aggregatedOutput.trim() : "";
        logActivityEvent(
          item.type + (item.command ? ": " + item.command : "") +
          (finished && typeof item.exitCode === "number" ? " → exit " + item.exitCode : "") +
          (finished && typeof item.exitCode !== "number" && item.status ? " → " + item.status : "") +
          (/item\/completed$/.test(type) && item.type === "fileChange" && item.path ? " " + item.path + " → " + (item.status || "done") : "") +
          (output ? "\n" + output.slice(-600) : "")
        );
      }
    }
  }

  function connectEvents(turnId) {
    closeStream();
    if (typeof window.EventSource !== "function") {
      showStatus("This browser does not support EventSource; status will be reconciled via polling.", "error");
      state.activeTurnId = turnId;
      startReconcilePoll(turnId);
      return;
    }
    state.activeTurnId = turnId;
    setExecutionState("running", "Listening for turn " + turnId + ".");
    setConnectionState("connected");
    setComposerState();
    var url = API + "/dev/sessions/" + encodeURIComponent(state.session.session_id) + "/events?turn_id=" + encodeURIComponent(turnId);
    var source = new window.EventSource(url); state.source = source;
    source.onmessage = function (event) { processEvent(event, turnId); };
    source.onerror = function () {
      if (state.source !== source) return;
      // A browser SSE disconnect never cancels the backend turn -- it only
      // stops this client's own live stream. EventSource normally
      // auto-reconnects with Last-Event-ID on its own (readyState
      // CONNECTING), but a live smoke test found that auto-reconnect can
      // itself get starved by unrelated same-origin traffic and never
      // actually recover (readyState stays CONNECTING, or the browser
      // gives up and closes it -- CLOSED). Either way, the bounded
      // fallback poll below is what actually guarantees F2.4B's
      // acceptance criterion: the real backend state is surfaced without
      // a manual refresh, regardless of whether the stream itself heals.
      var stillTrying = source.readyState === window.EventSource.CONNECTING;
      setConnectionState(stillTrying ? "reconnecting" : "lost");
      showStatus(
        stillTrying
          ? "Stream disconnected; the browser is retrying. The turn itself keeps running on the server."
          : "Stream connection lost. Still watching the server's own record of this turn.",
        "loading"
      );
      startReconcilePoll(turnId);
    };
  }

  function closeStream() {
    if (state.source) { state.source.close(); state.source = null; }
    stopReconcilePoll();
    setConnectionState("idle");
  }

  document.addEventListener("DOMContentLoaded", function () {
    var devReturn = $("dev-return"); if (devReturn) devReturn.addEventListener("click", exit);
    var projectSelect = $("dev-project-select"); if (projectSelect) projectSelect.addEventListener("change", function () { chooseProject(projectSelect.value); });
    var createButton = $("dev-create-session"); if (createButton) createButton.addEventListener("click", createSession);
    var backendSelect = $("dev-backend-select"); if (backendSelect) backendSelect.addEventListener("change", function () {
      state.newSessionBackend = ["omniroute", "claude_native"].indexOf(backendSelect.value) >= 0 ? backendSelect.value : "codex_native";
      loadModels(state.newSessionBackend).catch(function (error) { showStatus("Could not load models: " + error.message, "error"); });
    });
    var refreshReview = $("dev-review-refresh"); if (refreshReview) refreshReview.addEventListener("click", loadReview);
    var createReview = $("dev-create-review"); if (createReview) createReview.addEventListener("click", createIntegrationReview);
    var preflightReview = $("dev-integration-preflight"); if (preflightReview) preflightReview.addEventListener("click", checkIntegrationPreflight);
    var rejectReview = $("dev-reject-review"); if (rejectReview) rejectReview.addEventListener("click", rejectIntegrationReview);
    var applyReview = $("dev-apply-review"); if (applyReview) applyReview.addEventListener("click", applyIntegrationReview);
    var applyConfirm = $("dev-apply-confirm"); if (applyConfirm) applyConfirm.addEventListener("change", updateIntegrationControls);
    var modelSelect = $("dev-model-select"); if (modelSelect) modelSelect.addEventListener("change", function () {
      state.selectedModel = modelSelect.value || null; populateEffortSelect(); updateMissionControls();
    });
    var effortSelect = $("dev-effort-select"); if (effortSelect) effortSelect.addEventListener("change", function () { state.selectedEffort = effortSelect.value || null; });
    var modelSearch = $("dev-model-search"); if (modelSearch) modelSearch.addEventListener("input", populateModelSelect);
    var runButton = $("dev-run-button"); if (runButton) runButton.addEventListener("click", runTurn);
    var stopButton = $("dev-stop-button"); if (stopButton) stopButton.addEventListener("click", interruptTurn);
    var missionStart = $("dev-mission-start"); if (missionStart) missionStart.addEventListener("click", startAutonomousMission);
    var missionStop = $("dev-mission-stop"); if (missionStop) missionStop.addEventListener("click", interruptAutonomousMission);
    var missionProfile = $("dev-mission-test-profile"); if (missionProfile) missionProfile.addEventListener("change", updateMissionControls);
    setComposerState(); updateReviewControls(); renderIntegrationSummary();
  });

  window.NexusDev = { enter: enter, exit: exit };
})();
