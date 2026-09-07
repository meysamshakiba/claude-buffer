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

import ansi

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


def parse_result(text: str) -> dict:
    """Pull the one human sentence out of the CLI's result JSON.

    `claude -p --output-format json` ends with a blob carrying cache token
    counts, per-model costs and a `result` field. Pasting the blob into a
    handover buries the only line that matters -- "You've hit your session
    limit, resets 12:30pm" -- under 2 KB of telemetry, and the handover is
    read by someone working out what happened, often on a phone.
    """
    facts: dict = {"message": "", "turns": None, "cost": None}
    raw = (text or "").strip()
    if not raw:
        return facts

    blob = None
    leftovers = []
    for line in raw.splitlines():
        line = line.strip()
        if blob is None and line.startswith("{") and line.endswith("}"):
            try:
                blob = json.loads(line)
                continue
            except ValueError:
                pass
        if line:
            leftovers.append(line)

    if blob is None:
        facts["message"] = raw[-600:]        # not JSON; keep it as it came
        return facts

    facts["message"] = str(blob.get("result") or "").strip()
    if not facts["message"] and leftovers:
        facts["message"] = leftovers[-1][:600]
    turns = blob.get("num_turns")
    if isinstance(turns, int):
        facts["turns"] = turns
    cost = blob.get("total_cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        facts["cost"] = float(cost)
    return facts


def _changed(repo: str | None, start_head: str | None) -> list[str]:
    """The file-level story, as a short list rather than a diff dump."""
    commits = commits_since(repo, start_head)
    if commits:
        out = [f"- `{sha}` {subject}" for sha, subject in commits]
        # Subjects say what was intended; the file list says what actually
        # moved, which is what stops the next session redoing it.
        names = (_git(repo, "diff", "--name-status", f"{start_head}..HEAD")
                 if start_head else "")
        rows = [ln.split("	") for ln in names.splitlines() if ln.strip()]
        for status, *rest in rows[:25]:
            out.append(f"- `{status.strip()}` {rest[-1] if rest else ''}")
        if len(rows) > 25:
            out.append(f"- ...and {len(rows) - 25} more")
        return out
    if not repo:
        return ["- not a git repository, so no record of file changes"]
    dirty = [ln for ln in _git(repo, "status", "--porcelain").splitlines() if ln.strip()]
    if not dirty:
        return ["- nothing; the working tree is unchanged"]
    # Uncommitted work is the interesting case: it is what the next session
    # must not redo. Capped so a big sweep cannot crowd out the resume line.
    # Split on whitespace rather than porcelain's fixed columns: _git strips
    # the output, so the leading status column is gone on the first line and
    # a column slice silently eats the first character of the filename.
    shown = []
    for ln in dirty[:25]:
        status, _, name = ln.strip().partition(" ")
        shown.append(f"- `{status or '??'}` {name.strip()}")
    if len(dirty) > 25:
        shown.append(f"- ...and {len(dirty) - 25} more")
    return shown


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
    when: str | None = None,
) -> str:
    """Markdown handover for whatever picks this task up next.

    Deliberately short. Its job is to answer three questions fast -- why did
    it stop, what already landed, how do I continue -- and every extra line
    pushes those answers further from the top.
    """
    facts = parse_result(result_text)

    meta = [f"attempt {attempt}"]
    if facts["turns"]:
        meta.append(f"{facts['turns']} turns")
    if facts["cost"]:
        meta.append(f"${facts['cost']:.2f}")

    where = f"`{repo}`" if repo else "_not recorded_"
    if start_head:
        where += f" @ `{start_head[:8]}`"

    why = reason + (f" — {facts['message']}" if facts["message"] else "")
    lines = [
        f"# {task_id} — interrupted",
        "",
        f"**Why:** {why}",
        f"**When:** {when or now_iso()} · {' · '.join(meta)}",
        f"**Where:** {where}",
        "",
        "## Task",
        "",
        prompt.strip() or "_(empty)_",
        "",
        "## Changed",
        "",
        *_changed(repo, start_head),
        "",
        "## Resume",
        "",
    ]
    if session_id:
        lines += [
            "```",
            f"claude --resume {session_id}",
            "```",
            "",
            "That replays the real conversation and is the preferred route. "
            "Fall back to this file only if the session will not open — the "
            "work under **Changed** is already done, so continue from it.",
        ]
    else:
        lines.append(
            "No session id was captured, so the conversation cannot be "
            "reopened. Treat the work under **Changed** as done and continue."
        )
    lines.append("")
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


def render(md: str) -> str:
    """Colour a handover for a terminal.

    Returns the markdown untouched when colour is off, which is also what
    happens when stdout is redirected -- so `bq summary x > handover.md`
    still writes a clean document.
    """
    if not ansi.enabled():
        return md

    out, in_code = [], False
    for line in md.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue                      # the fence is noise on a screen
        if in_code:
            out.append("    " + ansi.cmd(line))
        elif line.startswith("# "):
            out.append(ansi.head(line[2:]))
        elif line.startswith("## "):
            out.append(ansi.paint(line[3:], "bold"))
        elif line.startswith("**") and "**" in line[2:]:
            lab, _, rest = line[2:].partition("**")
            out.append(f"{ansi.label(lab)}{rest}")
        else:
            out.append(line)
    return "\n".join(out)


MARKS = {
    "interrupted": ("~", "yellow"),
    "done": ("+", "green"),
    "failed": ("!", "red"),
}


def format_row(r: dict) -> str:
    """One run, as a few indented lines under a coloured status mark."""
    mark, colour = MARKS.get(r["status"], ("?", "grey"))
    head = (f"{ansi.paint(mark, colour)} run {r['run_id']:>4}  "
            f"{ansi.paint('[' + r['task_id'] + ']', 'cyan')}  "
            f"{ansi.paint(r['status'], colour):<11} {r['ended'] or ''}")
    lines = [head, f"        {(r['prompt'] or '')[:90]}"]
    if r["repo"]:
        lines.append(f"        {ansi.label('repo')} {r['repo']} @ {r['repo_head'] or '?'}")
    if r["session_id"]:
        # A bare id is something to copy and then work out what to do with.
        # The command is the thing the reader actually wants.
        lines.append(f"        {ansi.cmd('claude --resume ' + r['session_id'])}")
    if r["reason"]:
        lines.append(f"        {ansi.warn(r['reason'])}")
    return "\n".join(lines)


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
        print(render(text) if text else
              f"No summary recorded for {args.summary}.")
        return 0 if text else 1

    rows = recent(db, args.limit)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("No sessions recorded yet.")
        return 0
    for r in rows:
        print(format_row(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
