"""The handover, squeezed onto a phone.

ntfy caps a message body at 4096 bytes, so something has to be dropped from a
handover that names sixty changed files. Which thing gets dropped is the whole
design: the `claude --resume` line is the only part that cannot be worked out
again from the repository, so it is reserved before anything else is measured
and never truncated away. These tests pin that, and the arithmetic around it --
bytes rather than characters, and cuts on line boundaries rather than mid-name.
"""

import sessions

LIMIT_BLOB = (
    '{"is_error":true,"num_turns":54,"session_id":"47798101-4a7e",'
    '"total_cost_usd":4.19,'
    '"result":"You\'ve hit your session limit · resets 12:30pm",'
    '"type":"result"}'
)


def handover(changed=(), session_id="47798101-4a7e", prompt="do the thing"):
    """A real handover, with a Changed list of a chosen size.

    build_summary reads a git repository for that list, so the list is spliced
    into the finished markdown instead -- these tests are about the squeeze,
    not about git.
    """
    md = sessions.build_summary(
        task_id="a1b2", prompt=prompt, session_id=session_id, repo=None,
        start_head=None, attempt=2, reason="session limit",
        result_text=LIMIT_BLOB,
    )
    if changed:
        md = md.replace(
            "## Changed\n\n- not a git repository, so no record of file changes",
            "## Changed\n\n" + "\n".join(changed),
        )
    return md


def size(text):
    return len(text.encode("utf-8"))


# -- what it keeps ---------------------------------------------------------


def test_it_carries_why_it_stopped_what_changed_and_how_to_continue():
    out = sessions.digest(handover(["- `M` scripts/parse.py"]))
    assert "Why: session limit — You've hit your session limit" in out
    assert "- `M` scripts/parse.py" in out
    assert out.endswith("claude --resume 47798101-4a7e")


def test_it_drops_the_parts_a_phone_has_no_use_for():
    """The task text, the timestamps and the prose around the command are what
    the file is for; a lock screen has room for none of it."""
    out = sessions.digest(handover(prompt="a very long task description"))
    assert "a very long task description" not in out
    assert "## Task" not in out and "**Why:**" not in out
    assert "replays the real conversation" not in out


def test_a_lead_goes_first_because_it_says_what_happens_next():
    out = sessions.digest(handover(), lead="sleeping 41m, then [a1b2] continues")
    assert out.startswith("sleeping 41m, then [a1b2] continues")
    assert "claude --resume" in out


def test_without_a_session_it_says_so_rather_than_inventing_a_command():
    out = sessions.digest(handover(session_id=None))
    assert "claude --resume" not in out
    assert out.endswith(sessions.NO_SESSION)


def test_an_empty_handover_digests_to_nothing_worth_sending():
    """The daemon falls back to its one-liner on this, so it must be plainly
    empty rather than a lone stub of punctuation."""
    assert sessions.digest("").strip() == sessions.NO_SESSION


# -- what it drops first ---------------------------------------------------


def test_the_resume_command_survives_a_changed_list_that_fills_the_budget():
    changed = [f"- `M` scripts/module_{i:04d}_with_a_long_name.py" for i in range(500)]
    out = sessions.digest(handover(changed))
    assert size(out) <= sessions.DIGEST_LIMIT
    assert out.endswith("claude --resume 47798101-4a7e")


def test_it_cuts_between_lines_not_through_a_filename():
    changed = [f"- `M` scripts/module_{i:04d}_with_a_long_name.py" for i in range(500)]
    out = sessions.digest(handover(changed))
    kept = [ln for ln in out.splitlines() if ln.startswith("- `M`")]
    assert kept                                  # some of the list made it
    assert all(ln in changed for ln in kept)     # and every one of them is whole


def test_it_says_how_many_changes_it_left_out():
    """A truncated list read as a complete one is worse than no list: it says
    a file was not touched when it was."""
    changed = [f"- `M` scripts/module_{i:04d}_with_a_long_name.py" for i in range(500)]
    out = sessions.digest(handover(changed))
    shown = len([ln for ln in out.splitlines() if ln.startswith("- `M`")])
    assert f"- ...and {500 - shown} more" in out


def test_a_short_list_is_not_marked_as_truncated():
    out = sessions.digest(handover(["- `M` a.py", "- `M` b.py"]))
    assert "more" not in out.splitlines()[-2]
    assert "- `M` b.py" in out


def test_the_command_beats_the_changes_when_only_one_can_fit():
    changed = [f"- `M` scripts/module_{i:04d}.py" for i in range(50)]
    out = sessions.digest(handover(changed), limit=120)
    assert size(out) <= 120
    assert "claude --resume 47798101-4a7e" in out


def test_an_enormous_lead_cannot_squeeze_the_command_out():
    """The lead is the daemon's own text, so it is trusted -- but trusted text
    is still text, and a paragraph of it must not cost the resume line."""
    out = sessions.digest(handover(), lead="x" * 8000)
    assert size(out) <= sessions.DIGEST_LIMIT
    assert out.endswith("claude --resume 47798101-4a7e")


# -- the arithmetic --------------------------------------------------------


def test_the_budget_is_bytes_not_characters():
    """A task written in Japanese is three times its own length once encoded,
    and ntfy counts the bytes. Measuring characters sends a message that is
    rejected -- or silently turned into an attachment."""
    changed = [f"- `M` スクリプト/モジュール_{i:04d}.py" for i in range(500)]
    out = sessions.digest(handover(changed))
    assert size(out) <= sessions.DIGEST_LIMIT
    assert len(out) < size(out)                  # genuinely multi-byte
    assert out.endswith("claude --resume 47798101-4a7e")


def test_a_multibyte_character_is_never_cut_in_half():
    out = sessions.digest(handover(), lead="✓" * 4000)
    out.encode("utf-8").decode("utf-8")          # would raise on a split character
    assert "�" not in out


def test_it_fits_what_the_notifier_will_actually_send():
    """digest() and notify.post() have to agree about the cap, or the message
    is trimmed a second time -- from the end, where the resume line lives."""
    import notify

    assert sessions.DIGEST_LIMIT <= notify.MAX_BODY
