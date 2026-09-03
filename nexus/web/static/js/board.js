/* Nexus Local Mission Board V1 - dynamic frontend. */

(function () {
  "use strict";

  var COLUMNS = ["BACKLOG", "READY", "RUNNING", "REVIEW", "DONE", "FAILED"];

  var els = {
    missions: document.getElementById("mission-list"),
    board: document.getElementById("board"),
    agents: document.getElementById("agent-list"),
    sessions: document.getElementById("session-list"),
    refresh: document.getElementById("refresh-btn"),
    updated: document.getElementById("updated-at"),
    missionCount: document.getElementById("mission-count"),
    taskCount: document.getElementById("task-count"),
    agentCount: document.getElementById("agent-count"),
    sessionCount: document.getElementById("session-count"),
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
        card.appendChild(el("div", "task-caps", a.capabilities.map(function (c) {
          var cap = el("span", "cap");
          cap.textContent = c;
          return cap;
        })));
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
    }).finally(function () {
      els.refresh.disabled = false;
    });
  }

  els.refresh.addEventListener("click", function () { loadAll(); });
  loadAll();
  setInterval(loadAll, 15000);
})();