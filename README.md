# buffer

A [Claude Code](https://claude.com/claude-code) skill that queues tasks in FIFO
order and works through them one at a time — **surviving usage limits** by
handing off to a detached background daemon.

```bash
bq "regenerate the AXI testbench for the new burst-length param"
bq "run vivado synth on branch feat/dma and summarise timing failures"
bq                 # show queue + daemon status
```

Queue three things, close the terminal, go to bed. If a limit hits at 2am the
daemon sleeps through the reset and picks up exactly where it stopped.

## Why a daemon

A skill is instructions loaded into a running Claude. When a usage limit hits,
that process **stops** — there is no Claude left to wait out the reset.

`scripts/drain.py --daemon` is an ordinary detached OS process, not a Claude
session. It holds no context and burns no tokens while it sleeps. When the reset
arrives it runs the queued tasks through `claude -p`, reopening the conversation
it was interrupted in. It survives the terminal closing and the session that
started it.

| Part | Who | Survives a limit? |
|---|---|---|
| Enqueue, report position | Claude, in-session | n/a |
| Execute while the session is alive | Claude, in-session | No |
| Wait out the reset, then execute | Detached daemon | Yes |

## Two entry points

| | Works when locked out? | Use when |
|---|---|---|
| `bq "task"` (shell) | **Yes** | Always. This is the real entry point. |
| `/buffer task` (Claude Code) | No | Convenience while you're already in a session. |

`/buffer` needs a live session to type into, so it stops being available exactly
when you're locked out. Build the habit on `bq`.

## Install

Python 3.10+. No dependencies. Linux, macOS, Windows.

```bash
git clone https://github.com/meysamshakiba/claude-buffer.git buffer && cd buffer
```

Then follow [INSTALL.md](INSTALL.md) from step 1's second block onward — the
clone puts you in the same place unpacking the zip would.

## Layout

```
SKILL.md              instructions Claude loads for the skill
commands/buffer.md    the /buffer slash command
scripts/buffer_queue.py   queue state: add, claim, done, fail, list
scripts/drain.py          the daemon: executes tasks, sleeps through limits
bin/bq, bin/bq.cmd        shell entry point (POSIX / Windows)
```

State lives in `~/.claude/buffer/` — `queue.md` (plain markdown, hand-editable),
`drain.log`, `drain.pid`. Override the location with `CLAUDE_BUFFER_QUEUE`.

## Notes

**Write each task like a prompt to a stranger.** The daemon passes the text
straight through with no surrounding conversation. "Fix the bug we discussed"
will fail; "fix the off-by-one in axi_fifo.sv line 214" will not.

**Tasks are independent by default.** Each is a fresh session, so task 2 knows
nothing about task 1. `--chain` threads them together at the cost of a poisoned
early session infecting the rest.

**A task the limit cut in half resumes its own chat.** The daemon records the
session and reopens it after the reset, so the task continues rather than
starting over. The id is stored on the task in `queue.md`, so this holds even
if the daemon is stopped during the wait.

**Scope permissions per task, not globally.** An unattended overnight queue
running `--dangerously-skip-permissions` will do anything the queue contains.

**Queued work gets the same judgment as typed work.** Nobody is watching, which
raises the bar rather than lowering it — irreversible steps still wait for you.

## Development

```bash
pip install pytest ruff
python -m pytest        # 26 tests, no network, no `claude` needed
python -m ruff check scripts tests
```

CI runs the suite on Linux, macOS and Windows across Python 3.10 and 3.13 —
the locking, liveness and detachment paths differ per platform, so the matrix
is doing real work.

## License

MIT — see [LICENSE](LICENSE).
