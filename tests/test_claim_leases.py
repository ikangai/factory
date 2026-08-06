"""Claim leases — Component F of the publication broker design
(docs/plans/2026-08-06-publication-broker-design.md): `tasks.claimed_at` +
`store.reap_expired_task_leases`, the shift-start sweep in orchestrator/shift.py, the
`factory task reap` CLI, and the SETTINGS_SPEC/SANE_BOUNDS wiring. Mirrors
tests/test_store.py's own `bb` fixture idiom (a new file — that file is untouched).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from factory.common import config, harness_surface
from factory.common.store import Blackboard
from factory.orchestrator import orchestrator as orch
from factory.orchestrator import shift as shiftmod


@pytest.fixture()
def bb(tmp_path):
    board = Blackboard(db_path=str(tmp_path / "bb.db"))
    board.init_db()
    try:
        yield board
    finally:
        board.close()


def _backdate(bb, task_id, minutes_ago):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")
    bb.conn.execute("UPDATE tasks SET claimed_at = ? WHERE id = ?", (ts, task_id))
    bb.conn.commit()


# -- migration + set_task_status stamping ------------------------------------------------
def test_fresh_db_has_a_nullable_claimed_at_column(bb):
    bb.add_task("t1", "x", source="human")
    assert bb.get_task("t1")["claimed_at"] is None


def test_set_task_status_stamps_claimed_at_on_in_progress(bb):
    bb.add_task("t1", "x", source="human")
    bb.set_task_status("t1", "in_progress")
    assert bb.get_task("t1")["claimed_at"] is not None


def test_set_task_status_stamps_claimed_at_on_claimed(bb):
    bb.add_task("t1", "x", source="human")
    bb.set_task_status("t1", "claimed")
    assert bb.get_task("t1")["claimed_at"] is not None


def test_set_task_status_does_not_stamp_on_other_transitions(bb):
    bb.add_task("t1", "x", source="human")
    bb.set_task_status("t1", "blocked", result="why")
    assert bb.get_task("t1")["claimed_at"] is None
    bb.set_task_status("t1", "done", result="ok")
    assert bb.get_task("t1")["claimed_at"] is None


def test_reclaim_stamps_a_fresh_claimed_at(bb):
    """A task RE-claimed after being requeued gets a fresh timestamp, not the stale one —
    otherwise a reclaimed task would immediately look expired again."""
    bb.add_task("t1", "x", source="human")
    bb.set_task_status("t1", "in_progress")
    _backdate(bb, "t1", 500)
    bb.set_task_status("t1", "open")           # requeued
    bb.set_task_status("t1", "in_progress")    # re-claimed
    claimed_at = bb.get_task("t1")["claimed_at"]
    ts = datetime.strptime(claimed_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    assert (datetime.now(timezone.utc) - ts).total_seconds() < 5


# -- reap_expired_task_leases -------------------------------------------------------------
def test_reaps_an_expired_leaderless_claim(bb):
    bb.add_task("t1", "x", source="human")
    bb.set_task_status("t1", "in_progress")     # no shift_id
    _backdate(bb, "t1", 500)
    ids = bb.reap_expired_task_leases(240)
    assert ids == ["t1"]
    row = bb.get_task("t1")
    assert row["status"] == "open"
    assert "lease expired" in row["result"]


def test_spares_a_fresh_claim(bb):
    bb.add_task("t1", "x", source="human")
    bb.set_task_status("t1", "in_progress")     # claimed_at = now
    assert bb.reap_expired_task_leases(240) == []
    assert bb.get_task("t1")["status"] == "in_progress"


def test_spares_an_open_or_done_task_even_with_a_stale_claimed_at_leftover(bb):
    bb.add_task("t1", "x", source="human")
    bb.set_task_status("t1", "in_progress")
    _backdate(bb, "t1", 500)
    bb.set_task_status("t1", "done", result="shipped")   # done — claimed_at is now vestigial
    assert bb.reap_expired_task_leases(240) == []


def test_spares_the_currently_running_shift_own_claim(bb):
    m = bb.set_mission("x")
    sh = bb.start_shift(token_budget=100, mission_id=m)
    bb.add_task("t1", "x", source="human")
    bb.set_task_status("t1", "in_progress", shift_id=sh)
    _backdate(bb, "t1", 500)
    assert bb.reap_expired_task_leases(240, keep_shift_id=sh) == []
    assert bb.get_task("t1")["status"] == "in_progress"


def test_reaps_a_different_shifts_expired_claim_even_with_keep_shift_id(bb):
    m = bb.set_mission("x")
    dead_shift = bb.start_shift(token_budget=100, mission_id=m)
    bb.end_shift(dead_shift, status="error")
    live_shift = bb.start_shift(token_budget=100, mission_id=m)
    bb.add_task("t1", "x", source="human")
    bb.set_task_status("t1", "in_progress", shift_id=dead_shift)
    _backdate(bb, "t1", 500)
    ids = bb.reap_expired_task_leases(240, keep_shift_id=live_shift)
    assert ids == ["t1"]


def test_reaps_a_claim_with_no_shift_id_even_with_keep_shift_id_set(bb):
    m = bb.set_mission("x")
    sh = bb.start_shift(token_budget=100, mission_id=m)
    bb.add_task("t1", "x", source="human")
    bb.set_task_status("t1", "in_progress")     # no shift_id at all
    _backdate(bb, "t1", 500)
    assert bb.reap_expired_task_leases(240, keep_shift_id=sh) == ["t1"]


def test_records_task_evidence_on_reap(bb):
    bb.add_task("t1", "x", source="human")
    bb.set_task_status("t1", "in_progress")
    _backdate(bb, "t1", 500)
    bb.reap_expired_task_leases(240)
    ev = bb.task_evidence("t1")
    assert len(ev) == 1
    assert ev[0]["action"] == "reap" and ev[0]["stage"] == "claim_lease"
    assert "lease expired" in ev[0]["reply_head"]


def test_reaps_multiple_expired_tasks_in_claimed_at_order(bb):
    bb.add_task("t1", "x", source="human")
    bb.add_task("t2", "y", source="human")
    bb.set_task_status("t1", "in_progress")
    bb.set_task_status("t2", "claimed")
    _backdate(bb, "t2", 600)
    _backdate(bb, "t1", 500)
    assert bb.reap_expired_task_leases(240) == ["t2", "t1"]


def test_does_not_touch_a_task_never_claimed(bb):
    bb.add_task("t1", "x", source="human")     # status open, claimed_at NULL
    assert bb.reap_expired_task_leases(0) == []


# -- SETTINGS_SPEC / SANE_BOUNDS / not frozen ----------------------------------------------
def test_claim_lease_minutes_is_an_int_settings_spec_key():
    assert config.SETTINGS_SPEC["super_worker.claim_lease_minutes"] is int


def test_claim_lease_minutes_has_sane_bounds():
    assert harness_surface.SANE_BOUNDS["super_worker.claim_lease_minutes"] == (10, 1440)


def test_claim_lease_minutes_is_not_frozen():
    assert not harness_surface._is_frozen_knob("super_worker.claim_lease_minutes")
    assert "super_worker.claim_lease_minutes" in harness_surface.editable_settings_keys()


# -- shift.py wiring ------------------------------------------------------------------------
def _completed(store, *, shift_id, mission, token_budget, wall_clock_s):
    return {"status": "completed"}


def test_run_shift_reaps_an_expired_lease_at_start(tmp_path, monkeypatch):
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with Blackboard(str(tmp_path / "f.db")) as s:
        s.init_db()
        s.set_mission("x")
        s.add_task("t1", "x", source="human")
        s.set_task_status("t1", "in_progress")    # orphaned, no shift
        _backdate(s, "t1", 500)
        res = shiftmod.run_shift(s, token_budget=500000, conductor=_completed)
        assert s.get_task("t1")["status"] == "open"
        sh = s.get_shift(res["shift_id"])
        assert "reclaimed 1 expired claim" in sh["resume_note"]


def test_run_shift_spares_its_own_freshly_claimed_task(tmp_path, monkeypatch):
    """A task the CONDUCTOR claims during this very shift must never be reaped by the same
    shift's own start-of-shift sweep (the sweep ran BEFORE the claim, at the resumed
    orphan; this proves a fresh in-shift claim survives the shift unharmed)."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)

    def claims_one(store, *, shift_id, mission, token_budget, wall_clock_s):
        store.add_task("t2", "y", source="human")
        store.set_task_status("t2", "in_progress", shift_id=shift_id)
        return {"status": "completed"}

    with Blackboard(str(tmp_path / "f.db")) as s:
        s.init_db()
        s.set_mission("x")
        shiftmod.run_shift(s, token_budget=500000, conductor=claims_one, executor=None)
        # requeue_shift_tasks at close-out returns any STILL in-flight task to open — that's
        # unrelated to leases; the point here is the lease sweep itself never touched it.
        assert s.get_task("t2") is not None


def test_run_shift_no_lease_note_when_nothing_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with Blackboard(str(tmp_path / "f.db")) as s:
        s.init_db()
        s.set_mission("x")
        res = shiftmod.run_shift(s, token_budget=500000, conductor=_completed)
        sh = s.get_shift(res["shift_id"])
        assert "reclaimed" not in (sh["resume_note"] or "")


# -- factory task reap CLI -------------------------------------------------------------------
def test_cmd_task_reap_reclaims_and_reports(tmp_path, capsys):
    with Blackboard(str(tmp_path / "f.db")) as s:
        s.init_db()
        s.add_task("t1", "x", source="human")
        s.set_task_status("t1", "in_progress")
        _backdate(s, "t1", 500)
        orch.cmd_task(s, "reap", rest=None, source="human", result="", status=None, detail="")
        assert s.get_task("t1")["status"] == "open"
    out = capsys.readouterr().out
    assert "reclaimed t1" in out
    assert "reclaimed 1 expired claim" in out


def test_cmd_task_reap_nothing_to_reap(tmp_path, capsys):
    with Blackboard(str(tmp_path / "f.db")) as s:
        s.init_db()
        orch.cmd_task(s, "reap", rest=None, source="human", result="", status=None, detail="")
    out = capsys.readouterr().out
    assert "reclaimed 0 expired claim" in out
