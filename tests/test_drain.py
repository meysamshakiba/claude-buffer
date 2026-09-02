"""Usage-limit detection and the clock arithmetic behind it."""

from datetime import datetime, timedelta

import drain
import pytest
from drain import CLOCK_GRACE, detect_limit, parse_clock

NOW = datetime(2026, 9, 2, 14, 30, 0)  # a Wednesday


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(drain, "datetime", Frozen)


def at(*, hour, minute=0, day=NOW.day):
    return datetime(NOW.year, NOW.month, day, hour, minute).timestamp()


# -- detection -------------------------------------------------------------


def test_epoch_form_is_exact():
    epoch, kind = detect_limit("Claude AI usage limit reached|1735689600")
    assert (epoch, kind) == (1735689600, "usage")


def test_epoch_form_counts_even_on_success():
    epoch, _ = detect_limit("usage limit reached | 1735689600", failed=False)
    assert epoch == 1735689600


def test_session_clock_form():
    epoch, kind = detect_limit("You've hit your session limit · resets 3:45pm")
    assert kind == "session"
    assert epoch == at(hour=15, minute=45)


def test_weekly_clock_form_walks_to_the_weekday():
    epoch, kind = detect_limit("You've hit your weekly limit · resets Mon 12:00am")
    assert kind == "weekly"
    assert epoch == at(hour=0, day=7)  # the following Monday


def test_ordinary_output_is_not_a_limit():
    assert detect_limit("Wrote 3 files. All tests passed.") == (None, "")


# -- regression: a task that merely discusses rate limiting -----------------
#
# The fuzzy phrase match runs against the CLI's own channel only. Matching it
# against task output made the daemon sleep on a schedule of its own invention,
# and since limits don't consume retries, retry that task forever.


def test_task_output_mentioning_429_is_not_a_limit():
    stdout = '{"result": "The server returned 429 Too Many Requests", "is_error": false}'
    assert detect_limit(stdout, failed=True, harness_text="") == (None, "")


def test_task_output_mentioning_rate_limits_is_not_a_limit():
    stdout = "test_retry.py::test_rate_limited FAILED - reached your usage limit"
    assert detect_limit(stdout, failed=True, harness_text="") == (None, "")


def test_the_cli_saying_it_on_stderr_is_a_limit():
    epoch, kind = detect_limit(
        "some task output", failed=True, harness_text="429 Too Many Requests"
    )
    assert (epoch, kind) == (0, "unknown")


def test_fuzzy_match_requires_failure():
    assert detect_limit(
        "x", failed=False, harness_text="429 too many requests"
    ) == (None, "")


# -- regression: a reset time that has only just passed --------------------
#
# Rolling it forward a day made the daemon wait ~24h, or exceed --max-sleep
# and exit, for a limit that had already lifted.


def test_reset_just_in_the_past_does_not_roll_forward():
    just_passed = (NOW - timedelta(minutes=2)).strftime("%I:%M%p").lower().lstrip("0")
    epoch = parse_clock(just_passed)
    assert epoch <= NOW.timestamp()
    assert epoch == at(hour=14, minute=28)


def test_reset_well_in_the_past_rolls_forward_a_day():
    stale = (NOW - CLOCK_GRACE - timedelta(minutes=5)).strftime("%I:%M%p").lower()
    epoch = parse_clock(stale)
    assert epoch > NOW.timestamp()
    assert epoch == at(hour=14, minute=15, day=NOW.day + 1)


def test_reset_later_today_stays_today():
    assert parse_clock("3:45pm") == at(hour=15, minute=45)


def test_24h_clock_is_understood():
    assert parse_clock("15:45") == at(hour=15, minute=45)


def test_unparseable_clock_is_none():
    assert parse_clock("half past four") is None
