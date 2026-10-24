---
name: multi-agent-development-manager
description: Repository-owned runtime contract for Nexus-managed Codex workers.
---

# Nexus Worker Runtime

This skill is installed only into a per-process Nexus `CODEX_HOME`.

- Work only inside the repository supplied by the Nexus wrapper.
- Never delete or modify paths outside that repository.
- Never invoke `cmd /c rmdir`, `cmd /c rd`, `rmdir /s`, or `rd /s`.
- Treat sandbox or boundary failures as terminal; do not bypass them.
- Return a concise final result for the Nexus result channel.
