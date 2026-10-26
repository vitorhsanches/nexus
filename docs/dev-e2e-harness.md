# Development Mode E2E harness

`scripts/run_dev_e2e.py` drives the existing Development Mode HTTP routes
against a **disposable** Git fixture and checks the whole path up to, but not
including, the human decision to integrate. It is native-first: by default it
creates a `codex_native` session (the local Codex login, no OmniRoute).
`--backend omniroute` keeps the previous OmniRoute-backed session.

The runner never decides an approval, never calls Apply or Reject, and never
removes anything. It has no method for any of those: they are manual actions in
the Nexus Development cockpit.

## What one smoke covers

1. **Gates and project.** The fixture project is registered, enabled and
   Development-authorized, points at this fixture, has no previous Development
   session (including failed or closed sessions), and the source repository is
   clean. The server-side execution gate must already be on (the harness reads
   it, never sets it); the integration gate is only reported. The requested
   model must appear in a non-empty live catalog.
2. **Session and worktree.** One session is created with the chosen backend; a
   session on any other backend is refused before a turn starts. The worktree
   must be outside the source fixture.
3. **Execution and completion.** One real turn is posted exactly once. Events
   are consumed incrementally from the SSE stream (reconnecting from the last
   cursor) until the durable turn status is `COMPLETED`.
4. **Approvals.** Pending approvals (for example the agent asking to run pytest
   outside its sandbox) are reported on stderr as `[approval pending] ...`.
   You decide them in the cockpit; the harness waits and records the final
   statuses.
5. **Changes and tests.** Changed files and the diff must be non-empty, and the
   fixture's pytest suite is run in the isolated worktree. That run is bounded
   (`--test-timeout`, default 120 s) and isolated: no inherited
   `PYTEST_ADDOPTS`/`PYTEST_PLUGINS`, plugin autoload and cache off, no bytecode,
   stdin closed, output captured in files. On a timeout the whole process tree is
   killed (a plain `subprocess.run` timeout would wait for any grandchild holding
   the pipes) and the error names the command, cwd, Python, elapsed time and the
   partial output. No review is recorded after a failed test step.
6. **Review.** A review snapshot is recorded (`POST .../reviews`): it must be
   `REVIEWED`, approval-ready, bound to the session's base commit and list
   exactly the files of the inspected diff. The preflight result is reported
   (`INTEGRATION_DISABLED` is expected while the integration gate is off).
7. **Source untouched.** No integration was applied, and the source repository's
   HEAD, branch, working tree and `calc.py` are byte-for-byte what they were.

## Prepare one fresh smoke

Use a dedicated Nexus checkout, server port and persistent database for this
smoke. Keep the existing port 18742 and its data untouched. `codex login` must
already be available to that server's Windows user. The fixture and evidence
paths must be new, persistent paths; never point either at an earlier smoke.

```powershell
# 1. From this candidate checkout, prepare a new fixture and recovery log.
$smokeRoot = Join-Path (Split-Path (Get-Location) -Parent) 'p4-mvp-e2e-smokes'
New-Item -ItemType Directory -Force -Path $smokeRoot | Out-Null
$smokeId = [guid]::NewGuid().ToString('N')
$fixture = Join-Path $smokeRoot "fixture-$smokeId"
$evidence = Join-Path $smokeRoot "evidence-$smokeId.jsonl"
python scripts/run_dev_e2e.py --prepare $fixture --evidence $evidence

# 2. Add the printed unique project ID and absolute fixture path as a new,
#    enabled entry in config/projects.local.json of this dedicated checkout.
#    Preserve all existing entries. Example entry:
#    {"id":"<printed project_id>","name":"Disposable E2E","path":"<printed fixture>","enabled":true}
#    Registration is explicit; the harness never changes the registry.

# 3. Explicitly authorize only the printed project ID in this checkout.
python -m nexus.dev.operator authorize <printed-project-id>

# 4. Start the dedicated server without OmniRoute, with Development execution
#    on and integration off. Choose a free port other than 18742.
.\scripts\start-nexus.ps1 -EnableDevelopment -NexusPort <dedicated-port> -NoBrowser

# 5. Read-only catalog and preflight. Use an ID actually returned by this server.
Invoke-RestMethod 'http://127.0.0.1:<dedicated-port>/api/dev/models?backend=codex_native&refresh=true' | Select-Object -ExpandProperty models
python scripts/run_dev_e2e.py --check --base-url http://127.0.0.1:<dedicated-port> --project-id <printed-project-id> --fixture $fixture --evidence $evidence --model <announced-model-id>

# 6. After separate human authorization, run exactly one real turn.
python scripts/run_dev_e2e.py --base-url http://127.0.0.1:<dedicated-port> --project-id <printed-project-id> --fixture $fixture --evidence $evidence --model <announced-model-id> --confirm-agent-call
```

While step 6 runs, open the Development cockpit and approve or decline each
pending request yourself. Success prints a JSON report ending in
`"integration_applied": false` and `"source_unchanged": true`. Then inspect the
diff and, if you want to continue, run review -> preflight -> Apply by hand
(that needs `NEXUS_DEV_INTEGRATION_ENABLED=1`, which the harness never sets).

The append-only JSONL evidence file records `session_created` and `turn_started`
with their IDs as soon as each response arrives. On a timeout or an uncertain
response, inspect that same session and turn in Nexus before doing anything
else; never rerun against this fixture. Both `--check` and the real smoke refuse
any fixture with an earlier Development session. The runner never retries a
turn. A session response lost before its ID reaches the runner is still visible
through the server's project-scoped session list.

Useful options: `--backend omniroute`, `--model <id>` (must be offered by the
selected backend), `--prompt`, `--turn-timeout`.

## What depends on the integrated candidate

The harness only uses HTTP routes. The automated tests below pass on this
branch alone; the live flow needs these pieces from the native stack:

- `backend: "codex_native"` on `POST /api/dev/sessions` and
  `GET /api/dev/models?backend=codex_native` (Codex Native, PR #24/#26).
- `GET .../approvals` (human approvals, PR #26).
- `POST .../reviews`, `GET .../preflight` and `GET .../integrations` (the
  integration routes) and the execution/integration status routes.
- The native-first launcher (PR #31) to start Nexus without OmniRoute.

Against a server that lacks them the harness stops before any turn: a server
that ignores `backend` creates an OmniRoute session, which the harness refuses
(it never runs on a different backend than requested), and missing routes
surface as the server's own HTTP error. `--check` was exercised read-only
against the native execution stack (PR #26): it listed the local Codex
catalog and reported the gates without creating a session.

## Automated tests

```powershell
python -m pytest -q tests/test_dev_e2e_harness.py
```

The tests run the real backend adapters against a scripted fake Nexus server and
local temporary Git repositories. They verify the native session payload, the
single turn POST, approval reporting, the review checks, every refusal path
(disabled gate, wrong backend, dirty or modified source, unusable or mismatched
review, applied integration) and that Apply, Reject and approval decisions are
never called. They do not count as real-agent validation.
