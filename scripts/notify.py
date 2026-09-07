#!/usr/bin/env python3
"""Push one line to a phone when something happens in the queue.

The whole point of the daemon is that nobody is watching it. That is also its
weakness: every way of finding out what it did -- `bq`, `bq report`, the log --
needs the machine it runs on, and by 2am you are not at that machine. So the
events worth knowing about have to travel to where you actually are.

ntfy.sh is the entire transport: an HTTP POST to a topic URL. No account, no
SDK, no dependency, and self-hosting it is the same POST with another base URL.
Subscribing is installing the app and typing the topic name.

    export BUFFER_NTFY_TOPIC=buffer-9f3a1c7d          # topic to publish to
    export BUFFER_NTFY_URL=https://ntfy.example.com   # optional, for self-hosted

With neither set this module does nothing at all: no request, no error, no log
line. Notifications are opt-in, and a daemon without them configured has to
behave exactly as it did before they existed.

A topic name is the only secret involved -- anyone who knows it can read your
task titles and post to it. Use an unguessable one rather than "buffer".
"""

from __future__ import annotations

import http.client
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlsplit

DEFAULT_SERVER = "https://ntfy.sh"
TIMEOUT = 10        # a phone service that hangs must not hold up the queue
MAX_BODY = 3800     # ntfy rejects an oversized message; truncate instead

# kind -> (title, tags, priority). Priorities are ntfy's 1..5, and the choice
# is about sleep: at 3am a failure is the only thing worth a buzz, and a task
# merely starting is not. Anything louder and the first night teaches the user
# to mute the topic, which costs them the failure notification too.
EVENTS: dict[str, tuple[str, str, int]] = {
    "started": ("Task started", "arrow_forward", 2),
    "done": ("Task done", "white_check_mark", 3),
    "failed": ("Task failed", "rotating_light", 4),
    "limit": ("Usage limit", "hourglass_flowing_sand", 3),
    "drained": ("Queue drained", "sparkles", 3),
}


def _silent(msg: str) -> None:
    """Default reporter: say nothing. Unset means off, not broken."""


_reporter: Callable[[str], None] = _silent


def set_reporter(fn: Callable[[str], None] | None) -> None:
    """Send warnings -- a refused POST, a malformed URL -- somewhere visible.
    The daemon points this at its log; unconfigured, they are dropped."""
    global _reporter
    _reporter = fn or _silent


def endpoint() -> str | None:
    """The URL to POST to, or None when notifications are switched off.

    Two variables, because a topic on ntfy.sh needs one and a self-hosted
    server needs two:

        TOPIC=abc                       -> https://ntfy.sh/abc
        TOPIC=abc URL=https://n.example -> https://n.example/abc
        URL=https://n.example/abc       -> as given, topic already named
    """
    topic = (os.environ.get("BUFFER_NTFY_TOPIC") or "").strip().strip("/")
    base = (os.environ.get("BUFFER_NTFY_URL") or "").strip().rstrip("/")
    if not topic and not base:
        return None

    if not base:
        base = DEFAULT_SERVER
    elif "://" not in base:
        base = f"https://{base}"     # "ntfy.example.com" is a URL people type

    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        _reporter(f"BUFFER_NTFY_URL={base!r} is not an http(s) URL; not notifying")
        return None

    # Appending the topic is also how a server behind a path prefix works
    # (https://example.com/ntfy + /topic), so this needs no special case.
    if topic:
        return f"{base}/{topic}"
    if parts.path.strip("/"):
        return base
    _reporter("BUFFER_NTFY_URL names a server but no topic; set BUFFER_NTFY_TOPIC")
    return None


def _header(value: str, limit: int = 200) -> str:
    """Flatten a value into something an HTTP header can carry.

    ntfy takes the title and tags as headers, which are single-line and ASCII.
    Task text is neither: it is prose, it may contain an em dash or CJK, and --
    since anything that can write a file can queue work -- it may contain a
    newline someone else chose. Collapsing whitespace first is what stops that
    from splitting the header into headers of its own.
    """
    return " ".join(value.split()).encode("ascii", "ignore").decode("ascii")[:limit]


def post(message: str, *, title: str = "", tags: str = "",
         priority: int | None = None, url: str | None = None) -> bool:
    """POST one notification. True if it went out.

    Never raises. A phone that cannot be reached is not a reason to stop
    draining a queue -- the queue is the thing that must not be lost.
    """
    target = url or endpoint()
    if not target:
        return False

    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if title:
        headers["Title"] = _header(title)
    if tags:
        headers["Tags"] = _header(tags)
    if priority:
        headers["Priority"] = str(priority)

    body = (message or "").strip()[:MAX_BODY].encode("utf-8")
    req = urllib.request.Request(target, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        _reporter(f"could not notify {target}: {exc}")
        return False


def event(kind: str, message: str) -> bool:
    """Send one queue event: started, done, failed, limit, drained."""
    title, tags, priority = EVENTS.get(kind, (kind.replace("_", " ").capitalize(), "", 3))
    return post(message, title=title, tags=tags, priority=priority)
