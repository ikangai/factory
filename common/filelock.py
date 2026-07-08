"""Cross-process push lock: serialize every actor that can push the target repo outward
(an operator's Approve click via reporting.approvals.execute_approval, the gate-off
post-shift graduate in orchestrator._graduate_after_shift, and the manual `factory
graduate` CLI). Without it, two of those racing — e.g. an Approve landing while a
shift-end graduation runs in the autopilot process — could interleave fetch/merge/push
on the same checkout.

Mechanism: fcntl.flock on a lock FILE (mirrors orchestrator/autopilot.py's start_runner
lock — the same "serialize across threads AND processes" pattern), but NON-blocking with
a bounded poll loop: a pusher that can't get the lock within `timeout_s` raises
LockBusyError and the caller skips/fails closed ("lock-busy") instead of queueing behind
an arbitrarily long push+retest. flock conflicts between separate file descriptors even
within one process, so two threads of the same dashboard server serialize too.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import tempfile
import time

# Module-level so tests can monkeypatch it down to milliseconds; 30s comfortably covers a
# normal fetch+push but refuses to queue behind a full graduation retest (minutes).
DEFAULT_TIMEOUT_S = 30.0
_POLL_S = 0.2


class LockBusyError(RuntimeError):
    """The lock is held by another pusher and `timeout_s` elapsed — skip, don't push."""


def _lock_path(root: str, name: str) -> str:
    """Where the lock file lives. Preferred: inside <root>/.git (invisible to the work
    tree, dies with the clone). Fallback when .git is NOT a directory — a linked worktree
    (.git is a file, e.g. clive.factory-auto) or a not-yet-cloned/injected-test root — a
    tempdir path keyed by the root's realpath, so every process that resolves the SAME
    root contends on the SAME file. Known bound: two actors addressing one repo through
    DIFFERENT roots (base checkout vs a linked worktree) get different lock files; all
    current push-side call sites resolve root identically (config.get_adapter().entry()),
    so this doesn't arise today."""
    gitdir = os.path.join(root, ".git")
    if os.path.isdir(gitdir):
        return os.path.join(gitdir, f"{name}.lock")
    digest = hashlib.sha1(os.path.realpath(root).encode("utf-8")).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), f"{name}-{digest}.lock")


@contextlib.contextmanager
def repo_lock(root: str, name: str = "factory-push", timeout_s: float | None = None):
    """Exclusive cross-process lock scoped to `root`. Usage:

        with repo_lock(root):
            ... fetch/merge/push ...

    Raises LockBusyError if another holder keeps it beyond `timeout_s` (default
    DEFAULT_TIMEOUT_S). Always releases (flock also auto-releases if the process dies,
    so a crashed pusher can never wedge the factory)."""
    timeout_s = DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
    path = _lock_path(root, name)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockBusyError(
                        f"push lock busy: {path} held by another pusher") from None
                time.sleep(min(_POLL_S, max(timeout_s, 0.01)))
        try:
            yield path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
