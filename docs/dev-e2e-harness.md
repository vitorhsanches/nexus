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
   Development-authorized, points at this fixture, and the source repository is
   clean. The server-side execution gate must already be on (the harness reads
   it, never sets it); the integration gate is only reported.
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
   fixture's pytest suite is run in the isolated worktree.
6. **Review.** A review snapshot is recorded (`POST .../reviews`): it must be
   `REVIEWED`, approval-ready, bound to the session's base commit and list
   exactly the files of the inspected diff. The preflight result is reported
   (`INTEGRATION_DISABLED` is expected while the integration gate is off).
7. **Source untouched.** No integration was applied, and the source repository's
   HEAD, branch, working tree and `calc.py` are byte-for-byte what they were.

## Manual smoke after the native candidate is integrated

Prerequisites: the integrated candidate (Codex Native Development with approvals
and the review/apply routes, plus the native-first launcher), `codex login`
done, and no real project used as the fixture.

```powershell
# 1. Disposable fixture (refuses to overwrite an existing path)
$fixture = Join-Path $env:TEMP "nexus-dev-e2e-$(Get-Date -Format yyyyMMdd-HHmmss)"
python scripts/run_dev_e2e.py --prepare $fixture

# 2. Start Nexus without OmniRoute; Development execution on, integration gate off
.\scripts\start-nexus.ps1 -EnableDevelopment

# 3. In the Projects UI: register $fixture (e.g. id "dev-e2e-fixture") and enable Development Mode for it

# 4. Read-only readiness report: no session, no turn, no inference
python scripts/run_dev_e2e.py --check --project-id dev-e2e-fixture --fixture $fixture

# 5. The real smoke. Starts ONE real Codex turn with your local login (cost applies)
python scripts/run_dev_e2e.py --project-id dev-e2e-fixture --fixture $fixture --confirm-agent-call
```

While step 5 runs, open the Development cockpit and approve or decline each
pending request yourself. Success prints a JSON report ending in
`"integration_applied": false` and `"source_unchanged": true`. Then inspect the
diff and, if you want to continue, run review -> preflight -> Apply by hand
(that needs `NEXUS_DEV_INTEGRATION_ENABLED=1`, which the harness never sets).

On a timeout or an uncertain response, inspect that same session and turn in
Nexus before doing anything else; do not blindly rerun the command. The runner
never retries a turn.

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
