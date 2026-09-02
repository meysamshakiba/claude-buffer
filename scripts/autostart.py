#!/usr/bin/env python3
"""Keep a drain daemon running without anyone remembering to start one.

The daemon already survives a terminal closing, a Claude session ending and a
usage limit. What it does not survive is a reboot, or being killed — and the
whole point of a queue is that you are not watching, so nobody notices.

This registers a small recurring job that runs `drain.py --daemon`. That
command is a no-op when a daemon is already up, so running it every few minutes
costs nothing and repairs the one case that matters: no daemon at all.

    python3 autostart.py --install
    python3 autostart.py --status
    python3 autostart.py --uninstall
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from buffer_queue import setup_console

TASK_NAME = "ClaudeBufferDaemon"
EVERY_MINUTES = 15
DRAIN = Path(__file__).resolve().parent / "drain.py"
IS_WIN = os.name == "nt"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


# -- Windows: Task Scheduler ----------------------------------------------


def win_install(extra: list[str] | None = None) -> int:
    action = f'"{sys.executable}" "{DRAIN}" --daemon --watch'
    if extra:
        action += " " + " ".join(f'"{a}"' if " " in a else a for a in extra)
    # Every N minutes rather than at-logon only: it covers logon anyway, and
    # also brings the daemon back if it is killed mid-session. Runs only while
    # logged on, which is what we want — it uses the user's own credentials.
    proc = _run([
        "schtasks", "/Create", "/TN", TASK_NAME, "/TR", action,
        "/SC", "MINUTE", "/MO", str(EVERY_MINUTES), "/F",
    ])
    if proc.returncode != 0:
        print(proc.stderr.strip() or proc.stdout.strip())
        return proc.returncode
    print(f"Registered scheduled task '{TASK_NAME}' (every {EVERY_MINUTES}m).")

    # The first scheduled run is up to EVERY_MINUTES away; don't make the user
    # wait to find out whether it works.
    started = _run(["schtasks", "/Run", "/TN", TASK_NAME])
    print("Started it now." if started.returncode == 0 else started.stderr.strip())
    return 0


def win_uninstall() -> int:
    proc = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    print(proc.stdout.strip() or proc.stderr.strip() or "Removed.")
    return proc.returncode


def win_status() -> int:
    proc = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"])
    if proc.returncode != 0:
        print(f"No scheduled task named '{TASK_NAME}'. Run --install.")
        return 1
    wanted = ("TaskName", "Status", "Next Run Time", "Last Run Time")
    for line in proc.stdout.splitlines():
        if line.split(":")[0].strip() in wanted:
            print(line.strip())
    return 0


# -- POSIX: print a unit rather than half-manage one -----------------------

SYSTEMD = """[Unit]
Description=Claude buffer queue daemon

[Service]
Type=oneshot
ExecStart={python} {drain} --daemon --watch

[Install]
WantedBy=default.target
"""

SYSTEMD_TIMER = """[Unit]
Description=Keep the Claude buffer daemon running

[Timer]
OnBootSec=1min
OnUnitActiveSec={every}min

[Install]
WantedBy=timers.target
"""

LAUNCHD = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.claude.buffer.daemon</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{drain}</string>
    <string>--daemon</string>
    <string>--watch</string>
  </array>
  <key>StartInterval</key><integer>{seconds}</integer>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
"""


def posix_help() -> int:
    ctx = {"python": sys.executable, "drain": DRAIN, "every": EVERY_MINUTES,
           "seconds": EVERY_MINUTES * 60}
    print("Automatic start isn't wired up for this platform yet. Paste one of "
          "these; both re-run a command that does nothing when a daemon is "
          "already up.\n")
    print(f"--- ~/.config/systemd/user/claude-buffer.service ---\n{SYSTEMD.format(**ctx)}")
    print(f"--- ~/.config/systemd/user/claude-buffer.timer ---\n{SYSTEMD_TIMER.format(**ctx)}")
    print("  systemctl --user enable --now claude-buffer.timer\n")
    print(f"--- ~/Library/LaunchAgents/com.claude.buffer.daemon.plist ---\n"
          f"{LAUNCHD.format(**ctx)}")
    print("  launchctl load ~/Library/LaunchAgents/com.claude.buffer.daemon.plist")
    return 1


def main() -> int:
    setup_console()
    p = argparse.ArgumentParser(description="Keep the buffer daemon running")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--install", action="store_true")
    g.add_argument("--uninstall", action="store_true")
    g.add_argument("--status", action="store_true")
    p.add_argument("extra", nargs=argparse.REMAINDER,
                   help="after --, flags passed through to drain.py, e.g. "
                        "-- --claude-arg --allowedTools --claude-arg Read,Edit")
    args = p.parse_args()

    # argparse.REMAINDER keeps the "--" separator; drain.py should not see it.
    extra = [a for a in args.extra if a != "--"]

    if not IS_WIN:
        return posix_help()
    if args.install:
        return win_install(extra)
    if args.uninstall:
        return win_uninstall()
    return win_status()


if __name__ == "__main__":
    sys.exit(main())
