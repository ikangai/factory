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


def _always_confirm(_msg: str) -> bool:
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

    res = db_restore.restore(**_kwargs(tmp_path, yes=False, confirm_fn=lambda _m: False))
    assert res["ok"] is False and res["reason"] == "not-confirmed"
    assert Blackboard(live_path).active_mission()["statement"] == "original"


def test_yes_skips_the_confirm_prompt(tmp_path):
    _valid_db(str(tmp_path / "snap.db"), seed="from-snapshot")
    _valid_db(str(tmp_path / "live.db"), seed="original")

    def _boom(_msg):
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
    assert len(moved) == 3   # db + -wal + -shm
    db_bak = [p for p in moved if p.endswith(".db.bak-" + p.rsplit("bak-", 1)[1])][0]
    assert os.path.isfile(db_bak)
    assert Blackboard(db_bak).active_mission()["statement"] == "original"
    for p in moved:
        assert os.path.isfile(p)


def test_happy_path_reinits_schema_on_the_restored_db(tmp_path):
    """A snapshot taken with an OLDER schema still gains any new table via init_db's
    migration re-run — same guarantee `factory init`/every CLI entry already gives."""
    snap_path = str(tmp_path / "snap.db")
    import sqlite3
    conn = sqlite3.connect(snap_path)
    conn.execute("CREATE TABLE mission (id INTEGER PRIMARY KEY, statement TEXT NOT NULL, "
                "target_repo TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, "
                "active INTEGER NOT NULL DEFAULT 1)")
    conn.commit()
    conn.close()
    _valid_db(str(tmp_path / "live.db"))

    res = db_restore.restore(**_kwargs(tmp_path))
    assert res["ok"] is True
    with Blackboard(str(tmp_path / "live.db")) as s:
        cols = {r[1] for r in s.conn.execute("PRAGMA table_info(operations)").fetchall()}
        assert "idem_key" in cols   # the NEW crash-consistency table exists post-restore


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
    assert any(os.path.isfile(p) for p in res["moved_aside"])


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
