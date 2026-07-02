"""factory run (design step 6) — composes the loop: one bounded conductor shift (the
harness + an injected conductor) → mission-assess → surface. And `factory task` so the
conductor works the backlog. Hermetic: the conductor is injected, no live agent."""
import pytest

from factory.common.store import Blackboard
from factory.orchestrator import orchestrator, shift as shiftmod
from factory.roles import research_feed


@pytest.fixture(autouse=True)
def _no_real_research(monkeypatch):
    """Keep these cmd_run tests hermetic: stub the researcher (no live claude -p spawn) and
    the MISSION.md sync (FACTORY_ROOT is a real path, so cmd_run would otherwise read the
    operator's live MISSION.md). Tests exercising the sync re-monkeypatch _read_mission_md."""
    monkeypatch.setattr(research_feed, "propose_directions", lambda store, **k: [])
    monkeypatch.setattr(orchestrator, "_read_mission_md", lambda: None)
    monkeypatch.setattr(orchestrator, "_write_mission_md", lambda statement: None)
    # Staffing scans the real target root + runs git for the slug — stub it (like the mission
    # sync) so these cmd_run tests stay hermetic. A dedicated test exercises it un-stubbed.
    monkeypatch.setattr(orchestrator, "_seed_staffing", lambda store: [])


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


def test_cmd_run_syncs_mission_from_mission_md(tmp_path, monkeypatch):
    """Task 1.1: MISSION.md's ## Mission wins at run start (the file is the steering wheel);
    an unchanged file never re-steers (no new mission row)."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    monkeypatch.setattr(orchestrator, "_read_mission_md", lambda: "make clive bulletproof")
    with _store(tmp_path) as s:
        s.set_mission("stale store mission")                 # store says B; the file says A
        orchestrator.cmd_run(s, conductor=_completed, token_budget=1, wall_clock_s=1)
        m1 = s.active_mission()
        assert m1["statement"] == "make clive bulletproof"   # the file A steered the live loop
        orchestrator.cmd_run(s, conductor=_completed, token_budget=1, wall_clock_s=1)
        m2 = s.active_mission()
        assert m2["id"] == m1["id"]                           # unchanged file → no new steer


def test_cmd_run_explicit_mission_beats_the_file(tmp_path, monkeypatch):
    """Task 1.1: an explicit --mission always wins — the file sync is skipped when mission
    is passed."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    monkeypatch.setattr(orchestrator, "_read_mission_md", lambda: "file mission")
    wrote = {}
    monkeypatch.setattr(orchestrator, "_write_mission_md",
                        lambda statement: wrote.update(statement=statement))
    with _store(tmp_path) as s:
        orchestrator.cmd_run(s, mission="cli mission", conductor=_completed,
                             token_budget=1, wall_clock_s=1)
        assert s.active_mission()["statement"] == "cli mission"   # the flag, not the file
        assert wrote["statement"] == "cli mission"                # …and it's made durable to MISSION.md


def test_cmd_plan_add_list_status_and_full_id_discipline(tmp_path, capsys):
    """Task 2.3: factory plan add/list/status/link/estimate. link/estimate enforce full-id
    discipline — a partial task id prints '0 rows' and changes nothing (the task-claim bug class)."""
    with _store(tmp_path) as s:
        s.set_mission("reliable recovery")
        orchestrator.cmd_plan(s, "add", rest=["M1: recovery"], deliverable="corpus green",
                              acceptance="pass 3x", budget_tokens=800_000, order=1)
        ms = s.list_milestones()
        assert len(ms) == 1 and ms[0]["budget_tokens"] == 800_000
        mid = ms[0]["id"]
        capsys.readouterr()

        orchestrator.cmd_plan(s, "list")
        out = capsys.readouterr().out
        assert "M1: recovery" in out and "0/0 tasks" in out            # progress rendered

        s.add_task("task-abc12345", "slice", source="research")
        orchestrator.cmd_plan(s, "link", rest=["task-abc", str(mid)])   # PARTIAL id → no-op
        assert "0 rows" in capsys.readouterr().out
        assert s.get_task("task-abc12345")["milestone_id"] is None      # unchanged

        orchestrator.cmd_plan(s, "link", rest=["task-abc12345", str(mid)])  # FULL id → 1 row
        assert "1 row" in capsys.readouterr().out
        assert s.get_task("task-abc12345")["milestone_id"] == mid

        orchestrator.cmd_plan(s, "estimate", rest=["task-abc12345", "60000"], profile="python-dev")
        t = s.get_task("task-abc12345")
        assert t["est_tokens"] == 60_000 and t["profile"] == "python-dev"

        orchestrator.cmd_plan(s, "status", rest=[str(mid), "delivered"])
        assert s.list_milestones(status="delivered")[0]["id"] == mid


def test_cmd_plan_accepts_m_id_form_and_rejects_missing_milestones(tmp_path, capsys):
    """The write commands must take back the 'M<id>' form the tools DISPLAY (plan list / {PLAN}),
    must NOT print a false success for a nonexistent milestone (silent-no-op bug class), and
    estimate accepts the '60k' shorthand a conductor/human will type."""
    with _store(tmp_path) as s:
        s.set_mission("x")
        mid = s.add_milestone("M1", mission_id=s.active_mission()["id"])
        s.add_task("task-abc12345", "slice", source="research")
        capsys.readouterr()

        orchestrator.cmd_plan(s, "status", rest=[f"M{mid}", "active"])       # 'M<id>' accepted
        assert "1 row" in capsys.readouterr().out and s.get_milestone(mid)["status"] == "active"
        orchestrator.cmd_plan(s, "link", rest=["task-abc12345", f"M{mid}"])  # 'M<id>' accepted
        assert "1 row" in capsys.readouterr().out
        assert s.get_task("task-abc12345")["milestone_id"] == mid

        orchestrator.cmd_plan(s, "status", rest=["999", "delivered"])        # missing → 0 rows
        assert "0 rows" in capsys.readouterr().out and s.list_milestones(status="delivered") == []
        orchestrator.cmd_plan(s, "link", rest=["task-abc12345", "999"])      # missing → 0 rows
        assert "0 rows" in capsys.readouterr().out
        assert s.get_task("task-abc12345")["milestone_id"] == mid           # still the real link

        orchestrator.cmd_plan(s, "estimate", rest=["task-abc12345", "60k"])  # '60k' shorthand
        assert s.get_task("task-abc12345")["est_tokens"] == 60_000


def test_cmd_timesheet_prints_rows_and_rollup(tmp_path, capsys):
    """Task 3.2: the timesheet CLI shows the engagement + the per-role rollup."""
    with _store(tmp_path) as s:
        a = s.start_shift(token_budget=1)
        s.add_task("task-a", "add retry", source="research")
        s.add_budget("developer:task-a", 400, 0.04, shift_id=a, seconds=30,
                     notes="merged", profile="python-dev")
        orchestrator.cmd_timesheet(s)
        out = capsys.readouterr().out
    assert "developer:task-a" in out and "add retry" in out and "merged" in out
    assert "per-role" in out                          # the rollup section rendered


def test_cmd_evm_prints_totals_and_milestones(tmp_path, capsys):
    """Task 4.2: the evm CLI shows the totals line, the per-milestone table and the
    estimate-vs-actual list (a task with both an estimate and ledgered actuals)."""
    with _store(tmp_path) as s:
        sid = s.start_shift(token_budget=1)
        mid = s.add_milestone("recovery corpus", budget_tokens=100_000)
        s.set_milestone_status(mid, "delivered")
        s.add_task("task-e1", "slice one", source="research")
        s.set_task_milestone("task-e1", mid)
        s.set_task_estimate("task-e1", 80_000)
        s.set_task_status("task-e1", "done", result="x")
        s.add_budget("developer:task-e1", 40_000, 0.4, shift_id=sid, notes="merged")
        orchestrator.cmd_evm(s)
        out = capsys.readouterr().out
    assert "EVM" in out and "PV" in out and "CPI" in out
    assert "recovery corpus" in out
    assert "estimate vs actual" in out and "task-e1" in out


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
    from factory.orchestrator.develop import execute_claimed_tasks
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        s.add_task("t1", "fix a thing", source="research")

        def conductor(store, *, shift_id, mission, token_budget, wall_clock_s):
            orchestrator.cmd_task(store, "claim", rest="t1")     # the conductor only PLANS + claims
            return {"status": "completed", "tokens_used": 5}

        def executor(store, *, shift_id):                         # the RAIL executes (worker injected)
            return execute_claimed_tasks(store, shift_id,
                                         develop_fn=lambda text, **k: {"action": "merged", "merge_sha": "abc123"})

        res = orchestrator.cmd_run(s, conductor=conductor, executor=executor,
                                   token_budget=100, wall_clock_s=5)

        t1 = s.get_task("t1")
        assert t1["status"] == "done" and t1["shift_id"] == res["shift_id"]   # stamped to this shift
        assert t1["result"] == "abc123"
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
