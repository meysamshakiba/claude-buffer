"""Making a night of unattended edits reviewable.

A deny-list on shell commands stops accidents, not determined paths — `rm` is
reachable through `python -c` or `find -delete`. So reversibility rests on a
commit per task instead: every change is isolated, attributable to the prompt
that caused it, and undoable with `git revert`.
"""

import subprocess

import drain
import pytest
from buffer_queue import Queue
from test_resume import FakeCLI, args


def git(root, *a):
    return subprocess.run(
        ["git", "-C", str(root), *a], capture_output=True, text=True,
    )


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


def log_subjects(root):
    return git(root, "log", "--format=%s").stdout.strip().splitlines()


def test_a_task_that_changes_files_becomes_a_commit(qpath, repo, monkeypatch):
    with Queue(qpath) as q:
        task = q.add("edit the app", str(repo))

    def edit(*a, **kw):
        (repo / "app.py").write_text("changed\n", encoding="utf-8")
        return True, "ok", "sess", ""

    monkeypatch.setattr(drain, "run_task", edit)
    assert drain.drain(args(checkpoint=True), qpath) == 0

    subjects = log_subjects(repo)
    assert any(f"buffer[{task['id']}]" in s for s in subjects), subjects
    assert not git(repo, "status", "--porcelain").stdout.strip()


def test_work_already_in_the_tree_is_committed_separately(qpath, repo, monkeypatch):
    """Otherwise reverting the task takes unrelated changes with it."""
    (repo / "mine.txt").write_text("my own edit\n", encoding="utf-8")
    with Queue(qpath) as q:
        q.add("edit the app", str(repo))

    def edit(*a, **kw):
        (repo / "app.py").write_text("changed\n", encoding="utf-8")
        return True, "ok", "s", ""

    monkeypatch.setattr(drain, "run_task", edit)
    drain.drain(args(checkpoint=True), qpath)

    subjects = log_subjects(repo)
    assert "buffer: uncommitted work found before" in subjects[1]
    assert subjects[0].startswith("buffer[")


def test_a_failed_task_still_commits_what_it_touched(qpath, repo, monkeypatch):
    with Queue(qpath) as q:
        q.add("try something", str(repo))

    def half(*a, **kw):
        (repo / "app.py").write_text("half done\n", encoding="utf-8")
        return False, "boom", "s", "boom"

    monkeypatch.setattr(drain, "run_task", half)
    monkeypatch.setattr(drain.time, "sleep", lambda _: None)
    drain.drain(args(checkpoint=True, max_retries=1), qpath)

    assert any("(task failed)" in s for s in log_subjects(repo))


def test_a_task_that_changes_nothing_makes_no_commit(qpath, repo, monkeypatch):
    with Queue(qpath) as q:
        q.add("just read things", str(repo))
    monkeypatch.setattr(drain, "run_task", FakeCLI({"mode": "ok"}))
    drain.drain(args(checkpoint=True), qpath)
    assert log_subjects(repo) == ["initial"]


def test_checkpointing_is_off_unless_asked(qpath, repo, monkeypatch):
    with Queue(qpath) as q:
        q.add("edit the app", str(repo))

    def edit(*a, **kw):
        (repo / "app.py").write_text("changed\n", encoding="utf-8")
        return True, "ok", "s", ""

    monkeypatch.setattr(drain, "run_task", edit)
    drain.drain(args(), qpath)
    assert log_subjects(repo) == ["initial"]
    assert git(repo, "status", "--porcelain").stdout.strip()


def test_a_directory_that_is_not_a_repo_is_fine(qpath, tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    with Queue(qpath) as q:
        q.add("do something", str(plain))
    monkeypatch.setattr(drain, "run_task", FakeCLI({"mode": "ok"}))
    assert drain.drain(args(checkpoint=True), qpath) == 0


def test_the_report_names_deletions_and_failures(qpath, repo, monkeypatch, capsys):
    with Queue(qpath) as q:
        q.add("remove the old module", str(repo))
        q.add("this one breaks", str(repo))

    calls = {"n": 0}

    def act(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            (repo / "app.py").unlink()
            return True, "ok", "s", ""
        return False, "boom", "s", "boom"

    monkeypatch.setattr(drain, "run_task", act)
    monkeypatch.setattr(drain.time, "sleep", lambda _: None)
    drain.drain(args(checkpoint=True, max_retries=1), qpath)

    capsys.readouterr()
    drain.report(qpath, hours=24)
    out = capsys.readouterr().out

    assert "files were deleted" in out
    assert "app.py" in out
    assert "Needs you:" in out
    assert "git revert" in out
