"""The conductor-loop store spine (design: docs/plans/2026-06-25-conductor-loop-design.md,
build step 1). Five tables make bounded shifts resumable and the mission the terminator:
mission, tasks (backlog), shifts, digests (research<->dev feedback), mission_status.
Hermetic — a tmp SQLite db per test."""
from factory.common.store import Blackboard


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


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


# -- tasks: the backlog from issues / research / workers ---------------------
def test_task_backlog_lifecycle(tmp_path):
    with _store(tmp_path) as s:
        s.add_task("t1", "fix dead-pane detection", source="issue", source_ref="#41")
        s.add_task("t2", "add spotify toolset", source="research")
        assert {t["id"] for t in s.list_tasks(status="open")} == {"t1", "t2"}
        s.set_task_status("t1", "in_progress", shift_id=7)
        s.set_task_status("t1", "done", result="merged abc123", shift_id=7)
        assert s.list_tasks(status="open") == [t for t in s.list_tasks(status="open") if t["id"] == "t2"]
        t1 = s.get_task("t1")
        assert t1["status"] == "done" and t1["result"] == "merged abc123" and t1["shift_id"] == 7
        # the mission is the terminator, not the queue: a worker can ADD work
        s.add_task("t3", "found-but-not-fixed: flaky reconnect", source="worker")
        assert len(s.list_tasks(status="open")) == 2   # t2 + t3 open; t1 done


# -- shifts: bounded sessions that resume ------------------------------------
def test_shift_resume(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=500000)
        assert s.last_shift()["status"] == "running"
        s.end_shift(sh, status="completed", report="shipped 2 fixes",
                    resume_note="t2 blocked on missing fixture", tokens_used=412000)
        last = s.last_shift()
        assert last["id"] == sh and last["status"] == "completed"
        assert last["tokens_used"] == 412000
        assert last["resume_note"] == "t2 blocked on missing fixture"   # next shift picks this up
        sh2 = s.start_shift(token_budget=500000)
        assert s.last_shift()["id"] == sh2 and sh2 != sh                 # most-recent wins


# -- digests: the research<->dev feedback loop -------------------------------
def test_digest_feeds_research_then_is_consumed(tmp_path):
    with _store(tmp_path) as s:
        d = s.add_digest(shift_id=1, shipped=["t1", "t2"],
                         summary="shipped dead-pane fix + spotify toolset")
        un = s.unconsumed_digests()
        assert len(un) == 1 and un[0]["id"] == d
        assert un[0]["shipped"] == ["t1", "t2"]            # round-trips as a list
        s.mark_digest_consumed(d)
        assert s.unconsumed_digests() == []                # researchers have ingested it


# -- mission_status: the advancing / steady_state / blocked timeline ----------
def test_mission_status_timeline(tmp_path):
    with _store(tmp_path) as s:
        assert s.latest_mission_status() is None
        s.record_mission_status(shift_id=1, status="advancing", rationale="2 issues closed",
                                metrics={"backlog": 5, "research_dry_streak": 0})
        s.record_mission_status(shift_id=2, status="steady_state",
                                rationale="backlog empty, research dry 3 shifts",
                                metrics={"backlog": 0, "research_dry_streak": 3})
        latest = s.latest_mission_status()
        assert latest["status"] == "steady_state"                  # most recent
        assert latest["metrics"]["research_dry_streak"] == 3       # metrics round-trip
