"""orchestrator/db_restore.py — safe DB restore (Component D, design: docs/plans/
2026-08-08-crash-consistency-design.md). Hermetic: every test operates on tmp-dir
sqlite files ONLY — never store/blackboard.db, never a real runner/PID file.
"""
from __future__ import annotations

import os

import pytest

from factory.common.store import Blackboard
from factory.orchestrator import db_restore


def _valid_db(path: str, *, seed: str = "") -> str:
    """A real, schema-initialized, integrity-clean sqlite db at `path`."""
    with Blackboard(path) as s:
        s.init_db()
        if seed:
            s.set_mission(seed)
    return path


def _corrupt_file(path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"not a sqlite file at all")
    return path


def _always_confirm(_msg: str, _expected: str = "yes") -> bool:
    # confirm_fn now receives the token the operator must type: 'yes' for an explicit
    # --db target, the target PATH itself when the REAL store is in the crosshairs.
    return True


def _no_runner() -> None:
    return None


def _kwargs(tmp_path, *, halted=True, snapshot=None, db_path=None, yes=True,
           runner_alive_fn=_no_runner, confirm_fn=_always_confirm, reconcile_fn=None):
    return dict(
        snapshot_path=snapshot or str(tmp_path / "snap.db"),
        db_path=db_path or str(tmp_path / "live.db"),
        yes=yes, runner_alive_fn=runner_alive_fn, confirm_fn=confirm_fn,
        reconcile_fn=reconcile_fn or (lambda store: {"action": "reconciled", "examined": 0,
                                                      "resolved": [], "unknown": []}),
    )


@pytest.fixture(autouse=True)
def _stop_engaged(monkeypatch):
    """Every test defaults STOP to engaged (the happy-path precondition); tests of the
    refusal itself override this explicitly."""
    monkeypatch.setattr(db_restore.killswitch, "is_halted", lambda: True)


# -- refusal gates -----------------------------------------------------------

def test_refuses_when_stop_not_engaged(tmp_path, monkeypatch):
    monkeypatch.setattr(db_restore.killswitch, "is_halted", lambda: False)
    _valid_db(str(tmp_path / "snap.db"))
    live = _valid_db(str(tmp_path / "live.db"), seed="original")

    res = db_restore.restore(**_kwargs(tmp_path))
    assert res["ok"] is False
    assert res["reason"] == "stop-not-engaged"
    # nothing touched
    assert Blackboard(live).active_mission()["statement"] == "original"


def test_refuses_when_a_runner_is_alive(tmp_path):
    _valid_db(str(tmp_path / "snap.db"))
    live = _valid_db(str(tmp_path / "live.db"), seed="original")

    res = db_restore.restore(**_kwargs(tmp_path, runner_alive_fn=lambda: 4242))
    assert res["ok"] is False and res["reason"] == "runner-alive"
    assert "4242" in res["detail"]
    assert Blackboard(live).active_mission()["statement"] == "original"


def test_refuses_when_snapshot_is_missing(tmp_path):
    _valid_db(str(tmp_path / "live.db"), seed="original")
    res = db_restore.restore(**_kwargs(tmp_path, snapshot=str(tmp_path / "nope.db")))
    assert res["ok"] is False and res["reason"] == "snapshot-missing"


def test_refuses_when_snapshot_is_corrupt_and_touches_nothing(tmp_path):
    _corrupt_file(str(tmp_path / "snap.db"))
    live_path = str(tmp_path / "live.db")
    _valid_db(live_path, seed="original")

    res = db_restore.restore(**_kwargs(tmp_path))
    assert res["ok"] is False and res["reason"] == "snapshot-corrupt"
    # "before touching anything": the live db is completely untouched, and nothing was
    # ever moved aside (no .bak-* sibling created).
    assert Blackboard(live_path).active_mission()["statement"] == "original"
    assert not any(".bak-" in f for f in os.listdir(tmp_path))


def test_refuses_when_not_confirmed_and_touches_nothing(tmp_path):
    _valid_db(str(tmp_path / "snap.db"))
    live_path = str(tmp_path / "live.db")
    _valid_db(live_path, seed="original")

    res = db_restore.restore(**_kwargs(tmp_path, yes=False, confirm_fn=lambda _m, _e="yes": False))
    assert res["ok"] is False and res["reason"] == "not-confirmed"
    assert Blackboard(live_path).active_mission()["statement"] == "original"


def test_yes_skips_the_confirm_prompt(tmp_path):
    _valid_db(str(tmp_path / "snap.db"), seed="from-snapshot")
    _valid_db(str(tmp_path / "live.db"), seed="original")

    def _boom(_msg, _expected="yes"):
        raise AssertionError("confirm_fn must not be called when yes=True")

    res = db_restore.restore(**_kwargs(tmp_path, yes=True, confirm_fn=_boom))
    assert res["ok"] is True


# -- happy path ----------------------------------------------------------------

def test_happy_path_moves_aside_never_deletes_and_restores(tmp_path):
    snap = _valid_db(str(tmp_path / "snap.db"), seed="from-snapshot")
    live_path = str(tmp_path / "live.db")
    _valid_db(live_path, seed="original")
    # sidecar files a real WAL-mode db can leave behind
    open(live_path + "-wal", "w").close()
    open(live_path + "-shm", "w").close()

    res = db_restore.restore(**_kwargs(tmp_path))
    assert res["ok"] is True and res["reason"] == "restored"

    # the restored db really has the snapshot's content
    assert Blackboard(live_path).active_mission()["statement"] == "from-snapshot"

    # every moved-aside file still exists (NEVER deleted) and still has the ORIGINAL content
    moved = res["moved_aside"]
    # The live db is CHECKPOINTED before anything moves (PRAGMA wal_checkpoint(TRUNCATE)),
    # so its -wal/-shm are folded into the main file and there is normally just one file
    # to move. That is the D3 fix: a backup that is complete on its own, instead of a
    # main file whose sidecars were renamed out of association with it.
    assert len(moved) >= 1
    db_bak = [p for p in moved if p.endswith(".db.bak-" + p.rsplit("bak-", 1)[1])][0]
    assert os.path.isfile(db_bak)
    assert Blackboard(db_bak).active_mission()["statement"] == "original"
    for p in moved:
        assert os.path.isfile(p)


def test_happy_path_reinits_schema_on_the_restored_db(tmp_path):
    """A snapshot taken with an OLDER schema still gains any new table via init_db's
    migration re-run — same guarantee `factory init`/every CLI entry already gives.

    The fixture is a REAL blackboard with a newer table dropped, not a stub carrying one
    table: a snapshot that lacks the signature tables is now (correctly) refused as "not a
    factory blackboard", and a one-table stub was never a realistic old snapshot anyway."""
    import sqlite3
    snap_path = _valid_db(str(tmp_path / "snap.db"), seed="from-snapshot")
    conn = sqlite3.connect(snap_path)
    conn.execute("DROP TABLE operations")          # as if taken before Phase 2 existed
    conn.commit()
    conn.close()
    live_path = _valid_db(str(tmp_path / "live.db"))

    res = db_restore.restore(**_kwargs(tmp_path))
    assert res["ok"] is True
    with Blackboard(live_path) as store:
        assert store.operations() == []            # table re-created by init_db, queryable


def test_happy_path_runs_the_reconciler_with_stop_bypassed(tmp_path):
    """The default reconcile_fn must bypass the reconciler's own STOP gate — restore's
    OWN precondition requires STOP engaged, which would otherwise make the reconciler a
    permanent no-op here."""
    _valid_db(str(tmp_path / "snap.db"))
    live_path = str(tmp_path / "live.db")
    with Blackboard(live_path) as s:
        s.init_db()
        s.record_issue_sync(40, "sha1", "comment")   # a ledger row the reconciler can match

    # Seed an operations row INSIDE the snapshot (not live) so it survives the restore.
    snap_path = str(tmp_path / "snap.db")
    with Blackboard(snap_path) as s:
        s.record_issue_sync(40, "sha1", "comment")
        s.begin_operation("issue_sync", "issue:40:sha1", payload={"issue_number": 40, "sha": "sha1"})

    kwargs = _kwargs(tmp_path)
    kwargs.pop("reconcile_fn")   # use the REAL default reconcile_fn (ignore_stop=True)
    res = db_restore.restore(**kwargs)
    assert res["ok"] is True
    recon = res["reconcile"]
    assert recon["action"] == "reconciled"   # NOT 'halted', even though STOP is engaged
    assert len(recon["resolved"]) == 1
    assert recon["resolved"][0]["status"] == "reconciled"


def test_happy_path_reports_a_counts_summary(tmp_path):
    _valid_db(str(tmp_path / "snap.db"))
    snap_path = str(tmp_path / "snap.db")
    with Blackboard(snap_path) as s:
        s.add_task("t1", "x", source="human")
        s.set_task_status("t1", "done", result="sha1")
        s.add_task("t2", "y", source="human")
    _valid_db(str(tmp_path / "live.db"))

    res = db_restore.restore(**_kwargs(tmp_path))
    counts = res["counts"]
    assert counts["tasks_total"] == 2
    assert counts["tasks_by_status"]["done"] == 1
    assert counts["tasks_by_status"]["open"] == 1
    assert counts["shifts_total"] == 0


# -- rollback on a torn/corrupt post-copy result --------------------------------

def test_rolls_back_when_the_restored_copy_fails_integrity_check(tmp_path, monkeypatch):
    _valid_db(str(tmp_path / "snap.db"), seed="from-snapshot")
    live_path = str(tmp_path / "live.db")
    _valid_db(live_path, seed="original")

    real_check = db_restore._integrity_check

    def _fake_check(path):
        if path == live_path:      # only the POST-COPY check on the live path fails
            return False, "simulated torn copy"
        return real_check(path)
    monkeypatch.setattr(db_restore, "_integrity_check", _fake_check)

    res = db_restore.restore(**_kwargs(tmp_path))
    assert res["ok"] is False and res["reason"] == "restored-db-corrupt"

    # the ORIGINAL is back in place (rolled back)
    assert Blackboard(live_path).active_mission()["statement"] == "original"
    # the failed copy was moved aside too — never deleted
    assert os.path.isfile(res["failed_copy"])
    # the original's own .bak copy (made before the swap) is ALSO still there
    # `moved_aside` is empty on this path by design: the originals were moved BACK,
    # so listing them as "aside" would name paths that no longer exist (review D11).
    assert res["moved_aside"] == []


def test_copy_failure_restores_the_original_in_place(tmp_path, monkeypatch):
    _valid_db(str(tmp_path / "snap.db"))
    live_path = str(tmp_path / "live.db")
    _valid_db(live_path, seed="original")

    def _boom_copy(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(db_restore.shutil, "copy2", _boom_copy)

    res = db_restore.restore(**_kwargs(tmp_path))
    assert res["ok"] is False and res["reason"] == "copy-failed"
    assert Blackboard(live_path).active_mission()["statement"] == "original"


# -- integrity check helper ------------------------------------------------------

def test_integrity_check_ok_on_a_real_db(tmp_path):
    path = _valid_db(str(tmp_path / "ok.db"))
    ok, detail = db_restore._integrity_check(path)
    assert ok is True and detail.lower() == "ok"


def test_integrity_check_fails_on_a_non_sqlite_file(tmp_path):
    path = _corrupt_file(str(tmp_path / "bad.db"))
    ok, detail = db_restore._integrity_check(path)
    assert ok is False and detail


# ==========================================================================================
# Hardening regressions (Phase 2 review D1-D9). Every one of these FAILED before its fix;
# each was reproduced by probe first. Tmp dirs only — never the real store.
# ==========================================================================================
def _blackboard_snapshot(path, *, tasks=3):
    """A file that really is a blackboard (signature tables present, rows in them)."""
    from factory.common.store import Blackboard
    store = Blackboard(db_path=str(path))
    store.init_db()
    for i in range(tasks):
        store.add_task(f"task-snap{i}", "seeded", source="worker")
    store.close()
    return str(path)


def test_a_zero_byte_file_is_refused_as_a_snapshot(tmp_path, monkeypatch):
    """D2 — SQLite reports a 0-byte file as a valid, healthy, EMPTY database, so
    `integrity_check` alone let a truncated download wipe the store and report success."""
    monkeypatch.setattr(db_restore.killswitch, "is_halted", lambda: True)
    live = _blackboard_snapshot(tmp_path / "live.db", tasks=7)
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")

    res = db_restore.restore(str(empty), db_path=live, yes=True,
                             runner_alive_fn=lambda: None, printer=lambda *_: None)
    assert res["reason"] == "snapshot-not-a-blackboard"
    assert res["ok"] is False


def test_an_unrelated_sqlite_database_is_refused_as_a_snapshot(tmp_path, monkeypatch):
    """D2 — an agora chat.db passed every gate, wiped the store, and left its own tables
    sitting alongside the factory's after init_db bolted the schema on."""
    import sqlite3
    monkeypatch.setattr(db_restore.killswitch, "is_halted", lambda: True)
    live = _blackboard_snapshot(tmp_path / "live.db")
    foreign = tmp_path / "chat.db"
    conn = sqlite3.connect(str(foreign))
    conn.execute("CREATE TABLE messages(id INTEGER)")
    conn.commit()
    conn.close()

    res = db_restore.restore(str(foreign), db_path=live, yes=True,
                             runner_alive_fn=lambda: None, printer=lambda *_: None)
    assert res["reason"] == "snapshot-not-a-blackboard"


def test_the_moved_aside_backup_keeps_its_wal_and_loses_no_rows(tmp_path, monkeypatch):
    """D3 — the deep one. A crashed factory leaves an uncheckpointed -wal holding most of
    the recent writes. The old naming (`<db>-wal.bak-STAMP`) broke SQLite's association
    with the backup, so opening it returned a checkpoint-old database that reported
    `integrity_check = ok` — the safety net silently losing data in exactly the scenario
    it exists for."""
    import sqlite3
    monkeypatch.setattr(db_restore.killswitch, "is_halted", lambda: True)
    live = str(tmp_path / "crash.db")
    conn = sqlite3.connect(live)
    conn.execute("PRAGMA journal_mode=WAL")
    for t in ("tasks(id TEXT)", "shifts(id INT)", "learnings(id INT)", "budget_ledger(id INT)"):
        conn.execute(f"CREATE TABLE {t}")
    conn.commit()
    for i in range(7):
        conn.execute("INSERT INTO tasks VALUES(?)", (f"t{i}",))
    conn.commit()                       # committed, but deliberately NOT checkpointed/closed
    assert os.path.getsize(live + "-wal") > 0

    snap = _blackboard_snapshot(tmp_path / "snap.db")
    res = db_restore.restore(snap, db_path=live, yes=True,
                             runner_alive_fn=lambda: None, printer=lambda *_: None)
    assert res["ok"] is True

    main_bak = [p for p in res["moved_aside"]
                if not p.endswith("-wal") and not p.endswith("-shm")][0]
    got = sqlite3.connect(f"file:{main_bak}?mode=ro", uri=True).execute(
        "SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert got == 7, "the backup silently lost the uncheckpointed WAL"


def test_a_live_writer_blocks_the_restore(tmp_path, monkeypatch):
    """D6 — a PID file only knows about `run --loop`. The dashboard, a second CLI, a
    worker, or an open sqlite3 shell are writers it cannot see, and restoring under one
    drops their in-flight writes into the moved-aside inode."""
    import sqlite3
    monkeypatch.setattr(db_restore.killswitch, "is_halted", lambda: True)
    live = _blackboard_snapshot(tmp_path / "live.db")
    snap = _blackboard_snapshot(tmp_path / "snap.db")

    holder = sqlite3.connect(live, timeout=1)
    holder.execute("BEGIN IMMEDIATE")
    try:
        res = db_restore.restore(snap, db_path=live, yes=True,
                                 runner_alive_fn=lambda: None, printer=lambda *_: None)
    finally:
        holder.rollback()
        holder.close()
    assert res["reason"] == "db-busy"


def test_two_runs_in_the_same_second_do_not_clobber_the_first_backup(tmp_path, monkeypatch):
    """D5 — the stamp is second-resolution and shutil.move overwrites, so a second run
    destroyed the first run's backup: the one promise this tool makes."""
    from datetime import datetime, timezone
    monkeypatch.setattr(db_restore.killswitch, "is_halted", lambda: True)
    live = _blackboard_snapshot(tmp_path / "live.db", tasks=7)
    snap = _blackboard_snapshot(tmp_path / "snap.db", tasks=1)
    frozen = lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)   # noqa: E731

    first = db_restore.restore(snap, db_path=live, yes=True, now_fn=frozen,
                               runner_alive_fn=lambda: None, printer=lambda *_: None)
    second = db_restore.restore(snap, db_path=live, yes=True, now_fn=frozen,
                                runner_alive_fn=lambda: None, printer=lambda *_: None)

    b1 = [p for p in first["moved_aside"] if not p.endswith(("-wal", "-shm"))][0]
    b2 = [p for p in second["moved_aside"] if not p.endswith(("-wal", "-shm"))][0]
    assert b1 != b2 and os.path.exists(b1), "the first run's backup was clobbered"


def test_yes_alone_cannot_overwrite_the_real_store(tmp_path, monkeypatch):
    """D1/D8 — the accident. STOP being engaged is not consent: it is ambient state that
    can be months old. Overwriting the real store requires typing its path (or the
    explicit flag); `--yes` must not reach that decision."""
    from factory.common import paths
    monkeypatch.setattr(db_restore.killswitch, "is_halted", lambda: True)
    snap = _blackboard_snapshot(tmp_path / "snap.db")

    res = db_restore.restore(snap, db_path=paths.DB_PATH, yes=True,
                             runner_alive_fn=lambda: None,
                             confirm_fn=lambda *a: False,      # operator declines / no tty
                             printer=lambda *_: None)
    assert res["reason"] == "not-confirmed"
    assert "typing its path" in res["detail"]


def test_the_preflight_is_printed_even_with_yes(tmp_path, monkeypatch):
    """D8 — with `--yes` (what the runbooks instruct) the tool printed NOTHING before
    overwriting a database. The operator must always see what is about to happen."""
    monkeypatch.setattr(db_restore.killswitch, "is_halted", lambda: True)
    live = _blackboard_snapshot(tmp_path / "live.db", tasks=7)
    snap = _blackboard_snapshot(tmp_path / "snap.db", tasks=1)
    out = []

    db_restore.restore(snap, db_path=live, yes=True, runner_alive_fn=lambda: None,
                       printer=out.append)

    text = "\n".join(out)
    assert "db-restore preflight" in text
    assert "tasks=7" in text and "tasks=1" in text, "row counts must be visible BEFORE acting"


def test_a_post_restore_failure_still_prints_the_recovery_breadcrumb(tmp_path, monkeypatch):
    """D7 — the post-copy phase was unguarded, so an exception (init_db running schema.sql
    before _migrate on an older snapshot is a REAL instance) escaped as a raw traceback
    after the point of no return, without ever printing the path back to the data."""
    monkeypatch.setattr(db_restore.killswitch, "is_halted", lambda: True)
    live = _blackboard_snapshot(tmp_path / "live.db", tasks=7)
    snap = _blackboard_snapshot(tmp_path / "snap.db")

    def boom(_store):
        raise RuntimeError("migration exploded")

    out = []
    res = db_restore.restore(snap, db_path=live, yes=True, runner_alive_fn=lambda: None,
                             reconcile_fn=boom, printer=out.append)

    assert res["reason"] == "post-restore-failed"
    assert res["recovery"] and res["moved_aside"]
    assert "NOT lost" in "\n".join(out)
