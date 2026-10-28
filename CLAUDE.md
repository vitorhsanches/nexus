# Nexus collaboration rules

- Work only on a dedicated branch and preserve existing worktrees and user files.
- Finalized tasks require a commit and normal push through `scripts/nexus-sync.ps1`.
- Stage only explicitly authorized paths; never use `git add .`.
- Refuse `main` and `master`, force push, automatic merge, and publication of credentials or machine-specific settings.
- Verify the remote SHA after pushing and report it with the task result.
- Use WIP commits/checkpoints for long tasks and focused tests locally; leave broad regression to CI.
- Preserve human approval, cost authorization, Worker approval, fencing and worktree isolation policies.
