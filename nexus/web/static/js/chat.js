/* Nexus Chat API client and DOM renderer. All conversation content is server-backed. */
(function () {
  "use strict";

  var API = "/api";
  var MAX_TOOL_TEXT = 6000;
  var HISTORY_PAGE_LIMIT = 50;
  var HISTORY_AUTO_PAGE_CAP = 10;
  // Bound for recoverMissingHistory()'s forward pagination after a replay
  // gap: generous enough to reach the end of a long-running thread, never
  // unbounded.
  var HISTORY_RECOVERY_PAGE_CAP = 20;
  var NEAR_BOTTOM_PX = 96;
  // Bounds for reconcileAfterTerminal()'s status poll. This bounded browser
  // poll is only for reconciling a missing SSE terminal event; it does not
  // control or cancel the backend turn, whose timeout is configured
  // independently by NEXUS_CHAT_TURN_TIMEOUT_SECONDS.
  var RECONCILE_BASE_DELAY_MS = 250;
  var RECONCILE_MAX_DELAY_MS = 5000;
  var RECONCILE_MAX_ATTEMPTS = 60;
  var state = {
    previousMode: null, previousView: null, previousNavId: null, previousFocus: null,
    projects: [], sessions: [], projectId: "", session: null, turns: [],
    source: null, activeTurnId: null, sending: false, uncertainSend: false,
    uncertainCreateProjects: new Set(),
    bubbles: new Map(), toolCards: new Map(), seenEventIds: new Map(),
    // Dedup key: Codex's own stable item id. Shared between history loads and
    // live SSE so the same historical item is never rendered twice.
    seenItemIds: new Set(),
    // Turn IDs whose authoritative history fetch failed with the active-turn
    // concurrency conflict (HTTP 409 HISTORY_UNAVAILABLE) rather than a real
    // error -- reconcileAfterTerminal() retries the fetch for these once the
    // turn is confirmed terminal, instead of settling for just response_text.
    pendingHistorySync: new Set(),
    historyNextCursor: null, historyLoading: false, historyUnavailable: false,
    modelOptions: [], defaultModel: null, modelsLoaded: false, selectedNewSessionModel: null,
    selectedNewSessionEffort: null, favoriteModels: new Set(), activityStartedAt: null,
    // setInterval handle ticking the elapsed-time display once per second
    // while a turn is running; always cleared via stopElapsedTicker() so
    // leaving Chat, switching sessions or finishing a turn never leaves a
    // timer running against a torn-down DOM.
    elapsedTimer: null,
    pendingDelta: "", deltaFrame: 0, loadGeneration: 0,
  };

  var $ = function (id) { return document.getElementById(id); };
  var chatReturn = $("chat-return");
  var chatInput = $("chat-message-input");
  var contextToggle = $("chat-context-toggle");
  var chatLayout = $("chat-layout");
  var chatLoadOlder = $("chat-load-older");
  var sessionStateNodes = document.querySelectorAll("[data-session-state]");

  function setViewBadge(mode) {
    var badge = $("view-mode-badge");
    if (!badge) return;
    badge.textContent = mode === "chat" ? "CHAT" : mode === "program" ? "PROGRAM VIEW" : mode === "run" ? "RUN VIEW" : "OVERVIEW";
    badge.className = "view-mode-badge vm-" + (mode || "none");
  }

  function setActiveNavigation(targetId) {
    document.querySelectorAll(".nav-item").forEach(function (item) {
      var active = item.id === targetId;
      item.classList.toggle("active", active);
      if (active) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current");
    });
  }

  function enter() {
    if (document.body.dataset.view !== "chat") {
      state.previousView = document.body.dataset.view || "overview";
      state.previousMode = (window.NexusMode && window.NexusMode.mode) || "overview";
      var activeNav = document.querySelector(".nav-item.active");
      state.previousNavId = activeNav ? activeNav.id : null;
      state.previousFocus = document.activeElement || null;
    }
    document.body.dataset.view = "chat";
    document.body.setAttribute("data-view", "chat");
    setViewBadge("chat");
    setActiveNavigation("nav-chat");
    if (chatReturn) chatReturn.hidden = state.previousMode === "overview";
    var chatView = $("ws-chat");
    if (chatView) chatView.setAttribute("aria-hidden", "false");
    if (chatInput && !chatInput.disabled) chatInput.focus({ preventScroll: true });
  }

  function exit() {
    if (document.body.dataset.view !== "chat") return;
    var previousView = state.previousView || "overview";
    var previousMode = state.previousMode || "overview";
    document.body.dataset.view = previousView;
    document.body.setAttribute("data-view", previousView);
    if (window.NexusMode) window.NexusMode.mode = previousMode;
    setViewBadge(previousMode);
    setActiveNavigation(state.previousNavId);
    var chatView = $("ws-chat");
    if (chatView) chatView.setAttribute("aria-hidden", "true");
    if (chatReturn) chatReturn.hidden = true;
    if (state.previousFocus && typeof state.previousFocus.focus === "function") state.previousFocus.focus();
    state.previousView = state.previousMode = state.previousNavId = state.previousFocus = null;
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

  // Field names treated as sensitive for generic key=value / key: value
  // redaction. The pattern is anchored directly on these names (with a
  // non-key boundary required on both sides) rather than matching any
  // generic key and classifying it afterward: that earlier approach let
  // an unrelated outer label (e.g. "aggregatedOutput: password=...") be
  // consumed as one opaque match, hiding the embedded sensitive
  // "password=" pair from ever being seen as its own match. Anchoring on
  // the sensitive names means "token" is found wherever it legitimately
  // occurs, while a compound like "qa-marker-token" still never matches
  // (the boundary check rejects "token" preceded by "-"). Known
  // limitation: a sensitive field whose name isn't one of these exact
  // forms (e.g. "oauthToken") will not be redacted here — widen this
  // list rather than falling back to a bare "token" substring match,
  // which is what caused the original over-redaction.
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
    // Bearer tokens first, so the generic key=value pass below can see that
    // the value was already handled and avoid redacting it a second time.
    str = str.replace(/\b(Bearer\s+)[A-Za-z0-9._~+\/-]+=*/gi, "$1[redacted]");
    str = str.replace(KEY_VALUE_PATTERN, function (full, lead, key, separator, val) {
      // Already redacted (or a Bearer marker handled above) — don't double-redact.
      // Must match the WHOLE value, not just contain the marker: a value like
      // "[redacted]EXAMPLE_SECRET" is not fully redacted and must still be
      // collapsed, or an attacker could smuggle a real secret past this guard
      // by prefixing it with a fake "[redacted]" marker.
      if (/^bearer$/i.test(val) || /^\[redacted[^\]]*\]$/i.test(val)) return full;
      return lead + key + separator + "[redacted]";
    });
    str = str.replace(/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g, "[redacted private key]");
    str = str.replace(/\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b/g, "[redacted token]");
    return str;
  }

  function showStatus(message, kind) {
    var status = $("chat-status-text");
    var banner = $("chat-backend-status");
    if (status) status.textContent = redacted(message);
    if (banner) banner.dataset.state = kind || "info";
  }

  function setSessionListState(nextState, message) {
    sessionStateNodes.forEach(function (node) {
      var active = node.getAttribute("data-session-state") === nextState;
      node.hidden = !active;
      if (active && message) node.textContent = redacted(message);
    });
  }

  function setComposerState() {
    var projectEnabled = state.session && state.projects.some(function (project) { return project.id === state.session.project_id; });
    var usable = Boolean(projectEnabled) && !state.sending && !state.activeTurnId && !state.uncertainSend;
    if (chatInput) chatInput.disabled = !projectEnabled;
    var send = $("chat-send-button");
    if (send) send.disabled = !usable;
    var stop = $("chat-stop-button");
    if (stop) stop.disabled = !state.session || !state.activeTurnId;
    var projectSelect = $("chat-project-select");
    if (projectSelect) projectSelect.disabled = !state.projects.length || Boolean(state.sending);
    var modelSelect = $("chat-model-select");
    if (modelSelect) {
      modelSelect.disabled = !state.modelsLoaded || Boolean(state.sending || state.activeTurnId);
      if (state.selectedNewSessionModel) modelSelect.value = state.selectedNewSessionModel;
    }
    var effortSelect = $("chat-effort-select");
    if (effortSelect) effortSelect.disabled = !state.modelsLoaded || Boolean(state.sending || state.activeTurnId) || !state.selectedNewSessionModel;
    var search = $("chat-model-search"); if (search) search.disabled = !state.modelsLoaded || Boolean(state.sending || state.activeTurnId);
    var favorite = $("chat-model-favorite"); if (favorite) favorite.disabled = !state.selectedNewSessionModel || !state.modelsLoaded;
    var modelPlaceholder = $("chat-model-placeholder");
    var modelNote = $("chat-session-model-note");
    if (modelNote) modelNote.textContent = state.session ?
      "This session's model and effort are fixed. Create a new conversation to use the selected configuration." :
      "Selection applies to the next new conversation.";
    if (modelPlaceholder && state.session) {
      var conversationModel = state.session.requested_model || state.session.model;
      modelPlaceholder.textContent = conversationModel ?
        "Conversation model: " + conversationModel + (state.session.requested_effort ? " · effort " + state.session.requested_effort : " · effort default/legacy") + (state.session.provider ? " · " + state.session.provider : "") :
        "Conversation model: legacy session (provider/model reported after first turn)";
    }
    var help = $("chat-composer-help");
    if (help) {
      help.textContent = state.uncertainSend ? "Send result is uncertain; reconcile or reload before sending again." :
        state.activeTurnId ? "A turn is running. You can interrupt it with Stop." :
          state.session ? "Enter to send · Shift+Enter for a new line." : "Select or create a session to send a message.";
    }
  }

  function setEmpty(title, description, note) {
    var transcript = $("chat-transcript");
    if (transcript) transcript.replaceChildren();
    var empty = document.createElement("div");
    empty.className = "chat-empty-state";
    var mark = document.createElement("div");
    mark.className = "chat-empty-mark"; mark.setAttribute("aria-hidden", "true"); mark.textContent = "N";
    var heading = document.createElement("h2"); heading.textContent = title;
    var copy = document.createElement("p"); copy.textContent = description;
    var hint = document.createElement("span"); hint.className = "chat-empty-note"; hint.textContent = note || "";
    empty.append(mark, heading, copy, hint);
    if (transcript) transcript.appendChild(empty);
  }

  function isNearBottom(transcript) {
    if (!transcript) return true;
    return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < NEAR_BOTTOM_PX;
  }

  function scrollToBottom(transcript) {
    if (transcript) transcript.scrollTop = transcript.scrollHeight;
  }

  function makeMessage(role, text, id) {
    var transcript = $("chat-transcript");
    if (!transcript) return null;
    var empty = transcript.querySelector(".chat-empty-state");
    if (empty) empty.remove();
    var wasNear = isNearBottom(transcript);
    var article = document.createElement("article");
    article.className = "chat-message chat-message-" + role;
    if (id) article.dataset.turnId = id;
    var label = document.createElement("div"); label.className = "chat-message-label";
    label.textContent = role === "user" ? "You" : role === "assistant" ? "Nexus" : "System";
    var body = document.createElement("div"); body.className = "chat-message-body";
    body.textContent = redacted(text);
    article.append(label, body);
    transcript.appendChild(article);
    // Never force-scroll a user reading older history; always follow the
    // user's own just-sent message or live output while already at/near
    // the bottom.
    if (wasNear || role === "user") scrollToBottom(transcript);
    if (id && role === "assistant") state.bubbles.set(id, body);
    return body;
  }

  function makeTurnStatus(status, turnId) {
    var transcript = $("chat-transcript");
    if (!transcript) return null;
    var normalized = (status || "").toLowerCase();
    var badge = document.createElement("div");
    badge.className = "chat-turn-status chat-turn-status-" + normalized;
    if (turnId) badge.dataset.turnId = turnId;
    badge.textContent = normalized;
    transcript.appendChild(badge);
    return badge;
  }

  function sessionStatusLabel(session) {
    var parts = [session.status || "UNKNOWN"];
    if (session.model || session.provider) parts.push([session.provider, session.model].filter(Boolean).join(" · "));
    return parts.join(" — ");
  }

  function syncSessionContext() {
    var sessionValue = $("chat-session-value");
    var workspace = $("chat-workspace-detail");
    var detail = $("chat-session-detail");
    var model = $("chat-model-placeholder");
    var activeProject = state.projects.find(function (project) { return project.id === state.projectId; });
    if (workspace) workspace.textContent = activeProject ? activeProject.name + " · " + activeProject.path : "Select a registered project.";
    if (sessionValue) sessionValue.textContent = state.session ? state.session.session_id + " · " + state.session.status : (state.projectId ? "No session selected" : "Select a project");
    if (detail) detail.textContent = state.session ? sessionStatusLabel(state.session) : "No Chat session selected.";
    if (model) model.textContent = state.session && (state.session.provider || state.session.model) ?
      "Provider/model: " + [state.session.provider, state.session.model].filter(Boolean).join(" · ") : "Provider/model: reported after first turn";
  }

  function renderSessions() {
    var container = $("chat-session-groups");
    if (!container) return;
    container.replaceChildren();
    if (!state.sessions.length) {
      setSessionListState("empty");
      return;
    }
    setSessionListState("ready");
    var groups = new Map();
    state.sessions.forEach(function (session) {
      var projectName = (state.projects.find(function (project) { return project.id === session.project_id; }) || {}).name || session.project_id;
      if (!groups.has(session.project_id)) groups.set(session.project_id, { name: projectName, sessions: [] });
      groups.get(session.project_id).sessions.push(session);
    });
    groups.forEach(function (group, projectId) {
      var section = document.createElement("section"); section.className = "chat-session-group";
      var heading = document.createElement("h3"); heading.textContent = group.name; section.appendChild(heading);
      group.sessions.forEach(function (session) {
        var button = document.createElement("button");
        button.type = "button"; button.className = "chat-session-item";
        button.classList.toggle("selected", state.session && state.session.session_id === session.session_id);
        button.setAttribute("aria-pressed", state.session && state.session.session_id === session.session_id ? "true" : "false");
        button.textContent = session.session_id + " · " + (session.status || "UNKNOWN");
        button.title = sessionStatusLabel(session);
        button.addEventListener("click", function () { selectSession(session.session_id); });
        section.appendChild(button);
      });
      container.appendChild(section);
    });
  }

  function populateProjects(projects) {
    state.projects = (projects || []).filter(function (project) { return project.enabled; });
    var select = $("chat-project-select");
    if (!select) return;
    select.replaceChildren();
    var prompt = document.createElement("option"); prompt.value = "";
    prompt.textContent = state.projects.length ? "Select a registered project…" : "No enabled projects";
    select.appendChild(prompt);
    state.projects.forEach(function (project) {
      var option = document.createElement("option"); option.value = project.id; option.textContent = project.name;
      select.appendChild(option);
    });
    select.disabled = state.projects.length === 0;
    var topProject = $("top-project");
    var current = topProject && topProject.textContent.trim();
    var match = state.projects.find(function (project) { return project.id === current || project.name === current; });
    if (match) { state.projectId = match.id; select.value = match.id; }
    updateNewButton();
    syncSessionContext();
  }

  function updateNewButton() {
    var button = $("chat-new-conversation");
    if (!button) return;
    var uncertain = state.projectId && state.uncertainCreateProjects.has(state.projectId);
    button.disabled = !state.projectId || state.sending || uncertain;
    button.title = !state.projectId ? "Select a project to start a conversation" :
      state.sending ? "Creating a conversation…" :
        uncertain ? "Creation outcome is uncertain. Refresh sessions and select a confirmed session before retrying." :
          "Create a new conversation for the selected project";
  }

  function loadProjects() {
    showStatus("Loading registered projects…", "loading");
    var projectsRequest = request("/projects").then(function (payload) {
      populateProjects(payload && payload.projects);
      return loadSessions();
    });
    var modelsRequest = loadModels();
    return Promise.allSettled([projectsRequest, modelsRequest]).then(function (results) {
      if (results[0].status === "rejected") {
        var error = results[0].reason;
        showStatus("Could not load projects: " + error.message, "error");
        setSessionListState("error", "Could not load projects: " + error.message);
        throw error;
      }
      if (results[1].status === "rejected") {
        var errorNode = $("chat-model-error");
        if (errorNode) { errorNode.hidden = false; errorNode.textContent = "Model choices unavailable; existing sessions remain accessible."; }
        showStatus("Chat connected; model choices unavailable. Existing sessions can still be opened.", "connected");
      } else {
        showStatus("Chat backend connected. Select a project or reopen a conversation.", "connected");
      }
      return results[0].value;
    });
  }

  function loadModels() {
    var backend = state.newSessionBackend || "codex_native";
    return request("/chat/models?backend=" + encodeURIComponent(backend) + "&refresh=" + (state.forceModelRefresh ? "true" : "false")).then(function (models) {
        state.forceModelRefresh = false;
        state.modelsBackend = backend;
        var select = $("chat-model-select");
        if (select) {
          select.replaceChildren();
          state.modelOptions = (models.models || []).filter(function (model) { return model.selectable === true && typeof model.id === "string"; });
          state.defaultModel = models.default_model;
          state.favoriteModels = new Set();
          state.selectedNewSessionModel = state.modelOptions.some(function (item) { return item.id === models.default_model; }) ? models.default_model : null;
          state.selectedNewSessionEffort = null;
        }
        state.modelsLoaded = Boolean(state.modelOptions.length);
        populateModelSelect();
        populateEffortSelect();
        var modelError = $("chat-model-error");
        if (modelError) { modelError.hidden = !models.catalog_error; modelError.textContent = models.catalog_error || ""; }
        return models;
    });
  }

  function populateModelSelect() {
    var select = $("chat-model-select"); if (!select) return;
    var search = $("chat-model-search"); var query = search ? search.value.trim().toLowerCase() : "";
    var previous = state.selectedNewSessionModel;
    var filter = $("chat-model-filter"); var favoritesOnly = filter && filter.value === "favorites";
    var matches = state.modelOptions.filter(function (model) {
      return (!query || (model.id + " " + model.name).toLowerCase().includes(query)) && (!favoritesOnly || state.favoriteModels.has(model.id));
    });
    // Keep native select controls quick with large catalogs. Search narrows
    // the complete in-memory catalog, so every advertised route remains reachable.
    var visible = matches.slice(0, 50);
    select.replaceChildren();
    visible.forEach(function (model) {
      var option = document.createElement("option"); option.value = model.id;
      option.textContent = (state.favoriteModels.has(model.id) ? "★ " : "") + model.name + " · " + model.id +
        (model.availability === "codex-native" ? (model.is_default ? " · Codex default" : "") : " · cost unknown · activation unverified");
      if (model.reason) option.title = model.reason;
      select.appendChild(option);
    });
    var resultCount = $("chat-model-result-count");
    if (resultCount) resultCount.textContent = matches.length > visible.length ?
      "Showing " + visible.length + " of " + matches.length + " routes. Refine search to find a route." :
      matches.length + (matches.length === 1 ? " route" : " routes");
    if (visible.some(function (model) { return model.id === previous; })) select.value = previous;
    updateFavoriteButton();
  }

  function populateEffortSelect() {
    var select = $("chat-effort-select"); if (!select) return;
    var model = state.modelOptions.find(function (item) { return item.id === state.selectedNewSessionModel; });
    var selected = state.selectedNewSessionEffort;
    select.replaceChildren();
    var options = model && Array.isArray(model.supported_efforts) ? model.supported_efforts : [];
    var defaultOption = document.createElement("option"); defaultOption.value = "";
    defaultOption.textContent = "App-Server default (not explicitly confirmed)"; select.appendChild(defaultOption);
    options.forEach(function (effort) {
      var option = document.createElement("option"); option.value = effort; option.textContent = effort; select.appendChild(option);
    });
    if (selected && options.includes(selected)) select.value = selected;
    else if (model && model.default_effort && options.includes(model.default_effort)) {
      state.selectedNewSessionEffort = model.default_effort; select.value = model.default_effort;
    } else { state.selectedNewSessionEffort = null; select.value = ""; }
  }

  function updateFavoriteButton() {
    var button = $("chat-model-favorite"); if (!button) return;
    var favorite = state.favoriteModels.has(state.selectedNewSessionModel);
    button.textContent = favorite ? "★ Favorited" : "☆ Favorite";
    button.setAttribute("aria-pressed", favorite ? "true" : "false");
  }

  // Four canonical, user-facing execution states -- never a fabricated
  // percentage, step count, or made-up intermediate phase. "idle" is a
  // fifth, internal-only resting state (no turn to report) and is never
  // itself claimed as one of the four.
  var EXECUTION_STATE_LABELS = { running: "Running", completed: "Completed", failed: "Failed", interrupted: "Interrupted", idle: "Idle" };

  // Accepts whatever vocabulary the caller has on hand -- Nexus's own
  // turn.status ("PENDING"/"RUNNING"/"COMPLETED"/"FAILED"/"CANCELLED"),
  // Codex's native lifecycle event/status strings ("turn/completed",
  // "interrupted", ...), or a short free-form phrase -- and classifies it
  // into exactly one of the four canonical states. Unrecognized input
  // resolves to null so a caller can choose not to change the displayed
  // state rather than show something misleading.
  function normalizeExecutionState(raw) {
    var text = String(raw || "").toLowerCase();
    if (/fail/.test(text)) return "failed";
    if (/cancel|interrupt/.test(text)) return "interrupted";
    if (/complet/.test(text)) return "completed";
    if (/pending|running|started/.test(text)) return "running";
    return null;
  }

  function renderElapsed() {
    var elapsed = $("chat-activity-elapsed");
    if (!elapsed) return;
    elapsed.textContent = state.activityStartedAt ?
      "Elapsed: " + Math.max(0, Math.floor((Date.now() - state.activityStartedAt) / 1000)) + "s" :
      "Elapsed: —";
  }

  function stopElapsedTicker() {
    if (state.elapsedTimer) { clearInterval(state.elapsedTimer); state.elapsedTimer = null; }
  }

  // Ticks once a second only while a turn is genuinely running, so
  // "Elapsed" stays live during long silent tool/thinking periods instead
  // of only updating whenever an SSE event happens to arrive. Always
  // derived from a real timestamp (see setExecutionState()), never a
  // guess.
  function startElapsedTicker() {
    stopElapsedTicker();
    renderElapsed();
    state.elapsedTimer = setInterval(renderElapsed, 1000);
  }

  // Appends one line to the real, backend-reported tool-activity log
  // without touching the primary execution-state label -- a tool running
  // is detail *within* an already-"running" turn, not a new overall
  // state, so it must never overwrite "Running" with e.g. "Tool activity".
  function logActivityEvent(detail) {
    if (!detail) return;
    var list = $("chat-activity-events");
    if (!list) return;
    var item = document.createElement("li");
    item.textContent = redacted(detail);
    list.appendChild(item);
    while (list.children.length > 30) list.removeChild(list.children[0]);
  }

  // The single place that sets the primary "Executing / Completed / Failed
  // / Interrupted" indicator. startedAtIso, when given, is the backend's
  // own turn.started_at (reliable even across a reconnect/reload); absent
  // that, elapsed time is only ever computed from a timestamp this client
  // itself recorded at the moment it knows the turn is running -- never
  // fabricated.
  function setExecutionState(rawState, detail, startedAtIso) {
    var key = normalizeExecutionState(rawState) || "idle";
    var node = $("chat-activity-status");
    if (node) {
      node.textContent = EXECUTION_STATE_LABELS[key];
      node.className = "chat-activity-status chat-activity-status-" + key;
    }
    var model = $("chat-activity-model");
    if (model) model.textContent = "Model: " + redacted((state.session && (state.session.model || state.session.requested_model)) || "not reported");
    if (key === "running") {
      var parsed = startedAtIso ? Date.parse(startedAtIso) : NaN;
      state.activityStartedAt = !isNaN(parsed) ? parsed : (state.activityStartedAt || Date.now());
      startElapsedTicker();
    } else {
      // Freeze the final elapsed duration on a terminal state (it stays
      // meaningful -- "it took Ns") instead of resetting to "--"; only a
      // genuinely idle state (no turn to report) clears it.
      stopElapsedTicker();
      if (key === "idle") state.activityStartedAt = null;
      renderElapsed();
    }
    if (detail) logActivityEvent(detail);
  }

  function loadSessions() {
    setSessionListState("loading");
    return request("/chat/sessions").then(function (payload) {
      var sessions = payload && Array.isArray(payload.sessions) ? payload.sessions.slice() : [];
      // A list request can have started before a successful create POST and
      // resolve afterward with a stale snapshot. Keep locally known,
      // server-confirmed sessions visible until a later list includes them.
      var locallyKnown = state.sessions.slice();
      if (state.session && !locallyKnown.some(function (item) { return item.session_id === state.session.session_id; })) {
        locallyKnown.unshift(state.session);
      }
      locallyKnown.forEach(function (known) {
        if (!sessions.some(function (item) { return item.session_id === known.session_id; })) sessions.push(known);
      });
      state.sessions = sessions;
      renderSessions();
      return state.sessions;
    }).catch(function (error) {
      setSessionListState("error", "Could not load conversations: " + error.message);
      showStatus("Could not load conversations: " + error.message, "error");
      throw error;
    });
  }

  function updateSessionUrl(sessionId) {
    if (!window.history || !window.location) return;
    var url = new URL(window.location.href);
    if (sessionId) url.searchParams.set("chat_session", sessionId); else url.searchParams.delete("chat_session");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
  }

  function closeStream() {
    if (state.source) { state.source.close(); state.source = null; }
    if (state.deltaFrame) { cancelAnimationFrame(state.deltaFrame); state.deltaFrame = 0; }
    state.pendingDelta = "";
  }

  function resetTranscript() {
    closeStream();
    state.bubbles.clear(); state.toolCards.clear(); state.seenEventIds.clear(); state.activeTurnId = null;
    state.seenItemIds.clear(); state.pendingHistorySync.clear();
    state.historyNextCursor = null; state.historyLoading = false; state.historyUnavailable = false;
    state.activityStartedAt = null; setExecutionState("idle");
    var activityList = $("chat-activity-events"); if (activityList) activityList.replaceChildren();
    var transcript = $("chat-transcript"); if (transcript) transcript.replaceChildren();
    var loadOlder = $("chat-load-older"); if (loadOlder) loadOlder.hidden = true;
  }

  function turnFromList(turn) {
    var status = (turn.status || "").toUpperCase();
    if (turn.response_text) makeMessage("assistant", turn.response_text, turn.turn_id);
    else if (status === "FAILED" || status === "CANCELLED") {
      makeMessage("system", (turn.error_code ? turn.error_code + ": " : "") + (turn.error_message || "Turn did not complete."));
    }
    if (status === "RUNNING" || status === "PENDING") {
      state.activeTurnId = turn.turn_id;
      makeMessage("system", "Reconnected to active turn " + turn.turn_id + ". The persisted user prompt is available from the history API.");
    }
  }

  // -- Backend T4 history adapter --------------------------------------
  //
  // Isolates every assumption about the committed history contract
  // (nexus/chat/service.py::get_history, nexus/web/routes.py::get_chat_history)
  // in one place: GET /chat/sessions/{id}/history?turn_id=&cursor=&limit=
  // -> {session_id, items:[{turn_id,item_id,type,text?,command?,exit_code?,
  // output?,status?,name?,tool?,server?,error?,started_at_ms?,
  // completed_at_ms?}], turns:[{turn_id,status?,started_at?,completed_at?,
  // error?}], next_cursor, source}.
  //
  // IMPORTANT, verified against the real backend (not assumed): items are
  // returned sort_direction="asc" (oldest first) starting from the very
  // beginning of the thread when no cursor is given, and next_cursor pages
  // FORWARD (towards more recent items) -- there is no "start from the end
  // and page backward" mode. "Restoring the conversation" therefore means
  // auto-paginating forward from the start, bounded by
  // HISTORY_AUTO_PAGE_CAP, with a manual "Load more messages" control for
  // anything beyond that cap -- never prepending "older" content above
  // what's shown.
  // No turn_id parameter: the history API's turn_id filter expects a
  // Codex-native turn id (verified against the App-Server's own turn list --
  // see nexus/chat/service.py::_verify_codex_turn), which chat.js never has.
  // The Nexus turn id used everywhere else here (SSE subscription,
  // interrupt, session/turn authorization) is a different identifier space
  // -- sending it as turn_id here 404s (TURN_NOT_FOUND) even after the turn
  // has fully completed. Always fetch by cursor only; see
  // recoverMissingHistory() for how a specific turn's items get found.
  function fetchHistory(sessionId, params) {
    var query = [];
    if (params && params.cursor) query.push("cursor=" + encodeURIComponent(params.cursor));
    query.push("limit=" + (params && params.limit ? params.limit : HISTORY_PAGE_LIMIT));
    return request("/chat/sessions/" + encodeURIComponent(sessionId) + "/history?" + query.join("&"));
  }

  function itemRole(type) {
    if (type === "userMessage") return "user";
    if (type === "agentMessage") return "assistant";
    return null;
  }

  function isToolItemType(type) {
    return Boolean(type) && !["userMessage", "agentMessage", "reasoning"].includes(type);
  }

  // Renders one historical item exactly once (dedup key: Codex's own stable
  // item_id, shared with live SSE rendering in processEvent()). Never
  // fabricates content: only ever reflects what the history API returned.
  function renderHistoryItem(item) {
    if (!item || !item.item_id) return;
    if (state.seenItemIds.has(item.item_id)) return;
    state.seenItemIds.add(item.item_id);
    var role = itemRole(item.type);
    if (role === "user") { makeMessage("user", item.text || "", item.item_id); return; }
    if (role === "assistant") { makeMessage("assistant", item.text || "", item.item_id); return; }
    if (isToolItemType(item.type)) {
      var card = getOrCreateToolCard(item.item_id, item.type);
      var text = toolText(item) || (item.output ? "output: " + item.output : "");
      if (text) setToolCardText(card, text, false);
      return;
    }
    // Unknown item type: show nothing invented -- only a neutral marker so
    // the gap in the transcript isn't silently unexplained.
    makeMessage("system", "Historical item of type \"" + (item.type || "unknown") + "\" is not yet rendered.");
  }

  // Backend T4's history page also carries lightweight turn lifecycle
  // metadata (turns:[{turn_id,status,...}]) -- render a status badge for
  // any non-trivial outcome (interrupted/cancelled/failed) exactly once.
  function renderHistoryTurnStatuses(turns) {
    if (!Array.isArray(turns)) return;
    turns.forEach(function (turn) {
      if (!turn || !turn.turn_id) return;
      var status = String(turn.status || "").toLowerCase();
      if (!["interrupted", "cancelled", "failed"].includes(status)) return;
      var marker = "turn-status-" + turn.turn_id;
      if (state.seenItemIds.has(marker)) return;
      state.seenItemIds.add(marker);
      makeTurnStatus(status, turn.turn_id);
      if (turn.error) makeMessage("system", turn.error);
    });
  }

  function renderHistoryPage(payload) {
    var items = (payload && Array.isArray(payload.items)) ? payload.items : [];
    items.forEach(renderHistoryItem);
    renderHistoryTurnStatuses(payload && payload.turns);
    state.historyNextCursor = (payload && payload.next_cursor) || null;
    var loadMore = $("chat-load-older");
    if (loadMore) { loadMore.hidden = !state.historyNextCursor; loadMore.disabled = false; }
    return items.length;
  }

  // Auto-paginates forward from wherever state.historyNextCursor currently
  // is (null on the first call -- the true start of the thread), bounded
  // by remainingPages so one reopened session can never trigger unbounded
  // network/DOM work. Returns the total item count actually rendered.
  function autoLoadHistory(sessionId, remainingPages, countSoFar) {
    return fetchHistory(sessionId, { cursor: state.historyNextCursor }).then(function (payload) {
      var total = countSoFar + renderHistoryPage(payload);
      if (state.historyNextCursor && remainingPages > 1) {
        return autoLoadHistory(sessionId, remainingPages - 1, total);
      }
      return total;
    });
  }

  // Recovers items missing from the transcript after an SSE replay gap, by
  // paginating FORWARD from the session's current known frontier
  // (state.historyNextCursor) to the real end of the persisted thread --
  // never by filtering on turn_id (see fetchHistory() above for why that
  // would 404). Items are oldest-to-newest, so the turn that triggered the
  // gap (always the latest/still-active one) is always reachable this way,
  // even if it lands beyond the first page. Dedup is by Codex's own stable
  // item_id (state.seenItemIds, via renderHistoryPage/renderHistoryItem,
  // shared with live SSE rendering), so pages already rendered are skipped
  // even when re-fetched. Bounded by HISTORY_RECOVERY_PAGE_CAP as
  // defense-in-depth against an endless pagination loop (the backend
  // itself already rejects a non-advancing cursor). Bails out without
  // rendering if the session was switched mid-recovery (loadGeneration),
  // so switching sessions can never append an earlier session's items.
  // Resolves with the exact DOM nodes THIS call appended (never a
  // text-based search across the whole transcript) -- the caller uses
  // this to tell a just-recovered, authoritative node for the turn it is
  // reconciling apart from an unrelated, merely textually-identical node
  // belonging to some other turn (see dropOptimisticDuplicates()).
  // Resolves with {nodes, capped}. capped is true only when pagination
  // stopped because HISTORY_RECOVERY_PAGE_CAP was exhausted while
  // state.historyNextCursor was STILL non-null -- i.e. real, newer items
  // are known to remain unread. This is never conflated with a genuine,
  // complete recovery (cursor reaching null on its own): callers must
  // surface a capped result as a visible, recoverable state (see
  // finishReconciliation()), never report it as a clean success.
  function recoverMissingHistory(sessionId) {
    var generation = state.loadGeneration;
    var transcript = $("chat-transcript");
    var startCount = transcript ? transcript.children.length : 0;
    var capped = false;
    function step(remainingPages) {
      return fetchHistory(sessionId, { cursor: state.historyNextCursor, limit: HISTORY_PAGE_LIMIT }).then(function (payload) {
        if (generation !== state.loadGeneration) return;
        renderHistoryPage(payload);
        if (state.historyNextCursor) {
          if (remainingPages > 1) return step(remainingPages - 1);
          capped = true;
        }
      });
    }
    return step(HISTORY_RECOVERY_PAGE_CAP).then(function () {
      // transcript.children is a live HTMLCollection in a real browser,
      // not an Array -- materialize it before slicing.
      return {
        nodes: transcript && generation === state.loadGeneration ? Array.prototype.slice.call(transcript.children, startCount) : [],
        capped: generation === state.loadGeneration && capped,
      };
    });
  }

  function loadMoreHistory() {
    if (!state.session || !state.historyNextCursor || state.historyLoading) return;
    state.historyLoading = true;
    var button = $("chat-load-older");
    if (button) { button.disabled = true; button.textContent = "Loading…"; }
    fetchHistory(state.session.session_id, { cursor: state.historyNextCursor, limit: HISTORY_PAGE_LIMIT })
      .then(function (payload) {
        state.historyLoading = false;
        renderHistoryPage(payload);
        if (button) button.textContent = "Load more messages";
      }).catch(function (error) {
        state.historyLoading = false;
        if (button) { button.disabled = false; button.textContent = "Load more messages"; }
        showStatus("Could not load more messages: " + error.message, "error");
      });
  }

  function renderHistoryFallback(detail, note) {
    var turns = Array.isArray(detail.turns) ? detail.turns.slice().reverse() : [];
    state.turns = turns;
    if (!turns.length) {
      setEmpty("Start a repository conversation", "Ask a question about this registered project's code.", "No messages have been recorded in this session yet.");
      return null;
    }
    makeMessage("system", note);
    turns.forEach(turnFromList);
    return turns.find(function (turn) { return ["RUNNING", "PENDING"].includes((turn.status || "").toUpperCase()); });
  }

  function renderHistory(detail) {
    resetTranscript();
    if (!detail.codex_thread_id) {
      // Never call the history API for a session that has not started a
      // Codex thread yet -- there is nothing to fetch, and "no history" is
      // not the same as "history unavailable".
      var running = renderHistoryFallback(detail, "New conversation. Send a message to start.");
      if (running) connectEvents(running.turn_id, running.started_at);
      return;
    }
    autoLoadHistory(detail.session_id, HISTORY_AUTO_PAGE_CAP, 0).then(function (total) {
      if (!total) {
        var running = renderHistoryFallback(detail, "No persisted history items yet.");
        if (running) connectEvents(running.turn_id, running.started_at);
        return;
      }
      var runningTurn = (detail.turns || []).find(function (turn) { return ["RUNNING", "PENDING"].includes((turn.status || "").toUpperCase()); });
      if (runningTurn) { state.activeTurnId = runningTurn.turn_id; connectEvents(runningTurn.turn_id, runningTurn.started_at); }
    }).catch(function (error) {
      // Friendly, explicit "history unavailable" -- never fabricated, and
      // never a hard failure: fall back to the turn-summary view the
      // session detail endpoint already provides.
      state.historyUnavailable = true;
      var reason = error && error.status === 404 ?
        "The history API is not available yet; showing recorded turn summaries only." :
        "Could not load full history (" + (error ? error.message : "unknown error") + "); showing recorded turn summaries only.";
      var running = renderHistoryFallback(detail, reason);
      if (running) connectEvents(running.turn_id, running.started_at);
    });
  }

  function selectSession(sessionId) {
    var generation = ++state.loadGeneration;
    showStatus("Opening session " + sessionId + "…", "loading");
    return request("/chat/sessions/" + encodeURIComponent(sessionId)).then(function (detail) {
      if (generation !== state.loadGeneration) return;
      state.session = detail;
      state.projectId = detail.project_id;
      state.uncertainCreateProjects.delete(detail.project_id);
      var select = $("chat-project-select"); if (select) select.value = detail.project_id;
      state.uncertainSend = false;
      renderHistory(detail);
      syncSessionContext(); setComposerState(); updateNewButton(); renderSessions();
      updateSessionUrl(sessionId);
      showStatus("Session " + sessionId + " · " + detail.status + ". Read-only sandbox.", "connected");
      if (!state.projects.some(function (project) { return project.id === detail.project_id; })) {
        showStatus("This session's project is not currently enabled. History is available read-only; sending is disabled.", "error");
      }
    }).catch(function (error) {
      if (generation !== state.loadGeneration) return;
      showStatus("Could not open session: " + error.message, "error");
      setEmpty("Session unavailable", error.message, "Choose another session or refresh the session list.");
    });
  }

  function chooseProject(projectId) {
    state.projectId = projectId;
    state.session = null; state.turns = []; state.uncertainSend = false;
    ++state.loadGeneration;
    resetTranscript();
    updateNewButton(); syncSessionContext(); setComposerState(); renderSessions(); updateSessionUrl(null);
    if (!projectId) setEmpty("Choose a project", "Select a registered project to start or reopen a conversation.", "Only enabled projects are available.");
    else setEmpty("Project selected", "Create a conversation or choose an existing session from the sidebar.", "Session metadata is stored by Nexus; transcript content comes from the Chat API.");
  }

  function createSession() {
    if (state.sending || (state.projectId && state.uncertainCreateProjects.has(state.projectId))) return;
    if (!state.projectId) {
      var projectError = $("chat-model-error");
      if (projectError) { projectError.hidden = false; projectError.textContent = "Choose an enabled project before creating a conversation."; }
      return;
    }
    if (!state.modelsLoaded || !state.selectedNewSessionModel || !state.modelOptions.some(function (item) { return item.id === state.selectedNewSessionModel; })) {
      var errorNode = $("chat-model-error");
      if (errorNode) { errorNode.hidden = false; errorNode.textContent = "Model choices are unavailable. Reload model discovery before creating a conversation."; }
      return;
    }
    var projectId = state.projectId;
    var selectedModel = state.selectedNewSessionModel;
    var selectedEffort = state.selectedNewSessionEffort || null;
    var selectedRoute = state.modelOptions.find(function (item) { return item.id === selectedModel; });
    var costConsent = $("chat-unknown-cost-confirm");
    var needsCostConsent = !selectedRoute || !["free", "known"].includes(selectedRoute.cost_status);
    if (needsCostConsent && (!costConsent || !costConsent.checked)) {
      var costError = $("chat-model-error");
      if (costError) { costError.hidden = false; costError.textContent = "This route has unknown cost. Confirm the unknown-cost option before creating a session."; }
      return;
    }
    var modelError = $("chat-model-error");
    if (modelError) { modelError.hidden = true; modelError.textContent = ""; }
    state.sending = true; updateNewButton(); setComposerState();
    showStatus("Creating a session for the selected project…", "loading");
    request("/chat/sessions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_id: projectId, backend: state.modelsBackend || "codex_native", model: selectedModel, effort: selectedEffort, allow_unknown_cost: Boolean(needsCostConsent && costConsent && costConsent.checked) }) })
      .then(function (session) {
        if (!session || !session.session_id || session.project_id !== projectId ||
            session.requested_model !== selectedModel || (session.requested_effort || null) !== selectedEffort) {
          throw new Error("Backend did not confirm the selected project, model, and effort.");
        }
        // The POST response is the persisted session record. Activate it
        // immediately instead of making selection depend on a follow-up list
        // request and a second detail request succeeding.
        state.session = session;
        state.projectId = projectId;
        state.turns = Array.isArray(session.turns) ? session.turns : [];
        state.uncertainSend = false;
        state.uncertainCreateProjects.delete(projectId);
        var projectSelect = $("chat-project-select"); if (projectSelect) projectSelect.value = projectId;
        var existingIndex = state.sessions.findIndex(function (item) { return item.session_id === session.session_id; });
        if (existingIndex >= 0) state.sessions[existingIndex] = session;
        else state.sessions.unshift(session);
        renderSessions(); renderHistory(session); syncSessionContext();
        state.sending = false; updateNewButton(); setComposerState(); updateSessionUrl(session.session_id);
        showStatus("Session " + session.session_id + " · " + (session.status || "CREATED") + ". Read-only sandbox.", "connected");
        // Reconcile the sidebar in the background. The created session stays
        // selected and visible if the refresh fails.
        loadSessions().catch(function () {});
      }).catch(function (error) {
        var errorNode = $("chat-model-error");
        var detail = error.code ? error.code + ": " + error.message : error.message;
        if (errorNode) { errorNode.hidden = false; errorNode.textContent = detail; }
        if (!error.status || error.status >= 500) state.uncertainCreateProjects.add(projectId);
        state.sending = false; updateNewButton(); setComposerState();
        showStatus("Could not confirm session creation: " + detail + ". " +
          (state.uncertainCreateProjects.has(projectId) ? "The server may have created it; select it from the refreshed list before creating another." : "Review the error before retrying."), "error");
        loadSessions().catch(function () {});
      });
  }

  function idempotencyKey() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
    return "nexus-chat-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
  }

  function reconcileTurn(key, text) {
    return request("/chat/sessions/" + encodeURIComponent(state.session.session_id)).then(function (detail) {
      state.session = detail;
      var turn = (detail.turns || []).find(function (item) { return item.idempotency_key === key; });
      if (turn) {
        makeMessage("user", text, turn.turn_id);
        state.uncertainSend = false;
        if (turn.response_text) makeMessage("assistant", turn.response_text, turn.turn_id);
        else if (["PENDING", "RUNNING"].includes((turn.status || "").toUpperCase())) {
          state.activeTurnId = turn.turn_id; connectEvents(turn.turn_id, turn.started_at);
        } else if (turn.error_message) makeMessage("system", (turn.error_code || "TURN_FAILED") + ": " + turn.error_message);
        showStatus("Send reconciled with server turn " + turn.turn_id + ".", "connected");
      } else {
        // The server detail is authoritative: the idempotency key was not recorded.
        state.uncertainSend = false;
        showStatus("Server confirms no turn was recorded. You can safely send again.", "connected");
      }
      setComposerState(); syncSessionContext();
    }).catch(function (error) {
      state.uncertainSend = true;
      showStatus("Send outcome is uncertain and reconciliation failed: " + error.message + ". Reload this session before another send.", "error");
      setComposerState();
    });
  }

  function sendMessage() {
    var text = chatInput && chatInput.value.trim();
    if (!text || !state.session || state.sending || state.activeTurnId || state.uncertainSend) return;
    var key = idempotencyKey();
    state.sending = true; setComposerState(); showStatus("Submitting turn…", "loading");
    request("/chat/sessions/" + encodeURIComponent(state.session.session_id) + "/turns", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text, idempotency_key: key }),
    }).then(function (turn) {
      state.sending = false; chatInput.value = "";
      makeMessage("user", text, turn.turn_id);
      state.activeTurnId = turn.turn_id; setComposerState();
      state.turns.push(turn); connectEvents(turn.turn_id);
      showStatus("Turn " + turn.turn_id + " started. Read-only sandbox; provider is managed by OmniRoute.", "connected");
      loadSessions().catch(function () {});
    }).catch(function (error) {
      state.sending = false;
      if (error.status && error.status < 500) {
        showStatus((error.code ? error.code + ": " : "") + error.message, "error");
        if (error.status === 409 && state.session) {
          request("/chat/sessions/" + encodeURIComponent(state.session.session_id)).then(function (detail) {
            state.session = detail;
            var active = (detail.turns || []).find(function (turn) { return ["PENDING", "RUNNING"].includes((turn.status || "").toUpperCase()); });
            if (active) {
              state.activeTurnId = active.turn_id;
              makeMessage("system", "This session already has an active turn. Reconnected to " + active.turn_id + "; its historical user prompt is not returned by the API.");
              connectEvents(active.turn_id, active.started_at);
            }
            setComposerState(); syncSessionContext();
          }).catch(function () {});
        }
        setComposerState();
      } else {
        state.uncertainSend = true; setComposerState();
        showStatus("Connection interrupted after submit; checking whether the server recorded the turn…", "loading");
        reconcileTurn(key, text);
      }
    });
  }

  function flushDelta() {
    state.deltaFrame = 0;
    if (!state.pendingDelta || !state.activeTurnId) return;
    var transcript = $("chat-transcript");
    var wasNear = isNearBottom(transcript);
    var body = state.bubbles.get(state.activeTurnId);
    if (!body) body = makeMessage("assistant", "", state.activeTurnId);
    if (body) body.textContent = redacted(body.textContent + state.pendingDelta);
    state.pendingDelta = "";
    if (wasNear) scrollToBottom(transcript);
  }

  function eventItem(event) {
    var params = event.params || {};
    return params.item || params.itemUpdate || params;
  }

  function toolText(item) {
    var fields = ["command", "commandLine", "cwd", "filePath", "path", "searchQuery", "tool", "name", "arguments", "result", "changes", "aggregatedOutput", "output", "error", "exitCode", "status"];
    return fields.map(function (key) {
      var value = item[key];
      if (value == null || value === "") return "";
      var rendered = typeof value === "string" ? value : JSON.stringify(value);
      return key + ": " + rendered;
    }).filter(Boolean).join("\n").slice(0, MAX_TOOL_TEXT);
  }

  // Shared between live SSE tool events (renderToolEvent) and historical
  // tool items (renderHistoryItem) so both produce the identical,
  // consistent <details> card -- never two different visual treatments
  // for the same kind of record.
  function getOrCreateToolCard(key, type) {
    var card = state.toolCards.get(key);
    if (card) return card;
    var transcript = $("chat-transcript");
    var empty = transcript && transcript.querySelector(".chat-empty-state");
    if (empty) empty.remove();
    var details = document.createElement("details"); details.className = "chat-tool-event";
    var summary = document.createElement("summary"); summary.textContent = "Tool activity · " + redacted(type || "unknown");
    var pre = document.createElement("pre"); pre.className = "chat-tool-output";
    details.append(summary, pre);
    if (transcript) transcript.appendChild(details);
    card = { details: details, output: pre };
    state.toolCards.set(key, card);
    return card;
  }

  function setToolCardText(card, text, appendMode) {
    if (!card || !text) return;
    var transcript = $("chat-transcript");
    var wasNear = isNearBottom(transcript);
    if (appendMode) card.output.textContent = redacted((card.output.textContent + text).slice(-MAX_TOOL_TEXT));
    else card.output.textContent = redacted(text);
    if (wasNear) scrollToBottom(transcript);
  }

  function renderToolEvent(event) {
    var item = eventItem(event);
    var method = event.method || "tool/event";
    var type = item.type || item.kind || method.split("/").pop();
    var key = item.id || (event.params || {}).itemId || event.id || (method + "-" + Date.now());
    var card = getOrCreateToolCard(key, type);
    var isOutputDelta = /commandExecution\/outputDelta|fileChange\/outputDelta/.test(method);
    var text = isOutputDelta && typeof (event.params || {}).delta === "string" ? (event.params || {}).delta : toolText(item);
    if (!text && item.status) text = "Status: " + item.status;
    var file = item.filePath || item.path;
    logActivityEvent((/failed|error|denied/i.test(String(item.status || "") + " " + String(item.error || "")) ? "Tool error or block · " : "Tool activity · ") +
      redacted(type) + (item.status ? " · " + redacted(item.status) : "") + (typeof file === "string" ? " · " + redacted(file) : ""));
    if (text) setToolCardText(card, text, isOutputDelta);
    else if (!card.output.textContent) setToolCardText(card, "Backend reported " + redacted(method) + ". Details are not present in this event.", false);
    // A completed item (not just an output delta) is a stable, final
    // record -- mark it seen so a later historical reload of the same
    // turn never renders the identical card a second time.
    if (item.id && /item\/completed$/.test(method || "")) state.seenItemIds.add(item.id);
  }

  // generation defaults to the CURRENT state.loadGeneration on the first
  // call and is then threaded through every retry/recursion unchanged, so
  // a session switch mid-reconciliation (detected by comparing against
  // state.loadGeneration, bumped by selectSession()/chooseProject()) stops
  // this chain from mutating state.session or rendering into whatever
  // transcript is now on screen -- never an earlier session's items
  // appended to a later one.
  // The optimistic nodes sendMessage() (the user's own prompt) and
  // flushDelta()/processEvent() (the streamed assistant reply) create are
  // tagged with dataset.turnId = the NEXUS turn id (see makeMessage()).
  // recoverMissingHistory() renders the SAME content from the history API
  // keyed by Codex's own item_id instead (a different identifier space --
  // see fetchHistory() above), so if a gap swallowed the SSE events that
  // would normally have confirmed those optimistic nodes, recovery adds a
  // second, separately-identified copy of the identical message.
  //
  // newlyRenderedNodes (from recoverMissingHistory()'s own return value)
  // is the ONLY place this looks for "the authoritative copy" -- never a
  // text search across the whole transcript. Two different turns can
  // legitimately share byte-identical request_text/response_text (the
  // user repeating themselves, or a model giving the same short answer
  // twice); matching on text alone across the whole history would
  // sometimes bind THIS turn's state.bubbles entry to a DIFFERENT turn's
  // node, corrupting it on a later update. Scoping to exactly the nodes
  // this specific recovery call just appended removes that ambiguity:
  // they can only belong to the turn being reconciled right now.
  function dropOptimisticDuplicates(turnId, turn, newlyRenderedNodes) {
    var transcript = $("chat-transcript");
    if (!transcript || !turn || !newlyRenderedNodes || !newlyRenderedNodes.length) return;
    var findAuthoritative = function (role, text) {
      return newlyRenderedNodes.find(function (node) {
        return node.className && node.className.includes("chat-message-" + role) &&
          node.children[1] && node.children[1].textContent === text;
      });
    };
    // Bind state.bubbles to a just-recovered assistant node for this
    // turn's own response_text BEFORE the fallback in finishReconciliation
    // runs -- otherwise, if no optimistic node existed either (e.g. the
    // gap swallowed every live delta for this turn), state.bubbles.get()
    // finds nothing and that fallback creates a redundant second copy.
    if (turn.response_text != null && !state.bubbles.has(turnId)) {
      var authoritativeAssistant = findAuthoritative("assistant", redacted(turn.response_text));
      if (authoritativeAssistant) state.bubbles.set(turnId, authoritativeAssistant.children[1]);
    }
    // transcript.children is a live HTMLCollection in a real browser, not
    // an Array -- materialize it before filtering.
    var optimisticNodes = Array.prototype.filter.call(transcript.children, function (node) {
      return node.dataset && node.dataset.turnId === turnId && newlyRenderedNodes.indexOf(node) === -1;
    });
    optimisticNodes.forEach(function (node) {
      var isUser = node.className && node.className.includes("chat-message-user");
      var isAssistant = node.className && node.className.includes("chat-message-assistant");
      var text = node.children[1] && node.children[1].textContent;
      var isAuthoritativeText =
        (isUser && turn.request_text != null && text === redacted(turn.request_text)) ||
        (isAssistant && turn.response_text != null && text === redacted(turn.response_text));
      if (!isAuthoritativeText) return;
      var authoritativeSibling = findAuthoritative(isUser ? "user" : "assistant", text);
      if (!authoritativeSibling) return;
      node.remove();
      if (isAssistant) state.bubbles.set(turnId, authoritativeSibling.children[1]);
    });
  }

  // recoveredNodes/recoveryCapped carry forward the result of an EARLIER,
  // direct recoverMissingHistory() call (from the nexus/replayGap
  // handler), so dropOptimisticDuplicates() and the capped-recovery
  // notice below can use them too -- never re-derived by searching the
  // transcript for matching text (see dropOptimisticDuplicates() for why
  // that would be unsafe).
  function reconcileAfterTerminal(turnId, attempt, generation, recoveredNodes, recoveryCapped) {
    attempt = attempt || 0;
    if (generation === undefined) generation = state.loadGeneration;
    recoveredNodes = recoveredNodes || [];
    if (!state.session) return;
    var sessionId = state.session.session_id;
    request("/chat/sessions/" + encodeURIComponent(sessionId)).then(function (detail) {
      if (generation !== state.loadGeneration) return;
      state.session = detail;
      var turn = (detail.turns || []).find(function (item) { return item.turn_id === turnId; });
      if (!turn) return;
      var pending = ["PENDING", "RUNNING"].includes((turn.status || "").toUpperCase());
      if (pending && attempt < RECONCILE_MAX_ATTEMPTS) {
        state.activeTurnId = turnId; setComposerState();
        var delay = Math.min(RECONCILE_BASE_DELAY_MS * Math.pow(2, attempt), RECONCILE_MAX_DELAY_MS);
        setTimeout(function () { reconcileAfterTerminal(turnId, attempt + 1, generation, recoveredNodes, recoveryCapped); }, delay);
        return;
      }
      var finishReconciliation = function (recoveryError, nodes, capped) {
        dropOptimisticDuplicates(turnId, turn, nodes || recoveredNodes);
        if (turn.response_text != null) {
          var body = state.bubbles.get(turnId) || makeMessage("assistant", "", turnId);
          if (body) body.textContent = redacted(turn.response_text);
        } else if (turn.error_message) makeMessage("system", (turn.error_code || "TURN_FAILED") + ": " + turn.error_message);
        state.activeTurnId = pending ? turnId : null; state.sending = false; setComposerState(); syncSessionContext();
        if (!pending) setExecutionState(turn.status || "completed", turn.error_message || "Turn status confirmed by backend.");
        if (pending) {
          showStatus("The server still reports this turn as active. Sending remains disabled while status is uncertain.", "error");
          return;
        }
        if (recoveryError) {
          // Surfaced, never hidden behind a silent catch: the turn itself
          // did finish, but some of its history could not be recovered.
          showStatus("Turn " + turn.status.toLowerCase() + ", but some history could not be recovered (" + recoveryError.message + ").", "error");
        } else if (capped) {
          // HISTORY_RECOVERY_PAGE_CAP was exhausted while more items were
          // still known to remain (state.historyNextCursor stayed
          // non-null) -- never reported as a clean, complete success.
          // "Load more messages" (already driven by that same cursor, see
          // renderHistoryPage()) is the visible, recoverable path forward.
          showStatus("Turn " + turn.status.toLowerCase() + ", but history recovery stopped at its page limit with more messages still unread; use “Load more messages” to continue.", "error");
        } else {
          showStatus("Turn " + turn.status.toLowerCase() + ".", turn.status === "COMPLETED" ? "connected" : "error");
        }
        loadSessions().catch(function () {});
      };
      // The turn is no longer active, so the earlier replay-gap history
      // fetch's active-turn conflict (409 HISTORY_UNAVAILABLE) no longer
      // applies -- retry it now so tool output/messages dropped during the
      // gap are not permanently missing from the transcript.
      if (!pending && state.pendingHistorySync.has(turnId)) {
        state.pendingHistorySync.delete(turnId);
        recoverMissingHistory(sessionId).then(
          function (result) { finishReconciliation(null, result.nodes, result.capped); },
          function (error) { finishReconciliation(error, [], false); }
        );
        return;
      }
      finishReconciliation(null, recoveredNodes, recoveryCapped);
    }).catch(function (error) { if (generation === state.loadGeneration) showStatus("Could not refresh turn status: " + error.message, "error"); });
  }

  function processEvent(rawEvent, turnId) {
    var seq = rawEvent.lastEventId;
    var seen = state.seenEventIds.get(turnId);
    if (!seen) { seen = new Set(); state.seenEventIds.set(turnId, seen); }
    if (seq) { if (seen.has(seq)) return; seen.add(seq); }
    var event;
    try { event = JSON.parse(rawEvent.data); } catch (_error) { return; }
    if (!event || typeof event !== "object") return;
    var type = event.type || event.method || "";
    var params = event.params || {};
    if (type === "nexus/turnSnapshot") {
      var snapshot = event.turn || params.turn || {};
      setExecutionState(snapshot.status || "completed", snapshot.error_message || "Turn status reconciled from backend.");
      if (snapshot.response_text) {
        var bubble = state.bubbles.get(turnId) || makeMessage("assistant", "", turnId);
        if (bubble) bubble.textContent = redacted(snapshot.response_text);
      } else if (snapshot.error_message) makeMessage("system", (snapshot.error_code || "TURN_FAILED") + ": " + snapshot.error_message);
      closeStream(); reconcileAfterTerminal(turnId); return;
    }
    if (type === "nexus/noTurn") { setExecutionState("idle", "Backend reports no active turn."); closeStream(); reconcileAfterTerminal(turnId); return; }
    if (type === "nexus/replayGap") {
      closeStream();
      showStatus("Some live events expired. Fetching authoritative history for this turn…", "loading");
      // Required reconciliation: do not assume missing events never
      // happened -- fetch the real, persisted record for this turn before
      // falling back to the plain status reconciliation.
      if (state.session) {
        recoverMissingHistory(state.session.session_id).catch(function (error) {
          // The turn may still be RUNNING in its own App-Server process,
          // which legitimately refuses a concurrent thread_resume for
          // history (HTTP 409 HISTORY_UNAVAILABLE) -- not a permanent
          // failure, so defer the retry to reconcileAfterTerminal() once
          // the turn has actually finished. This is the ONLY error
          // swallowed here; anything else is a real failure and must be
          // surfaced, never hidden behind an unconditional empty catch.
          if (error && error.status === 409 && error.code === "HISTORY_UNAVAILABLE") {
            state.pendingHistorySync.add(turnId);
            return { nodes: [], capped: false };
          }
          throw error;
        }).catch(function (error) {
          showStatus("Could not recover full history for this turn: " + error.message + ".", "error");
          return { nodes: [], capped: false };
        }).then(function (result) { reconcileAfterTerminal(turnId, undefined, undefined, result.nodes, result.capped); });
      } else reconcileAfterTerminal(turnId);
      return;
    }
    if (type === "nexus/turnEnded") { closeStream(); reconcileAfterTerminal(turnId); return; }

    if (/turn\/started$/.test(type)) setExecutionState("running", "Backend reported turn started.");
    if (/turn\/(failed|interrupted|completed)$/.test(type)) {
      var lifecycleTurn = params.turn || {};
      setExecutionState(lifecycleTurn.status || type.split("/").pop(), lifecycleTurn.error && lifecycleTurn.error.message ? lifecycleTurn.error.message : "Backend reported turn " + type.split("/").pop() + ".");
    }

    if (/agentMessage.*delta|delta.*agentMessage/i.test(type)) {
      var delta = params.delta || params.textDelta || "";
      if (typeof delta === "string" && delta) {
        state.pendingDelta += delta;
        if (!state.deltaFrame) {
          state.deltaFrame = -1;
          var frameId = requestAnimationFrame(function () { state.deltaFrame = 0; flushDelta(); });
          if (state.deltaFrame === -1) state.deltaFrame = frameId;
        }
      }
      return;
    }
    if (/commandExecution\/outputDelta|fileChange\/outputDelta/.test(type)) {
      renderToolEvent(event); return;
    }
    if (/item\/completed$/.test(type) && eventItem(event).type === "userMessage") {
      // A reconnect can replay a turn's own user message; never duplicate
      // it if history or an earlier event already rendered this same item.
      var userItem = eventItem(event);
      if (userItem.id && state.seenItemIds.has(userItem.id)) return;
      if (userItem.id) state.seenItemIds.add(userItem.id);
      return; // The user's own message is already shown optimistically by sendMessage().
    }
    if (/item\/completed$/.test(type) && (eventItem(event).type === "agentMessage")) {
      var finalText = eventItem(event).text;
      var assistant = state.bubbles.get(turnId) || makeMessage("assistant", "", turnId);
      if (typeof finalText === "string" && assistant) assistant.textContent = redacted(finalText);
      if (eventItem(event).id) state.seenItemIds.add(eventItem(event).id);
      return;
    }
    if (/turn\/completed$/.test(type)) {
      closeStream(); reconcileAfterTerminal(turnId); return;
    }
    if (/item\/(started|updated|completed)$/.test(type) || /commandExecution|fileChange|tool/i.test(eventItem(event).type || "")) {
      if (eventItem(event).type !== "agentMessage") renderToolEvent(event);
    }
    if (/turn\/(started|failed|interrupted)$/.test(type)) showStatus("Turn event: " + type, "loading");
  }

  function connectEvents(turnId, startedAtIso) {
    closeStream();
    if (typeof window.EventSource !== "function") {
      showStatus("This browser does not support EventSource; turn status is being reconciled through the session API.", "error");
      reconcileAfterTerminal(turnId); return;
    }
    state.activeTurnId = turnId;
    setExecutionState("running", "Listening to backend events for " + turnId + ".", startedAtIso);
    setComposerState();
    var url = API + "/chat/sessions/" + encodeURIComponent(state.session.session_id) + "/events?turn_id=" + encodeURIComponent(turnId);
    var source = new window.EventSource(url); state.source = source;
    source.onmessage = function (event) { processEvent(event, turnId); };
    source.onerror = function () {
      if (state.source === source) showStatus("Stream disconnected; EventSource is reconnecting. Duplicate events are ignored.", "loading");
    };
  }

  function interruptTurn() {
    if (!state.session || !state.activeTurnId) return;
    var turnId = state.activeTurnId;
    var button = $("chat-stop-button"); if (button) button.disabled = true;
    request("/chat/sessions/" + encodeURIComponent(state.session.session_id) + "/interrupt?turn_id=" + encodeURIComponent(turnId), { method: "POST" })
      .then(function (result) {
        showStatus("Interrupt status: " + result.status + (result.error ? " · " + result.error : ""), result.status === "INTERRUPT_FAILED" ? "error" : "loading");
        if (result.status === "NOT_ACTIVE" || result.status === "INTERRUPT_FAILED") reconcileAfterTerminal(turnId);
      }).catch(function (error) { showStatus("Could not interrupt turn: " + error.message, "error"); setComposerState(); });
  }

  if (chatReturn) chatReturn.addEventListener("click", exit);
  if (contextToggle && chatLayout) contextToggle.addEventListener("click", function () {
    var expanded = contextToggle.getAttribute("aria-expanded") === "true";
    contextToggle.setAttribute("aria-expanded", expanded ? "false" : "true");
    contextToggle.textContent = expanded ? "Expand" : "Collapse";
    contextToggle.setAttribute("aria-label", expanded ? "Expand context panel" : "Collapse context panel");
    chatLayout.classList.toggle("context-collapsed", expanded);
  });
  if (chatInput) chatInput.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229 && !event.repeat) { event.preventDefault(); sendMessage(); }
  });
  var composer = $("chat-composer");
  if (composer) composer.addEventListener("submit", function (event) { event.preventDefault(); sendMessage(); });
  var projectSelect = $("chat-project-select");
  if (projectSelect) projectSelect.addEventListener("change", function () { chooseProject(projectSelect.value); });
  var backendSelect = $("chat-backend-select"); if (backendSelect) backendSelect.addEventListener("change", function () {
    state.newSessionBackend = backendSelect.value; state.modelsLoaded = false; state.forceModelRefresh = true;
    populateModelSelect(); setComposerState();
    loadModels().catch(function () {
      var node = $("chat-model-error"); if (node) { node.hidden = false; node.textContent = "Model choices unavailable for this backend."; }
    });
  });
  var modelSelect = $("chat-model-select"); if (modelSelect) modelSelect.addEventListener("change", function () {
    state.selectedNewSessionModel = modelSelect.value || null;
    state.selectedNewSessionEffort = null; populateEffortSelect(); updateFavoriteButton(); setComposerState();
  });
  var effortSelect = $("chat-effort-select"); if (effortSelect) effortSelect.addEventListener("change", function () { state.selectedNewSessionEffort = effortSelect.value || null; });
  var modelSearch = $("chat-model-search"); if (modelSearch) modelSearch.addEventListener("input", populateModelSelect);
  var modelFilter = $("chat-model-filter"); if (modelFilter) modelFilter.addEventListener("change", populateModelSelect);
  var favoriteButton = $("chat-model-favorite"); if (favoriteButton) favoriteButton.addEventListener("click", function () {
    if (!state.selectedNewSessionModel) return;
    if (state.favoriteModels.has(state.selectedNewSessionModel)) state.favoriteModels.delete(state.selectedNewSessionModel);
    else state.favoriteModels.add(state.selectedNewSessionModel);
    populateModelSelect(); updateFavoriteButton();
  });
  var newConversation = $("chat-new-conversation"); if (newConversation) newConversation.addEventListener("click", createSession);
  var stop = $("chat-stop-button"); if (stop) stop.addEventListener("click", interruptTurn);
  if (chatLoadOlder) chatLoadOlder.addEventListener("click", loadMoreHistory);

  var ready = loadProjects().then(function () {
    var requestedSession = new URLSearchParams(window.location.search).get("chat_session");
    if (requestedSession) return selectSession(requestedSession);
  }).catch(function () {});

  window.NexusChat = {
    enter: enter, exit: exit, setSessionListState: setSessionListState,
    loadProjects: loadProjects, loadSessions: loadSessions, selectSession: selectSession,
    createSession: createSession, sendMessage: sendMessage, ready: ready,
    loadMoreHistory: loadMoreHistory, fetchHistory: fetchHistory,
    historyAutoPageCap: HISTORY_AUTO_PAGE_CAP,
    historyRecoveryPageCap: HISTORY_RECOVERY_PAGE_CAP,
  };

  setComposerState(); syncSessionContext();
})();
