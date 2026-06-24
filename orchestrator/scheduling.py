"""launchd scheduling for the 09:00 daily update (macOS).

The human wants a plain-language update every morning at 09:00. We install a
per-user LaunchAgent that runs `factory daily` (a bounded autonomous session that
ends in the executive summary) at 09:00 every day. The plist is generated here —
and unit-tested — so the schedule (09:00) and the command (`factory daily`) can't
silently drift. Per-user (no sudo); trivially reversible (`schedule-uninstall`).
"""
from __future__ import annotations

import os
import plistlib

PLIST_LABEL = "com.harness-factory.daily"


def plist_path() -> str:
    """Per-user LaunchAgent path (runs as the logged-in user; no sudo)."""
    return os.path.expanduser(f"~/Library/LaunchAgents/{PLIST_LABEL}.plist")


def launchd_plist(factory_root: str, python_bin: str = "python3",
                  hour: int = 9, minute: int = 0) -> str:
    """Return the LaunchAgent plist XML that runs `<factory_root>/bin/factory daily`
    at hour:minute every day. stdout/stderr land in <factory_root>/logs so the human
    can inspect the night's run; the run itself NEVER promotes (autonomy guarantee).
    `python_bin` is propagated as FACTORY_PYTHON so the agent runs under an
    interpreter that has the factory's deps (launchd's PATH is minimal)."""
    log_dir = os.path.join(factory_root, "logs")
    spec = {
        "Label": PLIST_LABEL,
        "ProgramArguments": [os.path.join(factory_root, "bin", "factory"), "daily"],
        "WorkingDirectory": factory_root,
        "EnvironmentVariables": {"FACTORY_PYTHON": python_bin},
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        "RunAtLoad": False,
        "StandardOutPath": os.path.join(log_dir, "daily-launchd.out.log"),
        "StandardErrorPath": os.path.join(log_dir, "daily-launchd.err.log"),
    }
    return plistlib.dumps(spec).decode("utf-8")
