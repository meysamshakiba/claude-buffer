"""What the scheduled task actually runs.

The launcher is generated, so a setting that lives only in the user's shell
never reaches it. That failure is invisible: notifications unconfigured means
no notification and no error, which looks exactly like a quiet night.
"""

import autostart
import pytest


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart, "queue_path", lambda: tmp_path / "queue.md")
    for var in ("BUFFER_NTFY_TOPIC", "BUFFER_NTFY_URL"):
        monkeypatch.delenv(var, raising=False)
    return lambda extra=(): autostart.write_launcher(list(extra)).read_text(
        encoding="utf-8"
    )


def test_the_topic_is_pinned_rather_than_inherited(launcher, monkeypatch):
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "buffer-9f3a1c7d")
    assert 'set "BUFFER_NTFY_TOPIC=buffer-9f3a1c7d"' in launcher()


def test_a_self_hosted_server_is_pinned_too(launcher, monkeypatch):
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    monkeypatch.setenv("BUFFER_NTFY_URL", "https://ntfy.example.com")
    assert 'set "BUFFER_NTFY_URL=https://ntfy.example.com"' in launcher()


def test_nothing_is_written_when_notifications_are_off(launcher):
    assert "BUFFER_NTFY" not in launcher()


def test_a_blank_topic_is_not_pinned(launcher, monkeypatch):
    """An exported-but-empty variable is off, not a topic named ""."""
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "   ")
    assert "BUFFER_NTFY" not in launcher()


def test_the_env_does_not_displace_the_policy(launcher, monkeypatch):
    """The whole reason this file exists is that the policy is too long for
    schtasks /TR; pinning must come before the command, not replace it."""
    monkeypatch.setenv("BUFFER_NTFY_TOPIC", "abc")
    text = launcher(["--checkpoint", "--", "--allowedTools", "Read,Edit"])
    assert "--checkpoint" in text and '"Read,Edit"' in text
    assert text.index('set "BUFFER_NTFY_TOPIC') < text.index("--daemon")
    assert text.rstrip().endswith('--allowedTools "Read,Edit"')
