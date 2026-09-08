"""Telling a phone what happened while nobody was watching.

Two halves, tested separately: where a notification goes and what it carries
(the ntfy shape), and whether the drain loop actually emits one per event
(the daemon's side). The first never touches the network -- `urlopen` is
replaced -- because a test that reaches ntfy.sh fails on a train.

The property that matters most has no visible output: with no topic set,
nothing at all happens. Notifications are opt-in, and a daemon without them
configured has to behave exactly as it did before they existed.
"""

import urllib.error
import urllib.request

import drain
import notify
import pytest
from buffer_queue import Queue
from test_resume import PAST, FakeCLI, args


@pytest.fixture(autouse=True)
def no_topic(monkeypatch):
    """Never inherit the developer's own topic; each test says what it wants."""
    monkeypatch.delenv("BUFFER_NTFY_TOPIC", raising=False)
    monkeypatch.delenv("BUFFER_NTFY_URL", raising=False)
    monkeypatch.delenv("BUFFER_NTFY_ATTACH", raising=False)
    notify.set_reporter(None)


class FakeHTTP:
    """Stands in for urlopen, recording the request it was given."""

    def __init__(self, status=200, raises=None):
        self.status = status
        self.raises = raises
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.raises:
            raise self.raises
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def http(monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


# -- where it goes ---------------------------------------------------------


def test_nothing_configured_means_no_endpoint():
    assert notify.endpoint() is None


def test_a_topic_alone_goes_to_ntfy_sh(monkeypatch):
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "buffer-9f3a1c7d")
    assert notify.endpoint() == "https://ntfy.sh/buffer-9f3a1c7d"


def test_a_self_hosted_server_takes_the_topic(monkeypatch):
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    monkeypatch.setenv("BUFFER_NTFY_URL", "https://ntfy.example.com/")
    assert notify.endpoint() == "https://ntfy.example.com/abc"


def test_a_server_behind_a_path_prefix_still_works(monkeypatch):
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    monkeypatch.setenv("BUFFER_NTFY_URL", "https://example.com/ntfy")
    assert notify.endpoint() == "https://example.com/ntfy/abc"


def test_a_bare_hostname_is_assumed_https(monkeypatch):
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    monkeypatch.setenv("BUFFER_NTFY_URL", "ntfy.example.com")
    assert notify.endpoint() == "https://ntfy.example.com/abc"


def test_a_url_that_already_names_the_topic_is_enough(monkeypatch):
    """One variable, copied out of the ntfy app, has to be a working config."""
    monkeypatch.setenv("BUFFER_NTFY_URL", "https://ntfy.sh/buffer-9f3a1c7d")
    assert notify.endpoint() == "https://ntfy.sh/buffer-9f3a1c7d"


def test_a_server_with_no_topic_is_refused_out_loud(monkeypatch):
    said = []
    notify.set_reporter(said.append)
    monkeypatch.setenv("BUFFER_NTFY_URL", "https://ntfy.sh")
    assert notify.endpoint() is None
    assert "BUFFER_NTFY_TOPIC" in said[0]


def test_a_non_http_url_is_refused(monkeypatch):
    said = []
    notify.set_reporter(said.append)
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    monkeypatch.setenv("BUFFER_NTFY_URL", "file:///etc/passwd")
    assert notify.endpoint() is None
    assert said


# -- what it carries -------------------------------------------------------


def test_a_post_carries_the_message_and_the_event_headers(monkeypatch, http):
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    assert notify.event("failed", "[a1b2] the build broke") is True

    (req,) = http.requests
    assert req.full_url == "https://ntfy.sh/abc"
    assert req.data == b"[a1b2] the build broke"
    assert req.get_method() == "POST"
    assert req.headers["Title"] == "Task failed"
    assert req.headers["Priority"] == "4"      # the one worth waking someone


def test_a_task_starting_is_quieter_than_one_failing(monkeypatch, http):
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    notify.event("started", "x")
    notify.event("failed", "y")
    started, failed = (int(r.headers["Priority"]) for r in http.requests)
    assert started < failed


def test_a_newline_in_task_text_cannot_split_the_header(monkeypatch, http):
    """Anything that can write a file can queue work, so task text is not
    trusted input -- and it lands in a header when it becomes a title."""
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    notify.post("body", title="hello\r\nPriority: 5\nX-Evil: yes")
    (req,) = http.requests
    assert req.headers["Title"] == "hello Priority: 5 X-Evil: yes"
    assert "\n" not in req.headers["Title"]


def test_non_ascii_in_a_title_is_dropped_rather_than_raising(monkeypatch, http):
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    notify.post("body", title="✓ done — 完了")
    (req,) = http.requests
    req.headers["Title"].encode("ascii")           # would raise if it got through
    assert "done" in req.headers["Title"]


def test_the_body_keeps_its_unicode(monkeypatch, http):
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    notify.post("finished ✓")
    assert http.requests[0].data.decode("utf-8") == "finished ✓"


def test_an_oversized_message_is_truncated_not_rejected(monkeypatch, http):
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    notify.post("x" * 10_000)
    assert len(http.requests[0].data) == notify.MAX_BODY


def test_an_unreachable_server_is_reported_not_raised(monkeypatch):
    said = []
    notify.set_reporter(said.append)
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    monkeypatch.setattr(
        urllib.request, "urlopen",
        FakeHTTP(raises=urllib.error.URLError("no route to host")),
    )
    assert notify.event("done", "x") is False
    assert "could not notify" in said[0]


def test_a_rejected_post_is_not_reported_as_sent(monkeypatch):
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    monkeypatch.setattr(
        urllib.request, "urlopen",
        FakeHTTP(raises=urllib.error.HTTPError(
            "https://ntfy.sh/abc", 403, "Forbidden", {}, None)),
    )
    assert notify.post("x") is False


def test_with_nothing_configured_no_request_is_made(monkeypatch):
    def explode(*a, **kw):
        raise AssertionError("posted without a topic configured")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    assert notify.event("done", "x") is False


# -- attachments -----------------------------------------------------------
#
# A different bargain from a message: on ntfy.sh the topic is public and
# unauthenticated, and what is uploaded sits on someone else's disk for three
# hours. So the default is off, and the tests that matter most are the ones
# where nothing is sent.


@pytest.fixture
def summary(tmp_path):
    md = tmp_path / "a1b2-1757000000.md"
    md.write_text("# a1b2 — interrupted\n\n**Why:** session limit\n", encoding="utf-8")
    return md


def test_an_attachment_is_a_put_carrying_the_file_and_its_name(
    monkeypatch, http, summary
):
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    monkeypatch.setenv("BUFFER_NTFY_ATTACH", "1")

    assert notify.attach(summary, title="Handover [a1b2]", priority=1) is True

    (req,) = http.requests
    assert req.get_method() == "PUT"             # ntfy's upload verb, not POST
    assert req.full_url == "https://ntfy.sh/abc"
    assert req.headers["Filename"] == "a1b2-1757000000.md"
    assert req.data == summary.read_bytes()
    assert req.headers["Title"] == "Handover [a1b2]"
    assert req.headers["Priority"] == "1"        # the message already buzzed


def test_attachments_are_off_until_asked_for(monkeypatch, summary):
    """The one that matters: an unset variable uploads nothing, anywhere."""
    def explode(*a, **kw):
        raise AssertionError("uploaded a file without being asked to")

    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    monkeypatch.setattr(urllib.request, "urlopen", explode)
    assert notify.attach(summary) is False


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  "])
def test_the_usual_ways_of_writing_no_are_all_off(monkeypatch, value, summary):
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    monkeypatch.setenv("BUFFER_NTFY_ATTACH", value)
    assert notify.attaching() is False


def test_a_topic_is_still_required(monkeypatch, summary):
    monkeypatch.setenv("BUFFER_NTFY_ATTACH", "1")
    def explode(*a, **kw):
        raise AssertionError("uploaded without a topic configured")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    assert notify.attach(summary) is False


def test_a_file_past_the_cap_is_refused_before_it_is_sent(monkeypatch, http, tmp_path):
    """ntfy.sh rejects anything over 15MB, and a queue should not spend a
    minute uploading something it already knows will bounce."""
    said = []
    notify.set_reporter(said.append)
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    monkeypatch.setenv("BUFFER_NTFY_ATTACH", "1")
    monkeypatch.setattr(notify, "ATTACH_MAX", 64)
    big = tmp_path / "big.md"
    big.write_bytes(b"x" * 128)

    assert notify.attach(big) is False
    assert http.requests == []
    assert "attachment cap" in said[0]


def test_a_file_that_is_not_there_is_reported_not_raised(monkeypatch, http, tmp_path):
    said = []
    notify.set_reporter(said.append)
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    monkeypatch.setenv("BUFFER_NTFY_ATTACH", "1")

    assert notify.attach(tmp_path / "gone.md") is False
    assert "could not read" in said[0]
    assert http.requests == []


def test_an_upload_that_cannot_be_delivered_never_raises(monkeypatch, summary):
    said = []
    notify.set_reporter(said.append)
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    monkeypatch.setenv("BUFFER_NTFY_ATTACH", "1")
    monkeypatch.setattr(
        urllib.request, "urlopen",
        FakeHTTP(raises=urllib.error.URLError("no route to host")),
    )
    assert notify.attach(summary) is False
    assert "could not attach" in said[0]


def test_a_rejected_upload_is_not_reported_as_sent(monkeypatch, summary):
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    monkeypatch.setenv("BUFFER_NTFY_ATTACH", "1")
    monkeypatch.setattr(
        urllib.request, "urlopen",
        FakeHTTP(raises=urllib.error.HTTPError(
            "https://ntfy.sh/abc", 413, "Payload Too Large", {}, None)),
    )
    assert notify.attach(summary) is False


def test_a_filename_the_header_cannot_carry_is_flattened(monkeypatch, http, tmp_path):
    """Task ids are ours, but a summary path is still a filename, and headers
    are single-line ASCII."""
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    monkeypatch.setenv("BUFFER_NTFY_ATTACH", "1")
    odd = tmp_path / "résumé — handover.md"
    odd.write_text("x", encoding="utf-8")

    assert notify.attach(odd, message="full handover") is True
    (req,) = http.requests
    req.headers["Filename"].encode("ascii")      # would raise if it got through
    assert "\n" not in req.headers["Filename"]
    assert req.headers["Message"] == "full handover"


# -- what the daemon sends -------------------------------------------------


@pytest.fixture
def sent(monkeypatch):
    """Every event the drain loop emits, as (kind, message)."""
    events = []

    def record(kind, message):
        events.append((kind, message))
        return True

    monkeypatch.setattr(notify, "event", record)
    return events


def kinds(sent):
    return [k for k, _ in sent]


def test_a_run_reports_start_finish_and_an_empty_queue(qpath, monkeypatch, sent):
    with Queue(qpath) as q:
        task = q.add("write the report")
    monkeypatch.setattr(drain, "run_task", FakeCLI({"mode": "ok"}))

    assert drain.drain(args(), qpath) == 0

    assert kinds(sent) == ["started", "done", "drained"]
    assert task["id"] in sent[0][1]
    assert "write the report" in sent[1][1]
    assert "1 done" in sent[2][1]


def test_a_usage_limit_says_when_it_lifts(qpath, monkeypatch, sent):
    with Queue(qpath) as q:
        q.add("write the report")
    monkeypatch.setattr(
        drain, "run_task", FakeCLI({"mode": "limit", "epoch": PAST}, {"mode": "ok"})
    )

    assert drain.drain(args(), qpath) == 0

    assert kinds(sent) == ["started", "limit", "started", "done", "drained"]
    limit = sent[1][1]
    assert "usage limit hit" in limit
    assert "Resets" in limit


def test_a_lockout_longer_than_max_sleep_says_the_daemon_is_stopping(
    qpath, monkeypatch, sent, tmp_path
):
    monkeypatch.setattr(drain, "state_dir", lambda: tmp_path)
    with Queue(qpath) as q:
        q.add("write the report")
    monkeypatch.setattr(
        drain, "run_task", FakeCLI({"mode": "limit", "epoch": PAST + 7200})
    )

    assert drain.drain(args(max_sleep=0), qpath) == 2

    (limit,) = [m for k, m in sent if k == "limit"]
    assert "--max-sleep" in limit and "queue is intact" in limit
    assert "drained" not in kinds(sent)      # it stopped; it did not finish


def test_a_failed_api_key_retry_corrects_what_the_phone_was_told(
    qpath, monkeypatch, sent
):
    """The first message says the queue is still moving on metered billing.
    When that attempt fails too, the daemon goes back to waiting -- and saying
    nothing would leave that wrong until the reset, hours later."""
    with Queue(qpath) as q:
        q.add("write the report")
    monkeypatch.setenv("BUFFER_FALLBACK_API_KEY", "sk-ant-test")
    monkeypatch.setattr(
        drain, "run_task",
        FakeCLI({"mode": "limit", "epoch": PAST}, {"mode": "fail"}, {"mode": "ok"}),
    )

    assert drain.drain(args(fallback_api_key=True), qpath) == 0

    first, second = [m for k, m in sent if k == "limit"]
    assert "API-key billing" in first
    assert "sleeping" in second


def test_only_a_terminal_failure_is_worth_a_notification(qpath, monkeypatch, sent):
    """A phone that buzzes for an attempt that then succeeds gets muted."""
    with Queue(qpath) as q:
        q.add("write the report")
    monkeypatch.setattr(drain, "run_task", FakeCLI({"mode": "fail"}, {"mode": "ok"}))
    monkeypatch.setattr(drain.time, "sleep", lambda _: None)

    drain.drain(args(max_retries=2), qpath)

    assert kinds(sent) == ["started", "started", "done", "drained"]


def test_giving_up_notifies_with_the_reason(qpath, monkeypatch, sent):
    with Queue(qpath) as q:
        q.add("write the report")
    monkeypatch.setattr(drain, "run_task", FakeCLI({"mode": "fail"}))
    monkeypatch.setattr(drain.time, "sleep", lambda _: None)

    drain.drain(args(max_retries=1), qpath)

    assert kinds(sent) == ["started", "failed", "drained"]
    assert "Gave up after 1 attempts" in sent[1][1]
    assert "0 done, 1 failed" in sent[2][1]


def test_an_empty_queue_is_not_worth_saying_anything_about(qpath, monkeypatch, sent):
    """A supervisor restarts the daemon every 15 minutes. If an empty queue
    counted as drained, that is 96 notifications a day saying nothing."""
    with Queue(qpath):
        pass
    assert drain.drain(args(), qpath) == 0
    assert sent == []


def test_watching_announces_the_drain_once_not_once_per_poll(qpath, monkeypatch, sent):
    with Queue(qpath) as q:
        q.add("write the report")
    monkeypatch.setattr(drain, "run_task", FakeCLI({"mode": "ok"}))

    polls = []

    def stop_after_two_idle_polls(_):
        polls.append(1)
        if len(polls) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(drain.time, "sleep", stop_after_two_idle_polls)
    with pytest.raises(KeyboardInterrupt):
        drain.drain(args(watch=True, poll=1), qpath)

    assert kinds(sent).count("drained") == 1


def test_the_daemon_makes_no_requests_when_no_topic_is_set(qpath, monkeypatch):
    """The integration equivalent of the unit test above: a whole drain, with
    urlopen booby-trapped, and the real notify module in place."""
    def explode(*a, **kw):
        raise AssertionError("the daemon posted without a topic configured")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    with Queue(qpath) as q:
        q.add("write the report")
    monkeypatch.setattr(drain, "run_task", FakeCLI({"mode": "ok"}))
    assert drain.drain(args(), qpath) == 0


# -- the handover on the phone ---------------------------------------------


def test_a_limit_notification_carries_the_handover_not_just_a_one_liner(
    qpath, monkeypatch, sent
):
    """"Usage limit" alone sends you to the machine to find out what happened,
    which is the trip the notification existed to save."""
    with Queue(qpath) as q:
        q.add("write the report")
    monkeypatch.setattr(
        drain, "run_task",
        FakeCLI({"mode": "limit", "epoch": PAST, "sid": "sess-abc"}, {"mode": "ok"}),
    )

    assert drain.drain(args(), qpath) == 0

    (limit,) = [m for k, m in sent if k == "limit"]
    assert "usage limit hit" in limit             # the plan still leads
    assert "Why:" in limit                        # ... and the handover follows
    assert limit.endswith("claude --resume sess-abc")
    assert len(limit.encode("utf-8")) <= notify.MAX_BODY


def test_a_task_that_runs_out_of_attempts_carries_one_too(qpath, monkeypatch, sent):
    with Queue(qpath) as q:
        q.add("write the report")
    monkeypatch.setattr(drain, "run_task", FakeCLI({"mode": "fail"}))
    monkeypatch.setattr(drain.time, "sleep", lambda _: None)

    drain.drain(args(max_retries=1), qpath)

    (failed,) = [m for k, m in sent if k == "failed"]
    assert "Gave up after 1 attempts" in failed
    assert failed.endswith("claude --resume sess-1")


def test_the_handover_file_goes_up_only_when_attachments_are_on(
    qpath, monkeypatch, sent, http
):
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    with Queue(qpath) as q:
        q.add("write the report")
    monkeypatch.setattr(
        drain, "run_task", FakeCLI({"mode": "limit", "epoch": PAST}, {"mode": "ok"})
    )

    assert drain.drain(args(), qpath) == 0
    assert http.requests == []                    # off by default: nothing uploaded

    monkeypatch.setenv("BUFFER_NTFY_ATTACH", "1")
    with Queue(qpath) as q:
        task = q.add("write the report again")
    monkeypatch.setattr(
        drain, "run_task", FakeCLI({"mode": "limit", "epoch": PAST}, {"mode": "ok"})
    )
    assert drain.drain(args(), qpath) == 0

    (req,) = http.requests
    assert req.get_method() == "PUT"
    assert req.headers["Filename"].startswith(task["id"])
    assert b"write the report again" in req.data  # the whole markdown, not the digest


def test_an_upload_that_fails_does_not_stop_the_drain(qpath, monkeypatch, sent):
    """The queue is the thing that must not be lost. A phone that cannot be
    reached is a notification nobody gets, and nothing more than that."""
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    monkeypatch.setenv("BUFFER_NTFY_ATTACH", "1")
    monkeypatch.setattr(
        urllib.request, "urlopen",
        FakeHTTP(raises=urllib.error.URLError("no route to host")),
    )
    with Queue(qpath) as q:
        q.add("write the report")
    monkeypatch.setattr(
        drain, "run_task", FakeCLI({"mode": "limit", "epoch": PAST}, {"mode": "ok"})
    )

    assert drain.drain(args(), qpath) == 0
    assert kinds(sent) == ["started", "limit", "started", "done", "drained"]


# -- the message itself ----------------------------------------------------


def test_the_limit_message_names_the_fallback_when_one_is_configured():
    msg = drain.limit_message("weekly", "a1b2", 0, 3600, fallback=True)
    assert "API-key billing" in msg
    assert "weekly limit hit on [a1b2]" in msg


def test_an_unknown_reset_time_still_says_what_happens_next():
    msg = drain.limit_message("session", "a1b2", 0, 3600, fallback=False)
    assert "Reset time unknown" in msg
    assert f"retrying in {drain.DEFAULT_BACKOFF // 60}m" in msg
