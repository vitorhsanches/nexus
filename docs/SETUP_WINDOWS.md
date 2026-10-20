# Windows setup

## Requirements

- Windows PowerShell 5.1 or PowerShell 7.
- Python 3.10 or newer.
- Node.js for the frontend checks.
- Codex CLI available to Nexus for Codex App-Server and worker execution.
- OmniRoute installed and configured locally when model-backed features are needed.

Nexus does not include Codex or OmniRoute credentials. Sign in and configure
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

The launcher expects .venv to exist, checks the local OmniRoute service, then
starts the Nexus Control Center. Codex CLI/App-Server calls are launched by
Nexus as needed. Start or configure OmniRoute locally before using model-backed
features.

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

The launcher now checks Python imports, Codex CLI, the dedicated
`~/.codex-omniroute-worker/config.toml` profile, and OmniRoute readiness.
Start an operational Development server with an explicit per-process opt in:

    .\scripts\start-nexus.ps1 -EnableDevelopment

Omitting `-EnableDevelopment` keeps write-capable execution disabled unless
the operator already set `NEXUS_DEV_EXECUTION_ENABLED=1` in that process
environment. Model selection in Development Mode is limited to routes that are
both advertised live by OmniRoute and approved in Nexus for coding. Free and
low-cost routes are listed first; Nexus never substitutes a different route.
