"""The autopilot supervisor — turns the dashboard's AUTO toggle into ACTUAL work.

The toggle alone only sets the mode file; something has to RUN. This module is that
something: toggling AUTO sets the mode AND ensures a single `factory run --loop` runner is
alive and detached, so the factory self-drives shift after shift. Toggling SHIFT just sets
the mode — the running loop reads it between shifts and winds down after the current shift
(no hard kill; a shift is atomic). The runner outlives the dashboard server (its own
session); a PID file tracks it so we never double-spawn and the board can show its status.
"""
from __future__ import annotations

import os
import subprocess
from typing import Optional

from ..common import config, paths


def pid_path() -> str:
    return os.path.join(paths.FACTORY_ROOT, ".autopilot.pid")


def _read_pid() -> Optional[int]:
    try:
        with open(pid_path(), "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)            # signal 0 = liveness probe, no signal delivered
        return True
    except OSError:
        return False


def runner_alive() -> Optional[int]:
    """The live runner's PID from the PID file, or None — cheap (no process scan), so the
    dashboard can poll it. Cleans up a stale PID file (process already exited)."""
    pid = _read_pid()
    if pid and _alive(pid):
        return pid
    if pid:                        # recorded but dead → stale file, remove it
        try:
            os.remove(pid_path())
        except OSError:
            pass
    return None


def _scan_for_runner() -> Optional[int]:
    """Backstop for a LOST/missing PID file: find a live `run --loop` runner by process scan,
    so a dropped PID file can't let a toggle double-spawn. Verifies via `ps` that the match is
    the python runner, not a shell that merely mentions the string. Only used at spawn time."""
    try:
        out = subprocess.run(["pgrep", "-f", "orchestrator.orchestrator run --loop"],
                            capture_output=True, text=True, timeout=5)
    except Exception:  # noqa: BLE001 — no pgrep → no backstop (PID file stays primary)
        return None
    for tok in out.stdout.split():
        try:
            pid = int(tok)
        except ValueError:
            continue
        if pid == os.getpid() or not _alive(pid):
            continue
        try:
            cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=5).stdout
        except Exception:  # noqa: BLE001
            cmd = ""
        if "orchestrator" in cmd and "run --loop" in cmd:   # a real runner, not a shell echo
            return pid
    return None


def _spawn(argv: list, log_path: str) -> int:
    """Spawn the runner DETACHED (own session, survives the server), logging to a file."""
    log = open(log_path, "a", encoding="utf-8")    # noqa: SIM115 — handed to the child
    proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True,
                            cwd=paths.FACTORY_ROOT)
    return proc.pid


def start_runner(*, real: Optional[bool] = None, prod: Optional[bool] = None,
                 factory_bin: Optional[str] = None, log_path: Optional[str] = None,
                 spawn_fn=None) -> dict:
    """Ensure ONE `factory run --loop` runner is alive (no-op if already running). Returns
    {started, pid, argv, log}. real/prod default from config.autopilot (real=True: merges
    land on the reversible factory/auto branch — the useful default; prod=False: dev/same-user
    while testing). spawn_fn is injectable so tests never launch a real process."""
    existing = runner_alive() or _scan_for_runner()    # PID file, then a process-scan backstop
    if existing:
        try:                                           # re-adopt into the PID file
            with open(pid_path(), "w", encoding="utf-8") as fh:
                fh.write(str(existing))
        except OSError:
            pass
        return {"started": False, "pid": existing}

    ap = config.load_config().get("autopilot", {}) or {}
    real = ap.get("real", True) if real is None else real
    prod = ap.get("prod", False) if prod is None else prod
    factory_bin = factory_bin or os.path.join(paths.FACTORY_ROOT, "bin", "factory")
    log_path = log_path or os.path.join(paths.FACTORY_ROOT, "logs", "autopilot.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    argv = [factory_bin, "run", "--loop"]
    if real:
        argv.append("--real")
    if prod:
        argv.append("--prod")

    pid = (spawn_fn or _spawn)(argv, log_path)
    with open(pid_path(), "w", encoding="utf-8") as fh:
        fh.write(str(pid))
    return {"started": True, "pid": pid, "argv": argv, "log": log_path}


def status() -> dict:
    """Autopilot status for the dashboard: running + pid (None when idle)."""
    pid = runner_alive()
    return {"running": pid is not None, "pid": pid}
