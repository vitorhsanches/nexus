# Development Mode E2E harness

The harness exercises the current Development Mode routes and never applies a
reviewed change. The current turn path is **not independent of OmniRoute**:
`POST /api/dev/sessions/{id}/turns` calls `nexus.dev.executor`, which constructs
`AppServerClient`; that client requires the `omniroute` profile and enforces
the OmniRoute provider. Session creation itself creates an isolated Git
worktree, but a real inference turn still depends on OmniRoute. The runner
does not call OmniRoute directly. Automated tests use a fake backend; only the
explicitly confirmed live mode can start a real agent turn.

The runner separates its workflow from transport through the
`DevelopmentSmokeBackend` protocol. `CurrentOmniRouteDevBackend` is the only
adapter implemented today. This is the extension seam for a native backend;
the harness will add that adapter only after P4.0B publishes and implements its
actual contract. The protocol describes harness operations, not new Nexus
routes, and current tests do not claim native execution works.

## Prepare a disposable project

From the repository root in PowerShell, choose a new scratch path. The command
refuses to overwrite an existing path:

```powershell
$fixture = Join-Path $env:TEMP "nexus-dev-e2e-fixture-$(Get-Date -Format yyyyMMdd-HHmmss)"
python scripts/run_dev_e2e.py --prepare $fixture
```

Register that directory as a project in the Nexus Projects UI and explicitly
enable Development Mode for that project. Do not use the Nexus repository or
another real project as the fixture. Start the Nexus server with
`NEXUS_DEV_EXECUTION_ENABLED=1`; this gate is server-side and defaults off.
Leave `NEXUS_DEV_INTEGRATION_ENABLED` off. Select a model supported by the
current Development executor. Omitting `--model` uses that executor's
configured default; it does **not** remove the current OmniRoute dependency. A
native Codex smoke remains blocked until P4.0B supplies its backend contract
and implementation.

## Run the real smoke

Only after confirming the chosen model/account and approving a real agent
request, run:

```powershell
python scripts/run_dev_e2e.py `
  --base-url http://127.0.0.1:8000 `
  --project-id <registered-fixture-id> `
  --fixture $fixture `
  --confirm-agent-call
```

The runner checks project selection/authorization, creates one Dev session,
sends one real turn, incrementally consumes the current SSE endpoint using
`Last-Event-ID` replay cursors, and checks durable turn completion,
changed-files and diff endpoints, runs pytest in the isolated worktree, and
confirms the source fixture HEAD and working tree stayed unchanged. SSE read
timeouts reconnect from the last cursor under one total turn deadline. A POST
is issued exactly once; ambiguous completion requires inspecting the existing
session/turn before any manual follow-up.
It prints the observed IDs and evidence. On timeout or an uncertain response,
inspect that same session/turn in Nexus before doing anything else; do not
blindly rerun the command. The runner never retries a turn, removes a fixture,
or applies integration.

## Human review and P4.0B

P4.0A validation ends with a read-only diff and passing fixture tests. Inspect
the change in the Nexus Development cockpit and record human approval there;
the harness does not synthesize approval or invoke Apply. The post-P4.0B
editing/review checks remain disabled until that phase's accepted UI/API
contract is available. No future endpoint is assumed here. After P4.0B, extend
the harness against the published contract and keep Apply as a manual action.

## Automated tests

```powershell
python -m pytest -q tests/test_dev_e2e_harness.py
```

These tests use a fake backend, one fake incremental HTTP response, and local temporary Git repositories.
They verify the fixture's intentional failing baseline and a simulated
successful execution path; they do not count as real-agent validation.
