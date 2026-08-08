"""orchestrator/db_restore.py — safe DB restore (design: docs/plans/2026-08-08-crash-
consistency-design.md, Component D).

`scripts/backup_blackboard.sh` is the CORRECT half (sqlite3 `.backup` + `PRAGMA
integrity_check`, never a torn `cp`). The unsafe half was the only documented restore
path — a bare `cp` over the live DB, with no daemon-stop check and no handling of the
stale `-wal`/`-shm` sidecars. A backup you cannot safely restore is not a backup.

`restore()` refuses unless STOP is engaged and no runner is alive, checks the SNAPSHOT's
own integrity before touching anything, moves the current db + `-wal` + `-shm` aside
TIMESTAMPED (never deletes), copies the snapshot in, checks the RESULT's integrity
(rolling back to the moved-aside original — itself never deleted either — on failure),
re-runs `init_db` (migrations), runs the crash-consistency reconciler (Component C) to
repair anything the corruption/restore window left stale, and returns a counts summary
for the operator to eyeball against `git log`.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime, timezone
from typing import Callable, Optional

from ..common import killswitch, paths


def _integrity_check(path: str) -> tuple[bool, str]:
    """`PRAGMA integrity_check` via Python's own sqlite3 driver (no dependency on the
    `sqlite3` CLI binary being on PATH). Returns (ok, detail) — ok iff the single result
    row reads 'ok'; detail carries the first problem line otherwise (or the exception
    text for an unreadable/non-sqlite file)."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return False, str(e)
    detail = (row[0] if row else "") or ""
    return detail.strip().lower() == "ok", detail


def _default_confirm(prompt_text: str) -> bool:
    print(prompt_text)
    try:
        ans = input("Type 'yes' to proceed: ")
    except EOFError:
        return False
    return ans.strip().lower() == "yes"


def _counts_summary(store) -> dict:
    """A cheap post-restore sanity sheet — NOT a git comparison itself (this module has
    no opinion on the target repo's layout); the runbook tells the operator what to
    diff it against (`git log --oneline factory/auto | wc -l`, `factory task list`)."""
    tasks = store.list_tasks()
    by_status: dict = {}
    for t in tasks:
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
    ops = store.operations(limit=1_000_000)
    ops_by_status: dict = {}
    for o in ops:
        ops_by_status[o["status"]] = ops_by_status.get(o["status"], 0) + 1
    return {
        "tasks_total": len(tasks), "tasks_by_status": by_status,
        "shifts_total": store.count_shifts(),
        "operations_total": len(ops), "operations_by_status": ops_by_status,
    }


def _default_reconcile(store):
    from . import reconcile as reconcile_mod
    # ignore_stop=True: restore's OWN precondition requires STOP to be engaged, but the
    # reconciler's own STOP check (Component C, binding rule 4) would otherwise make it
    # a no-op here — exactly the drill-4 step this function exists to run. This mirrors
    # reporting.approvals.execute_approval's documented STOP-bypass reasoning: an
    # operator's explicit act (running `factory db-restore`) is not autonomous work.
    return reconcile_mod.run_reconcile(store, ignore_stop=True)


def restore(snapshot_path: str, *, db_path: Optional[str] = None, yes: bool = False,
           runner_alive_fn: Optional[Callable[[], Optional[int]]] = None,
           confirm_fn: Optional[Callable[[str], bool]] = None,
           now_fn: Optional[Callable[[], datetime]] = None,
           reconcile_fn: Optional[Callable] = None) -> dict:
    """Restore `snapshot_path` over `db_path` (default `common.paths.DB_PATH`).

    `runner_alive_fn` defaults to `orchestrator.autopilot.runner_alive` (imported
    lazily — autopilot pulls in the dashboard/mode stack that a restore CLI shouldn't
    need at import time); `confirm_fn` defaults to an interactive y/N prompt, skipped
    entirely when `yes=True`. Both are injectable so tests never touch a real PID file
    or block on stdin. Returns `{'ok': bool, 'reason': str, 'detail'?, 'moved_aside'?,
    'reconcile'?, 'counts'?}`."""
    db_path = db_path or paths.DB_PATH
    now = (now_fn or (lambda: datetime.now(timezone.utc)))()
    stamp = now.strftime("%Y%m%dT%H%M%SZ")

    if not killswitch.is_halted():
        return {"ok": False, "reason": "stop-not-engaged",
                "detail": "engage STOP first (`touch STOP`, or the dashboard brake) — "
                          "db-restore refuses to run against a live factory"}

    if runner_alive_fn is None:
        from . import autopilot
        runner_alive_fn = autopilot.runner_alive
    pid = runner_alive_fn()
    if pid is not None:
        return {"ok": False, "reason": "runner-alive",
                "detail": f"a runner is still alive (pid {pid}) — stop it first"}

    if not os.path.isfile(snapshot_path):
        return {"ok": False, "reason": "snapshot-missing", "detail": snapshot_path}

    snap_ok, snap_detail = _integrity_check(snapshot_path)
    if not snap_ok:
        return {"ok": False, "reason": "snapshot-corrupt", "detail": snap_detail}

    if not yes:
        confirm = confirm_fn or _default_confirm
        if not confirm(f"Restore {snapshot_path!r} over {db_path!r}? This moves the "
                       f"current db aside (never deletes it)."):
            return {"ok": False, "reason": "not-confirmed"}

    # Move the current db + sidecars aside, TIMESTAMPED — never delete.
    moved: dict = {}
    for suffix in ("", "-wal", "-shm"):
        src = db_path + suffix
        if os.path.isfile(src):
            dest = f"{src}.bak-{stamp}"
            shutil.move(src, dest)
            moved[suffix] = dest

    try:
        shutil.copy2(snapshot_path, db_path)
    except OSError as e:
        if "" in moved:                      # best-effort undo: never leave NO db at all
            shutil.move(moved[""], db_path)
        return {"ok": False, "reason": "copy-failed", "detail": str(e),
                "moved_aside": list(moved.values())}

    result_ok, result_detail = _integrity_check(db_path)
    if not result_ok:
        # Roll back — never delete: the bad copy is moved aside too, not removed.
        failed_copy = f"{db_path}.failed-{stamp}"
        shutil.move(db_path, failed_copy)
        if "" in moved:
            shutil.copy2(moved[""], db_path)   # copy (not move) — the .bak stays as history
        return {"ok": False, "reason": "restored-db-corrupt", "detail": result_detail,
                "moved_aside": list(moved.values()), "failed_copy": failed_copy,
                "note": "the original db was restored in place"}

    from ..common.store import Blackboard
    with Blackboard(db_path) as store:
        store.init_db()                        # re-run migrations against the restored db
        recon = (reconcile_fn or _default_reconcile)(store)
        counts = _counts_summary(store)

    return {"ok": True, "reason": "restored", "moved_aside": list(moved.values()),
            "reconcile": recon, "counts": counts}
