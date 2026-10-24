# Development Mode E2E harness

The harness exercises the existing Codex App-Server Development Mode routes.
It makes no direct OmniRoute requests and never applies a reviewed change.
Automated tests use a fake Nexus API; only the explicitly confirmed live mode
can start a real agent turn.

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
Leave `NEXUS_DEV_INTEGRATION_ENABLED` off. Select a Codex model in Nexus if the
server requires one; omitting `--model` lets App-Server use its configured
default and avoids coupling the runner to the OmniRoute catalog.

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
sends one real turn, consumes the existing SSE endpoint and checks durable
turn completion, changed-files and diff endpoints, runs pytest in the isolated
worktree, and confirms the source fixture HEAD stayed at the session base.
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

These tests use fake HTTP responses and local temporary Git repositories.
They verify the fixture's intentional failing baseline and a simulated
successful execution path; they do not count as real-agent validation.
