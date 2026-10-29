# Claude Code instructions for Nexus

This repo's cross-agent delivery rules live in [`AGENTS.md`](AGENTS.md)
and apply to Claude the same as any other agent working here — read it
first. The short version: every commit/push/PR for this repository goes
through `scripts/nexus-sync.ps1`, which stages only authorized files,
verifies the push actually reached the remote, and opens or updates a PR
against an explicitly named base (never `main` by default).

## Before delivering a task's changes

1. Know the real base. If the task brief names one, use it. If not, work
   out what the working branch/worktree was actually created from
   (`git merge-base --fork-point`, or the branch you passed when
   creating the worktree) — do not default to `main` just because it is
   the repository default branch. Most work in this repo lands on a
   stacked feature branch, not directly on `main`.
2. Know the exact file list the task authorized. Run `git status` in the
   worktree and confirm it matches what you intended to change — nothing
   more.
3. Run the regression suites relevant to what changed
   (`pytest tests/...`, `node --test tests/frontend/*.cjs`) and the
   syntax checks (`node --check`, `py_compile`, `git diff --check`)
   before delivering, exactly as prior phases in this repo have done.

## Delivering

```powershell
powershell -File scripts/nexus-sync.ps1 `
  -Files @("file1", "file2", "...") `
  -Message "<commit message>" `
  -Branch "<your working branch>" `
  -Base "<the real integration branch>" `
  -Title "<PR title>" `
  -Body "<PR body>"
```

Report back using the script's structured result (`Result`, `Url`,
`Number`, `Base`, `Branch`, `Commit`, `RemoteSha`, `CiStatus`) rather
than re-deriving these facts by hand. If the script blocks (missing
`gh` auth, a protected branch, a missing authorized file, a rejected
push, ...), report that exact blocker to the user — never report the
task as delivered when the script did not complete.

## What this flow replaces

Do not call `git commit` / `git push` / `gh pr create` / `gh pr edit`
directly for repository delivery. The script exists specifically because
ad hoc git/gh calls in earlier sessions produced a PR with the wrong
base (comparing against a long-stale `main` instead of the branch the
work was actually built from) — see the P6.C2 → P7 delivery automation
history for the incident this flow was built to prevent.

Reserve direct `git`/`gh` calls for read-only inspection
(`git status`, `git diff`, `git log`, `gh pr view`, `gh pr checks`), not
for mutating repository or PR state.
