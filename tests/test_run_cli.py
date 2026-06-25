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


# --------------------------------------------------------------------------- #
# COMPOSITION — the review's key gap: prove the WHOLE lifecycle, not isolated units #
# --------------------------------------------------------------------------- #
def test_full_lifecycle_drains_backlog_counts_shipped_and_emits_digest(tmp_path, monkeypatch):
    """The bug the review caught: nothing closed tasks or counted shipments, so the loop
    could never drain or terminate. This pins the contract that fixes it."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        s.add_task("t1", "fix a thing", source="research")

        def conductor(store, *, shift_id, mission, token_budget, wall_clock_s):
            # what the real conductor does via `./bin/factory task ...` (here in-process):
            orchestrator.cmd_task(store, "claim", rest="t1")
            orchestrator.cmd_task(store, "done", rest="t1", result="abc123")
            return {"status": "completed", "tokens_used": 5}

        res = orchestrator.cmd_run(s, conductor=conductor, token_budget=100, wall_clock_s=5)

        t1 = s.get_task("t1")
        assert t1["status"] == "done" and t1["shift_id"] == res["shift_id"]   # stamped to this shift
        assert s.list_tasks(status="open") == []                              # backlog DRAINED
        latest = s.latest_mission_status()
        assert latest["status"] == "advancing" and latest["metrics"]["shipped"] == 1  # shipped COUNTED
        digs = s.unconsumed_digests()
        assert len(digs) == 1 and digs[0]["shipped"] == ["t1"]                # research<->dev loop FUELED


def test_cmd_run_idles_after_k_steady_shifts_instead_of_respawning(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        s.set_mission("x")
        quiet = lambda *a, **k: {"status": "completed", "tokens_used": 0}    # ships nothing
        for _ in range(3):
            orchestrator.cmd_run(s, conductor=quiet, token_budget=1, wall_clock_s=1, plateau_k=3)
        capsys.readouterr()
        spawned = {"n": 0}

        def counting(*a, **k):
            spawned["n"] += 1
            return {"status": "completed"}

        res = orchestrator.cmd_run(s, conductor=counting, token_budget=1, wall_clock_s=1, plateau_k=3)
        assert res["action"] == "idle" and spawned["n"] == 0                 # did NOT spawn a conductor
        assert "idle" in capsys.readouterr().out.lower()


def test_cmd_task_claim_stamps_the_running_shift_and_block_works(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1)
        orchestrator.cmd_task(s, "add", rest="thing", source="issue")
        tid = s.list_tasks(status="open")[0]["id"]
        orchestrator.cmd_task(s, "claim", rest=tid)
        assert s.get_task(tid)["status"] == "in_progress" and s.get_task(tid)["shift_id"] == sh
        orchestrator.cmd_task(s, "block", rest=tid, result="needs fixture")
        assert s.get_task(tid)["status"] == "blocked" and s.get_task(tid)["result"] == "needs fixture"


def test_cmd_run_live_conductor_closure_runs_and_parses(tmp_path, monkeypatch):
    """Exercise the conductor=None branch (the live closure + run_conductor import) the
    review flagged as untested — with the transport stubbed, no agent spawned."""
    from factory.roles import common as rcommon
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    monkeypatch.setattr(rcommon, "claude_super",
                        lambda prompt, **k: ('```json\n{"status":"completed","report":"r",'
                                             '"resume_note":"n"}\n```', 7, 0.0))
    with _store(tmp_path) as s:
        s.set_mission("x")
        res = orchestrator.cmd_run(s, token_budget=1, wall_clock_s=1)   # conductor=None → live path
        assert res["action"] == "completed" and s.last_shift()["report"] == "r"
