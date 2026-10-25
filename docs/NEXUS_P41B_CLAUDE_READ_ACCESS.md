# P4.1B Claude Development Read Access

Claude Native Development currently offers only `Edit` and `Write`. `Read`,
`Grep`, and `Glob` stay out of its tool allowlist because SDK-host
`can_use_tool` is not a reliable interception point for reads.

Claude Code `PreToolUse` hooks run before each tool call, including calls that
would otherwise be auto-allowed. A hook can deny a call before the built-in
tool executes. The installed CLI was `2.1.288` during P4.1B final verification;
its `--settings` option accepts hook configuration, and a no-prompt
`stream-json` initialization succeeded with the SDK-host protocol.

There is one important bypass: file references such as `@path` are inserted
into the prompt without calling a tool, so `PreToolUse` does not run. The
official `CLAUDE_CODE_DISABLE_FILE_MENTIONS=1` setting is supported only from
Claude Code `2.1.299`. Therefore the installed version cannot provide a
verifiable guarantee that every file read passes through the hook. Do not
restore `Read` until the supported CLI version disables file mentions and a
`PreToolUse` hook validates absolute paths, repository metadata, credential
files, symlinks, and worktree containment before allowing the read. Keep
recursive `Grep` and `Glob` disabled until equivalent path and result-scope
controls are available.

The simplest safe alternative for the current MVP is to keep source reading
disabled and let Claude make only human-approved `Edit` and `Write` operations.
No second file-reading engine is provided.
