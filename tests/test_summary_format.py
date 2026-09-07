"""The handover has to be readable by a person, not just complete.

The first version pasted the CLI's whole result JSON into the file. It was
complete and useless: the one sentence that mattered sat under two kilobytes
of cache-token telemetry. These tests pin the shape that replaced it.
"""

import ansi
import pytest
import sessions

# Trimmed from a real interrupted run -- the case that prompted this.
REAL_BLOB = (
    '{"is_error":true,"num_turns":54,"session_id":"47798101-4a7e",'
    '"total_cost_usd":4.191645,"usage":{"cache_read_input_tokens":4071628,'
    '"output_tokens":39947},"modelUsage":{"claude-opus-5":{"costUSD":4.19}},'
    '"api_error_status":429,'
    '"result":"You\'ve hit your session limit \u00b7 resets 12:30pm",'
    '"type":"result"}\n\nYou\'ve hit your session limit \u00b7 resets 12:30pm'
)


@pytest.fixture(autouse=True)
def _no_colour(monkeypatch):
    """Colour off by default, so assertions match plain text."""
    monkeypatch.setenv("NO_COLOR", "1")
    ansi.reset()
    yield
    ansi.reset()


def test_the_telemetry_is_thrown_away_and_the_sentence_kept():
    facts = sessions.parse_result(REAL_BLOB)
    assert facts["message"].startswith("You've hit your session limit")
    assert facts["turns"] == 54
    assert facts["cost"] == pytest.approx(4.191645)


def test_non_json_output_is_kept_as_it_came():
    assert sessions.parse_result("Traceback: boom")["message"] == "Traceback: boom"
    assert sessions.parse_result("")["message"] == ""


def test_a_blob_with_no_result_field_falls_back_to_the_trailing_text():
    facts = sessions.parse_result('{"num_turns":2}\nsomething went wrong')
    assert facts["message"] == "something went wrong"


def test_the_summary_shows_the_reason_not_the_token_counts():
    md = sessions.build_summary(
        task_id="abc", prompt="do it", session_id="sess-1", repo=None,
        start_head=None, attempt=1, reason="session limit",
        result_text=REAL_BLOB,
    )
    assert "You've hit your session limit" in md
    assert "54 turns" in md and "$4.19" in md
    for noise in ("cache_read_input_tokens", "modelUsage", "is_error"):
        assert noise not in md


def test_the_resume_command_is_present_and_runnable():
    md = sessions.build_summary(
        task_id="abc", prompt="do it", session_id="47798101-4a7e", repo=None,
        start_head=None, attempt=1, reason="session limit",
    )
    assert "claude --resume 47798101-4a7e" in md


def test_without_a_session_it_says_so_rather_than_printing_a_dead_command():
    md = sessions.build_summary(
        task_id="abc", prompt="do it", session_id=None, repo=None,
        start_head=None, attempt=1, reason="failed",
    )
    assert "claude --resume" not in md
    assert "cannot be reopened" in md


def test_it_stays_short():
    """Long prompts are the norm; the file must not become a wall."""
    md = sessions.build_summary(
        task_id="abc", prompt="do it", session_id="s", repo=None,
        start_head=None, attempt=1, reason="session limit",
        result_text=REAL_BLOB,
    )
    assert len(md.splitlines()) < 30


# -- colour ---------------------------------------------------------------


def test_redirected_output_stays_clean_markdown():
    """`bq summary x > handover.md` must not write escape codes."""
    md = "# Title\n\n**Why:** limit\n\n```\nclaude --resume abc\n```\n"
    assert sessions.render(md) == md


def test_colour_drops_the_fences_and_paints_the_command(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    ansi.reset()
    out = sessions.render("# Title\n\n```\nclaude --resume abc\n```\n")
    assert "```" not in out
    assert "\033[" in out
    assert "claude --resume abc" in out


def test_no_color_beats_force_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    ansi.reset()
    assert not ansi.enabled()
    assert ansi.paint("x", "red") == "x"


def test_a_row_offers_the_command_rather_than_a_bare_id():
    row = {"run_id": 5, "task_id": "abc123", "status": "interrupted",
           "ended": "2026-09-07T20:00:00Z", "prompt": "do it",
           "repo": r"D:\p", "repo_head": "c7c4dba",
           "session_id": "47798101-4a7e", "reason": "session limit"}
    out = sessions.format_row(row)
    assert "claude --resume 47798101-4a7e" in out
    assert "session limit" in out


def test_the_timestamp_can_be_supplied_rather_than_assumed_to_be_now():
    """Rebuilding an old handover must not restamp it with today's date --
    "when did this stop" is the field someone debugging actually trusts."""
    md = sessions.build_summary(
        task_id="abc", prompt="do it", session_id="s", repo=None,
        start_head=None, attempt=1, reason="session limit",
        when="2026-09-07T05:50:37Z",
    )
    assert "2026-09-07T05:50:37Z" in md
