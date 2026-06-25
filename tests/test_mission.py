"""Mission-check + steady-state (design step 5). Deterministic: classify the mission
status from store signals and recommend stopping only after K steady shifts. The
terminator is the MISSION reaching steady state (surfaced for the human), never a silent
binary 'done' — `assess` never asserts 'reached' itself (accomplishment is a human call)."""
from factory.common.store import Blackboard
from factory.orchestrator import mission


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


def test_advancing_when_there_is_open_work_or_shipped(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1)
        s.add_task("t1", "do a thing", source="issue")
        out = mission.assess(s, shift_id=sh)
        assert out["status"] == "advancing" and out["recommend_stop"] is False
        assert s.latest_mission_status()["status"] == "advancing"   # recorded


def test_advancing_when_shipped_even_if_backlog_now_empty(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1)
        out = mission.assess(s, shift_id=sh, shipped_count=2)   # empty backlog but progress made
        assert out["status"] == "advancing"


def test_blocked_when_work_remains_but_none_is_open(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1)
        s.add_task("b", "needs a fixture", source="issue")
        s.set_task_status("b", "blocked")
        out = mission.assess(s, shift_id=sh)
        assert out["status"] == "blocked" and out["recommend_stop"] is False


def test_steady_state_but_not_stop_until_k_consecutive(tmp_path):
    with _store(tmp_path) as s:
        # nothing open, nothing blocked, nothing shipped → steady_state, but don't stop yet
        out1 = mission.assess(s, shift_id=s.start_shift(token_budget=1), plateau_k=3)
        out2 = mission.assess(s, shift_id=s.start_shift(token_budget=1), plateau_k=3)
        assert out1["status"] == "steady_state" and out1["recommend_stop"] is False
        assert out2["recommend_stop"] is False                  # only 2 of 3 steady shifts
        out3 = mission.assess(s, shift_id=s.start_shift(token_budget=1), plateau_k=3)
        assert out3["status"] == "steady_state" and out3["recommend_stop"] is True   # K reached → surface


def test_advancing_resets_the_steady_streak(tmp_path):
    with _store(tmp_path) as s:
        mission.assess(s, shift_id=s.start_shift(token_budget=1), plateau_k=2)   # steady
        s.add_task("t", "new work", source="research")
        mission.assess(s, shift_id=s.start_shift(token_budget=1), plateau_k=2)   # advancing (work)
        s.set_task_status("t", "done")
        out = mission.assess(s, shift_id=s.start_shift(token_budget=1), plateau_k=2)
        assert out["status"] == "steady_state" and out["recommend_stop"] is False  # streak broke → 1 of 2


def test_assess_never_asserts_reached(tmp_path):
    """Accomplishment is the human's call — the deterministic check tops out at steady_state."""
    with _store(tmp_path) as s:
        for _ in range(5):
            out = mission.assess(s, shift_id=s.start_shift(token_budget=1), plateau_k=2)
        assert out["status"] != "reached"   # surfaces 'recommend_stop', never claims done
