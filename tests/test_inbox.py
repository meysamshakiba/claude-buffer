"""Capturing work without a terminal or a live session.

`bq` needs a shell and `/buffer` needs a Claude session, and a usage limit takes
the session away — so the moment you most need to record an idea is the moment
both ingresses are gone. Nothing outside can push into a Claude session, so the
daemon reads a directory instead: anything that can write a file can queue work.
"""

import time
from pathlib import Path

import drain
import pytest
from buffer_queue import Queue


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    d = tmp_path / "inbox"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_BUFFER_INBOX", str(d))
    return d


def drop(inbox, name, text, age=60):
    f = inbox / name
    f.write_text(text, encoding="utf-8")
    old = time.time() - age
    import os
    os.utime(f, (old, old))
    return f


def tasks(qpath):
    with Queue(qpath) as q:
        return q.tasks


def test_a_dropped_file_becomes_a_task(qpath, inbox):
    drop(inbox, "idea.txt", "rewrite the booking flow")
    assert drain.ingest_inbox(qpath) == 1
    (task,) = tasks(qpath)
    assert task["text"] == "rewrite the booking flow"
    assert task["status"] == "pending"


def test_the_file_is_archived_not_deleted(qpath, inbox):
    drop(inbox, "idea.txt", "do the thing")
    drain.ingest_inbox(qpath)
    assert not (inbox / "idea.txt").exists()
    archived = list((inbox / "done").iterdir())
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8").strip() == "do the thing"


def test_it_is_not_queued_twice(qpath, inbox):
    drop(inbox, "idea.txt", "only once")
    assert drain.ingest_inbox(qpath) == 1
    assert drain.ingest_inbox(qpath) == 0
    assert len(tasks(qpath)) == 1


def test_a_file_still_being_written_is_left_alone(qpath, inbox):
    """A sync client or a slow pipe can leave a half-written file. Queueing
    half an idea is not something that can be repaired afterwards."""
    drop(inbox, "arriving.txt", "half an ide", age=0)
    assert drain.ingest_inbox(qpath) == 0
    assert (inbox / "arriving.txt").exists()

    # once it has settled it goes through
    assert drain.ingest_inbox(qpath, settle=0) == 1


def test_without_a_header_it_runs_in_home_not_the_daemons_directory(
    qpath, inbox, tmp_path, monkeypatch
):
    """Launched from Task Scheduler the daemon's cwd is system32. Inheriting it
    would point unattended sessions at a system directory."""
    monkeypatch.chdir(tmp_path)
    drop(inbox, "idea.txt", "no header here")
    drain.ingest_inbox(qpath)
    (task,) = tasks(qpath)
    assert task["cwd"] == str(Path.home())
    assert task["cwd"] != str(tmp_path)


def test_a_cwd_header_says_where_it_runs(qpath, inbox):
    drop(inbox, "idea.txt", "cwd: /srv/project\nfix the failing test")
    drain.ingest_inbox(qpath)
    (task,) = tasks(qpath)
    assert task["cwd"] == "/srv/project"
    assert task["text"] == "fix the failing test"


def test_multi_line_text_becomes_one_task(qpath, inbox):
    drop(inbox, "idea.md", "add a report page\nwith weekly totals")
    drain.ingest_inbox(qpath)
    (task,) = tasks(qpath)
    assert task["text"] == "add a report page with weekly totals"


def test_arrival_order_is_preserved(qpath, inbox):
    drop(inbox, "01-first.txt", "first")
    drop(inbox, "02-second.txt", "second")
    drain.ingest_inbox(qpath)
    assert [t["text"] for t in tasks(qpath)] == ["first", "second"]


def test_empty_and_hidden_files_are_skipped(qpath, inbox):
    drop(inbox, "blank.txt", "   ")
    drop(inbox, ".syncthing.tmp", "partial")
    assert drain.ingest_inbox(qpath) == 0
    assert tasks(qpath) == []
    assert (inbox / ".syncthing.tmp").exists()  # not ours to touch


def test_a_missing_inbox_is_not_an_error(qpath, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BUFFER_INBOX", str(tmp_path / "nope"))
    assert drain.ingest_inbox(qpath) == 0
