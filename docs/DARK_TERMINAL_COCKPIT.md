# Dark Terminal cockpit — Codex Native flow

Open it at `/nexus/workspace/cockpit` (also linked from the workspace). The
cockpit keeps its identity (Maestro, panes, sidebar, layout controls) and adds a
**Codex Native** pane that drives a real Development session using the same
`/api/dev` routes as the Development view. No orchestration engine was added.

## What works (real data, real APIs)

| Capability | How it behaves |
|---|---|
| Project and sessions | Uses the selected project; lists its real sessions. Codex Native sessions and legacy OmniRoute sessions are labelled differently (`Codex Native` / `OmniRoute (legado)`). |
| Create a Codex Native session | Only by clicking *Criar sessão Codex Native*, with a model you chose from the catalog announced by your local Codex login. Creates an isolated worktree. |
| Send a task | Only with an active Codex Native session, an enabled server gate, a chosen model, a non-empty task **and** the confirmation checkbox (it resets after every send). The POST is never repeated automatically; after a failed POST the draft is kept and the result is flagged as uncertain. |
| Follow the run | Live text and activity over SSE, reconnecting from the last event id; polling of the persisted turn is the fallback and the source of truth. The final answer comes from the stored turn. |
| Interrupt | Explicit *Interromper* click on the active turn. |
| Human approvals | Pending requests are shown exactly (command, directory, reason, write access) with *Aprovar uma vez* / *Recusar*. Nothing is decided automatically. |
| Changed files and diff | Lists the worktree's changes and shows the patch as inert text (secrets redacted). |
| Review | *Registrar revisão* records the review snapshot; *Verificar preflight* shows whether integration could proceed. Both are read-only with respect to your repository. |

## What is intentionally not in the cockpit

- **Apply and Reject** (and the integration confirmation) stay in the Development
  view, which is one click away: `/nexus#development`. The cockpit never applies,
  integrates or pushes anything, and never approves a request for you.
- **Legacy OmniRoute sessions** are read-only here (files and diff). Operate them
  in the Development view.
- Mission/test-profile runs for Codex Native sessions are not exposed in this pane
  yet; use the Development view for those.

## Still preview in the cockpit

The Maestro/agent panes that go through OmniRoute keep their existing behaviour;
the Revisão pane and the overview counters still describe those workspace agents,
not Codex Native sessions.

## Safety properties (covered by `tests/frontend/test_cockpit_native.cjs`)

- Every state-changing call (create session, send task, interrupt, decide an
  approval, record a review) is a separate user click; loading, polling,
  reconnecting and re-rendering never POST.
- The module has no Apply/Reject URL, builds no HTML from data and stores nothing
  in the browser.
- Failures fail closed: an unknown execution gate or catalog blocks sending and
  creation, and responses that arrive after a project/session switch are discarded.
