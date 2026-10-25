# Windows setup

## Requirements

- Windows PowerShell 5.1 or PowerShell 7.
- Python 3.10 or newer.
- Node.js for the frontend checks.
- At least one native model backend, signed in locally (both are optional and
  independent; Nexus starts without either):
  - Codex CLI, for Codex Native.
  - Claude Code CLI (`claude.exe`), for Claude Native.
- OmniRoute is optional. Install and configure it only if you want the
  OmniRoute backend; it is not needed for a default start.

Nexus does not include Codex, Claude or OmniRoute credentials. Sign in and configure
those tools separately on each computer; do not copy authentication files from
another machine.

## Clone and install

From PowerShell:

    git clone https://github.com/vitorhsanches/nexus.git
    cd nexus
    py -3 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install --upgrade pip
    python -m pip install -r requirements-dev.txt

requirements-dev.txt includes the runtime dependencies plus pytest and Ruff.
The GitHub Actions workflow also runs the Python test suite and JavaScript
syntax/frontend checks.

## Configure projects

config/projects.json contains safe shared defaults and registers Nexus with a
repository-relative path. Add machine-specific projects in the ignored file
config/projects.local.json. You can copy config/projects.local.example.json
and edit its project name, path, aliases, and enabled flag. Relative paths are
resolved from the Nexus repository root; absolute paths are supported in this
local-only file.

The local overlay can override a shared entry by project ID or add projects.
It is ignored by Git and stays on that computer. The orchestrator-poc example
worker requires a local project entry with that ID before it can run.

## Start Nexus

From the repository root:

    .\scripts\start-nexus.ps1

The launcher expects .venv to exist, checks that Nexus imports, reports which
native backends it can find, then starts the Nexus Control Center. This is the
native-first default: OmniRoute is neither checked nor started, and the
`~/.codex-omniroute-worker` profile is not required.

- **Codex CLI** is looked up where Nexus launches it
  (`%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe`); a `codex` that is only on
  PATH is not enough.
- **Claude Code CLI** must be the native `claude.exe` (matched by file name,
  case-insensitively): `NEXUS_CLAUDE_PATH`, the npm install (a `claude.cmd` shim
  on PATH is resolved to the `claude.exe` it launches), or
  `~/.local/bin/claude.exe`. Shims, scripts and other names or extensions
  (`.cmd`, `.bat`, `.ps1`, `.com`, `claude-2.exe`, ...) are never accepted, so
  `NEXUS_CLAUDE_PATH` must point at a file named `claude.exe`.
- A missing CLI only prints a `[WARN]` and the summary shows `NOT FOUND`;
  startup is never blocked, even if neither is installed. Sign-in is not
  checked by the launcher; Nexus verifies each login when you pick a backend.
- The launcher only reports availability. It never starts external services on
  its own and never chooses or switches a backend: you pick Codex Native,
  Claude Native or OmniRoute per Development session.

To use OmniRoute, opt in explicitly:

    .\scripts\start-nexus.ps1 -EnableOmniRoute

With `-EnableOmniRoute` the launcher requires Codex and the dedicated
`~/.codex-omniroute-worker/config.toml` profile (or `NEXUS_OMNIROUTE_CODEX_HOME`),
checks the OmniRoute service on `-OmniRoutePort` (default 20128) and starts it
in the background with `-OmniRouteCommand` (default `omniroute`) when it is
offline. Startup stops with a clear message if any of these is missing.

To check your machine without starting anything, add `-PreflightOnly`; it runs
every check, prints the summary, and does not start OmniRoute, the Control
Center or the browser:

    .\scripts\start-nexus.ps1 -PreflightOnly

The SQLite database and runtime logs are created locally under Nexus storage
or the Windows local application data directory. They are not required for a
fresh clone and should not be copied between computers.

## Integration safety smoke (F3.6A)

Development Mode's Git integration (worktree -> review -> preflight ->
human-approved Apply) has an automated safety smoke suite that exercises
four real, disposable Git repositories: a successful Apply, an integration
conflict, a stale review, and a duplicate Apply attempt. Run all four with
one command and get a PASS/FAIL/BLOCKED report:

    python scripts/run_integration_safety_smoke.py

It never touches the real Nexus repository and never calls a model.


## Operational Development missions

Development write access stays off by default. After registering a project in
`config/projects.local.json`, authorize only that project from the repository
root:

    .\.venv\Scripts\python -m nexus.dev.operator authorize example-project

Check or revoke it with `status` or `revoke` in place of `authorize`.
This is a local operator command; Nexus does not expose a web route that changes
Development authorization.

Trusted verification commands are also local. Copy
`config/dev-test-profiles.local.example.json` to
`config/dev-test-profiles.local.json`, bind each profile to explicit
`project_ids`, and use an argv array. Nexus rejects shell launchers, paths as
executables, malformed arguments, and profiles not bound to the selected
project. Mission text and model output can select a profile ID but cannot
supply or extend its command.

Start an operational Development server with an explicit per-process opt in
(independent of `-EnableOmniRoute`, and still subject to the per-project
authorization):

    .\scripts\start-nexus.ps1 -EnableDevelopment

Omitting `-EnableDevelopment` keeps write-capable execution disabled unless
the operator already set `NEXUS_DEV_EXECUTION_ENABLED=1` in that process
environment. The Git integration gate (`NEXUS_DEV_INTEGRATION_ENABLED`) is
never set by the launcher. For OmniRoute sessions, model selection is limited to
routes that are both advertised live by OmniRoute and approved in Nexus for
coding. Free and low-cost routes are listed first; Nexus never substitutes a
different route or backend.

## Agent workspace (P1)

Open `http://127.0.0.1:8000/nexus/workspace` for the multi-pane workspace. The
existing Control Center stays at `/nexus` (linked as "Avançado").

- The Maestro turns a task into an auditable pane proposal
  (`workspace-policy/1`, deterministic, no model call). Panes are created only
  when you click "Criar".
- Every pane owns one real Development session and therefore one isolated
  worktree. Hiding a pane keeps its session and history.
- A pane's model applies to future missions only, and every mission must
  re-confirm the exact route. When no free route exists the Maestro does not
  pre-select a model.
- Limits: `NEXUS_WORKSPACE_MAX_PANES` (open panes per project, default 6, max 8)
  and `NEXUS_WORKSPACE_MAX_AUTO_PANES` (panes per proposal, default 3, max 4).
- Apply, merge and push remain manual, in the Development view.

### Intelligent Maestro (P2A)

- The Maestro chat is a real, persistent conversation per project, backed by a
  read-only Nexus Chat session (Codex App-Server via OmniRoute). It can read
  the project but never writes to it.
- Activate it with "Ativar Maestro": pick an approved route; non-free routes
  need explicit confirmation. Planning authorization never authorizes
  execution. Without it, answers come from the labelled deterministic policy.
- Every answer must contain a JSON plan (`maestro-plan/1`) that is strictly
  validated: approved models only, at most `NEXUS_WORKSPACE_MAX_AUTO_PANES`
  agents, one agent for simple tasks, dependencies only on earlier agents.
  Invalid answers and failed turns fall back to the deterministic proposal,
  clearly labelled, and never create anything.
- "Aprovar e criar agentes" creates every planned pane in one step with the
  exact models confirmed, a turn limit per agent and a mission timeout. No
  mission starts on its own; planned agents run one after another
  (dependencies must be ready for review first).
- Each pane accepts direct messages as real Development turns in its own
  worktree. Replies are marked "Somente conversa" or "Alterou código —
  verificar". Apply, merge and push stay manual in the Development view.
