"""orchestrator/db_restore.py — safe DB restore (design: docs/plans/2026-08-08-crash-
consistency-design.md, Component D).

`scripts/backup_blackboard.sh` is the CORRECT half (sqlite3 `.backup` + `PRAGMA
integrity_check`, never a torn `cp`). The unsafe half was the only documented restore
path — a bare `cp` over the live DB, with no daemon-stop check and no handling of the
stale `-wal`/`-shm` sidecars. A backup you cannot safely restore is not a backup.

HARDENED 2026-08-08 after an adversarial review found this tool unsafe to hand an
operator — and after an agent developing it accidentally ran it against the real store
(recoverable only by luck of the move-aside). Every guard below exists because a probe
defeated its absence:

- The snapshot must LOOK LIKE a blackboard (`_SIGNATURE_TABLES`). `PRAGMA
  integrity_check` alone is no defence: SQLite reports a 0-byte file as a valid, healthy,
  EMPTY database, so a truncated download or an unrelated `chat.db` passed every gate,
  wiped the store, and reported success.
- The target is explicit. `--db` exists so the tool can be exercised anywhere; hitting
  the REAL store additionally requires typing its path (or `--i-mean-the-real-store`),
  and `--yes` CANNOT satisfy that. STOP being engaged is not consent: STOP means "the
  factory is braked", it is ambient state that can be months old, and that is precisely
  how the accident happened.
- A live writer is detected by trying to take a write lock (`BEGIN IMMEDIATE`), not by
  reading one PID file: the dashboard, a second CLI, a worker, or an open `sqlite3` shell
  are all writers that no PID file knows about.
- The moved-aside set keeps its sidecars ASSOCIATED (`<db>.bak-STAMP`,
  `<db>.bak-STAMP-wal`, `<db>.bak-STAMP-shm`) and the live DB is checkpointed first. The
  old naming (`<db>-wal.bak-STAMP`) broke the association, so opening the backup after a
  real crash silently yielded a checkpoint-old database that reported itself healthy —
  the safety net quietly losing data in exactly the scenario it exists for.
- Nothing is ever deleted, and a second run in the same second cannot clobber the first
  run's backup.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime, timezone
from typing import Callable, Optional

from ..common import killswitch, paths

# Tables a real blackboard always has. Presence of ALL of these is the identity check
# that `PRAGMA integrity_check` cannot provide (an empty file is "healthy"; an agora
# chat.db is "healthy"). Deliberately a small, stable core — not the full schema, so a
# snapshot predating a recent table still restores.
_SIGNATURE_TABLES = ("tasks", "shifts", "learnings", "budget_ledger")

# Counted and shown to the operator BEFORE anything destructive happens, for both the
# snapshot and the current db, so a wipe is visible rather than inferred afterwards.
_PREFLIGHT_COUNT_TABLES = ("tasks", "shifts", "learnings")


def _integrity_check(path: str) -> tuple[bool, str]:
    """`PRAGMA integrity_check` via Python's own sqlite3 driver (no dependency on the
    `sqlite3` CLI binary being on PATH). Returns (ok, detail) — ok iff the single result
    row reads 'ok'; detail carries the first problem line otherwise (or the exception
    text for an unreadable/non-sqlite file).

    NOTE this is a CORRUPTION check, never an identity one — see `_identity_check`."""
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


def _table_names(path: str) -> set:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return set()
    return {r[0] for r in rows}


def _identity_check(path: str) -> tuple[bool, str]:
    """Is this file actually a factory blackboard? A 0-byte file and an unrelated sqlite
    database both pass `integrity_check`; only this stops them overwriting the store."""
    names = _table_names(path)
    missing = [t for t in _SIGNATURE_TABLES if t not in names]
    if missing:
        return False, (f"not a factory blackboard — missing table(s) "
                       f"{', '.join(missing)} (found {len(names)} table(s))")
    return True, ""


def _row_counts(path: str) -> dict:
    """Best-effort per-table row counts for the preflight sheet. A table that cannot be
    read reports None rather than aborting — this is operator information, not a gate."""
    out: dict = {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return {t: None for t in _PREFLIGHT_COUNT_TABLES}
    try:
        for table in _PREFLIGHT_COUNT_TABLES:
            try:
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                out[table] = None
    finally:
        conn.close()
    return out


def _writer_is_live(db_path: str) -> tuple[bool, str]:
    """Try to take SQLite's RESERVED lock. A PID file only knows about `run --loop`; the
    dashboard server, a second `factory` CLI, a worker subprocess and an open `sqlite3`
    shell are all writers it cannot see — and a restore under any of them loses their
    in-flight writes into the moved-aside inode."""
    if not os.path.isfile(db_path):
        return False, ""
    try:
        conn = sqlite3.connect(db_path, timeout=2)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        return True, str(e)
    except sqlite3.Error as e:
        return False, str(e)      # not a lock problem; the integrity/identity gates own it
    return False, ""


def _checkpoint(db_path: str) -> None:
    """Fold the -wal back into the main file BEFORE moving anything aside, so the backup
    is complete on its own. Best-effort: a DB we cannot open is handled by the gates."""
    if not os.path.isfile(db_path):
        return
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def _free_stamp(db_path: str, stamp: str) -> str:
    """A backup name nothing already occupies. The stamp is second-resolution, so two
    runs in the same second would otherwise silently clobber the first run's backup —
    and this tool's whole promise is that it never destroys the old database."""
    candidate = stamp
    n = 1
    while any(os.path.exists(f"{db_path}{sfx}.bak-{candidate}")
              for sfx in ("", "-wal", "-shm")) or os.path.exists(
                  f"{db_path}.bak-{candidate}-wal"):
        candidate = f"{stamp}-{n}"
        n += 1
    return candidate


def _bak_path(db_path: str, stamp: str, suffix: str) -> str:
    """`<db>.bak-STAMP`, `<db>.bak-STAMP-wal`, `<db>.bak-STAMP-shm`.

    The sidecar suffix goes AFTER the stamp so SQLite still associates them: it looks for
    `<name>-wal` beside `<name>`. The original naming put the stamp last
    (`<db>-wal.bak-STAMP`), which orphaned the WAL — opening the backup then returned a
    checkpoint-old database that reported `integrity_check = ok`, i.e. silent data loss
    presented as a healthy restore."""
    return f"{db_path}.bak-{stamp}{suffix}"


def _move_aside(db_path: str, stamp: str) -> dict:
    moved: dict = {}
    for suffix in ("", "-wal", "-shm"):
        src = db_path + suffix
        if os.path.isfile(src):
            dest = _bak_path(db_path, stamp, suffix)
            shutil.move(src, dest)
            moved[suffix] = dest
    return moved


def _move_back(moved: dict, db_path: str) -> None:
    """Undo a move-aside as a SET. Restoring only the main file leaves the WAL orphaned,
    which is the same silent truncation the naming fix above exists to prevent — and the
    caller reports 'the original db was restored in place', so nothing would contradict
    it."""
    for suffix, src in moved.items():
        if os.path.exists(src):
            shutil.move(src, db_path + suffix)


def _default_confirm(prompt_text: str, expected: str) -> bool:
    print(prompt_text)
    try:
        ans = input(f"Type {expected!r} to proceed: ")
    except EOFError:
        return False
    return ans.strip() == expected


def _render_preflight(snapshot_path: str, db_path: str, snap_counts: dict,
                      live_counts: dict, is_real_store: bool) -> str:
    try:
        st = os.stat(snapshot_path)
        size = f"{st.st_size:,} bytes"
        mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%SZ")
    except OSError:
        size, mtime = "?", "?"

    def _row(label, counts):
        cells = "  ".join(f"{t}={counts.get(t) if counts.get(t) is not None else '?'}"
                          for t in _PREFLIGHT_COUNT_TABLES)
        return f"  {label:<22} {cells}"

    lines = [
        "",
        "  ── db-restore preflight ─────────────────────────────────────────",
        f"  target        {db_path}" + ("   *** THE REAL STORE ***" if is_real_store else ""),
        f"  snapshot      {snapshot_path}",
        f"  taken         {mtime}   ({size})",
        "",
        _row("snapshot contains", snap_counts),
        _row("current db has", live_counts),
        "",
        "  The current db is moved aside (never deleted), then the snapshot is copied in.",
        "  Rows present now but absent from the snapshot will be gone from the live db.",
        "  ─────────────────────────────────────────────────────────────────",
    ]
    return "\n".join(lines)


def _counts_summary(store) -> dict:
    """A cheap post-restore sanity sheet — NOT a git comparison itself (this module has
    no opinion on the target repo's layout); the runbook tells the operator what to
    diff it against."""
    tasks = store.list_tasks()
    by_status: dict = {}
    for t in tasks:
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
    ops_by_status = store.operations_count_by_status()
    return {
        "tasks_total": len(tasks), "tasks_by_status": by_status,
        "shifts_total": store.count_shifts(),
        "operations_total": sum(ops_by_status.values()),
        "operations_by_status": ops_by_status,
    }


def _default_reconcile(store):
    from . import reconcile as reconcile_mod
    # ignore_stop=True: restore's OWN precondition requires STOP to be engaged, but the
    # reconciler's own STOP check would otherwise make it a no-op here — exactly the
    # drill-4 step this function exists to run. An operator's explicit act (running
    # `factory db-restore`) is not autonomous work.
    return reconcile_mod.run_reconcile(store, ignore_stop=True)


def restore(snapshot_path: str, *, db_path: Optional[str] = None, yes: bool = False,
           allow_default_target: bool = False,
           runner_alive_fn: Optional[Callable[[], Optional[int]]] = None,
           confirm_fn: Optional[Callable[..., bool]] = None,
           now_fn: Optional[Callable[[], datetime]] = None,
           reconcile_fn: Optional[Callable] = None,
           printer: Callable[[str], None] = print) -> dict:
    """Restore `snapshot_path` over `db_path` (default `common.paths.DB_PATH`).

    Targeting the REAL store (`db_path` unset or equal to `paths.DB_PATH`) additionally
    requires `allow_default_target=True` or a confirmation in which the operator types
    the target PATH — `yes=True` does NOT satisfy it. Every other target may be confirmed
    with `yes=True`, which is what makes the tool testable and drillable at all.

    Returns `{'ok': bool, 'reason': str, ...}`. Fails CLOSED: any doubt about the
    snapshot's identity, or any sign of a live writer, refuses rather than proceeding —
    a refused restore is always recoverable, a wrong one may not be."""
    db_path = db_path or paths.DB_PATH
    is_real_store = os.path.abspath(db_path) == os.path.abspath(paths.DB_PATH)
    now = (now_fn or (lambda: datetime.now(timezone.utc)))()
    confirm = confirm_fn or _default_confirm

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

    ident_ok, ident_detail = _identity_check(snapshot_path)
    if not ident_ok:
        return {"ok": False, "reason": "snapshot-not-a-blackboard", "detail": ident_detail}

    live, lock_detail = _writer_is_live(db_path)
    if live:
        return {"ok": False, "reason": "db-busy",
                "detail": f"{lock_detail} — something still holds a write lock on "
                          f"{db_path}. Likely the dashboard/fleet server, another "
                          f"`factory` process, a worker, or an open sqlite3 shell."}

    snap_counts = _row_counts(snapshot_path)
    live_counts = _row_counts(db_path) if os.path.isfile(db_path) else {}
    printer(_render_preflight(snapshot_path, db_path, snap_counts, live_counts,
                              is_real_store))

    if is_real_store and not allow_default_target:
        # `yes` deliberately does NOT reach here: an ambient flag must not be able to
        # authorize overwriting the live store. The operator types the path or passes
        # --i-mean-the-real-store.
        if not confirm(f"This overwrites the REAL store at {db_path}.", db_path):
            return {"ok": False, "reason": "not-confirmed",
                    "detail": "targeting the real store requires typing its path (or "
                              "--i-mean-the-real-store)"}
    elif not yes:
        if not confirm(f"Restore {snapshot_path!r} over {db_path!r}?", "yes"):
            return {"ok": False, "reason": "not-confirmed"}

    _checkpoint(db_path)
    stamp = _free_stamp(db_path, now.strftime("%Y%m%dT%H%M%SZ"))
    moved = _move_aside(db_path, stamp)
    recovery = (f"cp {moved.get('', '<no previous db>')} {db_path}"
                if moved else "(there was no previous db to move aside)")

    try:
        shutil.copy2(snapshot_path, db_path)
    except OSError as e:
        _move_back(moved, db_path)
        return {"ok": False, "reason": "copy-failed", "detail": str(e),
                "moved_aside": [], "note": "the previous db (and its sidecars) were "
                                           "moved back — nothing changed"}

    result_ok, result_detail = _integrity_check(db_path)
    if not result_ok:
        failed_copy = f"{db_path}.failed-{stamp}"
        shutil.move(db_path, failed_copy)
        _move_back(moved, db_path)
        return {"ok": False, "reason": "restored-db-corrupt", "detail": result_detail,
                "failed_copy": failed_copy, "moved_aside": [],
                "note": "the previous db (and its sidecars) were moved back in place"}

    # PAST THE POINT OF NO RETURN. Anything that raises from here must still leave the
    # operator holding the recovery breadcrumb — `init_db` runs schema.sql BEFORE
    # _migrate(), so an older snapshot whose table shape a current index does not match
    # raises right here, and an unguarded traceback would bury the one line that gets
    # their data back.
    try:
        from ..common.store import Blackboard
        with Blackboard(db_path) as store:
            store.init_db()                    # re-run migrations against the restored db
            recon = (reconcile_fn or _default_reconcile)(store)
            counts = _counts_summary(store)
    except Exception as e:                     # noqa: BLE001 — breadcrumb first, then raise
        printer(f"\n  RESTORE COPIED, POST-RESTORE STEP FAILED: {e}\n"
                f"  Your previous database is NOT lost. To put it back:\n"
                f"    {recovery}\n"
                f"  (and its sidecars: {', '.join(moved.values()) or 'none'})\n")
        return {"ok": False, "reason": "post-restore-failed", "detail": str(e),
                "moved_aside": list(moved.values()), "recovery": recovery}

    return {"ok": True, "reason": "restored", "moved_aside": list(moved.values()),
            "recovery": recovery, "reconcile": recon, "counts": counts}
