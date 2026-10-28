# Nexus development rules

- Use a dedicated work branch for every task; never publish directly from `main` or `master`.
- Commit and push the authorized files when a task is complete. Use `scripts/nexus-sync.ps1`; never use `git add .` or force push.
- Confirm the remote branch SHA after every push. Do not claim synchronization without that confirmation.
- Use remote checkpoints for long tasks and label unfinished work as WIP.
- Run focused local tests; rely on CI for broad regression coverage when available.
- Preserve worktrees, untracked user files, human approvals, cost gates, fencing and isolation.
- Do not change the internal Worker approval policy without an explicit task requiring it.
