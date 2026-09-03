# Setup

Python 3.10+. No dependencies. Linux, macOS, Windows.

## 1. Install

**Both blocks below run from the extracted `buffer/` directory** — the one
containing `SKILL.md`, `scripts/`, `commands/` and `bin/`. Every path in them is
relative to it. Unpack first:

```bash
unzip buffer-skill.zip && cd buffer
```

```powershell
Expand-Archive buffer-skill.zip -DestinationPath $env:TEMP\bq-install -Force
Set-Location $env:TEMP\bq-install\buffer
```

Linux / macOS:

```bash
[ -f SKILL.md ] || echo "WRONG DIRECTORY - cd into the extracted buffer/ first"
mkdir -p ~/.claude/skills/buffer ~/.claude/commands ~/.local/bin
cp -r SKILL.md scripts ~/.claude/skills/buffer/
cp commands/buffer.md ~/.claude/commands/
cp bin/bq ~/.local/bin/ && chmod +x ~/.local/bin/bq
```

Windows (PowerShell) — you install `bin\bq.cmd` rather than `bin/bq`, but you
still type `bq`. The whole thing is one `& { ... }` block on purpose: pasted into
a console, a bare sequence of lines would keep going after a failure and edit
your PATH even though nothing was copied.

```powershell
& {
  if (-not (Test-Path .\SKILL.md)) {
    Write-Error "Run this from the extracted buffer\ folder - no SKILL.md here."
    return
  }
  $skill = "$env:USERPROFILE\.claude\skills\buffer"
  $cmds  = "$env:USERPROFILE\.claude\commands"
  $bin   = "$env:LOCALAPPDATA\Programs\bq"
  New-Item -ItemType Directory -Force $skill, $cmds, $bin | Out-Null

  Copy-Item SKILL.md $skill
  Copy-Item scripts $skill -Recurse -Force
  Copy-Item commands\buffer.md $cmds
  Copy-Item bin\bq.cmd $bin

  # put bq on PATH for future terminals
  $user = [Environment]::GetEnvironmentVariable("Path", "User")
  if ($user -notlike "*$bin*") {
    [Environment]::SetEnvironmentVariable("Path", "$user;$bin", "User")
  }
  "Installed. Open a new terminal, then run: bq"
}
```

The PATH change only affects **new** terminals. Open a fresh one and run `bq` to
confirm, then restart Claude Code so it picks up the skill and `/buffer`.

## 2. Two entry points, and when each applies

| | Works when locked out? | Use when |
|---|---|---|
| `bq "task"` (shell) | **Yes** | Always. This is the real entry point. |
| `/buffer task` (Claude Code) | No | Convenience while you're already in a session. |

`/buffer` needs a live session to type into, so it stops being available exactly
when you're locked out. `bq` is a plain shell command and always works. Build the
habit on `bq`.

Both append to the same queue and both start the daemon if it isn't running.

```bash
bq "regenerate the AXI testbench for the new burst-length param"
bq "run vivado synth on branch feat/dma and summarise timing failures"
bq                 # show queue + daemon status
bq log             # tail the daemon log
bq stop
```

## 3. The daemon

The piece that survives usage limits. `bq` starts it automatically; to manage it:

```bash
python3 ~/.claude/skills/buffer/scripts/drain.py --daemon --watch
python3 ~/.claude/skills/buffer/scripts/drain.py --status
python3 ~/.claude/skills/buffer/scripts/drain.py --tail 30
python3 ~/.claude/skills/buffer/scripts/drain.py --stop
```

On Windows the interpreter is `python` (the `python3` alias is usually absent)
and the skill lives under `%USERPROFILE%\.claude\skills\buffer`. `bq` honours
a `PYTHON` environment variable if you need a specific interpreter.

It detaches from the terminal, so closing the terminal or losing the Claude
session doesn't kill it. While waiting for a reset it's an idle OS process —
zero tokens, zero context.

When the reset arrives it reopens the conversation the interrupted task was in,
rather than running it again from scratch. Nothing to configure; you'll see
`Resuming [id] in session ...` in the log.

One queue, many writers - `bq`, the daemon, and any Claude session draining
inline all share it. A claim records who took it and keeps a heartbeat, so
`bq` shows you `<- drain-host:9182, last seen 2m ago` rather than a bare `[~]`
you cannot interpret. Anything that stops checking in for `--stale-after` is
treated as abandoned and retried; anything still checking in is left alone.
That is also why `reset` no longer requeues everything - pass `--force` for the
old behaviour, and only when you know you are the only worker.

State lives together, in `~/.claude/buffer/` — on Windows,
`%USERPROFILE%\.claude\buffer\`:

- `queue.md` — the queue (plain markdown, hand-editable)
- `drain.log` — daemon output
- `drain.pid` — running daemon

Override with `CLAUDE_BUFFER_QUEUE`. For a per-project queue, set it to
`.claude/buffer/queue.md` and install the skill under the repo's `.claude/`.

## 4. Make it automatic

The daemon survives a terminal closing, a session ending and a usage limit. It
does not survive a reboot, and nothing notices if it dies -- which defeats the
point, since you are not watching.

```bash
bq autostart            # register it; also starts one now
bq autostart status
bq autostart uninstall
```

On Windows this registers a Task Scheduler entry that runs `drain.py --daemon`
every 15 minutes. That command does nothing when a daemon is already up, so it
is a cheap supervisor rather than a second worker: it covers logon, and it
brings the daemon back if it is ever killed. It runs only while you are logged
on, because it uses your own Claude credentials.

Elsewhere `bq autostart` prints a systemd user timer and a launchd plist to
paste; the same reasoning applies to both.

**Tasks remember where they were queued.** `bq "fix the failing test"` records
your current directory and the daemon runs that task there, so one daemon
started anywhere serves every project. A task queued before this existed, or
from a directory since deleted, runs wherever the daemon is -- with a line in
the log saying so.

## 5. Capturing work when you're away

`bq` needs a terminal and `/buffer` needs a live Claude session. Both are on the
machine, and a usage limit takes the session away -- so the moment you most need
to record an idea is the moment both ingresses are gone.

Nothing outside Claude can push a message into a Claude session; input to a
Remote Control session flows cloud to laptop, one way. So the ingress is
something the daemon reads instead:

```
~/.claude/buffer/inbox/        drop a file here; it becomes a task
~/.claude/buffer/inbox/done/   where it goes once queued
```

One file, one task; the text is the prompt. An optional first line `cwd: <path>`
says where to run it. Files are archived rather than deleted, so a bad enqueue
is still readable. A file younger than a few seconds is left alone, because a
sync client may still be writing it and half an idea cannot be repaired later.

That makes the transport your choice -- anything that can write a file:

- a folder synced from your phone (Dropbox, OneDrive, Syncthing) pointed at the
  inbox with `CLAUDE_BUFFER_INBOX`
- a chat bot that appends what you send it
- `scp`, a cron job, a Shortcut, an email fetcher

The queue then does what it always did: one at a time, in order, through limits.

## 6. Leaving it running overnight

The case this is built for: queue what you already know you want at 8pm, read a
report at 8am.

```bash
bq autostart -- -- --checkpoint   --allowedTools "Read,Grep,Glob,Edit,Write,Bash"   --disallowedTools "Bash(rm *),Bash(git push *),Bash(git reset --hard *)"
bq report            # in the morning
```

`--checkpoint` commits the working tree after every task, tagged with the task
id, so the morning is "review seven commits" rather than "diff twelve hours of
mixed edits" -- and any one of them is `git revert`-able. Anything already
uncommitted when a task starts is committed separately first, so reverting the
task does not take your own work with it. It never pushes.

**A deny-list is not a boundary.** `rm` is reachable through `python -c`,
`find -delete`, `git clean`. Denying the obvious commands prevents accidents;
it does not contain a determined path. The commit per task is what actually
makes the night reversible, which is why the two belong together.

`bq report` prints what ran, the commit and file stats for each, anything that
deleted files, and a "needs you" list of failures with their reasons.

## 7. Don't wait at all (optional)

API billing is metered separately from your subscription, so a locked-out
subscription doesn't block an API key:

```bash
export BUFFER_FALLBACK_API_KEY=sk-ant-...
python3 ~/.claude/skills/buffer/scripts/drain.py --daemon --watch --fallback-api-key
```

```powershell
$env:BUFFER_FALLBACK_API_KEY = "sk-ant-..."
python "$env:USERPROFILE\.claude\skills\buffer\scripts\drain.py" `
  --daemon --watch --fallback-api-key
```

Set the key in the shell that *starts* the daemon. The detached process inherits
the environment as it exists at spawn time, so exporting it afterwards has no
effect on a daemon that's already running.

On a usage limit the daemon retries the task on the API key instead of sleeping.
This costs money per token — that's the trade. Without the flag it just waits.

## 8. Flags

| Flag | Why |
|---|---|
| `--fallback-api-key` | Keep working through a lockout on metered billing |
| `--chain` | Thread tasks into one session so task2 sees task1's work |
| `--max-sleep 21600` | Stop rather than sleep past this (weekly-limit guard) |
| `--stale-after 1800` | How long a silent claim is left alone before it's retried |
| `--checkpoint` | Commit the tree after each task, so a night's work is reviewable |
| `--report [h]` | What ran, what changed, what needs you (also `bq report`) |
| `--max-retries 3` | Attempts before a task is marked failed |
| `--timeout 3600` | Per-task wall clock |
| `--` | Everything after it goes to the CLI: `-- --model opus --allowedTools Read,Edit` |

Scope permissions per task rather than globally. An unattended overnight queue
running `--dangerously-skip-permissions` will do anything the queue contains:

```bash
python3 drain.py --daemon --watch -- --allowedTools "Read,Edit,Bash(vivado *)"
```

Everything after `--` goes straight to the CLI. `--claude-arg` takes a single
value and needs `--claude-arg=--flag` for anything starting with a dash, so `--`
is usually what you want.

## 9. Windows notes

Supported natively. You install `bin\bq.cmd` rather than `bin/bq`, but you
still *type* `bq` — PowerShell and cmd resolve it through `PATHEXT`.

- **The queue lock is real.** `msvcrt` byte-range locks stand in for `fcntl`, so
  `bq` and the daemon can write to the queue simultaneously — which is the
  normal case, not an edge case. Without it, concurrent `bq` calls silently drop
  tasks and two workers can claim the same one.
- **Writes retry.** `os.replace` is atomic on Windows but fails outright if an
  indexer or AV scanner has the queue open for a moment, so it retries briefly.
- **`bq stop` uses `taskkill /T`**, taking the daemon's in-flight `claude` down
  with it rather than orphaning it.
- **Console output is forced to UTF-8**, so a task containing `✓` or CJK doesn't
  crash `bq list` on a legacy codepage. The queue file is always UTF-8.
- Detachment uses `DETACHED_PROCESS`; liveness uses `tasklist`, matched on the
  PID column rather than by substring.

Under WSL you get the POSIX path (`fcntl`) instead, unchanged.
