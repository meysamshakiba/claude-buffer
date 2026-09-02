---
name: buffer
description: Queue tasks in FIFO order and work through them one at a time, surviving usage limits by handing off to a detached background daemon. Use this skill whenever the user prefixes a request with /buffer, or asks to queue, buffer, defer, stack up, or line up work to run later — and also when they ask what's in the queue, want to drain or resume a queue, want tasks to continue automatically after a usage limit resets, or want work to keep going overnight or while they're away. Use it even if they don't say the word "queue".
---

# Buffer

Append tasks to a durable FIFO queue and execute them in the order they were
asked. If a usage limit interrupts things, a detached daemon waits out the
reset and resumes from where it stopped — nothing is lost, nothing jumps the
line.

## Architecture, and why it's split

A skill is instructions loaded into a running Claude. When a usage limit hits,
that process **stops**. There is no Claude left to wait out the reset.

The way around this is `scripts/drain.py --daemon`: an ordinary detached OS
process, not a Claude session. It holds no context and burns no tokens while it
sleeps. When the reset arrives it runs the queued tasks through `claude -p` —
reopening the conversation it was interrupted in, or starting a fresh one for a
task not yet begun. It survives the terminal closing and the session that
started it.

| Part | Who | Survives a limit? |
|---|---|---|
| Enqueue, report position | You, in-session | n/a |
| Execute while the session is alive | You, in-session | No |
| Wait out the reset, then execute | Detached daemon | Yes |

So: **never tell the user you'll personally wait for their limit to reset.** You
won't be running. Either the daemon is up — say so, and say where the log is —
or the queue is merely saved and they must restart something. A user who
believes work is grinding away overnight and finds an untouched queue in the
morning has lost a night.

## Commands

`scripts/buffer_queue.py` owns all state. Add `--json` for parseable output.
The interpreter below is `python3` on Linux/macOS and `python` on Windows,
where the `python3` alias usually isn't on PATH — use whichever exists.

```bash
python3 scripts/buffer_queue.py add "do task1"   # append to the back
python3 scripts/buffer_queue.py list [--all]     # --all includes done/failed
python3 scripts/buffer_queue.py claim            # mark next pending as running
python3 scripts/buffer_queue.py done <id>
python3 scripts/buffer_queue.py fail <id> --note "why"
python3 scripts/buffer_queue.py requeue <id>     # running/failed -> pending
python3 scripts/buffer_queue.py reset            # crash recovery
python3 scripts/buffer_queue.py remove <id>
python3 scripts/buffer_queue.py status
python3 scripts/buffer_queue.py clear [--failed]
```

`scripts/drain.py` executes them:

```bash
python3 scripts/drain.py --daemon --watch    # detach; survives this session
python3 scripts/drain.py --status            # is a daemon up?
python3 scripts/drain.py --tail 30           # recent daemon log
python3 scripts/drain.py --stop
python3 scripts/drain.py                     # foreground, exit when empty
```

Queue lives at `$CLAUDE_BUFFER_QUEUE`, else `~/.claude/buffer/queue.md`. It's
plain markdown with checkboxes — the user can open and hand-edit it.

## Handling `/buffer`

**`/buffer <task>`** — add it, then make sure something will run it:

1. `add` the task verbatim. Don't rewrite, expand, or "improve" the wording. The
   user may be queuing five things quickly and won't be around to correct a
   misreading.
2. Report the position: `Queued at #3. Two ahead of it.`
3. Check `drain.py --status`. If a daemon is up, stop here — it'll be picked up.
   Two workers on one queue duplicates work.
4. If no daemon and the queue has more than one task, or the user has signalled
   they're leaving, start one: `drain.py --daemon --watch`. Tell them the pid
   and the log path.
5. Otherwise, for a single task with the user present, just do it inline (below)
   — spawning a daemon for one task they're watching is overkill.

**`/buffer` with no task** — show `list` and daemon status, then offer to drain.

**Other subcommands** — `status`, `list`, `drain`, `stop`, `clear`,
`remove <id>`.

## Draining inline

Work strictly in queue order, one task at a time:

1. `reset` first, to recover anything a previous crash left as running.
2. `claim` the next task.
3. Do the task — normally, with full tool access. It's a real request.
4. `done` it, or `fail` it with a one-line reason.
5. Repeat until `claim` returns nothing.

Between tasks give one line of progress, not a report: `✓ task1 done. Starting
task2 (1 left).` They queued things to avoid watching; don't make them read a
lot to find out where you are.

If a task is ambiguous, **fail it with a note and move on** rather than stopping
to ask. The point of a queue is that nobody is watching; blocking the whole line
on one question defeats it. Collect such questions and surface them at the end.

If you can feel the session degrading — very long context, or a task that turns
out to be enormous — say so and hand off to the daemon rather than starting
something you may not finish.

## Things worth telling the user

**Tasks are independent by default.** Each daemon-run task is a fresh `claude -p`
session, so task2 knows nothing about task1. If the tasks build on each other,
`--chain` threads them into one session via `--resume`. Default is off, because
chained context grows and a poisoned early session then infects the rest.

**An interrupted task resumes its own conversation.** If a limit stops a task
halfway, the daemon records which session it was in and reopens that same chat
after the reset, so the half-finished work is still in view and the task carries
on instead of starting over. Automatic, and separate from `--chain` — that
threads *different* tasks together, this repairs *one* task. The session id
lives on the task in `queue.md`, so it survives the daemon being stopped
mid-wait. If the conversation can't be reopened, the task runs cold rather than
stalling on a chat that no longer exists.

**Two clocks.** Session limits reset in hours; weekly limits can be days out.
`--max-sleep` (default 6h) stops the daemon rather than letting it silently
sleep for a week. The queue survives; they restart it after the weekly reset.

**Write the task like a prompt to a stranger.** The daemon passes the text
straight through with no surrounding conversation. "Fix the bug we discussed"
will fail. "Fix the off-by-one in axi_fifo.sv line 214" will not.

## Judgment

Queued tasks run with less oversight, because the user isn't watching. That
doesn't lower the bar for what's acceptable — it raises it. A queued task gets
the same treatment as one typed directly: if it's something you'd decline or
check in about interactively, `fail` it with a note explaining why and continue
down the queue. Don't let the batching framing carry a request past a judgment
you'd otherwise make.

Same for irreversible actions — sending, publishing, deleting, purchasing.
Queuing is not blanket pre-authorization for whatever the queue contains. Do the
reversible work and leave the irreversible step for the user to confirm.
