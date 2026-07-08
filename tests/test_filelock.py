"""common/filelock.py — the cross-process push lock (quality review fix 3): every actor
that pushes the target repo outward (Approve click, gate-off shift-end graduate, manual
`factory graduate`) serializes on one flock'd file. flock conflicts between separate
file descriptors even within one process, so contention is testable in-process (nested
acquire) and across threads without spawning subprocesses."""
import os
import threading
import time

import pytest

from factory.common import filelock


def test_repo_lock_acquires_yields_and_releases(tmp_path):
    root = str(tmp_path)
    with filelock.repo_lock(root) as path:
        assert os.path.exists(path)
    # released → a second acquisition succeeds immediately
    with filelock.repo_lock(root, timeout_s=0.05):
        pass


def test_repo_lock_prefers_the_git_dir(tmp_path):
    (tmp_path / ".git").mkdir()
    with filelock.repo_lock(str(tmp_path)) as path:
        assert path == str(tmp_path / ".git" / "factory-push.lock")


def test_repo_lock_falls_back_to_tempdir_without_a_git_dir(tmp_path):
    """A linked worktree (.git is a FILE) or a bare/injected test root still locks — on a
    tempdir path keyed by the root's realpath, identical for every process."""
    (tmp_path / ".git").write_text("gitdir: /elsewhere")   # linked-worktree shape
    with filelock.repo_lock(str(tmp_path)) as path:
        assert path.startswith(filelock.tempfile.gettempdir())
        assert "factory-push-" in os.path.basename(path)
    # same root → same lock path (cross-process contention actually meets)
    assert filelock._lock_path(str(tmp_path), "factory-push") == path


def test_repo_lock_nested_acquire_times_out_busy(tmp_path):
    """flock is per file descriptor: a second acquire of the held lock — even in the same
    thread — must time out with LockBusyError, never deadlock or silently succeed."""
    root = str(tmp_path)
    with filelock.repo_lock(root):
        t0 = time.monotonic()
        with pytest.raises(filelock.LockBusyError):
            with filelock.repo_lock(root, timeout_s=0.05):
                pass
        assert time.monotonic() - t0 < 5                   # bounded, no hang


def test_repo_lock_contends_across_threads(tmp_path):
    """A spawned thread holding the lock blocks the main thread's acquire (the dashboard
    server is threaded — two request handlers must serialize)."""
    root = str(tmp_path)
    held = threading.Event()
    release = threading.Event()

    def holder():
        with filelock.repo_lock(root):
            held.set()
            release.wait(timeout=10)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert held.wait(timeout=10)
    try:
        with pytest.raises(filelock.LockBusyError):
            with filelock.repo_lock(root, timeout_s=0.05):
                pass
    finally:
        release.set()
        t.join(timeout=10)
    # holder released → acquirable again
    with filelock.repo_lock(root, timeout_s=1):
        pass


def test_repo_lock_release_survives_a_body_exception(tmp_path):
    root = str(tmp_path)
    with pytest.raises(RuntimeError):
        with filelock.repo_lock(root):
            raise RuntimeError("boom")
    with filelock.repo_lock(root, timeout_s=0.05):         # not wedged
        pass
