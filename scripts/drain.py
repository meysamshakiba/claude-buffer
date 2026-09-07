#!/usr/bin/env python3
"""Drain the buffer queue through `claude -p`, sleeping through usage limits.

This is the part that cannot live inside a skill. When a usage limit hits, the
Claude process exits — so something outside it has to notice, wait for the
reset, and start the next attempt. That's this script. While it sleeps it is
an ordinary OS process: it costs no tokens and holds no context.

    python3 drain.py                  # drain in the foreground, exit when empty
    python3 drain.py --watch          # stay alive, pick up new tasks as they arrive
    python3 drain.py --daemon --watch # detach and survive this terminal/session
    python3 drain.py --stop           # stop a running daemon
    python3 drain.py --tail           # show recent daemon log

Tasks run strictly in queue order. A task interrupted by a usage limit keeps
its position and is retried first after the reset — nothing jumps the line.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import notify
import sessions
from buffer_queue import (
    STALE_AFTER,
    Queue,
    QueueLocked,
    default_worker,
    queue_path,
    setup_console,
)  # noqa: E402

IS_WIN = os.name == "nt"

# Claude Code reports limits in a few shapes. The epoch form is unambiguous:
#   "Claude AI usage limit reached|1735689600"
LIMIT_EPOCH_RE = re.compile(r"usage limit reached\s*\|\s*(\d{9,})", re.I)
# The human form carries a reset time but no date:
#   "You've hit your session limit · resets 3:45pm"
#   "You've hit your weekly limit · resets Mon 12:00am"
LIMIT_CLOCK_RE = re.compile(
    r"(?:hit|reached) your (?P<kind>session|weekly|opus|usage)?\s*limit"
    r".{0,40}?resets?\s+(?P<when>(?:\w{3}\s+)?\d{1,2}:\d{2}\s*(?:am|pm)?)",
    re.I | re.S,
)
LIMIT_TEXT_RE = re.compile(
    r"(usage limit reached"
    r"|reached your (usage |session |weekly )?limit"
    r"|hit your (usage |session |weekly |opus )?limit"
    r"|limit reached\s*[·\-–]\s*resets"
    r"|429 too many requests"
    r"|rate.?limit(?:ed)? by the api)",
    re.I,
)

DEFAULT_BACKOFF = 15 * 60          # reset time unknown
DEFAULT_MAX_SLEEP = 6 * 3600       # refuse to silently sleep longer than this
CLOCK_GRACE = timedelta(minutes=10)  # a reset this recently past has passed

# Sent when resuming a task the limit cut short. The conversation being resumed
# already contains the original request and whatever work got done, so repeating
# the task verbatim would invite starting over.
RESUME_PROMPT = (
    "You were interrupted by a usage limit partway through this task:\n\n"
    "{text}\n\n"
    "The conversation above is your own work on it so far. Continue from where "
    "you stopped: finish what is unfinished, and don't redo what is already done."
)
# Used when the conversation itself is gone — expired, pruned, another machine.
# The handover the daemon assembled is all the context that survives.
COLD_RESUME_PROMPT = (
    "This task was started before and interrupted by a usage limit. The earlier "
    "conversation can no longer be opened, so here is what is known about it.\n\n"
    "{summary}\n\n"
    "---\n\nThe original task was:\n\n{text}\n\n"
    "Continue it. Treat the work described above as already done — verify it "
    "rather than repeating it — and finish what is left."
)
# A stored session that the CLI won't resume — expired, pruned, or from another
# machine — must not strand the task on a conversation that no longer exists.
RESUME_FAILED_RE = re.compile(
    r"(no conversation found|session .{0,60}?not found"
    r"|could not resume|invalid session|--resume)",
    re.IGNORECASE,
)
# A limit doesn't count against a task's retries, so a task misread as limited
# would retry forever. Bound it: past this many, treat it as a real failure.
MAX_LIMIT_HITS = 5
HEARTBEAT_EVERY = 300              # well inside STALE_AFTER, cheap to write
INBOX_SETTLE = 5                   # let a syncing file finish landing
WORKER_ID = f"drain-{default_worker()}"


# Set once the queue actually in use is known. Without it every derived file --
# the log, the pid, the database, the summaries -- lands next to the *default*
# queue even when --file points somewhere else, so a second queue silently
# shares the first one's state. See issue #5.
_state_root: Path | None = None


def use_state_for(queue: Path) -> None:
    global _state_root
    _state_root = queue.parent


def state_dir() -> Path:
    d = _state_root or queue_path().parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def pid_file() -> Path:
    return state_dir() / "drain.pid"


def state_file() -> Path:
    return state_dir() / "drain.state"


def db_path() -> Path:
    return state_dir() / "sessions.db"


def where_of(cwd: str | None) -> str:
    return f" in {cwd}" if cwd else ""


def summaries_dir() -> Path:
    return state_dir() / "summaries"


def set_state(**kw) -> None:
    """Publish what the daemon is doing.

    Sleeping off a limit and being wedged look identical from the queue: the
    task sits at [~] and nothing happens for hours. That ambiguity reads as a
    broken tool at exactly the moment the tool is doing its job, so the daemon
    says so out loud. Best effort — bookkeeping must never break the drain.
    """
    try:
        state_file().write_text(
            json.dumps({"pid": os.getpid(), **kw}), encoding="utf-8"
        )
    except OSError:
        pass


def read_state() -> dict:
    try:
        return json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def describe_state(state: dict, now: float | None = None) -> str:
    """One line for `bq`, or empty when there is nothing worth saying."""
    now = time.time() if now is None else now
    kind = state.get("state")
    task = f" [{state['task']}]" if state.get("task") else ""

    if kind == "locked_out":
        left = int(state.get("until", 0) - now)
        if left > 0:
            when = datetime.fromtimestamp(state["until"]).strftime("%a %H:%M")
            return (f"locked out by the {state.get('reason', 'limit')} until "
                    f"{when} ({left // 3600}h {left % 3600 // 60}m); "
                    f"not spending calls until then")
        return "lockout has lifted; next check picks the queue back up"
    if kind == "sleeping":
        left = int(state.get("until", 0) - now)
        if left > 0:
            when = datetime.fromtimestamp(state["until"]).strftime("%a %H:%M")
            reason = state.get("reason", "limit")
            return (f"waiting out the {reason} until {when} "
                    f"({left // 60}m left), then resumes{task}")
        return f"reset reached; picking{task} back up"
    if kind == "running":
        for_min = int((now - state.get("since", now)) // 60)
        return f"running{task} for {for_min}m"
    if kind == "idle":
        return "idle, watching for new tasks"
    return ""


def log_file() -> Path:
    return state_dir() / "drain.log"


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# A notification that silently isn't going out is worse than none at all: the
# whole point is being told without having to ask. Route ntfy's own complaints
# into the daemon log, where `bq log` will find them.
notify.set_reporter(lambda msg: log(f"ntfy: {msg}"))


# -- daemon lifecycle ------------------------------------------------------


def daemon_pid() -> int | None:
    """PID of a live daemon, or None. Cleans up stale pid files."""
    pf = pid_file()
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text().strip())
    except ValueError:
        pf.unlink(missing_ok=True)
        return None
    if IS_WIN:
        # /NH /FO CSV so we compare the PID field itself. A bare substring
        # search also matches the memory column ("12,296 K") of any process.
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout
        alive = any(
            len(row) >= 2 and row[1].strip() == str(pid)
            for row in csv.reader(io.StringIO(out))
        )
    else:
        try:
            os.kill(pid, 0)
            alive = True
        except (ProcessLookupError, PermissionError):
            alive = False
    if not alive:
        pf.unlink(missing_ok=True)
        return None
    return pid


def spawn_daemon(argv: list[str]) -> int:
    """Relaunch this script detached from the current process group, so it
    outlives the terminal and the Claude session that started it."""
    existing = daemon_pid()
    if existing:
        log(f"Daemon already running (pid {existing}). Nothing to do.")
        return existing

    cmd = [sys.executable, str(Path(__file__).resolve()), *argv]
    logf = open(log_file(), "a", encoding="utf-8")
    kwargs: dict = {
        "stdout": logf,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if IS_WIN:
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **kwargs)
    pid_file().write_text(str(proc.pid))
    log(f"Daemon started (pid {proc.pid}). Log: {log_file()}")
    return proc.pid


def stop_daemon() -> int:
    pid = daemon_pid()
    if not pid:
        log("No daemon running.")
        return 1
    if IS_WIN:
        # /T: the daemon's in-flight `claude` is a child, and Windows has no
        # process-group signal that would reach it otherwise.
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        os.kill(pid, signal.SIGTERM)
    pid_file().unlink(missing_ok=True)
    state_file().unlink(missing_ok=True)
    log(f"Stopped daemon (pid {pid}). In-flight task returns to pending on next start.")
    return 0


# -- limit detection -------------------------------------------------------


def parse_clock(when: str) -> int | None:
    """Turn 'resets 3:45pm' or 'resets Mon 12:00am' into an epoch.

    Only a time-of-day is given, so assume the next occurrence. For weekday
    forms, walk forward to that weekday.
    """
    when = when.strip()
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    target_dow = None
    parts = when.split()
    if len(parts) == 2:
        cand = parts[0][:3].lower()
        if cand in days:
            target_dow = days.index(cand)
        when = parts[1]

    compact = when.replace(" ", "").lower()
    for fmt in ("%I:%M%p", "%H:%M"):
        try:
            t = datetime.strptime(compact, fmt)
            break
        except ValueError:
            continue
    else:
        return None

    now = datetime.now()
    # A reset time a little in the past has just passed — clock skew against
    # the server, or the message sat in a buffer for a moment. Rolling those
    # forward a whole day makes the daemon wait ~24h for a limit that has
    # already lifted, or exceed --max-sleep and quit outright. Treat the
    # recent past as now and let sleep_until fall through to an immediate retry.
    cutoff = now - CLOCK_GRACE
    candidate = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if target_dow is not None:
        delta = (target_dow - candidate.weekday()) % 7
        candidate += timedelta(days=delta)
        if candidate <= cutoff:
            candidate += timedelta(days=7)
    elif candidate <= cutoff:
        candidate += timedelta(days=1)
    return int(candidate.timestamp())


def detect_limit(
    text: str, failed: bool = True, harness_text: str | None = None
) -> tuple[int | None, str]:
    """Returns (reset_epoch | 0 | None, kind).

    0 means "limit hit, reset time unknown". None means "not a limit".

    The epoch and clock forms are specific enough to trust anywhere in the
    output. The fuzzy text form is not: phrases like "429 too many requests"
    show up in the output of any task that touches an HTTP client, and reading
    one as a usage limit puts the daemon to sleep on a schedule of its own
    invention. So it is matched only against `harness_text` — the channel the
    CLI itself speaks on — and only when the command actually failed.
    """
    m = LIMIT_EPOCH_RE.search(text)
    if m:
        return int(m.group(1)), "usage"

    m = LIMIT_CLOCK_RE.search(text)
    if m and (failed or m.group("kind")):
        epoch = parse_clock(m.group("when"))
        return (epoch or 0), (m.group("kind") or "session").lower()

    fuzzy = text if harness_text is None else harness_text
    if failed and LIMIT_TEXT_RE.search(fuzzy):
        return 0, "unknown"
    return None, ""


def wait_for(epoch: int, max_sleep: int, pad: int = 60) -> tuple[int, bool]:
    """Seconds until a reset, and whether that is longer than we will wait.

    Shared with the notification so the phone is told the plan the daemon
    actually follows, rather than a second guess at it.
    """
    remaining = max(0, epoch + pad - int(time.time()))
    return remaining, remaining > max_sleep


def limit_message(kind: str, tid: str, reset_epoch: int, max_sleep: int,
                  fallback: bool) -> str:
    """One line for a phone: which limit, when it lifts, what happens next.

    "Usage limit" on its own is the message that gets someone out of bed to
    check. What they actually need to know is whether the queue is still
    moving without them.
    """
    when = (f"Resets {datetime.fromtimestamp(reset_epoch + 60):%a %H:%M}"
            if reset_epoch else "Reset time unknown")
    if fallback:
        plan = "retrying now on API-key billing instead of waiting"
    elif not reset_epoch:
        plan = f"retrying in {DEFAULT_BACKOFF // 60}m"
    else:
        remaining, too_long = wait_for(reset_epoch, max_sleep)
        plan = (
            "beyond --max-sleep, so the daemon is stopping; the queue is intact "
            "and picks up when it is restarted after the reset"
            if too_long else f"sleeping {remaining // 60}m, then [{tid}] continues"
        )
    return f"{kind} limit hit on [{tid}]. {when}; {plan}."


def drained_message(completed: dict[str, int]) -> str:
    done, failed = completed.get("done", 0), completed.get("failed", 0)
    tally = f"{done} done" + (f", {failed} failed" if failed else "")
    return f"{tally}. Nothing left in the queue."


def sleep_until(epoch: int, kind: str, max_sleep: int, pad: int = 60) -> bool:
    """Sleep until the reset. Returns False if the wait exceeds max_sleep, so
    a 7-day weekly lockout doesn't turn into a silent week-long sleep."""
    target = epoch + pad
    remaining, too_long = wait_for(epoch, max_sleep, pad)
    if remaining <= 0:
        return True
    if too_long:
        log(
            f"{kind} limit resets {datetime.fromtimestamp(target):%a %H:%M} "
            f"({remaining // 3600}h away), beyond --max-sleep. Stopping. "
            f"Queue is intact — restart the daemon after the reset."
        )
        return False
    log(
        f"{kind} limit hit. Sleeping {remaining // 60}m until "
        f"{datetime.fromtimestamp(target):%a %H:%M:%S}."
    )
    while remaining > 0:
        time.sleep(min(60, remaining))
        remaining = max(0, target - int(time.time()))
    return True


# -- task execution --------------------------------------------------------


def run_task(text: str, cli: str, extra: list[str], timeout: int,
             resume: str | None, use_api_key: bool = False,
             cwd: str | None = None) -> tuple[bool, str, str | None, str]:
    """Run one task. Returns (ok, combined_output, session_id, harness_output).

    harness_output is the subset of the output the CLI itself produced —
    stderr, plus the result payload when it is flagged as an error. Task prose
    lands in stdout, so keeping the two apart is what stops a task that merely
    *discusses* rate limits from being mistaken for one (see detect_limit).

    With use_api_key, run against BUFFER_FALLBACK_API_KEY instead of the
    subscription. API billing is metered separately, so this keeps working
    while the subscription window is exhausted.
    """
    cmd = [cli, "-p", text, "--output-format", "json"]
    if resume:
        cmd += ["--resume", resume]
    cmd += extra

    env = os.environ.copy()
    if use_api_key:
        env["ANTHROPIC_API_KEY"] = os.environ["BUFFER_FALLBACK_API_KEY"]

    # The task was queued from a particular project; the daemon may have been
    # started at logon from somewhere else entirely. Run it where it belongs,
    # but don't fail outright if that directory has since moved.
    if cwd and not Path(cwd).is_dir():
        log(f"Directory {cwd} no longer exists; running in {os.getcwd()} instead.")
        cwd = None

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", env=env, cwd=cwd,
        )
    except FileNotFoundError:
        msg = f"`{cli}` not found on PATH"
        return False, msg, None, msg
    except subprocess.TimeoutExpired:
        msg = f"timed out after {timeout}s"
        return False, msg, None, msg

    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    harness = proc.stderr or ""
    session_id = None
    result_text = ""
    try:
        payload = json.loads((proc.stdout or "").strip())
        session_id = payload.get("session_id")
        result_text = payload.get("result") or ""
        if payload.get("is_error"):
            combined += "\n" + str(result_text)
            harness += "\n" + str(result_text)
    except (json.JSONDecodeError, AttributeError, TypeError):
        result_text = (proc.stdout or "").strip()

    if result_text:
        print(str(result_text).rstrip()[:4000], flush=True)
    return proc.returncode == 0, combined, session_id, harness


def inbox_dir() -> Path:
    env = os.environ.get("CLAUDE_BUFFER_INBOX")
    return Path(env).expanduser() if env else state_dir() / "inbox"


def ingest_inbox(path: Path, settle: float = INBOX_SETTLE) -> int:
    """Turn files dropped in the inbox into queued tasks.

    Capture is the part that breaks down when you are over Claude's pace: `bq`
    needs a terminal and `/buffer` needs a live session, and a usage limit takes
    the session away. Nothing outside can push into a Claude session, so the
    ingress has to be something the daemon reads instead — and a directory is
    the most generic version of that. Anything able to write a file becomes a
    way to queue work: a synced folder from a phone, a bot, scp, a cron job.

    One file, one task. First line may be `cwd: <path>` to say where it runs.
    """
    inbox = inbox_dir()
    if not inbox.is_dir():
        return 0

    done = inbox / "done"
    now = time.time()
    added = 0

    for item in sorted(inbox.iterdir(), key=lambda p: p.name):
        if item.is_dir() or item.name.startswith("."):
            continue
        # A file arriving over a sync client or a slow pipe may still be being
        # written. Reading it now would queue half an idea, and the task text
        # is not something we can repair later.
        if now - item.stat().st_mtime < settle:
            continue

        try:
            raw = item.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            log(f"Inbox: cannot read {item.name}: {exc}")
            continue

        # Without a header, fall back to home rather than inheriting the
        # daemon's directory: started from Task Scheduler that is system32, and
        # an unattended session should not be pointed there. Explicit beats
        # "wherever this process happened to be launched".
        cwd = str(Path.home())
        if raw.lower().startswith("cwd:"):
            head, _, rest = raw.partition("\n")
            cwd = head[4:].strip() or cwd
            raw = rest.strip()

        if not raw:
            log(f"Inbox: {item.name} is empty; ignoring.")
        else:
            task = queue_op(path, lambda q: q.add(raw, cwd))
            if task is None:
                continue  # queue busy; leave the file and retry next pass
            added += 1
            log(f"Inbox: queued [{task['id']}] from {item.name}")

        # Move rather than delete: if the enqueue was wrong, the original text
        # is still there to look at.
        try:
            done.mkdir(parents=True, exist_ok=True)
            item.replace(done / f"{int(now)}-{item.name}")
        except OSError as exc:
            log(f"Inbox: queued {item.name} but could not archive it: {exc}")
    return added


# -- git checkpoints -------------------------------------------------------
#
# The permission deny-list stops accidents, not determined paths: rm is
# reachable through `python -c`, `find -delete`, `git clean`. So reversibility
# cannot rest on it. A commit per task is the thing that actually makes a night
# of unattended work undoable -- and it turns the morning into "review seven
# commits" rather than "diff twelve hours of mixed edits".


def _git(root, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def git_root(cwd: str | None) -> Path | None:
    if not cwd or not Path(cwd).is_dir():
        return None
    proc = _git(cwd, "rev-parse", "--show-toplevel")
    out = proc.stdout.strip()
    return Path(out) if proc.returncode == 0 and out else None


def git_dirty(root: Path) -> bool:
    return bool(_git(root, "status", "--porcelain").stdout.strip())


def checkpoint(root: Path, message: str) -> str | None:
    """Commit the working tree. Returns the short sha, or None if there was
    nothing to commit. Never pushes -- publishing stays the user's call."""
    if not git_dirty(root):
        return None
    _git(root, "add", "-A")
    proc = _git(root, "commit", "-m", message)
    if proc.returncode != 0:
        log(f"Checkpoint failed: {(proc.stderr or proc.stdout).strip()[:200]}")
        return None
    return _git(root, "rev-parse", "--short", "HEAD").stdout.strip()


@contextlib.contextmanager
def heartbeating(path: Path, tid: str, worker: str):
    """Keep saying the claim is alive while a task runs.

    A task may legitimately run for --timeout, which is longer than the window
    after which another worker treats a claim as abandoned. Without this beat,
    a healthy long task would eventually be reclaimed and run twice.
    """
    stop = threading.Event()

    def beat():
        while not stop.wait(HEARTBEAT_EVERY):
            queue_op(path, lambda q: q.heartbeat(tid, worker))

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)


def queue_op(path: Path, fn, retries: int = 3):
    """Run one queue mutation, tolerating a busy lock. The daemon must not die
    because `bq` happened to hold the queue at the wrong moment."""
    for i in range(retries):
        try:
            with Queue(path) as q:
                return fn(q)
        except QueueLocked as exc:
            log(f"{exc} (attempt {i + 1}/{retries})")
            time.sleep(5)
    log("Could not update the queue; it may now be out of date.")
    return None


def drain(args, path: Path) -> int:
    use_state_for(path)

    # A previous run hit a limit resetting further out than it was willing to
    # wait. Until that passes there is nothing to do but leave: claiming a task
    # would only spend a call to be told the same thing.
    previous = read_state()
    if previous.get("state") == "locked_out":
        left = int(previous.get("until", 0) - time.time())
        if left > 0:
            log(f"Still locked out for {left // 60}m "
                f"({previous.get('reason', 'limit')}). Nothing to do yet.")
            return 0
        log("Lockout has lifted; picking the queue back up.")
    # Only claims nobody is maintaining. The queue is shared with `bq` and with
    # any Claude session draining inline, so a blanket requeue here would drag
    # someone else's in-flight task back to pending underneath them.
    stale = args.stale_after
    n = queue_op(path, lambda q: q.reset_running(stale))
    if n:
        log(f"Requeued {n} abandoned task(s) (no heartbeat for {stale / 60:.0f}m).")

    attempts: dict[str, int] = {}
    limit_hits: dict[str, int] = {}
    chain_session: str | None = None
    completed = {"done": 0, "failed": 0}

    def announce_drained() -> None:
        """Say the queue is empty -- once per drain, not once per idle poll,
        and not at all when this worker never had anything to do."""
        if any(completed.values()):
            notify.event("drained", drained_message(completed))
            completed.update(done=0, failed=0)

    while True:
        # Before claiming, so anything dropped while the last task ran joins
        # the queue in the order it arrived rather than waiting for an idle moment.
        ingest_inbox(path)

        task = queue_op(path, lambda q: q.claim(WORKER_ID))

        if task is None:
            if not args.watch:
                announce_drained()
                log("Queue empty. Done.")
                return 0
            # Idle is the right moment to notice that another worker died
            # holding a task: nothing pending means nothing else to do, and
            # otherwise an abandoned claim would sit there until a restart.
            recovered = queue_op(path, lambda q: q.reset_running(stale))
            if recovered:
                log(f"Recovered {recovered} abandoned task(s) from another worker.")
                continue
            announce_drained()
            set_state(state="idle", since=int(time.time()))
            time.sleep(args.poll)
            continue

        tid, text = task["id"], task["text"]
        attempts[tid] = attempts.get(tid, 0) + 1

        # A limit that cut a previous attempt short left its conversation id on
        # the task. Resuming it means the retry continues the same chat with the
        # work already done still in view, rather than starting the task over.
        resume_sid = task.get("session")
        task_cwd = task.get("cwd")
        start_head = sessions.head_of(task_cwd)
        handover = sessions.latest_summary(db_path(), tid)

        if resume_sid:
            prompt = RESUME_PROMPT.format(text=text)
            log(f"Resuming [{tid}] in session {resume_sid}{where_of(task_cwd)}")
        elif handover:
            # No session to reopen, but this task has been attempted before.
            # Sending it cold would repeat work that is already committed.
            prompt = COLD_RESUME_PROMPT.format(summary=handover, text=text)
            log(f"Restarting [{tid}] from its summary{where_of(task_cwd)}")
        else:
            prompt = text
            log(f"Running [{tid}]{where_of(task_cwd)} {text}")
            resume_sid = chain_session if args.chain else None

        notify.event("started", f"[{tid}] {text}{where_of(task_cwd)}")

        # Commit anything already lying around first, so this task's diff is
        # its own and reverting it doesn't take unrelated work with it.
        root = git_root(task_cwd) if args.checkpoint else None
        if root and git_dirty(root):
            pre = checkpoint(root, f"buffer: uncommitted work found before [{tid}]")
            if pre:
                log(f"Checkpointed pre-existing changes as {pre}")

        set_state(state="running", task=tid, since=int(time.time()))
        with heartbeating(path, tid, WORKER_ID):
            ok, output, session_id, harness = run_task(
                prompt, args.cli, args.claude_arg, args.timeout, resume_sid,
                cwd=task_cwd,
            )
        reset_epoch, kind = detect_limit(output, failed=not ok, harness_text=harness)

        # Don't let a conversation the CLI won't reopen strand the task; drop it
        # and the next attempt starts cold, which is the old behaviour.
        if not ok and task.get("session") and RESUME_FAILED_RE.search(harness):
            log(f"Session {task['session']} could not be resumed; retrying cold.")
            queue_op(path, lambda q: q.set_session(tid, None))
            queue_op(path, lambda q: q.set_status(tid, "pending", "resume failed"))
            attempts[tid] -= 1
            continue

        # Repeatedly "limited" without ever running is indistinguishable from a
        # misdetection, and limits don't consume retries — so this task would
        # hold the head of the queue forever. Call it a failure and move on.
        if reset_epoch is not None and limit_hits.get(tid, 0) >= MAX_LIMIT_HITS:
            note = f"limit detected {MAX_LIMIT_HITS}x without progress; giving up"
            queue_op(path, lambda q: q.set_status(tid, "failed", note))
            completed["failed"] += 1
            notify.event("failed", f"[{tid}] {text}\n{note}")
            log(f"Giving up on [{tid}]: {note}")
            continue

        if reset_epoch is not None:
            attempts[tid] -= 1  # a limit is not the task's fault
            limit_hits[tid] = limit_hits.get(tid, 0) + 1

            # Whatever conversation the interrupted attempt was using is where
            # the half-finished work lives. Record it on the task so the retry
            # resumes it — after a wait that may outlive this process.
            interrupted = session_id or resume_sid
            if interrupted:
                queue_op(path, lambda q: q.set_session(tid, interrupted))
                log(f"Will resume [{tid}] in session {interrupted} after the reset.")

            # Write the handover now, while the daemon is alive and the
            # evidence is fresh. Nothing inside the session could have done
            # this: it was stopped mid-token, and summarising needs a model
            # call, which is the one thing a lockout forbids.
            try:
                summary = sessions.build_summary(
                    task_id=tid, prompt=text, session_id=interrupted,
                    repo=task_cwd, start_head=start_head,
                    attempt=attempts[tid] + 1, reason=f"{kind} limit",
                    result_text=output,
                )
                summaries_dir().mkdir(parents=True, exist_ok=True)
                md = summaries_dir() / f"{tid}-{int(time.time())}.md"
                md.write_text(summary, encoding="utf-8")
                sessions.record(
                    db_path(), task_id=tid, prompt=text, session_id=interrupted,
                    repo=task_cwd, repo_head=start_head, status="interrupted",
                    reason=f"{kind} limit", attempt=attempts[tid] + 1,
                    summary=summary, summary_path=str(md),
                )
                log(f"Wrote handover {md.name}")
            except (OSError, sqlite3.Error) as exc:
                # Bookkeeping must never cost us the queue.
                log(f"Could not record the handover: {exc}")

            # Subscription window is exhausted. If an API key is configured,
            # keep working on metered billing instead of sleeping.
            use_fallback = bool(
                args.fallback_api_key and os.environ.get("BUFFER_FALLBACK_API_KEY")
            )
            notify.event(
                "limit",
                limit_message(kind, tid, reset_epoch, args.max_sleep, use_fallback),
            )

            if use_fallback:
                log(f"{kind} limit hit — retrying [{tid}] on API-key billing.")
                with heartbeating(path, tid, WORKER_ID):
                    ok, output, session_id, harness = run_task(
                        RESUME_PROMPT.format(text=text) if interrupted else text,
                        args.cli, args.claude_arg, args.timeout,
                        interrupted, use_api_key=True, cwd=task_cwd,
                    )
                if ok:
                    if args.chain and session_id:
                        chain_session = session_id
                    queue_op(path, lambda q: q.set_session(tid, None))
                    queue_op(path, lambda q: q.set_status(tid, "done", "via api key"))
                    completed["done"] += 1
                    notify.event("done", f"[{tid}] {text} (via API key)")
                    log(f"Done [{tid}] (api key)")
                    continue
                # The phone was told the queue was still moving on metered
                # billing. It isn't, and the correction matters more than the
                # extra buzz: otherwise the next word arrives hours later.
                log("API-key attempt also failed. Falling back to waiting.")
                notify.event(
                    "limit",
                    limit_message(kind, tid, reset_epoch, args.max_sleep, fallback=False),
                )

            queue_op(path, lambda q: q.set_status(tid, "pending", ""))
            if reset_epoch:
                set_state(state="sleeping", task=tid, until=reset_epoch + 60,
                          reason=f"{kind} limit")
                if not sleep_until(reset_epoch, kind, args.max_sleep):
                    # Remember when the lockout lifts. The supervisor restarts
                    # this process every 15 minutes, and without a record each
                    # restart would claim the task, burn a call to rediscover
                    # the same limit, and exit -- all night, for nothing.
                    set_state(state="locked_out", task=tid, until=reset_epoch + 60,
                              reason=f"{kind} limit")
                    return 2
            else:
                log(f"Limit hit, reset time unknown. Sleeping {DEFAULT_BACKOFF // 60}m.")
                set_state(state="sleeping", task=tid,
                          until=int(time.time()) + DEFAULT_BACKOFF,
                          reason="limit, reset time unknown")
                time.sleep(DEFAULT_BACKOFF)
            continue

        # Commit on failure too: a task that got halfway leaves edits behind,
        # and they need to be as visible and revertable as a successful one's.
        if root:
            outcome = "" if ok else " (task failed)"
            sha = checkpoint(
                root,
                f"buffer[{tid}]: {text[:72]}{outcome}\n\n"
                f"Queued {task['ts']}, run by the buffer daemon.",
            )
            if sha:
                log(f"Committed {sha} in {root}")

        # The task is off the limit path either way now, so the stored session
        # has done its job. Leaving it would resume a finished conversation.
        if task.get("session"):
            queue_op(path, lambda q: q.set_session(tid, None))

        # Close the record either way, so the database shows how a task ended
        # and not only that it once stalled.
        if ok or attempts[tid] >= args.max_retries:
            try:
                sessions.record(
                    db_path(), task_id=tid, prompt=text, session_id=session_id,
                    repo=task_cwd, repo_head=sessions.head_of(task_cwd),
                    status="done" if ok else "failed",
                    reason="" if ok else "attempts exhausted",
                    attempt=attempts[tid],
                )
            except sqlite3.Error as exc:
                log(f"Could not record the run: {exc}")

        if ok:
            if args.chain and session_id:
                chain_session = session_id
            queue_op(path, lambda q: q.set_status(tid, "done"))
            completed["done"] += 1
            notify.event("done", f"[{tid}] {text}")
            log(f"Done [{tid}]")
        elif attempts[tid] < args.max_retries:
            # No notification: this one is coming back round, and a phone that
            # buzzes for an attempt that then succeeds teaches you to ignore it.
            queue_op(path, lambda q: q.set_status(tid, "pending", f"retry {attempts[tid]}"))
            log(f"Failed [{tid}], retrying ({attempts[tid]}/{args.max_retries}).")
            time.sleep(5 * attempts[tid])
        else:
            tail = output.strip().splitlines()[-1] if output.strip() else "no output"
            queue_op(path, lambda q: q.set_status(tid, "failed", tail[:160]))
            completed["failed"] += 1
            notify.event(
                "failed",
                f"[{tid}] {text}\nGave up after {attempts[tid]} attempts: {tail[:160]}",
            )
            log(f"Giving up on [{tid}] after {attempts[tid]} attempts.")


def report(path: Path, hours: float) -> int:
    """What happened while nobody was watching.

    The log is hundreds of lines of Claude prose by morning. What you need is
    which tasks ran, which need you, and what changed on disk -- with anything
    destructive called out rather than buried.
    """
    with Queue(path) as q:
        tasks = list(q.tasks)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent = []
    for t in tasks:
        try:
            when = datetime.strptime(t["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if when >= cutoff:
            recent.append((when, t))

    counts: dict[str, int] = {}
    for _, t in recent:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    headline = ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "nothing"
    print(f"Last {hours:g}h: {headline}.\n")

    needs_you, warnings = [], []
    for when, t in recent:
        mark = {"done": "+", "failed": "!", "pending": ".", "running": "~"}[t["status"]]
        print(f"{mark} [{t['id']}] {when.astimezone():%H:%M}  {t['text'][:88]}")
        if t.get("note"):
            print(f"      note: {t['note'][:100]}")
        if t["status"] == "failed":
            needs_you.append(t)

        root = git_root(t.get("cwd"))
        if not root:
            continue
        found = _git(root, "log", "--all", "--format=%h", "--grep",
                     f"buffer\\[{t['id']}\\]")
        for sha in found.stdout.split():
            stat = _git(root, "show", "--stat", "--format=", sha).stdout.strip()
            for line in stat.splitlines():
                print(f"      {line.strip()}")
            names = _git(root, "show", "--name-status", "--format=", sha).stdout
            deleted = [ln.split("\t")[-1] for ln in names.splitlines()
                       if ln.startswith("D")]
            if deleted:
                warnings.append((t["id"], sha, deleted))
        print()

    if warnings:
        print("Worth a look -- files were deleted:")
        for tid, sha, files in warnings:
            print(f"  [{tid}] {sha}: {', '.join(files[:6])}")
        print("  Undo one with: git revert <sha>\n")

    if needs_you:
        print("Needs you:")
        for t in needs_you:
            print(f"  [{t['id']}] {t['text'][:80]}")
            print(f"      {t.get('note') or 'no reason recorded'}")
    return 0


def main() -> int:
    setup_console()
    p = argparse.ArgumentParser(description="Drain the buffer queue")
    p.add_argument("--file", help="queue path override")
    p.add_argument("--cli", default="claude", help="Claude Code executable")
    p.add_argument("--watch", action="store_true", help="keep running when the queue empties")
    p.add_argument("--poll", type=int, default=20, help="seconds between polls in watch mode")
    p.add_argument("--timeout", type=int, default=3600, help="per-task timeout in seconds")
    p.add_argument("--max-retries", type=int, default=3, help="attempts per task before failing")
    p.add_argument("--max-sleep", type=int, default=DEFAULT_MAX_SLEEP,
                   help="refuse to wait longer than this many seconds (weekly-limit guard)")
    p.add_argument("--stale-after", type=float, default=STALE_AFTER,
                   help="seconds without a heartbeat before another worker's "
                        "claim counts as abandoned and is retried")
    p.add_argument("--chain", action="store_true",
                   help="thread tasks into one session so later tasks see earlier context")
    p.add_argument("--fallback-api-key", action="store_true",
                   help="on a usage limit, retry via $BUFFER_FALLBACK_API_KEY "
                        "(metered separately from the subscription) instead of sleeping")
    # --claude-arg --allowedTools looks natural and argparse rejects it: a value
    # beginning with "-" reads as another option, and the parser exits 2. Since
    # nearly everything worth forwarding is a flag, "--" is the usable form and
    # --claude-arg stays for single values (and needs = for flags).
    p.add_argument("--claude-arg", action="append", default=[],
                   help="one extra CLI argument, repeatable. For flags use "
                        "--claude-arg=--allowedTools, or prefer -- below")
    p.add_argument("cli_args", nargs=argparse.REMAINDER,
                   help="everything after -- is passed straight to the CLI, "
                        "e.g. -- --allowedTools Read,Edit")
    p.add_argument("--daemon", action="store_true", help="detach and run in the background")
    p.add_argument("--stop", action="store_true", help="stop the running daemon")
    p.add_argument("--status", action="store_true", help="is a daemon running?")
    p.add_argument("--tail", type=int, nargs="?", const=30, help="show last N daemon log lines")
    p.add_argument("--report", type=float, nargs="?", const=12.0, metavar="HOURS",
                   help="what ran recently, what changed, what needs you "
                        "(default: last 12h)")
    p.add_argument("--checkpoint", action="store_true",
                   help="commit the working tree after each task, so a night of "
                        "unattended edits is reviewable and revertable")
    args = p.parse_args()
    # REMAINDER keeps the "--" itself; the CLI must not see it.
    forwarded = [a for a in args.cli_args if a != "--"]

    # A drain flag written after "--" is silently handed to `claude`, which
    # rejects it and fails every task. Easy to do and invisible once detached,
    # so say it out loud rather than letting a night's queue die on it.
    ours = {s for action in p._actions for s in action.option_strings}
    strays = [a for a in forwarded if a in ours]
    if strays:
        log(f"Warning: {' '.join(strays)} belongs to drain, not the CLI. "
            f"Put it before the '--'. Passing it on unchanged.")

    args.claude_arg = args.claude_arg + forwarded

    if args.stop:
        return stop_daemon()

    if args.status:
        pid = daemon_pid()
        # Where notifications go, if anywhere. A misconfigured topic is
        # otherwise discovered by not being notified, which is the one failure
        # mode this feature cannot afford. It is this shell's setting: a daemon
        # inherits its environment at spawn time, so one started before the
        # topic was exported is posting nowhere regardless of what this says.
        target = notify.endpoint()
        if not pid:
            print("no daemon running")
            if target:
                print(f"  ntfy: {target}")
            return 1
        print(f"daemon running (pid {pid})")
        doing = describe_state(read_state())
        if doing:
            print(f"  {doing}")
        if target:
            print(f"  ntfy: {target}")
        return 0

    if args.report is not None:
        return report(
            Path(args.file).expanduser() if args.file else queue_path(), args.report
        )

    if args.tail is not None:
        lf = log_file()
        if not lf.exists():
            print("no log yet")
            return 1
        print("\n".join(lf.read_text(encoding="utf-8").splitlines()[-args.tail:]))
        return 0

    if args.daemon:
        passthrough = [a for a in sys.argv[1:] if a != "--daemon"]
        return 0 if spawn_daemon(passthrough) else 1

    path = Path(args.file).expanduser() if args.file else queue_path()
    log(f"Queue: {path}")
    running = daemon_pid()
    if running and running != os.getpid():
        log(f"Warning: a daemon (pid {running}) is already draining this queue.")
    elif running == os.getpid():
        pid_file().write_text(str(os.getpid()))  # claim the pid file as our own
    return drain(args, path)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Interrupted. Running task returns to pending on next start.")
        sys.exit(130)
