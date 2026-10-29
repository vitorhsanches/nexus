# Agent instructions for Nexus

## Delivering work to GitHub

Any agent that commits, pushes, or opens/updates a pull request for this
repository must do it through `scripts/nexus-sync.ps1`, not by running
`git`/`gh` ad hoc. The script is the one place that enforces this repo's
delivery safety rules, so bypassing it silently drops those guarantees.

```powershell
powershell -File scripts/nexus-sync.ps1 `
  -Files @("path/to/file1.py", "path/to/file2.py") `
  -Message "feat: short summary" `
  -Branch "feat/your-working-branch" `
  -Base "the-real-integration-branch" `
  -Title "PR title" `
  -Body "PR description"
```

What it does, in order: stages exactly the files listed in `-Files`
(never `git add -A` / `git add .`), commits, pushes the working branch,
re-reads the remote (`git ls-remote`) to confirm the push actually
landed at the commit you made, then creates a pull request against
`-Base` or updates the existing open PR for that branch/base pair, and
finally reports the PR's CI status (`gh pr view --json
statusCheckRollup`).

Hard rules it enforces (do not try to work around these — if one blocks
you, fix the underlying situation instead):

- **`-Base` is required and is never defaulted to `main`.** Always name
  the actual branch you are integrating into (often another feature
  branch in a stacked chain, not `main`) — see
  [`docs/NEXUS_ARCHITECTURE_CONSOLIDATION_PLAN.md`](docs/NEXUS_ARCHITECTURE_CONSOLIDATION_PLAN.md)
  for how this repo's work is currently staged. Confirm the right base
  by checking what the working branch was actually created from
  (`git merge-base --fork-point` or your own worktree-creation command),
  not by assuming the newest or most obvious branch.
- **Never commit or push directly on `main` or `master`.** Always work
  in your own branch/worktree.
- **Never `--force` pushes, never `git merge`, `git apply`, or `gh pr
  merge`.** A rejected push means the remote moved; reconcile it, don't
  overwrite it. Merging a PR is a human decision, not something an agent
  automation does on its own.
- **Only the files you explicitly list get staged and committed.**
  Review `git status` yourself before calling the script — the script
  will refuse to run if an authorized path doesn't exist on disk, but it
  cannot tell *you* whether you meant to include a file you forgot to
  list.
- The script reuses whatever GitHub CLI session (`gh auth status`) is
  already configured on the machine. Never pass, print, or hardcode a
  token; if `gh` is not installed or not authenticated, the script exits
  with that exact blocker — report it verbatim, do not claim the work
  completed.

After running it, confirm success from its structured output (`Result`,
`Url`, `Number`, `Base`, `Branch`, `Commit`, `RemoteSha`, `CiStatus`) —
not by assuming the commands "probably worked." `Result` is `"created"`
on a brand-new PR or `"updated"` when a PR already existed for that
branch/base pair (title/body are refreshed in place, never duplicated).

`tests/test_nexus_sync.py` covers the script's guard rails (protected
branch, missing/duplicate base, unauthorized files, missing `gh`) against
disposable temp repos; run it with `pytest tests/test_nexus_sync.py` after
changing the script.

See also [`CLAUDE.md`](CLAUDE.md) for Claude-specific session conventions
that build on this same flow.
