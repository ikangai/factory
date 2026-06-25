"""factory run (design step 6) — composes the loop: one bounded conductor shift (the
harness + an injected conductor) → mission-assess → surface. And `factory task` so the
conductor works the backlog. Hermetic: the conductor is injected, no live agent."""
from factory.common.store import Blackboard
from factory.orchestrator import orchestrator, shift as shiftmod


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


def _completed(store, *, shift_id, mission, token_budget, wall_clock_s):
    return {"status": "completed", "report": "did 1", "resume_note": "", "tokens_used": 10}


def test_cmd_run_executes_a_shift_then_assesses(tmp_path, monkeypatch):
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        res = orchestrator.cmd_run(s, conductor=_completed, token_budget=100, wall_clock_s=5)
        assert res["action"] == "completed"
        assert s.last_shift()["status"] == "completed"
        assert s.latest_mission_status() is not None          # the shift was assessed


def test_cmd_run_needs_a_mission(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        res = orchestrator.cmd_run(s, conductor=_completed, token_budget=1, wall_clock_s=1)
        assert res["action"] == "no_mission" and s.last_shift() is None


def test_cmd_run_surfaces_steady_state_after_k_quiet_shifts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        s.set_mission("x")
        quiet = lambda *a, **k: {"status": "completed", "tokens_used": 0}   # ships nothing
        for _ in range(3):
            orchestrator.cmd_run(s, conductor=quiet, token_budget=1, wall_clock_s=1, plateau_k=3)
        out = capsys.readouterr().out
        assert "steady" in out.lower() and "mission" in out.lower()         # surfaced, not silent


def test_cmd_task_add_list_done(tmp_path, capsys):
    with _store(tmp_path) as s:
        orchestrator.cmd_task(s, "add", rest="fix the thing", source="worker")
        opened = s.list_tasks(status="open")
        assert len(opened) == 1 and opened[0]["title"] == "fix the thing"
        orchestrator.cmd_task(s, "done", rest=opened[0]["id"], result="merged abc")
        assert s.get_task(opened[0]["id"])["status"] == "done"
        assert s.get_task(opened[0]["id"])["result"] == "merged abc"
