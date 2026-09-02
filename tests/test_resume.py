"""Resuming the conversation a usage limit cut short.

The drain loop is driven through a fake run_task rather than a real CLI: what
matters here is which session id the retry is handed and what prompt goes with
it, and a subprocess would only add flakiness to that.
"""

import argparse
import time

import drain
import pytest
from buffer_queue import Queue
from drain import RESUME_PROMPT

PAST = int(time.time()) - 120      # already reset; sleep_until falls straight through
FUTURE = int(time.time()) + 3600   # still locked out


class FakeCLI:
    """Replays a scripted list of outcomes, recording how it was called."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, text, cli, extra, timeout, resume, use_api_key=False):
        step = self.script.pop(0) if self.script else {"mode": "ok"}
        self.calls.append({"text": text, "resume": resume, "api": use_api_key})
        sid = step.get("sid", "sess-1")
        mode = step["mode"]
        if mode == "limit":
            return False, f"usage limit reached|{step.get('epoch', PAST)}", sid, ""
        if mode == "resume_fail":
            return False, "err", sid, f"No conversation found with session ID: {resume}"
        if mode == "fail":
            return False, "boom", sid, "boom"
        return True, "ok", sid, ""


def args(**over):
    base = dict(
        watch=False, poll=0, cli="claude", claude_arg=[], timeout=60,
        chain=False, fallback_api_key=False, max_retries=3,
        max_sleep=drain.DEFAULT_MAX_SLEEP,
    )
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture
def queued(qpath):
    def _add(*texts):
        with Queue(qpath) as q:
            return [q.add(t) for t in texts]
    return _add


def tasks(qpath):
    with Queue(qpath) as q:
        return q.tasks


def test_retry_after_a_limit_resumes_the_interrupted_session(qpath, queued, monkeypatch):
    queued("write the report")
    fake = FakeCLI({"mode": "limit", "sid": "sess-abc"}, {"mode": "ok"})
    monkeypatch.setattr(drain, "run_task", fake)

    assert drain.drain(args(), qpath) == 0

    assert len(fake.calls) == 2
    first, second = fake.calls
    assert first["resume"] is None
    assert first["text"] == "write the report"
    # The retry continues the same conversation ...
    assert second["resume"] == "sess-abc"
    # ... and is told to continue rather than handed the task again.
    assert second["text"] == RESUME_PROMPT.format(text="write the report")
    assert "don't redo what is already done" in second["text"]

    (task,) = tasks(qpath)
    assert task["status"] == "done"
    assert task["session"] is None


def test_session_outlives_the_daemon(qpath, queued, monkeypatch):
    """--max-sleep can stop the daemon mid-wait, so the session id has to be on
    the task in the file, not in the worker's memory."""
    queued("write the report")
    monkeypatch.setattr(
        drain, "run_task", FakeCLI({"mode": "limit", "sid": "sess-xyz", "epoch": FUTURE})
    )
    assert drain.drain(args(max_sleep=0), qpath) == 2

    (task,) = tasks(qpath)
    assert task["status"] == "pending"
    assert task["session"] == "sess-xyz"
    assert "session=sess-xyz" in qpath.read_text(encoding="utf-8")

    # A brand new drain, as if the daemon were restarted after the reset.
    fake = FakeCLI({"mode": "ok"})
    monkeypatch.setattr(drain, "run_task", fake)
    assert drain.drain(args(), qpath) == 0
    assert fake.calls[0]["resume"] == "sess-xyz"


def test_unresumable_session_falls_back_to_a_cold_run(qpath, queued, monkeypatch):
    queued("write the report")
    fake = FakeCLI(
        {"mode": "limit", "sid": "sess-gone"},
        {"mode": "resume_fail"},
        {"mode": "ok"},
    )
    monkeypatch.setattr(drain, "run_task", fake)

    assert drain.drain(args(), qpath) == 0

    assert [c["resume"] for c in fake.calls] == [None, "sess-gone", None]
    assert fake.calls[2]["text"] == "write the report"  # cold, not the resume prompt
    (task,) = tasks(qpath)
    assert task["status"] == "done"


def test_ordinary_failure_does_not_resume(qpath, queued, monkeypatch):
    queued("write the report")
    fake = FakeCLI({"mode": "fail"}, {"mode": "ok"})
    monkeypatch.setattr(drain, "run_task", fake)
    monkeypatch.setattr(drain.time, "sleep", lambda _: None)

    assert drain.drain(args(), qpath) == 0
    assert [c["resume"] for c in fake.calls] == [None, None]


def test_limit_does_not_consume_retries_but_is_capped(qpath, queued, monkeypatch):
    queued("write the report")
    limits = [{"mode": "limit", "sid": "s"} for _ in range(drain.MAX_LIMIT_HITS + 1)]
    fake = FakeCLI(*limits)
    monkeypatch.setattr(drain, "run_task", fake)

    assert drain.drain(args(max_retries=2), qpath) == 0
    # More attempts than max_retries, because limits aren't the task's fault ...
    assert len(fake.calls) == drain.MAX_LIMIT_HITS + 1
    # ... but not unbounded.
    (task,) = tasks(qpath)
    assert task["status"] == "failed"
    assert "without progress" in task["note"]


def test_api_key_fallback_resumes_too(qpath, queued, monkeypatch):
    queued("write the report")
    monkeypatch.setenv("BUFFER_FALLBACK_API_KEY", "sk-ant-test")
    fake = FakeCLI({"mode": "limit", "sid": "sess-api"}, {"mode": "ok"})
    monkeypatch.setattr(drain, "run_task", fake)

    assert drain.drain(args(fallback_api_key=True), qpath) == 0

    assert fake.calls[1]["api"] is True
    assert fake.calls[1]["resume"] == "sess-api"
    (task,) = tasks(qpath)
    assert task["status"] == "done"
    assert task["session"] is None
