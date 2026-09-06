"""The handover that outlives a killed session.

A usage limit stops a session mid-token, so nothing inside it can write a
summary first — and summarising would need a model call, which is exactly what
a lockout forbids. The daemon assembles it instead, from evidence that survives:
the task text, the commits the attempt produced, and how it stopped.
"""

import sqlite3
import subprocess

import drain
import pytest
import sessions
from buffer_queue import Queue
from test_resume import FakeCLI, args

PAST = None


def git(root, *a):
    return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "app.py").write_text("original\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "initial")
    return r


@pytest.fixture
def state(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setattr(drain, "state_dir", lambda: d)
    return d


# -- the summary itself ----------------------------------------------------


def test_summary_names_what_was_asked_and_why_it_stopped():
    md = sessions.build_summary(
        task_id="abc123", prompt="rewrite the booking flow",
        session_id="sess-1", repo=None, start_head=None, attempt=2,
        reason="session limit",
    )
    assert "rewrite the booking flow" in md
    assert "session limit" in md
    assert "sess-1" in md
    assert "Attempt:** 2" in md


def test_summary_lists_the_commits_the_attempt_made(repo):
    head = sessions.head_of(str(repo))
    (repo / "app.py").write_text("changed\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "buffer[abc]: half the work")

    md = sessions.build_summary(
        task_id="abc", prompt="do it", session_id=None, repo=str(repo),
        start_head=head, attempt=1, reason="session limit",
    )
    assert "half the work" in md
    assert "app.py" in md  # diffstat


def test_summary_reports_an_untouched_tree_honestly(repo):
    md = sessions.build_summary(
        task_id="abc", prompt="do it", session_id=None, repo=str(repo),
        start_head=sessions.head_of(str(repo)), attempt=1, reason="weekly limit",
    )
    assert "unchanged" in md


def test_summary_mentions_uncommitted_leftovers(repo):
    head = sessions.head_of(str(repo))
    (repo / "app.py").write_text("edited but not committed\n", encoding="utf-8")
    md = sessions.build_summary(
        task_id="abc", prompt="do it", session_id=None, repo=str(repo),
        start_head=head, attempt=1, reason="session limit",
    )
    assert "Uncommitted changes" in md
    assert "app.py" in md


def test_summary_survives_a_directory_that_is_not_a_repo(tmp_path):
    md = sessions.build_summary(
        task_id="abc", prompt="do it", session_id=None, repo=str(tmp_path),
        start_head=None, attempt=1, reason="session limit",
    )
    assert isinstance(md, str) and "do it" in md


# -- the database ----------------------------------------------------------


def test_a_run_is_recorded_and_read_back(tmp_path):
    db = tmp_path / "s.db"
    rid = sessions.record(
        db, task_id="t1", prompt="the original prompt", session_id="sess-9",
        repo="/srv/app", repo_head="abc1234", status="interrupted",
        reason="session limit", summary="# notes",
    )
    assert rid > 0
    (row,) = sessions.recent(db)
    assert row["task_id"] == "t1"
    assert row["prompt"] == "the original prompt"
    assert row["session_id"] == "sess-9"
    assert row["repo"] == "/srv/app"
    assert row["summary"] == "# notes"


def test_latest_summary_returns_the_most_recent(tmp_path):
    db = tmp_path / "s.db"
    sessions.record(db, task_id="t1", prompt="p", session_id=None, repo=None,
                    repo_head=None, status="interrupted", summary="first")
    sessions.record(db, task_id="t1", prompt="p", session_id=None, repo=None,
                    repo_head=None, status="interrupted", summary="second")
    assert sessions.latest_summary(db, "t1") == "second"


def test_no_summary_for_an_unknown_task(tmp_path):
    assert sessions.latest_summary(tmp_path / "s.db", "nope") is None


def test_runs_without_a_summary_are_not_offered_as_handovers(tmp_path):
    db = tmp_path / "s.db"
    sessions.record(db, task_id="t1", prompt="p", session_id=None, repo=None,
                    repo_head=None, status="done")
    assert sessions.latest_summary(db, "t1") is None


# -- the loop --------------------------------------------------------------


def test_a_limit_writes_a_handover_and_a_row(qpath, state, monkeypatch):
    with Queue(qpath) as q:
        task = q.add("build the thing")
    monkeypatch.setattr(
        drain, "run_task",
        FakeCLI({"mode": "limit", "sid": "sess-int"}, {"mode": "ok"}),
    )
    assert drain.drain(args(), qpath) == 0

    rows = sessions.recent(state / "sessions.db")
    interrupted = [r for r in rows if r["status"] == "interrupted"]
    assert len(interrupted) == 1
    assert interrupted[0]["session_id"] == "sess-int"
    assert interrupted[0]["prompt"] == "build the thing"
    assert "limit" in interrupted[0]["reason"]
    assert "build the thing" in interrupted[0]["summary"]

    written = list((state / "summaries").glob(f"{task['id']}-*.md"))
    assert len(written) == 1


def test_the_finished_run_is_recorded_too(qpath, state, monkeypatch):
    with Queue(qpath) as q:
        q.add("build the thing")
    monkeypatch.setattr(drain, "run_task", FakeCLI({"mode": "ok"}))
    drain.drain(args(), qpath)
    assert [r["status"] for r in sessions.recent(state / "sessions.db")] == ["done"]


def test_a_lost_session_is_restarted_from_its_summary(qpath, state, monkeypatch):
    """The point of the whole mechanism: the conversation is gone, so the
    handover is the only context left, and it must reach the new session."""
    with Queue(qpath) as q:
        q.add("build the thing")

    fake = FakeCLI(
        {"mode": "limit", "sid": "sess-gone"},
        {"mode": "resume_fail"},
        {"mode": "ok"},
    )
    monkeypatch.setattr(drain, "run_task", fake)
    assert drain.drain(args(), qpath) == 0

    third = fake.calls[2]["text"]
    assert third != "build the thing"          # not sent cold
    assert "build the thing" in third          # original ask carried over
    assert "already done" in third             # told not to repeat work
    assert "usage limit" in third             # the handover is embedded


def test_a_broken_database_does_not_stop_the_queue(qpath, state, monkeypatch):
    with Queue(qpath) as q:
        q.add("build the thing")

    def boom(*a, **kw):
        raise sqlite3.OperationalError("disk is full")

    monkeypatch.setattr(sessions, "record", boom)
    monkeypatch.setattr(
        drain, "run_task", FakeCLI({"mode": "limit"}, {"mode": "ok"})
    )
    assert drain.drain(args(), qpath) == 0
    with Queue(qpath) as q:
        assert q.tasks[0]["status"] == "done"
