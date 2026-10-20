# Nexus — Architecture Consolidation & Selective Toolkit Adoption Plan

Status: planning document only. No production code was changed to produce this.
Scope: preserve the current GO engine, OmniRoute boundary, and Codex execution
model; evaluate selective adoption of ideas from
`soumatheusgomes/vibe-coding-toolkit` without installing it wholesale.

---

## 1. Current state of priorities (evidence-based)

There is no persisted roadmap document in the repository today (`README.md` is
empty; `docs/*.md` are individual spec docs, not a tracked plan; `Claude
outputs/*.md` are seven audit/fix reports, all dated 2026-09-28, describing
completed work rather than upcoming work). The only real "roadmap" that
exists is the sequence of phase prompts (F1.1 → F3.2B) issued directly to
Claude sessions and answered with RETORNO reports — never written down in one
place. This document is the first attempt to consolidate that history; **the
existing F-numbering is preserved below, not renumbered.**

**Committed vs. uncommitted (updated after F3.3).** At the time this
document was first written, HEAD was `8a688e9` and everything from F1.2
through F3.2B sat uncommitted in the working tree — flagged below (and in
section 6) as the single largest operational risk. That gap has since been
closed: F1.2→F3.2B landed as three atomic, reviewed commits (`19a7a62`
Overview-polling/SSE fix, `0ad6367` changed-files + diff viewer backend,
`ca3b397` Cockpit UI/API + the F3.2 integration gate), each validated
against the full test suite before committing. `git status` on `main` is
clean of Dev Mode source changes as of that consolidation.

**Completed and verified (by test evidence, live smoke tests, or both):**
- F1.1 Isolated Git worktrees per Development session.
- F1.2 Persistent Development sessions with DB-backed recovery.
- F2.1 Interactive Codex executor (workspace-write sandbox, live-verified).
- F2.2 Web/SSE integration, execution feature gate (default OFF).
- F2.3 Process-level approval-policy override, live-verified denial path.
- F2.4A/F2.4B First real coding execution through the Cockpit; SSE
  reconnection reliability (root-caused and fixed a real Overview-polling
  concurrency bug).
- F3.1 Diff Viewer, live-verified against a disposable repo (a real bug —
  `--literal-pathspecs` combined with an explicit `:(literal)` prefix
  silently returning empty diffs — was found and fixed during that
  verification, not assumed away).

**Committed but not independently audited by this document:** F3.2/F3.2B
(Git integration gate for Dev Mode — human-approved, snapshot-bound
fast-forward). Now part of commit `ca3b397`, covered by the full test
suite at consolidation time, but its design was not separately reviewed as
part of this architecture pass.

**Structurally present but never consolidated (pre-existing, not part of the
F-series):** the GO engine (`nexus/orchestration/go.py`), the Program runner
(`nexus/program/`), Mission/Task Board, capability routing, and the adaptive
routing core — all have substantial test coverage (117 test files, ~2,100+
tests) but three different pieces of code can decide which model a task
runs on (section 3), and the repository has no committed lint configuration
and no CI workflow (`.github/` does not exist) despite active use of a
linter (`# noqa: PLC0415`-style comments appear throughout `nexus/web/` and
elsewhere, implying Ruff is used locally/manually but not enforced).

---

## 2. Responsibility map

| Layer | Owns | Does NOT own |
|---|---|---|
| **Nexus** | Mission/Program/Task orchestration, GO engine (Manager→Workers→Review), capability + adaptive routing *decisions*, Dev Mode session/worktree/execution lifecycle, persistence (SQLite registries), the web UI/API, approval-policy and sandbox configuration for every Codex launch | Actually calling any LLM API |
| **OmniRoute** | The external model-access gateway Nexus's `config.toml` points Codex at (`model_provider="omniroute"`) | Orchestration, retries, worktree isolation, UI — it is purely the routing/provider layer Codex talks to |
| **Codex (CLI)** | The sole execution mechanism — every actual model call happens inside a `codex`/`codex app-server` subprocess | Deciding *which* model/effort to use (Nexus decides this before launch) or any orchestration logic |

Confirmed: no Nexus Python code calls an LLM API directly. All invocation is
via Codex subprocesses, launched from five independent call sites
(`nexus/chat/appserver_client.py`, `nexus/dev/executor.py`,
`nexus/dispatchers/manager.py`, `nexus/dispatchers/omniroute.py`,
`nexus/dispatchers/review.py`, plus `nexus/program/architect.py`), each
using the shared `nexus/process_env.py::codex_subprocess_env()` for
environment/config generation, but each building its own `Popen`/`run` call
independently rather than through one shared launcher.

---

## 3. Keep / simplify / delegate / remove matrix

| Item | Finding | Recommendation |
|---|---|---|
| Three model-selection code paths (`policies/escalation.py` ROUTE_LADDERS, `agents/adapters/omniroute.py::select_route`, `routing/` adaptive core) | The adaptive router only runs for *new* executions with no `route_override`; any retry/escalation silently falls back to the static ladder, skipping live telemetry entirely. The `routing/` package's own docstring says it is "intentionally standalone... not yet wired into" several execution paths. | **Simplify — needs a dedicated decision phase.** Do not touch now; this governs live execution and a wrong move degrades routing quality silently. Flagged as a future milestone (section 5), not an action here. |
| Two "Manager" implementations (`nexus/manager/` Board-oriented planner vs. `nexus/dispatchers/manager.py::run_manager`, the live GO-engine launcher) | Both exist, both are named Manager, unclear if one is legacy. | **Needs a decision**, not a default. Confirm with whoever owns the Mission/Task Board whether `nexus/manager/` is still live or superseded. |
| Two ownership/liveness mechanisms (`orchestration/ownership.py::establish_ownership` vs. `program/state.py::acquire_ownership`) | Different layers (Run vs. Program), may be intentional layering rather than duplication — both are tested and both passed in this session's own F3.1 triage. | **Keep**, document the layering explicitly (one line each, in-code) so it reads as deliberate rather than accidental. |
| Five independent Codex `Popen`/`run` call sites sharing only `codex_subprocess_env()` | Command-list construction is reimplemented per call site. | **Simplify, low priority.** A thin shared launcher (one function, no new abstraction layer) could remove duplication without adding a framework. Not urgent — all five are tested. |
| Failure/retry classification split across `dispatchers/omniroute.py` (`_looks_transient`), `failure_classification.py`, `failure_contract.py` | Possible overlapping taxonomies at dispatcher vs. top level. | **Needs a decision** to confirm scope boundaries before touching. |
| `*_poc.py`/`demo.py`/`show.py` files inside `nexus/orchestration/` | Sit alongside production code; unclear if dead. | **Remove candidate** — low risk, but confirm nothing production imports them before deleting (out of scope for this planning pass). |
| No committed lint config / no CI | Ruff is clearly used (noqa comments throughout) but not enforced or documented. | **Adopt a gate** (section 4) — concrete, low-risk, no new dependency. |
| No persisted roadmap | Confirmed by absence; this document is the first one. | **Adopt** — keep this file (or its successor) updated per phase instead of relying on conversation history. |

---

## 4. Selective adoption plan — Vibe Coding Toolkit

The toolkit is a prompt/documentation library for bootstrapping Next.js
projects with Claude Code/Codex, not an installable framework (the toolkit's
own README: *"The code stays in your project; this repository holds the
recipe."*). Nothing from it is installed. Only the following **patterns**
are evaluated:

### 4.1 Harness Evaluation — **recommended, concrete next step**

The toolkit's `docs/07-harness-evaluation.md` describes testing the agent's
*working setup* (instructions, tools, evidence, recovery) using disposable
fixtures with a deliberate, known bug, verified against an independent
evaluator that must itself be proven to fail on the broken baseline. This is
**exactly the ad hoc pattern already used informally in every F-phase of
this project** (F2.1's sandbox-escape diagnostic, F2.2's approval-policy
diagnostic, F2.3's escalation diagnostic, F2.4A's live coding smoke test,
F3.1's diff-viewer smoke test — all disposable repos, isolated registries,
real verification instead of assumption). The toolkit's contribution is
formalizing it as a **repeatable, recorded** practice rather than one-off
scratch scripts that get discarded:

- A disposable Dev Mode fixture repo with a known, intentional defect
  (e.g. a file that must not be touched outside the worktree).
- A small, independent checker script (outside the agent's writable scope)
  that fails on the untouched fixture and passes only on a correct fix.
- Recorded fields per run: toolkit/revision, date, model, permissions,
  task, commands, changed files, observed behavior, pass/fail/**not run**
  (never silently upgraded to pass when unobservable).
- Disposable test cases directly applicable to Dev Mode: boundary-fix
  (writes stay in worktree), stale-handoff (don't trust a claim without
  verifying file state), pause/resume (Stop then reselect must reconcile
  real backend state, not assume), partial-blocker (execution gate off
  must report the limitation, never fake success).

**Status: implemented (F3.4).** `tests/test_harness_evaluation.py` carries
three disposable-fixture scenarios (stale-claim evidence verification,
unauthorized-change detection for a "read-only"-claimed agent, safe
resume after a dead-owner interruption), reusing `tests/test_acceptance.py`
and `tests/test_dev_executor.py`'s existing fixtures/helpers by import
rather than copy. Landed as a flat `tests/test_harness_evaluation.py` file
rather than a new `tests/harness/` directory, to match this repository's
existing flat `tests/` convention (no new directory, no new framework). No
new dependency — pytest + disposable Git repos + the existing
`FakeAppServerClient`, identical to existing test infrastructure.

### 4.2 Subagent Orchestration — **mostly already present, adopt one convention only**

The toolkit's guide (delegate for context isolation/specialization/real
parallelization; one writer per shared file; read-only parallel reviewers;
never split by problem topic or phase) describes almost exactly what
`nexus/orchestration/go.py` (Manager → DAG → progressive worker execution →
Review) and `nexus/orchestration/dag.py`/`progressive.py` already implement
in production, with real tests. There is no gap here worth installing
anything for.

**Recommended, narrow takeaway:** the toolkit's `Files:`/`Depends-on:`/
`Resources:` task-annotation convention (missing or uncertain annotations
default to "depends on everything") is a lightweight documentation habit,
not a subsystem. If Mission/Task specs don't already carry this, it is
worth adding as a **field convention**, not a new feature.

### 4.3 Quality Gates — **adopt the principle, skip the JS tooling**

The toolkit's quality-gates document is ESLint/Biome/TypeScript-specific and
not portable as-is. The portable *principle* is real and directly addresses
this repository's own gap (section 1): a new lint rule should start as a
warning with a tracked count, becoming an error only once that count reaches
zero — never a hard break on introduction. Separately, "evidence over
appearance" (distinguishing a file existing from a feature actually being
exercised) is the exact discipline this project has already been applying
ad hoc in every F-phase's live smoke test (F2.4A/B, F3.1) — formalizing it
via section 4.1 covers it; no separate gate framework is needed.

**Proposed action (not performed here):** a committed Ruff configuration
(the linter already in informal use) with any new rule introduced as
`warn`, promoted to `error` only after the existing count is zero, plus an
optional, documented file-size *guideline* (not an enforced gate) — no new
dependency, no CI framework decision implied here.

### 4.4 RTK (token-reduction output proxy) — **do not adopt**

RTK is a standalone tool/proxy for condensing verbose command output. It is
explicitly out of scope under this mission's own restriction ("não instalar
dependências, plugins, hooks ou MCPs"). No action recommended; noted only
for completeness since it was named in the task.

---

## 5. Revised roadmap (existing milestones preserved, nothing renumbered)

```
F1.1  Isolated Git worktrees                              DONE
F1.2  Persistent Development sessions                      DONE
F2.1  Interactive Codex executor                            DONE
F2.2  Web/SSE integration + execution feature gate          DONE
F2.3  Process-level approval-policy verification             DONE
F2.4A First real coding execution via Cockpit                DONE
F2.4B SSE reconnection reliability                            DONE
F3.1  Diff Viewer                                              DONE
F3.2  Git integration gate (human-approved, snapshot-bound)   DONE (committed in ca3b397; not
                                                                independently audited by this pass)
F3.2B (follow-on to F3.2, scope not audited here)              DONE (committed in ca3b397)
F3.3  Commit discipline: land F1.2->F3.2B as atomic commits    DONE (19a7a62, 0ad6367, ca3b397)
F3.4  Harness Evaluation for Dev Mode (section 4.1)             DONE (tests/test_harness_evaluation.py)
```

Proposed next milestones (new, appended — not replacing or renumbering
anything above):

| Milestone | Scope | Depends on | Done when |
|---|---|---|---|
| **F3.5** (proposed) | Lint/CI baseline (section 4.3): committed Ruff config (warn-first), minimal CI running `pytest` + lint | None blocking; independent of Dev Mode work | A GitHub Actions workflow (or equivalent) runs on every push; lint starts at warn, not error |
| **F4.x** (future, requires an explicit decision before scoping) | Routing consolidation: reconcile the three model-selection layers (section 3) into one documented policy with explicit fallback semantics | A dedicated investigation phase — this touches live execution and must not be scoped casually | Out of scope for this document; flagged as a decision point only |

---

## 6. Architectural risks and decisions that need a human call

1. **Routing fallback gap** — retries/escalations silently bypass the
   adaptive router and its live telemetry, reverting to a static ladder.
   This may be intentional (a safe, simple fallback) or an unnoticed gap.
   Needs a decision before any routing code changes.
2. **Two "Manager" implementations** — confirm which is live vs. legacy
   before either is touched or removed.
3. **Program vs. Run ownership locks** — likely intentional layering;
   recommend documenting it explicitly rather than merging the two.
4. **Resolved: uncommitted Dev Mode history (F1.2→F3.2B).** Was the single
   largest procedural risk at the time this document was written; closed by
   F3.3 (three atomic commits, section 5).
5. **No CI** — nothing currently stops a regression from landing unnoticed
   outside of manually run test suites.

---

*This document was produced by read-only investigation (an Explore
subagent mapped the internal architecture; external research covered the
Vibe Coding Toolkit's public documentation). No production code, tests,
dependencies, hooks, or MCP configuration were modified to produce it.*
