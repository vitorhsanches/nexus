/* Nexus Local Mission Board V1 - dynamic frontend. */

(function () {
  "use strict";

  var COLUMNS = ["BACKLOG", "READY", "RUNNING", "REVIEW", "DONE", "FAILED"];

  var els = {
    missions: document.getElementById("mission-list"),
    board: document.getElementById("board"),
    agents: document.getElementById("agent-list"),
    sessions: document.getElementById("session-list"),
    createMission: document.getElementById("create-mission-btn"),
    modal: document.getElementById("mission-modal"),
    form: document.getElementById("mission-form"),
    title: document.getElementById("mission-title"),
    description: document.getElementById("mission-description"),
    submit: document.getElementById("mission-submit"),
    cancel: document.getElementById("mission-cancel"),
    modalClose: document.getElementById("mission-modal-close"),
    refresh: document.getElementById("refresh-btn"),
    updated: document.getElementById("updated-at"),
    missionCount: document.getElementById("mission-count"),
    taskCount: document.getElementById("task-count"),
    agentCount: document.getElementById("agent-count"),
    sessionCount: document.getElementById("session-count"),
    operationalRunList: document.getElementById("operational-run-list"),
    operationalRunDetail: document.getElementById("operational-run-detail"),
    operationalRunCount: document.getElementById("operational-run-count"),
  };

  var cells = {};
  COLUMNS.forEach(function (name) {
    cells[name] = document.querySelector('[data-cards-for="' + name + '"]');
  });

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function statusClass(status) {
    return String(status || "").toLowerCase();
  }

  function cardMeta(label, value) {
    var span = el("span", "meta-item");
    span.textContent = (label || "") + (value !== null && value !== undefined ? " " + value : "");
    return span;
  }

  function countsBar(mission) {
    var bar = el("div", "counts");
    var board = mission.board || {};
    (board.columns || []).forEach(function (col) {
      var pill = el("span", "count-pill count-" + String(col.name || "").toLowerCase());
      pill.textContent = (col.name || "").slice(0, 3) + ":" + (col.task_ids || []).length;
      bar.appendChild(pill);
    });
    return bar;
  }

  function renderMissions(missions) {
    els.missions.innerHTML = "";
    if (!missions || !missions.length) {
      els.missions.appendChild(el("div", "empty", "No missions registered."));
      return;
    }
    missions.forEach(function (m) {
      var card = el("div", "mission-card");
      card.appendChild(el("div", "mission-title", m.title || m.mission_id));
      var meta = el("div", "mission-meta");
      meta.appendChild(el("span", "mission-id", m.mission_id));
      meta.appendChild(el("span", "status-pill status-pill " + statusClass(m.status), m.status));
      card.appendChild(meta);
      card.appendChild(countsBar(m));
      els.missions.appendChild(card);
    });
  }

  function renderBoard(tasks) {
    var buckets = {};
    COLUMNS.forEach(function (name) { buckets[name] = []; });
    (tasks || []).forEach(function (t) {
      var col = t.board_column || "BACKLOG";
      if (buckets[col]) buckets[col].push(t);
      else buckets["BACKLOG"].push(t);
    });

    COLUMNS.forEach(function (name) {
      cells[name].innerHTML = "";
      var countEl = document.querySelector('[data-count-for="' + name + '"]');
      var list = buckets[name] || [];
      if (countEl) countEl.textContent = String(list.length);

      if (!list.length) {
        cells[name].appendChild(el("div", "empty", "No tasks"));
        return;
      }
      list.forEach(function (t) {
        var card = el("div", "task-card priority-" + String(t.priority || "medium").toLowerCase());
        card.appendChild(el("div", "task-title", t.title || t.task_id));
        if (t.assigned_agent) card.appendChild(el("div", "task-assignee", "Agent: " + t.assigned_agent));
        var meta = el("div", "task-meta");
        meta.appendChild(el("span", "task-id", t.task_id));
        meta.appendChild(el("span", "task-status", t.status));
        card.appendChild(meta);
        if (t.acceptance_criteria && t.acceptance_criteria.length) {
          card.appendChild(el("div", "task-caps ac", "AC: " + t.acceptance_criteria.length));
        }
        if (t.status !== "COMPLETED" && t.status !== "FAILED") {
          var actions = el("div", "task-actions");
          var btn = el("button", "execute-btn", "EXECUTE");
          btn.type = "button";
          btn.setAttribute("data-task-id", t.task_id);
          btn.addEventListener("click", function () { executeTask(t.task_id); });
          actions.appendChild(btn);
          card.appendChild(actions);
        }
        cells[name].appendChild(card);
      });
    });
  }

  function renderAgents(agents) {
    els.agents.innerHTML = "";
    if (!agents || !agents.length) {
      els.agents.appendChild(el("div", "empty", "No active agents."));
      return;
    }
    agents.forEach(function (a) {
      var card = el("div", "agent-card");
      card.appendChild(el("div", "agent-name", a.name || a.agent_id));
      var meta = el("div", "agent-meta");
      meta.appendChild(el("span", "agent-id", a.agent_id));
      meta.appendChild(el("span", "status-pill status-pill " + statusClass(a.status), a.status));
      meta.appendChild(el("span", "agent-model", a.model || "-"));
      card.appendChild(meta);
      if (a.capabilities && a.capabilities.length) {
        var caps = el("div", "task-caps");
        a.capabilities.forEach(function (c) {
          caps.appendChild(el("span", "cap", c));
        });
        card.appendChild(caps);
      }
      els.agents.appendChild(card);
    });
  }

  function renderSessions(sessions) {
    els.sessions.innerHTML = "";
    if (!sessions || !sessions.length) {
      els.sessions.appendChild(el("div", "empty", "No execution sessions."));
      return;
    }
    sessions.slice().reverse().forEach(function (s) {
      var card = el("div", "session-card session-" + statusClass(s.status));
      card.appendChild(el("div", "session-title", (s.task_title || s.task_id) + " / " + (s.agent_name || s.agent_id)));
      var meta = el("div", "session-meta");
      meta.appendChild(el("span", "session-id", s.session_id + " | " + s.status));
      card.appendChild(meta);
      if (s.current_action) card.appendChild(el("div", "session-meta", "Action: " + s.current_action));
      if (s.error) card.appendChild(el("div", "session-meta", "Error: " + s.error));
      els.sessions.appendChild(card);
    });
  }

  var selectedRunId = null;

  function formatRoutingBadge(routing) {
    if (!routing) return "legacy / routing metadata unavailable";
    var parts = [];
    if (routing.model) parts.push("model: " + routing.model);
    if (routing.provider) parts.push("provider: " + routing.provider);
    if (routing.effort) parts.push("effort: " + routing.effort);
    if (routing.execution_path) parts.push("path: " + routing.execution_path);
    if (routing.degraded) parts.push("degraded");
    if (routing.reason) parts.push("reason: " + routing.reason);
    return parts.length ? parts.join(" | ") : "routing metadata unavailable";
  }

  function renderAgentLineage(agents) {
    var wrap = el("div", "operational-agent-lineage");
    if (!agents || !agents.length) {
      wrap.appendChild(el("div", "empty", "No agents recorded for this run."));
      return wrap;
    }

    var attempts = {
      Worker: 0,
      ManagerReview: 0,
    };

    agents.forEach(function (a, index) {
      if (index > 0) {
        wrap.appendChild(el("div", "operational-lineage-arrow", "\u2193"));
      }

      var roleClass = String(a.role || "agent").toLowerCase();
      var roleLabel = a.role || "Agent";

      if (a.role === "Worker") {
        attempts.Worker += 1;
        roleLabel = "Worker attempt " + attempts.Worker;
      } else if (a.role === "ManagerReview") {
        attempts.ManagerReview += 1;
        roleLabel = "Reviewer attempt " + attempts.ManagerReview;
      } else if (a.role === "Manager") {
        roleLabel = "Manager";
      }

      var card = el(
        "div",
        "operational-agent-card operational-agent-" +
          statusClass(a.status) +
          " operational-role-" +
          roleClass
      );

      var head = el("div", "operational-agent-head");
      head.appendChild(el("span", "operational-agent-role", roleLabel));
      head.appendChild(el("span", "status-pill " + statusClass(a.status), a.status));
      card.appendChild(head);

      var meta = el("div", "operational-agent-meta");
      meta.appendChild(cardMeta("id:", a.id));
      meta.appendChild(cardMeta("provider:", a.provider || "-"));
      meta.appendChild(cardMeta("model:", a.model || "-"));
      meta.appendChild(cardMeta("effort:", a.effort || "-"));

      if (a.parent_agent_id) {
        meta.appendChild(cardMeta("parent:", a.parent_agent_id));
      }

      if (a.branch && a.role === "Worker") {
        meta.appendChild(cardMeta("branch:", a.branch));
      }

      if (a.worktree && a.role === "Worker") {
        meta.appendChild(cardMeta("worktree:", a.worktree));
      }

      card.appendChild(meta);

      if (a.role === "ManagerReview") {
        card.appendChild(
          el(
            "div",
            "operational-routing",
            formatRoutingBadge(a.reviewer_routing)
          )
        );
      }

      wrap.appendChild(card);
    });

    return wrap;
  }

  function renderOperationalDetail(run) {
    els.operationalRunDetail.innerHTML = "";
    if (!run) {
      els.operationalRunDetail.appendChild(el("div", "empty", "Select a run to view details."));
      return;
    }
    var head = el("div", "operational-detail-head");
    head.appendChild(el("div", "operational-detail-title", run.id));
    head.appendChild(el("span", "status-pill " + statusClass(run.status), run.status));
    els.operationalRunDetail.appendChild(head);
    var meta = el("div", "operational-detail-meta");
    meta.appendChild(cardMeta("project:", run.project_name || run.project_id || "-"));
    meta.appendChild(cardMeta("intent:", run.intent || "-"));
    meta.appendChild(cardMeta("risk:", run.risk || "-"));
    meta.appendChild(cardMeta("created:", run.created_at || "-"));

    if (run.started_at) {
      meta.appendChild(cardMeta("started:", run.started_at));
    }

    if (run.finished_at) {
      meta.appendChild(cardMeta("finished:", run.finished_at));
    }

    els.operationalRunDetail.appendChild(meta);

    if (run.input) {
      var inputText = String(run.input);
      if (inputText.length > 360) inputText = inputText.slice(0, 357) + "...";
      els.operationalRunDetail.appendChild(
        el("div", "operational-detail-input", inputText)
      );
    }

    if (run.result) {
      var resultText = String(run.result);
      if (resultText.length > 360) resultText = resultText.slice(0, 357) + "...";
      els.operationalRunDetail.appendChild(
        el("div", "operational-detail-result", resultText)
      );
    }

    els.operationalRunDetail.appendChild(renderAgentLineage(run.agents));
  }

  function selectOperationalRun(runId) {
    selectedRunId = runId;
    return fetchJson("/api/operational/runs/" + encodeURIComponent(runId))
      .then(function (data) {
        renderOperationalDetail(data.run);
      })
      .catch(function (err) {
        renderOperationalDetail(null);
        showOperationalError(err && err.message ? err.message : String(err));
      });
  }

  function renderOperationalRuns(runs) {
    els.operationalRunList.innerHTML = "";
    if (els.operationalRunCount) {
      els.operationalRunCount.textContent = runs && runs.length ? "(" + runs.length + ")" : "";
    }
    if (!runs || !runs.length) {
      els.operationalRunList.appendChild(el("div", "empty", "No operational runs recorded."));
      renderOperationalDetail(null);
      return;
    }
    var orderedRuns = runs.slice().sort(function (a, b) {
      return String(b.created_at || "").localeCompare(String(a.created_at || ""));
    });

    var stillPresent = orderedRuns.some(function (r) {
      return r.id === selectedRunId;
    });

    if (!stillPresent) {
      selectedRunId = orderedRuns[0].id;
    }

    orderedRuns.forEach(function (run) {
      var card = el("div", "operational-run-card" + (run.id === selectedRunId ? " selected" : ""));
      var head = el("div", "operational-run-head");
      head.appendChild(el("span", "operational-run-id", run.id));
      head.appendChild(el("span", "status-pill " + statusClass(run.status), run.status));
      card.appendChild(head);
      var meta = el("div", "operational-run-meta");
      meta.appendChild(cardMeta("project:", run.project_name || run.project_id || "-"));
      meta.appendChild(cardMeta("intent:", run.intent || "-"));
      card.appendChild(meta);
      card.addEventListener("click", function () {
        Array.prototype.forEach.call(
          els.operationalRunList.querySelectorAll(".operational-run-card"),
          function (c) { c.classList.remove("selected"); }
        );
        card.classList.add("selected");
        selectOperationalRun(run.id);
      });
      els.operationalRunList.appendChild(card);
    });

    if (selectedRunId) {
      selectOperationalRun(selectedRunId);
    } else {
      renderOperationalDetail(null);
    }
  }

  function showOperationalError(message) {
    var banner = els.operationalRunList.querySelector(".error-banner");
    if (banner) banner.remove();
    if (!message) return;
    els.operationalRunList.insertBefore(
      el("div", "error-banner", "Operational data load failed: " + message),
      els.operationalRunList.firstChild
    );
  }

  function loadOperational() {
    return fetchJson("/api/operational/runs")
      .then(function (data) {
        renderOperationalRuns(data.runs);
      })
      .catch(function (err) {
        showOperationalError(err && err.message ? err.message : String(err));
      });
  }

  function setCount(id, value) {
    if (id) id.textContent = value ? "(" + value + ")" : "";
  }

  function renderSummary(summary) {
    setCount(els.missionCount, summary.missions);
    setCount(els.taskCount, summary.tasks);
    setCount(els.agentCount, summary.agents);
    setCount(els.sessionCount, summary.sessions);
  }

  function showError(message) {
    var banner = document.querySelector(".error-banner");
    if (banner) banner.remove();
    if (!message) return;
    var node = el("div", "error-banner", "Data load failed: " + message);
    var layout = document.querySelector(".layout");
    if (layout && layout.firstChild) layout.insertBefore(node, layout.firstChild);
  }

  function fetchJson(path) {
    return fetch(path).then(function (res) {
      if (!res.ok) throw new Error(path + " -> HTTP " + res.status);
      return res.json();
    });
  }

  function loadAll() {
    els.refresh.disabled = true;
    return Promise.all([
      fetchJson("/api/missions"),
      fetchJson("/api/tasks"),
      fetchJson("/api/agents"),
      fetchJson("/api/sessions"),
      fetchJson("/api/summary"),
    ]).then(function (results) {
      renderMissions(results[0].missions);
      renderBoard(results[1].tasks);
      renderAgents(results[2].agents);
      renderSessions(results[3].sessions);
      renderSummary(results[4]);
      els.updated.textContent = "Updated " + new Date().toLocaleTimeString();
      showError("");
    }).catch(function (err) {
      showError(err && err.message ? err.message : String(err));
    }).then(function () {
      return loadOperational();
    }).finally(function () {
      els.refresh.disabled = false;
    });
  }

  function openModal() {
    els.modal.hidden = false;
    els.title.value = "";
    els.description.value = "";
    els.title.focus();
  }

  function closeModal() {
    els.modal.hidden = true;
  }

  function createMission() {
    var title = els.title.value.trim();
    if (!title) return;
    els.submit.disabled = true;
    var payload = {
      title: title,
      description: els.description.value.trim(),
    };
    return fetch("/api/missions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (res) {
      if (!res.ok) throw new Error("/api/missions -> HTTP " + res.status);
      return res.json();
    }).then(function () {
      closeModal();
      return loadAll();
    }).catch(function (err) {
      showError("Mission creation failed: " + (err && err.message ? err.message : String(err)));
    }).finally(function () {
      els.submit.disabled = false;
    });
  }

  els.createMission.addEventListener("click", function () { openModal(); });
  els.cancel.addEventListener("click", function () { closeModal(); });
  els.modalClose.addEventListener("click", function () { closeModal(); });
  els.modal.addEventListener("click", function (e) {
    if (e.target === els.modal) closeModal();
  });
  els.form.addEventListener("submit", function (e) {
    e.preventDefault();
    createMission();
  });

  function executeTask(taskId) {
    return fetch("/api/tasks/" + taskId + "/execute", { method: "POST" }).then(function (res) {
      if (!res.ok) throw new Error("/api/tasks/" + taskId + "/execute -> HTTP " + res.status);
      return res.json();
    }).then(function () {
      return loadAll();
    }).catch(function (err) {
      showError("Execution failed: " + (err && err.message ? err.message : String(err)));
    });
  }
  els.refresh.addEventListener("click", function () { loadAll(); });
  loadAll();
  setInterval(loadAll, 15000);
})();