#!/usr/bin/env python3
"""Fix the machine, then retry — instead of reporting a precondition failure.

Some tasks fail for a reason that has nothing to do with the task and everything
to do with the machine not being ready: no emulator attached, a container not
up, a VPN dropped. The daemon runs at 05:00 precisely because nobody is awake,
so "start the emulator and try again" is advice that arrives eight hours late
and costs the whole run.

A repair rule pairs a pattern with the command that fixes it:

    {"rules": [
      {"name": "android emulator",
       "match": "no device attached|no devices/emulators found|device offline",
       "run": ["powershell", "-NoProfile", "-File",
               "C:/dev/emulator_check.ps1", "-CreateAvd"],
       "timeout": 600}
    ]}

Rules live in $CLAUDE_BUFFER_REPAIRS, or `repairs.json` beside the queue state.
With no such file this module does nothing: repairs run commands, so they are
opt-in and come only from a file the user wrote.

A rule fires at most once per task per drain. If the repair runs and the task
fails the same way again, the failure is real and is reported as usual.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 600     # emulator boots are slow; still bounded
MAX_OUTPUT = 2000         # enough to see why a repair failed


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    run: list[str] | str
    shell: bool = False
    timeout: int = DEFAULT_TIMEOUT
    cwd: str | None = None


def config_path(state_dir: Path) -> Path:
    env = os.environ.get("CLAUDE_BUFFER_REPAIRS")
    return Path(env).expanduser() if env else state_dir / "repairs.json"


def load(state_dir: Path, warn: Callable[[str], None] = lambda _m: None) -> list[Rule]:
    """Read the rules. A broken config must never cost us the queue, so every
    problem is a warning and a skipped rule, not an exception."""
    path = config_path(state_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        warn(f"Ignoring repair rules in {path}: {exc}")
        return []

    entries = raw.get("rules", []) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        warn(f"Ignoring repair rules in {path}: expected a list of rules.")
        return []

    rules: list[Rule] = []
    for i, entry in enumerate(entries):
        try:
            pattern = re.compile(entry["match"], re.I)
            run = entry["run"]
            if not isinstance(run, (str, list)) or not run:
                raise ValueError("'run' must be a non-empty command or argument list")
            rules.append(Rule(
                name=str(entry.get("name") or f"rule {i + 1}"),
                pattern=pattern,
                run=run,
                shell=bool(entry.get("shell", isinstance(run, str))),
                timeout=int(entry.get("timeout", DEFAULT_TIMEOUT)),
                cwd=entry.get("cwd"),
            ))
        except (AttributeError, KeyError, TypeError, ValueError, re.error) as exc:
            warn(f"Skipping repair rule {i + 1} in {path}: {exc}")
    return rules


def match(rules: list[Rule], output: str) -> Rule | None:
    """The first rule whose pattern appears in a failed task's output."""
    for rule in rules:
        if rule.pattern.search(output or ""):
            return rule
    return None


def apply(rule: Rule, cwd: str | None = None) -> tuple[bool, str]:
    """Run the repair. Returns (ok, output) — the caller decides what a failed
    repair means, because a retry is often still worth attempting."""
    where = rule.cwd or cwd
    if where and not Path(where).is_dir():
        where = None
    try:
        proc = subprocess.run(
            rule.run, shell=rule.shell, capture_output=True, text=True,
            timeout=rule.timeout, encoding="utf-8", errors="replace", cwd=where,
        )
    except FileNotFoundError as exc:
        return False, str(exc)
    except subprocess.TimeoutExpired:
        return False, f"repair timed out after {rule.timeout}s"
    except OSError as exc:
        return False, str(exc)

    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return proc.returncode == 0, out[-MAX_OUTPUT:]
