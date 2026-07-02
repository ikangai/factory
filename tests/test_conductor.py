"""The conductor (design: 2026-06-25-conductor-loop-design.md, step 3) — the LLM lead the
shift harness runs. Hermetic: claude_super is monkeypatched, so the prompt assembly + the
result parsing are tested without spawning an agent (the live conductor is operator-run)."""
from factory.common import paths
from factory.common.store import Blackboard
from factory.roles import common, conductor


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


def test_build_conductor_prompt_carries_the_live_context(tmp_path, monkeypatch):
    from factory.roles import research_feed
    monkeypatch.setattr(research_feed, "fetch_issues",          # hermetic: no live gh
                        lambda repo, **k: "- #41: Self-learning tool discovery  [enhancement]")
    with _store(tmp_path) as s:
        m_id = s.set_mission("make clive reliable", target_repo="ikangai/clive")
        a = s.start_shift(token_budget=1)
        s.end_shift(a, status="completed", resume_note="t9 awaiting fixture")
        s.add_task("t1", "fix dead-pane detection", source="issue", source_ref="#41")
        s.add_digest(shift_id=a, shipped=["x"], summary="shipped the reconnect fix")
        cur = s.start_shift(token_budget=500000, mission_id=m_id)

        p = conductor.build_conductor_prompt(s, s.active_mission(), shift_id=cur, token_budget=500000)
        assert "make clive reliable" in p and "ikangai/clive" in p   # the mission + target
        assert "t9 awaiting fixture" in p                            # the PRIOR shift's resume note
        assert "fix dead-pane detection" in p and "#41" in p         # the open backlog (with source ref)
        assert "shipped the reconnect fix" in p                      # unconsumed research digest
        assert "Self-learning tool discovery" in p                   # the target's OPEN ISSUES (fetched)
        assert "500,000" in p                                         # the shift budget


def test_build_conductor_prompt_includes_the_plan(tmp_path, monkeypatch):
    """Task 2.4: the conductor's contract renders the current plan ({PLAN}) and points at the
    plan CLI so it maintains/estimates/revises milestones each shift."""
    from factory.roles import research_feed
    monkeypatch.setattr(research_feed, "fetch_issues", lambda repo, **k: "")
    with _store(tmp_path) as s:
        m = s.set_mission("make clive reliable", target_repo="ikangai/clive")
        mid = s.add_milestone("M1: recovery", mission_id=m, deliverable="corpus green",
                              acceptance="pass 3x", budget_tokens=800_000, planned_order=1)
        s.add_task("t1", "slice", source="research")
        s.set_task_milestone("t1", mid)
        s.set_task_estimate("t1", 50_000)                        # the conductor's estimate
        cur = s.start_shift(token_budget=1, mission_id=m)
        s.add_budget("developer:t1", 32_000, 0.3, shift_id=cur, notes="merged")   # the ACTUAL spend
        p = conductor.build_conductor_prompt(s, s.active_mission(), shift_id=cur, token_budget=1)
    assert "M1: recovery" in p                                   # the plan is rendered
    assert "0/1 tasks" in p or "0/1" in p                        # per-milestone progress
    assert "est 50,000 vs actual 32,000" in p                    # Task 2.4: estimates vs actuals
    assert "plan estimate" in p and "plan link" in p             # the plan CLI is in the contract


def test_build_conductor_prompt_includes_the_workforce(tmp_path, monkeypatch):
    """Task 5.6: the contract renders the active bench ({WORKERS}) with per-profile outcomes and
    the staffing levers, so the conductor assigns/generates/retires profiles on evidence."""
    from factory.roles import research_feed
    monkeypatch.setattr(research_feed, "fetch_issues", lambda repo, **k: "")
    with _store(tmp_path) as s:
        m = s.set_mission("make clive reliable")
        s.add_profile("python-dev", description="Python specialist", model="standard",
                      overlay="senior python")
        s.add_task("t1", "slice", source="research")
        s.set_task_estimate("t1", 100_000)
        cur = s.start_shift(token_budget=1, mission_id=m)
        s.add_budget("developer:t1", 50_000, 0.3, shift_id=cur, notes="merged", profile="python-dev")
        p = conductor.build_conductor_prompt(s, s.active_mission(), shift_id=cur, token_budget=1)
    assert "python-dev" in p and "1 eng" in p and "100% merged" in p     # bench + outcomes rendered
    assert "worker add" in p and "worker retire" in p                    # the staffing levers
    assert "--profile" in p                                              # assign a profile per task


def test_build_conductor_prompt_empty_plan_prompts_to_draft(tmp_path, monkeypatch):
    from factory.roles import research_feed
    monkeypatch.setattr(research_feed, "fetch_issues", lambda repo, **k: "")
    with _store(tmp_path) as s:
        m = s.set_mission("x")
        cur = s.start_shift(token_budget=1, mission_id=m)
        p = conductor.build_conductor_prompt(s, s.active_mission(), shift_id=cur, token_budget=1)
    assert "no plan yet" in p                                    # nudge to draft milestones


def test_run_conductor_spawns_a_full_lead_and_parses_its_result(tmp_path, monkeypatch):
    captured = {}

    def fake_super(prompt, **k):
        captured.update(prompt=prompt, **k)
        return ('working… ```json\n{"status":"completed","report":"shipped 2 fixes",'
                '"resume_note":"t9 still blocked"}\n```', 4321, 0.1)

    monkeypatch.setattr(common, "claude_super", fake_super)
    with _store(tmp_path) as s:
        m_id = s.set_mission("x")
        cur = s.start_shift(token_budget=9, mission_id=m_id)
        out = conductor.run_conductor(s, shift_id=cur, mission=s.active_mission(),
                                      token_budget=9, wall_clock_s=1800)

    assert out == {"status": "completed", "report": "shipped 2 fixes",
                   "resume_note": "t9 still blocked", "tokens_used": 4321}
    assert captured["settings"] == "user"                        # full instance: agora + diary + MCP
    assert captured["workdir"] == paths.FACTORY_ROOT             # drives ./bin/factory from the repo
    assert "Bash" in captured["allowed_tools"]                   # to run the CLI + agora
    assert "WebSearch" in captured["allowed_tools"] and "Skill" in captured["allowed_tools"]
    assert "Write" not in captured["allowed_tools"]              # it dispatches; it doesn't edit code
    assert captured["timeout"] == 1800                           # the wall-clock ceiling
    assert captured["extra_env"]["AGORA_SQUAD"]                  # its own squad → no barrier hang


def test_run_conductor_falls_back_when_reply_has_no_json(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "claude_super",
                        lambda prompt, **k: ("just prose, no fenced block", 5, 0.0))
    with _store(tmp_path) as s:
        m_id = s.set_mission("x")
        cur = s.start_shift(token_budget=1, mission_id=m_id)
        out = conductor.run_conductor(s, shift_id=cur, mission=s.active_mission(),
                                      token_budget=1, wall_clock_s=10)
    assert out["status"] == "completed" and "just prose" in out["report"]   # safe default
    assert out["tokens_used"] == 5


def test_run_conductor_coerces_an_invalid_shift_status(tmp_path, monkeypatch):
    """The conductor emitting status='blocked' would violate the shifts CHECK constraint —
    coerce any non-harness status to 'completed' (blockers live in the report)."""
    monkeypatch.setattr(common, "claude_super", lambda prompt, **k: (
        '```json\n{"status":"blocked","report":"r","resume_note":"n"}\n```', 1, 0.0))
    with _store(tmp_path) as s:
        m_id = s.set_mission("x")
        cur = s.start_shift(token_budget=1, mission_id=m_id)
        out = conductor.run_conductor(s, shift_id=cur, mission=s.active_mission(),
                                      token_budget=1, wall_clock_s=1)
    assert out["status"] == "completed"


def test_run_conductor_surfaces_a_failed_spawn_as_error_not_completed(tmp_path, monkeypatch):
    """A timed-out/crashed spawn returns the transport sentinel — it must NOT be recorded as
    a clean 'completed' with a blank resume note (that would make the wall-clock ceiling dead)."""
    monkeypatch.setattr(common, "claude_super",
                        lambda prompt, **k: ("[claude -p unavailable: timeout]", 0, 0.0))
    with _store(tmp_path) as s:
        m_id = s.set_mission("x")
        cur = s.start_shift(token_budget=1, mission_id=m_id)
        out = conductor.run_conductor(s, shift_id=cur, mission=s.active_mission(),
                                      token_budget=1, wall_clock_s=1)
    assert out["status"] == "error" and "wall-clock" in out["resume_note"]   # honest, not a fake success


def test_run_conductor_ledgers_its_own_spend(tmp_path, monkeypatch):
    """Task 0.4: the conductor records its own tokens/cost/seconds against the shift
    (the cost was previously discarded and nothing reached budget_ledger)."""
    monkeypatch.setattr(common, "claude_super",
                        lambda prompt, **k: ('{"status":"completed"}', 777, 0.03))
    with _store(tmp_path) as s:
        m_id = s.set_mission("x")
        cur = s.start_shift(token_budget=1, mission_id=m_id)
        conductor.run_conductor(s, shift_id=cur, mission=s.active_mission(),
                                token_budget=1, wall_clock_s=10)
        rows = [e for e in s.budget_entries() if e["role_or_run"] == "conductor"]
    assert len(rows) == 1
    assert rows[0]["tokens"] == 777 and rows[0]["cost"] == 0.03
    assert rows[0]["shift_id"] == cur and rows[0]["seconds"] >= 0


def test_run_conductor_ledgers_even_a_failed_spawn(tmp_path, monkeypatch):
    """Task 0.4: a timed-out/crashed spawn still spent tokens — the single ledger call
    sits before the sentinel branch so both return paths are covered."""
    monkeypatch.setattr(common, "claude_super",
                        lambda prompt, **k: ("[claude -p unavailable: timeout]", 42, 0.0))
    with _store(tmp_path) as s:
        m_id = s.set_mission("x")
        cur = s.start_shift(token_budget=1, mission_id=m_id)
        conductor.run_conductor(s, shift_id=cur, mission=s.active_mission(),
                                token_budget=1, wall_clock_s=1)
        rows = [e for e in s.budget_entries() if e["role_or_run"] == "conductor"]
    assert len(rows) == 1 and rows[0]["tokens"] == 42 and rows[0]["shift_id"] == cur


def test_run_conductor_is_dev_mode_same_user_by_default(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(common, "claude_super",
                        lambda prompt, **k: captured.update(k) or ("{}", 1, 0.0))
    with _store(tmp_path) as s:
        m_id = s.set_mission("x")
        cur = s.start_shift(token_budget=1, mission_id=m_id)
        conductor.run_conductor(s, shift_id=cur, mission=s.active_mission(),
                                token_budget=1, wall_clock_s=10)
    assert captured["as_user"] is None        # dev default: same-user (prod passes the Guest-House user)
