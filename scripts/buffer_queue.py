#!/usr/bin/env python3
"""Durable FIFO task queue backed by a human-editable markdown file.

Line format (one task per line):
    - [ ] id=a1b2c3d4 ts=2026-08-30T14:03:11Z | do task1

Status markers:
    [ ]  pending
    [~]  running (claimed by a worker)
    [x]  done
    [!]  failed

The file is plain markdown on purpose: you can open it, reorder lines,
delete things, or hand-edit a task while a worker is idle.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl                     # POSIX
except ImportError:
    fcntl = None
try:
    import msvcrt                    # Windows
except ImportError:
    msvcrt = None

LOCK_TIMEOUT = 60.0


class QueueLocked(RuntimeError):
    """Another process held the queue lock past the timeout."""


def setup_console() -> None:
    """The queue file is always UTF-8; a Windows console codepage usually
    isn't. Make stdout match instead of dying on a task containing '\u2713'."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except (AttributeError, OSError):
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def _lock_acquire(fh, timeout: float = LOCK_TIMEOUT) -> None:
    """Exclusive advisory lock on an open file, on either platform. Both
    backends are polled non-blocking so a wedged holder raises instead of
    hanging a daemon forever."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise QueueLocked(
                    f"queue lock busy for {timeout:.0f}s - another bq or drain "
                    f"process may be stuck"
                ) from None
            time.sleep(0.05)


def _lock_release(fh) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_UN)
        elif msvcrt is not None:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _replace_with_retry(src: str, dst: Path, attempts: int = 40) -> None:
    """os.replace is atomic on Windows but fails outright if anything else has
    the target open - an indexer or AV scanner is enough. Retry briefly."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05)

HEADER = "# Buffer queue\n\n"

STATUS_CHARS = {" ": "pending", "~": "running", "x": "done", "!": "failed"}
CHAR_FOR_STATUS = {v: k for k, v in STATUS_CHARS.items()}

LINE_RE = re.compile(
    r"^- \[(?P<mark>[ ~x!])\] id=(?P<id>\w+) ts=(?P<ts>\S+) \| (?P<text>.*?)(?: # (?P<note>.*))?$"
)


def queue_path() -> Path:
    env = os.environ.get("CLAUDE_BUFFER_QUEUE")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".claude" / "buffer" / "queue.md"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Queue:
    """Reads the whole file, mutates in memory, writes atomically.

    Every mutating command takes an exclusive lock for its full duration so
    two workers can't claim the same task.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(HEADER, encoding="utf-8")
        self._lock_file = None

    def __enter__(self):
        lock = self.path.parent / f".{self.path.name}.lock"
        # "a+" not "w": truncating would clobber a lock file another process
        # is holding a byte-range lock on.
        self._lock_file = open(lock, "a+", encoding="utf-8")
        try:
            _lock_acquire(self._lock_file)
        except BaseException:
            self._lock_file.close()
            self._lock_file = None
            raise
        self.tasks = self._read()
        return self

    def __exit__(self, *exc):
        if self._lock_file is not None:
            _lock_release(self._lock_file)
            self._lock_file.close()
            self._lock_file = None
        return False

    def _read(self) -> list[dict]:
        tasks = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            m = LINE_RE.match(line.rstrip())
            if m:
                tasks.append(
                    {
                        "id": m.group("id"),
                        "status": STATUS_CHARS[m.group("mark")],
                        "ts": m.group("ts"),
                        "text": m.group("text"),
                        "note": m.group("note"),
                    }
                )
        return tasks

    def _render(self, t: dict) -> str:
        line = (
            f"- [{CHAR_FOR_STATUS[t['status']]}] id={t['id']} "
            f"ts={t['ts']} | {t['text']}"
        )
        if t.get("note"):
            line += f" # {t['note']}"
        return line

    def save(self) -> None:
        body = HEADER + "\n".join(self._render(t) for t in self.tasks) + "\n"
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            _replace_with_retry(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- operations -------------------------------------------------

    def add(self, text: str) -> dict:
        task = {
            "id": secrets.token_hex(4),
            "status": "pending",
            "ts": now_iso(),
            "text": text.strip().replace("\n", " "),
            "note": None,
        }
        self.tasks.append(task)
        self.save()
        return task

    def find(self, task_id: str) -> dict | None:
        return next((t for t in self.tasks if t["id"] == task_id), None)

    def peek(self) -> dict | None:
        """Oldest pending task. Order is file order, which is insertion order."""
        return next((t for t in self.tasks if t["status"] == "pending"), None)

    def claim(self) -> dict | None:
        task = self.peek()
        if task:
            task["status"] = "running"
            self.save()
        return task

    def set_status(self, task_id: str, status: str, note: str | None = None) -> dict | None:
        task = self.find(task_id)
        if task:
            task["status"] = status
            if note is not None:
                task["note"] = note.replace("\n", " ")
            self.save()
        return task

    def reset_running(self) -> int:
        """Return interrupted tasks to pending. Run this at worker startup:
        a killed worker leaves [~] behind and nothing would pick it up."""
        n = 0
        for t in self.tasks:
            if t["status"] == "running":
                t["status"] = "pending"
                n += 1
        if n:
            self.save()
        return n

    def remove(self, task_id: str) -> bool:
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t["id"] != task_id]
        if len(self.tasks) != before:
            self.save()
            return True
        return False

    def clear(self, statuses: set[str]) -> int:
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t["status"] not in statuses]
        removed = before - len(self.tasks)
        if removed:
            self.save()
        return removed

    def counts(self) -> dict:
        c = {v: 0 for v in STATUS_CHARS.values()}
        for t in self.tasks:
            c[t["status"]] += 1
        return c


def emit(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False))
    elif obj is None:
        print("(none)")
    elif isinstance(obj, dict) and "text" in obj:
        print(f"[{obj['id']}] {obj['text']}")
    else:
        print(obj)


def main() -> int:
    setup_console()
    p = argparse.ArgumentParser(description="Buffer queue manager")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--file", help="override queue path")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="append a task to the back of the queue")
    a.add_argument("text", nargs="+")

    sub.add_parser("peek", help="show next pending task without claiming it")
    sub.add_parser("claim", help="mark next pending task as running and print it")
    sub.add_parser("reset", help="return running tasks to pending (crash recovery)")
    sub.add_parser("status", help="show counts")

    d = sub.add_parser("done", help="mark a task complete")
    d.add_argument("id")

    f = sub.add_parser("fail", help="mark a task failed")
    f.add_argument("id")
    f.add_argument("--note", default="")

    r = sub.add_parser("requeue", help="send a task back to pending")
    r.add_argument("id")

    rm = sub.add_parser("remove", help="delete a task outright")
    rm.add_argument("id")

    ls = sub.add_parser("list", help="list tasks")
    ls.add_argument("--all", action="store_true", help="include done and failed")

    cl = sub.add_parser("clear", help="drop finished tasks")
    cl.add_argument("--failed", action="store_true", help="also drop failed")

    args = p.parse_args()
    path = Path(args.file).expanduser() if args.file else queue_path()

    with Queue(path) as q:
        if args.cmd == "add":
            emit(q.add(" ".join(args.text)), args.json)
        elif args.cmd == "peek":
            emit(q.peek(), args.json)
        elif args.cmd == "claim":
            t = q.claim()
            emit(t, args.json)
            return 0 if t else 1
        elif args.cmd == "reset":
            emit({"requeued": q.reset_running()}, args.json)
        elif args.cmd == "done":
            emit(q.set_status(args.id, "done"), args.json)
        elif args.cmd == "fail":
            emit(q.set_status(args.id, "failed", args.note or "failed"), args.json)
        elif args.cmd == "requeue":
            emit(q.set_status(args.id, "pending", ""), args.json)
        elif args.cmd == "remove":
            emit({"removed": q.remove(args.id)}, args.json)
        elif args.cmd == "status":
            emit({"queue": str(path), **q.counts()}, args.json)
        elif args.cmd == "list":
            shown = (
                q.tasks
                if args.all
                else [t for t in q.tasks if t["status"] in ("pending", "running")]
            )
            if args.json:
                print(json.dumps(shown, ensure_ascii=False))
            elif not shown:
                print("Queue is empty.")
            else:
                for i, t in enumerate(shown, 1):
                    mark = CHAR_FOR_STATUS[t["status"]]
                    note = f"  # {t['note']}" if t.get("note") else ""
                    print(f"{i:>3}. [{mark}] {t['id']}  {t['text']}{note}")
        elif args.cmd == "clear":
            statuses = {"done", "failed"} if args.failed else {"done"}
            emit({"removed": q.clear(statuses)}, args.json)

    return 0


if __name__ == "__main__":
    sys.exit(main())
