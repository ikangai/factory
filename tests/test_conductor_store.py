"""The conductor-loop store spine (design: docs/plans/2026-06-25-conductor-loop-design.md,
build step 1). Five tables make bounded shifts RESUMABLE and the mission the terminator:
mission, tasks (backlog), shifts, digests (research<->dev feedback), mission_status.
Hermetic — a tmp SQLite db per test.

Hardened after the step-1 adversarial review: real FK linkage, single-active-mission as a
schema guarantee, and the crash-resume reconcile path (the spine's reason to exist)."""
import sqlite3

import pytest

from factory.common.store import Blackboard


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


def _active_count(s):
    return s.conn.execute("SELECT COUNT(*) FROM mission WHERE active = 1").fetchone()[0]


# -- mission: the human's single steer ---------------------------------------
def test_mission_is_singular_and_steerable(tmp_path):
    with _store(tmp_path) as s:
        assert s.active_mission() is None
        m1 = s.set_mission("make clive a reliable autonomous CLI agent", target_repo="ikangai/clive")
        cur = s.active_mission()
        assert cur["id"] == m1 and cur["target_repo"] == "ikangai/clive"
        assert "reliable" in cur["statement"]
        s.set_mission("now focus on tool discovery")     # re-steer
        cur = s.active_mission()
        assert cur["statement"] == "now focus on tool discovery"   # newest is active…
        assert cur["id"] != m1                                     # …the old one stepped down
        assert _active_count(s) == 1                               # EXACTLY one active, not masked by LIMIT 1


def test_fk_rejects_an_orphan_shift_reference(tmp_path):
    """The conductor tables carry the same REFERENCES discipline as the rest of the schema:
    a digest/task pinned to a non-existent shift is rejected, not silently accepted."""
    with _store(tmp_path) as s:
        with pytest.raises(sqlite3.IntegrityError):
            s.add_digest(shift_id=999, shipped=["x"], summary="orphan")   # no shift 999


def test_double_activation_is_a_schema_error(tmp_path):
    """The single-active invariant is a schema guarantee, not just app code."""
    with _store(tmp_path) as s:
        s.set_mission("first")
        with pytest.raises(sqlite3.IntegrityError):
            s.conn.execute(
                "INSERT INTO mission(statement, target_repo, created_at, active) "
                "VALUES ('second','', '2026-01-01T00:00:00.000000Z', 1)")


# -- tasks: the backlog from issues / research / workers ---------------------
def test_task_backlog_lifecycle(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        s.add_task("t1", "fix dead-pane detection", source="issue", source_ref="#41")
        s.add_task("t2", "add spotify toolset", source="research")
        assert {t["id"] for t in s.list_tasks(status="open")} == {"t1", "t2"}
        s.set_task_status("t1", "in_progress", shift_id=sh)
        s.set_task_status("t1", "done", result="merged abc123", shift_id=sh)
        assert {t["id"] for t in s.list_tasks(status="open")} == {"t2"}   # done excluded from open…
        assert {t["id"] for t in s.list_tasks(status="done")} == {"t1"}   # …and present in done
        t1 = s.get_task("t1")
        assert t1["status"] == "done" and t1["result"] == "merged abc123" and t1["shift_id"] == sh
        # the mission is the terminator, not the queue: a worker can ADD work
        s.add_task("t3", "found-but-not-fixed: flaky reconnect", source="worker")
        assert len(s.list_tasks(status="open")) == 2   # t2 + t3 open; t1 done


def test_task_blocked_and_dropped_states(tmp_path):
    with _store(tmp_path) as s:
        s.add_task("b", "needs a fixture", source="issue")
        s.add_task("d", "out of mission scope", source="research")
        s.set_task_status("b", "blocked", result="missing fixture X")
        s.set_task_status("d", "dropped", result="not aligned with the mission")
        assert {t["id"] for t in s.list_tasks(status="blocked")} == {"b"}
        assert {t["id"] for t in s.list_tasks(status="dropped")} == {"d"}
        assert s.get_task("b")["result"] == "missing fixture X"


def test_set_task_status_preserves_unspecified_fields(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1)
        s.add_task("t", "x", source="issue")
        s.set_task_status("t", "in_progress", result="partial", shift_id=sh)
        s.set_task_status("t", "blocked")          # no result / shift_id given
        t = s.get_task("t")
        assert t["status"] == "blocked"            # status changed…
        assert t["result"] == "partial" and t["shift_id"] == sh   # …others preserved


# -- shifts: bounded sessions that resume ------------------------------------
def test_shift_resume(tmp_path):
    with _store(tmp_path) as s:
        m = s.set_mission("ship it")
        sh = s.start_shift(token_budget=500000, mission_id=m)
        assert s.last_shift()["status"] == "running"
        s.end_shift(sh, status="completed", report="shipped 2 fixes",
                    resume_note="t2 blocked on missing fixture", tokens_used=412000)
        last = s.last_shift()
        assert last["id"] == sh and last["status"] == "completed"
        assert last["tokens_used"] == 412000 and last["mission_id"] == m
        assert last["resume_note"] == "t2 blocked on missing fixture"   # next shift picks this up
        sh2 = s.start_shift(token_budget=500000, mission_id=m)
        assert s.last_shift()["id"] == sh2 and sh2 != sh                 # most-recent wins


@pytest.mark.parametrize("status", ["halted", "timed_out", "budget_exhausted", "error"])
def test_abnormal_shift_exits_round_trip(tmp_path, status):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=10)
        s.end_shift(sh, status=status, resume_note=f"ended {status}", tokens_used=10)
        last = s.last_shift()
        assert last["status"] == status and last["resume_note"] == f"ended {status}"


def test_crashed_shift_is_reconciled_on_resume(tmp_path):
    """The spine's whole purpose: a shift killed by a ceiling (end_shift never ran) is
    detectable + recoverable, and its in-flight work returns to the backlog."""
    with _store(tmp_path) as s:
        m = s.set_mission("ship it")
        sh = s.start_shift(token_budget=100, mission_id=m)
        s.add_task("t1", "fix X", source="issue")
        s.set_task_status("t1", "in_progress", shift_id=sh)   # claimed by the shift
        s.set_shift_tokens(sh, 90)                            # harness kept the spend current
        # --- process killed here; end_shift never runs; the row stays 'running' ---
        assert [x["id"] for x in s.running_shifts()] == [sh]
        assert [t["id"] for t in s.tasks_in_flight()] == ["t1"]

        reaped = s.reap_orphaned_shifts()                     # next startup reconciles
        assert [x["id"] for x in reaped] == [sh]
        assert s.running_shifts() == []                       # no orphan left
        assert s.get_task("t1")["status"] == "open"           # work returned to the backlog
        dead = s.last_shift()
        assert dead["id"] == sh and dead["status"] == "error"
        assert dead["resume_note"]                            # a synthetic note, not blank
        assert dead["tokens_used"] == 90                      # spend preserved for resume math


# -- digests: the research<->dev feedback loop -------------------------------
def test_current_shift_id_and_requeue_in_flight_tasks(tmp_path):
    with _store(tmp_path) as s:
        assert s.current_shift_id() is None
        sh = s.start_shift(token_budget=1)
        assert s.current_shift_id() == sh                  # the running shift, for task stamping
        s.add_task("a", "x", source="issue")
        s.set_task_status("a", "in_progress", shift_id=sh)
        s.add_task("b", "y", source="issue")               # stays open
        assert s.requeue_shift_tasks(sh) == 1              # only the in-flight one
        assert s.get_task("a")["status"] == "open"
        s.end_shift(sh, status="completed")
        assert s.current_shift_id() is None                # no running shift now


def test_prior_shift_is_the_resume_anchor(tmp_path):
    """The conductor resumes from the PRIOR shift's note, not the current (just-started) one."""
    with _store(tmp_path) as s:
        assert s.prior_shift(1) is None
        a = s.start_shift(token_budget=1)
        s.end_shift(a, status="completed", resume_note="from A")
        b = s.start_shift(token_budget=1)          # the current shift
        prior = s.prior_shift(b)
        assert prior["id"] == a and prior["resume_note"] == "from A"   # the one before, not b


def test_digest_feeds_research_then_is_consumed(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1)
        d = s.add_digest(shift_id=sh, shipped=["t1", "t2"],
                         summary="shipped dead-pane fix + spotify toolset")
        un = s.unconsumed_digests()
        assert len(un) == 1 and un[0]["id"] == d
        assert un[0]["shipped"] == ["t1", "t2"]            # round-trips as a list
        s.mark_digest_consumed(d)
        assert s.unconsumed_digests() == []                # researchers have ingested it


# -- mission_status: the advancing / steady_state / blocked / reached timeline
def test_mission_status_timeline(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1)
        assert s.latest_mission_status() is None
        s.record_mission_status(shift_id=sh, status="advancing", rationale="2 issues closed",
                                metrics={"backlog": 5, "research_dry_streak": 0})
        s.record_mission_status(shift_id=sh, status="steady_state",
                                rationale="backlog empty, research dry 3 shifts",
                                metrics={"backlog": 0, "research_dry_streak": 3})
        latest = s.latest_mission_status()
        assert latest["status"] == "steady_state"                  # most recent
        assert latest["metrics"]["research_dry_streak"] == 3       # metrics round-trip
        s.record_mission_status(shift_id=sh, status="reached", rationale="mission met")
        assert s.latest_mission_status()["status"] == "reached"    # the terminal state
