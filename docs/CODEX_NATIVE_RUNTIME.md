# Codex Native Chat runtime

## Contract and boundaries

`NEXUS_CHAT_RUNTIME` selects exactly one default Chat runtime:

- `omniroute` (legacy default): existing route discovery, provider gate and
  no-auth isolated profile remain unchanged.
- `codex-native`: the official `codex app-server --listen stdio://` protocol,
  local Codex CLI authentication and provider `openai` are required.

The cockpit can select either runtime for a new session. The runtime is stored
with the session, so later turns, history reads and process restarts always use
the same adapter. A failed adapter is reported; Nexus never retries through the
other one.

Both adapters use the same minimal runtime shape: identity/provider metadata,
client construction, authentication validation and model discovery. This is
the extension point for a future Claude Code adapter; Claude is not implemented
or accepted as a runtime in this release.

Every Chat process has an isolated `CODEX_HOME`, scoped Git trust for exactly
the registered project and a process-level `sandbox_mode="read-only"` override.
Approval requests remain fail-closed. Development Execution is not enabled by
selecting Codex Native.

The backend may copy the local CLI credential file into that isolated private
runtime directory, using the existing process isolation helper. Credential
contents and account identity are never returned by the API. The browser sees
only a boolean authentication result and a non-secret account type.

## Protocol flow

1. Start App-Server over newline-delimited JSON on stdio.
2. Send `initialize`, await its response, then send `initialized`.
3. Read `account/read` and `model/list` without inference.
4. Create a non-ephemeral thread with `thread/start`, or continue it with
   `thread/resume` using the persisted thread ID and isolated `CODEX_HOME`.
5. Send `turn/start`; forward redacted item/turn notifications through the
   existing SSE channel; persist the final Nexus turn status.
6. Send `turn/interrupt` with both thread and turn IDs when the operator stops
   a response.

## Real read-only smoke (operator authorization required)

This smoke uses the real authenticated account and can consume account quota.
Do **not** run it as part of automated tests or without the operator explicitly
authorizing this exact execution.

Prerequisites:

    codex login status
    .\.venv\Scripts\python -m pytest tests/test_chat_native_runtime.py -q

After explicit authorization:

1. Register a disposable or deliberately readable project.
2. Start Nexus without Development Execution:

       .\scripts\start-nexus.ps1 -ChatRuntime codex-native

3. Open Nexus Chat, select `Codex Native`, select the registered project and a
   model advertised by the CLI, then confirm unknown cost/quota.
4. Send: `Read only: identify the project name from README.md. Do not modify files or run write commands.`
5. Send a follow-up in the same session: `What exact project name did you find?`
6. Verify both responses share one session/thread, streaming events appear,
   and `git status --short` is unchanged.
7. Start a third harmless read-only response and press **Stop**; verify the turn
   becomes cancelled/interrupted and a later message resumes the same session.

The implementation in this PR was validated only with mocks. No real App-Server
turn or model inference was executed because no operator authorization was
provided.
