"""Where a task runs.

A task is almost always about the project you were standing in when you queued
it, but the worker that runs it may be a background process started somewhere
else entirely — at logon, from the home directory. Without the directory
travelling with the task, "fix the failing test" runs in the wrong repo.
"""

import drain
import pytest
from buffer_queue import Queue
from test_resume import FakeCLI, args


@pytest.fixture
def queued(qpath):
    def _add(*texts, cwd=None):
        with Queue(qpath) as q:
            return [q.add(t, cwd) for t in texts]
    return _add


def test_add_records_the_current_directory(qpath, queued, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (task,) = queued("fix the failing test")
    assert task["cwd"] == str(tmp_path)


def test_cwd_can_be_given_explicitly(qpath, queued):
    (task,) = queued("do a thing", cwd="/srv/project")
    assert task["cwd"] == "/srv/project"


def test_cwd_round_trips_alongside_the_other_fields(qpath, queued):
    (task,) = queued("run make # and check", cwd=r"C:\projects\my repo")
    with Queue(qpath) as q:
        q.claim("worker-a")
        q.set_session(task["id"], "sess-1")
    with Queue(qpath) as q:
        got = q.find(task["id"])
    assert got["cwd"] == r"C:\projects\my repo"
    assert got["text"] == "run make # and check"
    assert got["session"] == "sess-1"
    assert got["worker"] == "worker-a"


def test_the_daemon_runs_a_task_where_it_was_queued(qpath, queued, monkeypatch):
    queued("build it", cwd="/srv/project-a")
    queued("test it", cwd="/srv/project-b")
    fake = FakeCLI({"mode": "ok"}, {"mode": "ok"})
    monkeypatch.setattr(drain, "run_task", fake)

    assert drain.drain(args(), qpath) == 0
    assert [c["cwd"] for c in fake.calls] == ["/srv/project-a", "/srv/project-b"]


def test_a_resumed_task_still_runs_in_its_own_directory(qpath, queued, monkeypatch):
    queued("build it", cwd="/srv/project-a")
    fake = FakeCLI({"mode": "limit", "sid": "sess-1"}, {"mode": "ok"})
    monkeypatch.setattr(drain, "run_task", fake)

    assert drain.drain(args(), qpath) == 0
    assert [c["cwd"] for c in fake.calls] == ["/srv/project-a", "/srv/project-a"]


def test_a_task_queued_before_cwd_existed_runs_wherever_the_daemon_is(
    qpath, queued, monkeypatch
):
    """Older rows have no cwd; they must still run rather than being skipped."""
    queued("legacy task", cwd=None)
    with Queue(qpath) as q:
        q.tasks[0]["cwd"] = None
        q.save()

    fake = FakeCLI({"mode": "ok"})
    monkeypatch.setattr(drain, "run_task", fake)
    assert drain.drain(args(), qpath) == 0
    assert fake.calls[0]["cwd"] is None


def test_a_directory_that_has_gone_away_does_not_kill_the_task(monkeypatch, tmp_path):
    """run_task drops a missing cwd rather than letting subprocess raise."""
    seen = {}

    class Proc:
        returncode = 0
        stdout = '{"session_id": "s", "result": "ok"}'
        stderr = ""

    def fake_run(cmd, **kw):
        seen.update(kw)
        return Proc()

    monkeypatch.setattr(drain.subprocess, "run", fake_run)

    ok, _, _, _ = drain.run_task(
        "t", "claude", [], 60, None, cwd=str(tmp_path / "deleted")
    )
    assert ok
    assert seen["cwd"] is None

    ok, _, _, _ = drain.run_task("t", "claude", [], 60, None, cwd=str(tmp_path))
    assert seen["cwd"] == str(tmp_path)
