"""What the daemon says it is doing.

A daemon sleeping off a usage limit and a daemon that has wedged look identical
from the queue: the task sits at [~] and nothing happens for hours. That
ambiguity reads as a broken tool at precisely the moment the tool is working,
so the state has to be legible without reading the log.
"""

import time

import drain


def test_sleeping_says_when_it_wakes_and_what_resumes():
    now = 1_700_000_000
    line = drain.describe_state(
        {"state": "sleeping", "until": now + 143 * 60,
         "reason": "session limit", "task": "68cb1c59"},
        now=now,
    )
    assert "waiting out the session limit" in line
    assert "143m left" in line
    assert "[68cb1c59]" in line


def test_a_reached_reset_reads_as_picking_back_up():
    now = 1_700_000_000
    line = drain.describe_state(
        {"state": "sleeping", "until": now - 5, "task": "abc"}, now=now
    )
    assert "picking" in line and "[abc]" in line


def test_running_reports_how_long():
    now = 1_700_000_000
    line = drain.describe_state(
        {"state": "running", "task": "abc", "since": now - 25 * 60}, now=now
    )
    assert "running [abc] for 25m" == line


def test_idle_is_distinguishable_from_stuck():
    assert "idle" in drain.describe_state({"state": "idle"})


def test_unknown_state_says_nothing_rather_than_guessing():
    assert drain.describe_state({}) == ""
    assert drain.describe_state({"state": "stopped"}) == ""


def test_state_round_trips_through_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(drain, "state_dir", lambda: tmp_path)
    drain.set_state(state="sleeping", until=123, reason="weekly limit")
    got = drain.read_state()
    assert got["state"] == "sleeping"
    assert got["until"] == 123
    assert got["pid"] == drain.os.getpid()


def test_unreadable_state_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(drain, "state_dir", lambda: tmp_path)
    (tmp_path / "drain.state").write_text("not json", encoding="utf-8")
    assert drain.read_state() == {}
    assert drain.describe_state(drain.read_state()) == ""


def test_the_drain_loop_publishes_what_it_is_doing(qpath, monkeypatch, tmp_path):
    """The loop must actually call set_state, not just be able to."""
    from buffer_queue import Queue
    from test_resume import FakeCLI, args

    monkeypatch.setattr(drain, "state_dir", lambda: tmp_path)
    with Queue(qpath) as q:
        q.add("do it")
    monkeypatch.setattr(drain, "run_task", FakeCLI({"mode": "ok"}))

    assert drain.drain(args(), qpath) == 0
    state = drain.read_state()
    assert state["state"] == "running"
    assert state["since"] <= int(time.time())
