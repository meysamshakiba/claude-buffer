---
description: Queue a task to run in order, surviving usage limits
argument-hint: <task> | status | list | drain | clear | remove <id>
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

Use the `buffer` skill to handle this request: `$ARGUMENTS`

- No arguments → show the queue and offer to drain it.
- `status`, `list`, `drain`, `clear`, `remove <id>`, `pause` → that subcommand.
- Anything else → treat the whole argument string as a task to append,
  verbatim, then follow the skill's draining rules.
