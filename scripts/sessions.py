#!/usr/bin/env python3
"""A record of what each attempt did, and a summary that outlives the session.

A usage limit stops a session mid-token. Nothing inside it gets to write a
handover note first -- and a note would need a model call, which is the one
thing you cannot make while locked out. So the summary is assembled afterwards
by the daemon, from evidence that survives the session dying:

    the task text .......... what was asked, verbatim
    the working tree ....... commits the attempt produced, and their diffstat
    the run itself ......... session id, attempt number, why it stopped

None of that needs the API, so it works during a lockout, which is when it is
needed. A transcript would be richer, but it is unreadable in some setups and
absent when a session is killed early; this is the part that is always there.

The summary matters even though --resume replays the real conversation:

  * a session the CLI won't reopen -- expired, pruned, another machine -- has
    no conversation left to replay, and the summary is what remains
  * resuming a long conversation re-primes its whole context; a measured
    trivial resume cost $0.23 in cache alone. Carrying a summary forward is
    cheaper than carrying an hour of transcript
  * you can read it
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    session_id  TEXT,
    prompt      TEXT NOT NULL,
    repo        TEXT,
    repo_head   TEXT,
    started     TEXT NOT NULL,
    ended       TEXT,
    status      TEXT NOT NULL,
    reason      TEXT,
    attempt     INTEGER DEFAULT 1,
    resumes     INTEGER REFERENCES runs(run_id),
    summary     TEXT,
    summary_path TEXT
);
CREATE INDEX IF NOT EXISTS runs_task ON runs(task_id);
CREATE INDEX IF NOT EXISTS runs_session ON runs(session_id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    # The daemon and any `bq` invocation may both touch this; WAL keeps a
    # reader from blocking the writer that is mid-task.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(repo, *args) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def head_of(repo: str | None) -> str | None:
    return _git(repo, "rev-parse", "--short", "HEAD") or None if repo else None


def commits_since(repo: str | None, since_head: str | None) -> list[tuple[str, str]]:
    """(sha, subject) added since the attempt started."""
    if not repo:
        return []
    rng = f"{since_head}..HEAD" if since_head else "HEAD"
    out = _git(repo, "log", "--format=%h\t%s", rng)
    rows = []
    for line in out.splitlines():
        sha, _, subject = line.partition("\t")
        if sha:
            rows.append((sha, subject))
    return rows


def build_summary(
    *,
    task_id: str,
    prompt: str,
    session_id: str | None,
    repo: str | None,
    start_head: str | None,
    attempt: int,
    reason: str,
    result_text: str = "",
) -> str:
    """Markdown handover for whatever picks this task up next."""
    lines = [
        f"# Task {task_id} — interrupted",
        "",
        f"- **Stopped because:** {reason}",
        f"- **When:** {now_iso()}",
        f"- **Attempt:** {attempt}",
        f"- **Session:** `{session_id or 'not captured'}`",
        f"- **Working directory:** `{repo or 'not recorded'}`",
        "",
        "## What was asked",
        "",
        prompt.strip() or "_(empty)_",
        "",
    ]

    commits = commits_since(repo, start_head)
    lines += ["## What landed on disk", ""]
    if commits:
        for sha, subject in commits:
            lines.append(f"- `{sha}` {subject}")
        stat = _git(repo, "diff", "--stat", f"{start_head}..HEAD") if start_head else ""
        if stat:
            lines += ["", "```", stat, "```"]
    elif repo:
        dirty = _git(repo, "status", "--porcelain")
        if dirty:
            lines += [
                "Uncommitted changes were left in the tree:",
                "", "```", dirty[:2000], "```",
            ]
        else:
            lines.append("Nothing. The working tree is unchanged.")
    else:
        lines.append("Not a git repository, so no record of file changes.")
    lines.append("")

    if result_text.strip():
        lines += [
            "## Last thing the session said",
            "",
            "```",
            result_text.strip()[-2000:],
            "```",
            "",
        ]

    lines += [
        "## Picking it up",
        "",
        "Resuming the session replays the real conversation and is preferred. "
        "This file is the fallback for when that session can no longer be "
        "opened — treat the work above as already done and continue from it.",
        "",
    ]
    return "\n".join(lines)


def record(
    db_path: Path,
    *,
    task_id: str,
    prompt: str,
    session_id: str | None,
    repo: str | None,
    repo_head: str | None,
    status: str,
    reason: str = "",
    attempt: int = 1,
    summary: str = "",
    summary_path: str | None = None,
    started: str | None = None,
    resumes: int | None = None,
) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO runs (task_id, session_id, prompt, repo, repo_head, "
            "started, ended, status, reason, attempt, resumes, summary, "
            "summary_path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, session_id, prompt, repo, repo_head, started or now_iso(),
             now_iso(), status, reason, attempt, resumes, summary, summary_path),
        )
        return int(cur.lastrowid)


def latest_summary(db_path: Path, task_id: str) -> str | None:
    """The most recent handover for a task, if one was ever written."""
    if not db_path.exists():
        return None
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT summary FROM runs WHERE task_id = ? AND summary != '' "
            "ORDER BY run_id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    return row["summary"] if row else None


def recent(db_path: Path, limit: int = 20) -> list[dict]:
    if not db_path.exists():
        return []
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def main() -> int:
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from buffer_queue import queue_path, setup_console

    setup_console()
    p = argparse.ArgumentParser(description="Session history")
    p.add_argument("--db")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.add_argument("--summary", metavar="TASK_ID",
                   help="print the latest handover for a task")
    args = p.parse_args()

    db = Path(args.db) if args.db else queue_path().parent / "sessions.db"

    if args.summary:
        text = latest_summary(db, args.summary)
        print(text or f"No summary recorded for {args.summary}.")
        return 0 if text else 1

    rows = recent(db, args.limit)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("No sessions recorded yet.")
        return 0
    for r in rows:
        mark = {"interrupted": "~", "done": "+", "failed": "!"}.get(r["status"], "?")
        print(f"{mark} run {r['run_id']:>4}  [{r['task_id']}]  {r['status']:<11} "
              f"{r['ended'] or ''}")
        print(f"        {(r['prompt'] or '')[:90]}")
        if r["repo"]:
            print(f"        repo {r['repo']} @ {r['repo_head'] or '?'}")
        if r["session_id"]:
            print(f"        session {r['session_id']}")
        if r["reason"]:
            print(f"        {r['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
