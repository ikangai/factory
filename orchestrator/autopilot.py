"""The autopilot supervisor — turns the dashboard's AUTO toggle into ACTUAL work.

The toggle alone only sets the mode file; something has to RUN. This module is that
something: toggling AUTO sets the mode AND ensures a single `factory run --loop` runner is
alive and detached, so the factory self-drives shift after shift. Toggling SHIFT just sets
the mode — the running loop reads it between shifts and winds down after the current shift
(no hard kill; a shift is atomic). The runner outlives the dashboard server (its own
session); a PID file tracks it so we never double-spawn and the board can show its status.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import time
from typing import Optional

from ..common import config, killswitch, mode, paths


def pid_path() -> str:
    return os.path.join(paths.FACTORY_ROOT, ".autopilot.pid")


def _lock_path() -> str:
    return pid_path() + ".lock"    # tracks pid_path so tests that move the pid file move the lock


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


def _is_runner_proc(pid: int) -> bool:
    """Identity check: is `pid` ACTUALLY a factory `run --loop` runner, not a recycled/foreign
    pid? os.kill(pid,0) only proves 'some process'; macOS recycles pids, so trusting it alone
    makes the board show a phantom runner (and a toggle adopt the wrong pid)."""
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                            capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return False
    return "orchestrator" in cmd and "run --loop" in cmd


def runner_alive() -> Optional[int]:
    """The runner's PID from the file — IDENTITY-CHECKED to be our runner (a recycled pid can't
    masquerade), else None. Cleans a stale/foreign entry. One ps when a pid is recorded — cheap
    enough for the dashboard poll."""
    pid = _read_pid()
    if pid and _alive(pid) and _is_runner_proc(pid):
        return pid
    if pid:                        # dead, or recycled to a non-runner → stale, remove it
        try:
            os.remove(pid_path())
        except OSError:
            pass
    return None


def _scan_for_runner() -> Optional[int]:
    """Backstop for a LOST/missing PID file: find a live `run --loop` runner by process scan,
    so a dropped PID file can't let a toggle double-spawn. Identity-verified, not a shell echo.
    Only used at spawn time."""
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
        if pid != os.getpid() and _alive(pid) and _is_runner_proc(pid):
            return pid
    return None


def clear_pid_if_mine() -> None:
    """Remove the PID file IFF it records THIS process — the runner calls this on exit so the
    file never outlives it (else the board shows a phantom runner until the next poll)."""
    if _read_pid() == os.getpid():
        try:
            os.remove(pid_path())
        except OSError:
            pass


def _spawn(argv: list, log_path: str) -> int:
    """Spawn the runner DETACHED (own session, survives the server), logging to a file. The
    parent's log handle is closed after Popen (the child keeps its own dup) — no fd leak."""
    with open(log_path, "a", encoding="utf-8") as log:
        proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True,
                                cwd=paths.FACTORY_ROOT)
    return proc.pid


def start_runner(*, real: Optional[bool] = None, prod: Optional[bool] = None,
                 factory_bin: Optional[str] = None, log_path: Optional[str] = None,
                 spawn_fn=None) -> dict:
    """Ensure EXACTLY ONE `factory run --loop` runner is alive (no-op if already running).
    The whole check→spawn→record is under an exclusive file lock, so two concurrent AUTO
    toggles (the threaded server) or two servers can't both pass the 'none running' check and
    double-spawn. Returns {started, pid, argv, log}. real/prod default from config.autopilot.
    spawn_fn is injectable so tests never launch a real process."""
    lock_fd = os.open(_lock_path(), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)             # serialize across threads AND processes
        existing = runner_alive() or _scan_for_runner()
        if existing:
            try:                                        # re-adopt into the PID file
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
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def status() -> dict:
    """Autopilot status for the dashboard: running + pid (None when idle)."""
    pid = runner_alive()
    return {"running": pid is not None, "pid": pid}


_RESTART_DEBOUNCE_SEC = 300.0    # at most one self-heal / 5 min — a crash-looping runner can't thrash
_last_restart_ts = 0.0           # MODULE-LEVEL monotonic clock of the last restart ATTEMPT (0 = never)


def restart_if_auto(*, now: Optional[float] = None,
                    debounce_sec: Optional[float] = None, spawn_fn=None) -> dict:
    """Brake-respecting self-heal for AUTO: if the mode is AUTO and no runner is alive, restart it
    — so a crashed runner doesn't stay down until a human re-toggles (today start_runner's only
    call site is the mode-toggle POST). Meant to be called from the dashboard's ~2s poll.

    Vetoed, in strict order:
      1. killswitch.is_halted()  — STOP wins over EVERYTHING, checked FIRST (safety before autonomy)
      2. not mode.is_auto()      — SHIFT (or unset) → human-in-the-loop, no auto-respawn
      3. runner_alive()          — a live runner needs no healing
      4. debounce window         — a module-level monotonic stamp caps restarts so a crash-looping
                                   runner can't thrash the box

    `now`/`debounce_sec` are injectable for deterministic tests; `spawn_fn` threads to start_runner
    so tests never launch a real process. Returns {'restarted': bool, 'reason': str[, 'start': …]}."""
    global _last_restart_ts
    if killswitch.is_halted():                      # STOP wins over EVERYTHING — first gate
        return {"restarted": False, "reason": "halted"}
    if not mode.is_auto():                          # SHIFT / unset → human-in-the-loop veto
        return {"restarted": False, "reason": "not_auto"}
    if runner_alive() is not None:                  # already up → nothing to heal
        return {"restarted": False, "reason": "alive"}
    now = time.monotonic() if now is None else now
    window = _RESTART_DEBOUNCE_SEC if debounce_sec is None else debounce_sec
    if _last_restart_ts and (now - _last_restart_ts) < window:
        return {"restarted": False, "reason": "debounced"}
    _last_restart_ts = now                          # stamp the ATTEMPT (thrash guard) before spawning
    res = start_runner(spawn_fn=spawn_fn)           # start_runner self-guards against a double-spawn
    return {"restarted": True, "reason": "started", "start": res}
