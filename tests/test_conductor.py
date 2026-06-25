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


def test_build_conductor_prompt_carries_the_live_context(tmp_path):
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
        assert "500,000" in p                                         # the shift budget


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
