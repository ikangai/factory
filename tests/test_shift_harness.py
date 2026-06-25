"""The bounded-shift harness (design: 2026-06-25-conductor-loop-design.md, step 2).
Deterministic rail: reap crashed shifts → resolve the mission → start ONE shift → run the
conductor under externally-enforced ceilings → record outcome + resume note. The conductor
is INJECTED here (live = the claude conductor, step 3), so these are hermetic — no agent."""
from factory.common.store import Blackboard
from factory.orchestrator import shift as shiftmod


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


def _completed(store, *, shift_id, mission, token_budget, wall_clock_s):
    return {"status": "completed", "report": "did 2 tasks",
            "resume_note": "t9 blocked", "tokens_used": 1234}


def test_run_shift_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        s.set_mission("ship clive")
        res = shiftmod.run_shift(s, token_budget=500000, conductor=_completed)
        assert res["action"] == "completed"
        sh = s.last_shift()
        assert sh["status"] == "completed" and sh["report"] == "did 2 tasks"
        assert sh["resume_note"] == "t9 blocked" and sh["tokens_used"] == 1234
        assert sh["mission_id"] == s.active_mission()["id"]   # the shift served the active mission


def test_run_shift_reaps_crashed_shift_before_starting(tmp_path, monkeypatch):
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        m = s.set_mission("x")
        crashed = s.start_shift(token_budget=10, mission_id=m)
        s.add_task("t1", "x", source="issue")
        s.set_task_status("t1", "in_progress", shift_id=crashed)   # in-flight, then process dies
        captured = {}

        def cond(store, *, shift_id, mission, token_budget, wall_clock_s):
            captured["open_at_start"] = [t["id"] for t in store.list_tasks(status="open")]
            return {"status": "completed"}

        res = shiftmod.run_shift(s, token_budget=10, conductor=cond)
        assert res["reaped"] == 1
        assert "t1" in captured["open_at_start"]            # orphan returned to backlog BEFORE the conductor ran
        assert s.get_task("t1")["status"] == "open"
        assert s.conn.execute("SELECT status FROM shifts WHERE id=?", (crashed,)).fetchone()[0] == "error"


def test_run_shift_halted_by_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: True)
    with _store(tmp_path) as s:
        s.set_mission("x")
        ran = {"c": False}

        def cond(*a, **k):
            ran["c"] = True
            return {"status": "completed"}

        res = shiftmod.run_shift(s, token_budget=10, conductor=cond)
        assert res["action"] == "halted" and ran["c"] is False    # never spawned the conductor
        assert s.last_shift() is None                              # no shift started


def test_run_shift_no_mission(tmp_path, monkeypatch):
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        res = shiftmod.run_shift(s, token_budget=10, conductor=lambda *a, **k: {"status": "completed"})
        assert res["action"] == "no_mission" and s.last_shift() is None


def test_run_shift_sets_mission_when_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        res = shiftmod.run_shift(s, token_budget=10, mission="make clive great",
                                 conductor=lambda *a, **k: {"status": "completed"})
        assert res["action"] == "completed"
        assert s.active_mission()["statement"] == "make clive great"


def test_run_shift_contains_a_conductor_blowup(tmp_path, monkeypatch):
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        s.set_mission("x")

        def boom(*a, **k):
            raise RuntimeError("conductor blew up")

        res = shiftmod.run_shift(s, token_budget=10, conductor=boom)
        assert res["action"] == "error"
        last = s.last_shift()
        assert last["status"] == "error" and "blew up" in last["resume_note"]   # recorded, never lost


def test_run_shift_timeout_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        s.set_mission("x")

        def slow(*a, **k):
            raise TimeoutError()

        res = shiftmod.run_shift(s, token_budget=10, conductor=slow)
        assert res["action"] == "timed_out" and s.last_shift()["status"] == "timed_out"


def test_run_shift_post_completion_halt_overrides(tmp_path, monkeypatch):
    """STOP appearing DURING the shift → the post-check downgrades 'completed' to 'halted'."""
    state = {"halted": False}
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: state["halted"])
    with _store(tmp_path) as s:
        s.set_mission("x")

        def cond(*a, **k):
            state["halted"] = True            # the kill-switch trips mid-shift
            return {"status": "completed"}

        res = shiftmod.run_shift(s, token_budget=10, conductor=cond)
        assert res["action"] == "halted" and s.last_shift()["status"] == "halted"
