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


def test_run_shift_tokens_used_is_full_ledgered_spend(tmp_path, monkeypatch):
    """Task 0.6: shifts.tokens_used = conductor + workers via the ledger, not the conductor's
    self-report alone. Mirrors the real flow (both ledger themselves as of Tasks 0.3/0.4)."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)

    def cond(store, *, shift_id, mission, token_budget, wall_clock_s):
        store.add_budget("conductor", 100, shift_id=shift_id)      # as of Task 0.4
        return {"status": "completed", "tokens_used": 100}

    def execu(store, *, shift_id):
        store.add_budget("developer:t1", 400, shift_id=shift_id)   # as of Task 0.3
        return 1

    with _store(tmp_path) as s:
        s.set_mission("x")
        out = shiftmod.run_shift(s, token_budget=10_000, conductor=cond, executor=execu)
        sh = s.last_shift()
    assert sh["tokens_used"] == 500 and out["tokens_used"] == 500   # 100 conductor + 400 worker


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


def test_run_shift_reaps_orphaned_executing_approvals_at_startup(tmp_path, monkeypatch):
    """Fix 4d (final whole-branch review): run_shift reaps orphaned 'executing' approvals
    alongside crashed shifts, so a push that crashed between claim and resolve can't leave an
    invisible, unapprovable row forever."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        m = s.set_mission("x")
        aid = s.add_pending_approval("graduation", {"n_commits": 1})
        assert s.claim_approval(aid) is True                       # stranded 'executing'
        # backdate claimed_at (Fix B: the reaper's age floor keys on CLAIM time, not
        # proposal/created_at time) so this row reads as stuck beyond the age floor
        s.conn.execute("UPDATE pending_approvals SET claimed_at = '2000-01-01T00:00:00.000000Z' "
                       "WHERE id = ?", (aid,))
        s.conn.commit()
        shiftmod.run_shift(s, token_budget=10, mission="x",
                           conductor=lambda *a, **k: {"status": "completed"})
        assert s.get_approval(aid)["status"] == "stale"            # reconciled before the shift ran


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


def test_run_shift_abnormal_end_reports_ledgered_spend_to_the_brake(tmp_path, monkeypatch):
    """A conductor that spends (its own row + the refill) then dies must not close the shift with
    tokens_used=0 — the loop's cumulative token brake would under-count on every crash/timeout."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)

    def refill(store):
        store.add_budget("researcher", 300, 0.0, shift_id=store.current_shift_id(), seconds=5)

    for conductor_end in ("timeout", "error"):
        s = Blackboard(str(tmp_path / f"{conductor_end}.db")); s.init_db()
        try:
            s.set_mission("x")

            def dies(store, *, shift_id, mission, token_budget, wall_clock_s):
                store.add_budget("conductor", 200, 0.0, shift_id=shift_id, seconds=3)
                raise (TimeoutError() if conductor_end == "timeout" else RuntimeError("boom"))

            res = shiftmod.run_shift(s, token_budget=10_000, conductor=dies,
                                     refill=refill, refill_threshold=1)
            sh = s.last_shift()
        finally:
            s.close()
        assert res["tokens_used"] == 500 and sh["tokens_used"] == 500   # 300 refill + 200 conductor
        assert res["action"] == ("timed_out" if conductor_end == "timeout" else "error")


def test_run_shift_requeues_claimed_work_when_the_conductor_errors(tmp_path, monkeypatch):
    """A conductor that claims a task then dies must not strand it in_progress — the next
    shift would never pick it up (this shift is 'error', not 'running', so reap skips it)."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        s.set_mission("x")
        s.add_task("t1", "x", source="issue")

        def claims_then_blows_up(store, *, shift_id, mission, token_budget, wall_clock_s):
            store.set_task_status("t1", "in_progress", shift_id=shift_id)
            raise RuntimeError("boom")

        res = shiftmod.run_shift(s, token_budget=1, conductor=claims_then_blows_up)
        assert res["action"] == "error"
        assert s.get_task("t1")["status"] == "open"      # claimed work returned to the backlog


def test_run_shift_requeues_on_a_returned_error_status(tmp_path, monkeypatch):
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        s.set_mission("x")
        s.add_task("t1", "x", source="issue")

        def claims_then_returns_error(store, *, shift_id, mission, token_budget, wall_clock_s):
            store.set_task_status("t1", "in_progress", shift_id=shift_id)
            return {"status": "error", "resume_note": "spawn failed"}   # e.g. the sentinel path

        res = shiftmod.run_shift(s, token_budget=1, conductor=claims_then_returns_error)
        assert res["action"] == "error" and s.get_task("t1")["status"] == "open"


def test_run_shift_refills_backlog_from_research_when_thin(tmp_path, monkeypatch):
    """The generative loop on the RAIL: when the open backlog is below the threshold,
    run_shift tops it up from research BEFORE the conductor plans — so the conductor has
    grounded work and research isn't left to the LLM's discretion (which left it dry)."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        s.set_mission("x")                              # empty backlog (< threshold)
        called = {"n": 0}

        def refill(store):
            called["n"] += 1
            store.add_task("r1", "researched direction", source="research")

        def conductor(store, *, shift_id, mission, token_budget, wall_clock_s):
            assert any(t["id"] == "r1" for t in store.list_tasks(status="open"))   # sees the refill
            return {"status": "completed"}

        res = shiftmod.run_shift(s, token_budget=1, conductor=conductor, refill=refill, refill_threshold=2)
        assert called["n"] == 1 and res["action"] == "completed"
        assert any(t["source"] == "research" for t in s.list_tasks())              # research produced work


def test_run_shift_skips_refill_when_backlog_is_full(tmp_path, monkeypatch):
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        s.set_mission("x")
        for i in "abc":
            s.add_task(i, i, source="issue")            # 3 open ≥ threshold 2
        called = {"n": 0}
        shiftmod.run_shift(s, token_budget=1, conductor=lambda *a, **k: {"status": "completed"},
                           refill=lambda store: called.__setitem__("n", called["n"] + 1),
                           refill_threshold=2)
        assert called["n"] == 0                          # backlog not thin → no researcher spawned


def test_run_shift_runs_the_executor_for_the_shift_and_returns_shipped(tmp_path, monkeypatch):
    """The conductor PLANS; the rail's executor runs the claimed work. run_shift invokes the
    executor for the just-run shift and surfaces its shipped count."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        s.set_mission("x")
        seen = {}

        def conductor(store, *, shift_id, mission, token_budget, wall_clock_s):
            seen["shift"] = shift_id
            return {"status": "completed"}

        def executor(store, *, shift_id):
            seen["exec_shift"] = shift_id
            return 3

        res = shiftmod.run_shift(s, token_budget=1, conductor=conductor, executor=executor)
        assert res["shipped"] == 3 and seen["exec_shift"] == seen["shift"]   # executor ran for THIS shift


def test_run_shift_requeues_unclosed_work_even_on_clean_completion(tmp_path, monkeypatch):
    """Live-smoke regression: the conductor claimed a task, backgrounded its dispatch, and
    ended the shift 'completed' without closing it — reap only rescues 'running' shifts, so
    the task was stranded in_progress forever. A completed shift now requeues it too."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        s.set_mission("x")
        s.add_task("t1", "x", source="issue")

        def claims_but_does_not_close(store, *, shift_id, mission, token_budget, wall_clock_s):
            store.set_task_status("t1", "in_progress", shift_id=shift_id)
            return {"status": "completed"}      # ends WITHOUT a task done

        res = shiftmod.run_shift(s, token_budget=1, conductor=claims_but_does_not_close)
        assert res["action"] == "completed"
        assert s.get_task("t1")["status"] == "open"     # rescued, not stranded in_progress


# --- Task 0.2: the per-shift token budget is a REAL brake, not a decorative column --------

def _spender(spend, resume_note="planned t1; blocked on t9"):
    """A conductor that ledgers `spend` tokens against its shift, then plans normally."""
    def cond(store, *, shift_id, mission, token_budget, wall_clock_s):
        store.add_budget("conductor", spend, shift_id=shift_id)
        return {"status": "completed", "resume_note": resume_note}
    return cond


def test_run_shift_budget_exhausted_skips_executor(tmp_path, monkeypatch):
    """spent ≥ token_budget after the conductor → the executor NEVER dispatches, the shift
    ends 'budget_exhausted', and the budget note is APPENDED to the conductor's own resume
    note (never replacing it — the next shift's {RESUME} seam needs both)."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    monkeypatch.setattr(shiftmod.config, "load_config",
                        lambda: {"autonomy": {"enforce_shift_budget": True}})
    with _store(tmp_path) as s:
        s.set_mission("x")
        ran = {"exec": False}

        def executor(store, *, shift_id):
            ran["exec"] = True
            return 1

        res = shiftmod.run_shift(s, token_budget=1000, conductor=_spender(1500),
                                 executor=executor)
        sh = s.last_shift()
    assert ran["exec"] is False                          # brake fired BEFORE dispatch
    assert res["action"] == "budget_exhausted" and res["shipped"] == 0
    assert sh["status"] == "budget_exhausted"
    assert sh["resume_note"].startswith("planned t1; blocked on t9")   # conductor's note kept
    assert "budget exhausted" in sh["resume_note"]                     # …with the brake note appended
    assert "1500" in sh["resume_note"] and "1000" in sh["resume_note"]


def test_run_shift_budget_trips_on_exact_equality_and_notes_alone(tmp_path, monkeypatch):
    """spent == token_budget trips (>=); a conductor with no resume note gets the budget
    note standing alone (no stray separator)."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    monkeypatch.setattr(shiftmod.config, "load_config",
                        lambda: {"autonomy": {"enforce_shift_budget": True}})
    with _store(tmp_path) as s:
        s.set_mission("x")
        res = shiftmod.run_shift(s, token_budget=500, conductor=_spender(500, resume_note=""),
                                 executor=lambda store, *, shift_id: 1)
        sh = s.last_shift()
    assert res["action"] == "budget_exhausted" and sh["status"] == "budget_exhausted"
    assert sh["resume_note"].startswith("budget exhausted")


def test_run_shift_budget_exhausted_requeues_claimed_tasks(tmp_path, monkeypatch):
    """The existing post-shift requeue still runs on the budget path — claimed work goes
    back to the backlog instead of stranding in_progress."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    monkeypatch.setattr(shiftmod.config, "load_config",
                        lambda: {"autonomy": {"enforce_shift_budget": True}})
    with _store(tmp_path) as s:
        s.set_mission("x")
        s.add_task("t1", "x", source="issue")

        def claims_and_overspends(store, *, shift_id, mission, token_budget, wall_clock_s):
            store.set_task_status("t1", "in_progress", shift_id=shift_id)
            store.add_budget("conductor", 2000, shift_id=shift_id)
            return {"status": "completed", "resume_note": "claimed t1"}

        res = shiftmod.run_shift(s, token_budget=1000, conductor=claims_and_overspends,
                                 executor=lambda store, *, shift_id: 1)
        assert res["action"] == "budget_exhausted"
        assert s.get_task("t1")["status"] == "open"      # requeued, not stranded


def test_run_shift_budget_zero_means_unlimited(tmp_path, monkeypatch):
    """token_budget == 0 is the 'unlimited' convention (matches loop_token_budget) — the
    brake never trips, the executor runs, no budget note is appended."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    monkeypatch.setattr(shiftmod.config, "load_config",
                        lambda: {"autonomy": {"enforce_shift_budget": True}})
    with _store(tmp_path) as s:
        s.set_mission("x")
        ran = {"exec": False}

        def executor(store, *, shift_id):
            ran["exec"] = True
            return 2

        res = shiftmod.run_shift(s, token_budget=0, conductor=_spender(5000), executor=executor)
        sh = s.last_shift()
    assert ran["exec"] is True and res["action"] == "completed" and res["shipped"] == 2
    assert "budget exhausted" not in sh["resume_note"]


def test_run_shift_budget_enforcement_defaults_on_when_knob_absent(tmp_path, monkeypatch):
    """autonomy.enforce_shift_budget missing from config → the brake is STILL on (a brake
    defaults engaged)."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    monkeypatch.setattr(shiftmod.config, "load_config", lambda: {})
    with _store(tmp_path) as s:
        s.set_mission("x")
        res = shiftmod.run_shift(s, token_budget=100, conductor=_spender(200),
                                 executor=lambda store, *, shift_id: 1)
    assert res["action"] == "budget_exhausted" and res["shipped"] == 0


def test_run_shift_budget_enforcement_can_be_disabled_in_config(tmp_path, monkeypatch):
    """autonomy.enforce_shift_budget: false → today's behavior (executor runs regardless)."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    monkeypatch.setattr(shiftmod.config, "load_config",
                        lambda: {"autonomy": {"enforce_shift_budget": False}})
    with _store(tmp_path) as s:
        s.set_mission("x")
        ran = {"exec": False}

        def executor(store, *, shift_id):
            ran["exec"] = True
            return 1

        res = shiftmod.run_shift(s, token_budget=100, conductor=_spender(200), executor=executor)
    assert ran["exec"] is True and res["action"] == "completed"


def test_enforce_shift_budget_is_config_only_and_defaults_true():
    """The knob is a BRAKE: shipped true in config.yaml and deliberately NOT board-toggleable
    (never in SETTINGS_SPEC — the operator-dial whitelist)."""
    from factory.common.config import SETTINGS_SPEC, load_config
    assert "autonomy.enforce_shift_budget" not in SETTINGS_SPEC
    assert (load_config().get("autonomy") or {}).get("enforce_shift_budget") is True


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
