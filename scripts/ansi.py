#!/usr/bin/env python3
"""Colour, but only when there is a terminal to put it on.

Two things make this less trivial than emitting escape codes. A Windows
console does not interpret them until VT processing is switched on, so the
codes show up as literal garbage like `←[36m`. And `bq summary x > out.md`
should produce a clean file, not one peppered with escapes -- which matters
here because the summary is a real Markdown document people read later.

So: colour is off unless stdout is a tty we could turn on. NO_COLOR (any
value, per no-color.org) forces it off; FORCE_COLOR forces it on, which is
what CI and `| less -R` need.
"""

from __future__ import annotations

import os
import sys

CODES = {
    "reset": 0, "bold": 1, "dim": 2, "italic": 3, "underline": 4,
    "red": 31, "green": 32, "yellow": 33, "blue": 34,
    "magenta": 35, "cyan": 36, "grey": 90,
}

_enabled: bool | None = None


def _enable_windows_vt() -> bool:
    """Ask the console to interpret escape codes. False if it won't."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError, ValueError):
        return False


def enabled() -> bool:
    """Whether to emit colour. Decided once, then remembered."""
    global _enabled
    if _enabled is not None:
        return _enabled

    if os.environ.get("NO_COLOR") is not None:
        _enabled = False
    elif os.environ.get("FORCE_COLOR"):
        _enabled = True
    elif not sys.stdout.isatty():
        _enabled = False          # redirected: keep the bytes clean
    elif os.name == "nt":
        _enabled = _enable_windows_vt()
    else:
        _enabled = os.environ.get("TERM", "") != "dumb"
    return _enabled


def reset() -> None:
    """Forget the decision. Only tests should need this."""
    global _enabled
    _enabled = None


def paint(text: str, *styles: str) -> str:
    """Wrap text in styles, or return it untouched when colour is off."""
    if not text or not styles or not enabled():
        return text
    codes = ";".join(str(CODES[s]) for s in styles if s in CODES)
    if not codes:
        return text
    return f"\033[{codes}m{text}\033[0m"


def head(text: str) -> str:
    return paint(text, "bold", "cyan")


def label(text: str) -> str:
    return paint(text, "grey")


def cmd(text: str) -> str:
    """A command the reader is meant to run. Green, so it stands out as the
    one line in a wall of prose that is actionable."""
    return paint(text, "green")


def warn(text: str) -> str:
    return paint(text, "yellow")


def bad(text: str) -> str:
    return paint(text, "red")


def good(text: str) -> str:
    return paint(text, "green")
