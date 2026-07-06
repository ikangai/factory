"""Autonomy mode (auto/shift) + the continuous runner. AUTO works shift-after-shift; SHIFT
runs one and pauses; the mode is read BETWEEN shifts so the dashboard toggle is live.
Hermetic — the runner's per-shift call is injected (no agents)."""
import pytest

from factory.common import killswitch, mode as modemod
from factory.orchestrator import orchestrator


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(modemod, "_mode_path", lambda: str(tmp_path / ".factory-mode"))
    monkeypatch.setattr(killswitch, "is_halted", lambda: False)


def test_mode_defaults_to_shift_and_round_trips():
    assert modemod.read_mode() == "shift"                      # safe default: human-in-the-loop
    assert modemod.set_mode("auto") == "auto" and modemod.is_auto()
    assert modemod.set_mode("SHIFT") == "shift" and not modemod.is_auto()
    with pytest.raises(ValueError):
        modemod.set_mode("nonsense")


def test_loop_in_shift_mode_runs_one_shift_then_pauses():
    modemod.set_mode("shift")
    calls = {"n": 0}

    def fake(store, **k):
        calls["n"] += 1
        return {"action": "completed", "shift_id": calls["n"]}

    n = orchestrator.cmd_run_loop(object(), run_fn=fake)
    assert n == 1 and calls["n"] == 1                          # exactly one shift


def test_loop_in_auto_mode_runs_until_convergence():
    modemod.set_mode("auto")
    seq = iter([{"action": "completed", "shift_id": 1},
                {"action": "completed", "shift_id": 2},
                {"action": "idle"}])                            # converged
    n = orchestrator.cmd_run_loop(object(), run_fn=lambda store, **k: next(seq))
    assert n == 3                                              # kept going until idle


def test_loop_stops_immediately_on_kill_switch(monkeypatch):
    modemod.set_mode("auto")
    monkeypatch.setattr(killswitch, "is_halted", lambda: True)
    n = orchestrator.cmd_run_loop(object(), run_fn=lambda store, **k: {"action": "completed"})
    assert n == 0                                              # never ran a shift


def test_loop_stops_on_wall_clock_deadline():
    modemod.set_mode("auto")
    clock = {"t": 0.0}

    def fake(store, **k):
        clock["t"] += 100                          # each shift advances the clock 100s
        return {"action": "completed", "shift_id": 1, "tokens_used": 0}

    n = orchestrator.cmd_run_loop(object(), run_fn=fake, now_fn=lambda: clock["t"],
                                  loop_deadline_s=250)
    assert n == 3                                  # shifts start at t=0,100,200; t=300 ≥ 250 → stop


def test_loop_stops_on_token_budget():
    modemod.set_mode("auto")

    def fake(store, **k):
        return {"action": "completed", "shift_id": 1, "tokens_used": 400_000}

    n = orchestrator.cmd_run_loop(object(), run_fn=fake, loop_token_budget=1_000_000)
    assert n == 3                                  # 400k×3 = 1.2M ≥ 1M → budget_exhausted


def test_loop_honours_a_live_toggle_to_shift_mid_run():
    modemod.set_mode("auto")
    calls = {"n": 0}

    def fake(store, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            modemod.set_mode("shift")      # operator toggles SHIFT on the dashboard mid-run
        return {"action": "completed", "shift_id": calls["n"]}

    n = orchestrator.cmd_run_loop(object(), run_fn=fake)
    assert n == 2                          # ran shift 1 (auto), shift 2 toggled shift → paused after


# --- durable brake on a safety-ceiling exit ------------------------------------------------
# A per-process ceiling (token budget / deadline / max_shifts) must not merely `break`: the
# dashboard's restart_if_auto() self-heal can't tell a deliberate ceiling-stop from a crash,
# so if the loop leaves mode=AUTO it is respawned with fresh counters (an unbounded treadmill).
# The fix: a ceiling exit engages a DURABLE brake by flipping AUTO→SHIFT, which restart_if_auto
# already vetoes (not_auto). Benign convergence (idle/no_mission) is NOT a ceiling and stays AUTO.

def test_ceiling_exit_token_budget_flips_mode_to_shift():
    modemod.set_mode("auto")
    orchestrator.cmd_run_loop(
        object(),
        run_fn=lambda store, **k: {"action": "completed", "shift_id": 1, "tokens_used": 400_000},
        loop_token_budget=1_000_000,
    )
    assert modemod.read_mode() == "shift"          # respawn now sees not_auto → no treadmill


def test_ceiling_exit_deadline_flips_mode_to_shift():
    modemod.set_mode("auto")
    clock = {"t": 0.0}

    def fake(store, **k):
        clock["t"] += 100
        return {"action": "completed", "shift_id": 1, "tokens_used": 0}

    orchestrator.cmd_run_loop(object(), run_fn=fake, now_fn=lambda: clock["t"],
                              loop_deadline_s=250)
    assert modemod.read_mode() == "shift"


def test_ceiling_exit_max_shifts_flips_mode_to_shift():
    modemod.set_mode("auto")
    orchestrator.cmd_run_loop(
        object(),
        run_fn=lambda store, **k: {"action": "completed", "shift_id": 1, "tokens_used": 0},
        max_shifts=3,
    )
    assert modemod.read_mode() == "shift"


def test_benign_convergence_leaves_mode_auto():
    modemod.set_mode("auto")
    seq = iter([{"action": "completed", "shift_id": 1}, {"action": "idle"}])
    orchestrator.cmd_run_loop(object(), run_fn=lambda store, **k: next(seq))
    assert modemod.read_mode() == "auto"           # idle is not a ceiling — stays AUTO to resume


# --- consecutive-failure circuit breaker ---------------------------------------------------
# error/timed_out shifts ledger ~0 tokens, so the token ceiling never trips on them; without
# a breaker the loop grinds through all of max_shifts (and, pre-fix, respawned forever) with
# no backoff. K consecutive failures trips the breaker, which is a ceiling → durable brake.

def test_consecutive_failures_trip_the_breaker_and_brake():
    modemod.set_mode("auto")
    calls = {"n": 0}

    def fake(store, **k):
        calls["n"] += 1
        return {"action": "error", "shift_id": calls["n"]}

    n = orchestrator.cmd_run_loop(object(), run_fn=fake, sleep_fn=lambda s: None)
    assert n == 3                                   # stopped after 3 consecutive fails, not 50
    assert modemod.read_mode() == "shift"           # breaker is a ceiling → durable brake


def test_a_healthy_shift_resets_the_failure_streak():
    modemod.set_mode("auto")
    seq = iter([{"action": "error", "shift_id": 1},
                {"action": "completed", "shift_id": 2},   # resets the streak
                {"action": "error", "shift_id": 3},
                {"action": "timed_out", "shift_id": 4},
                {"action": "idle", "shift_id": 5}])        # converged before 3 in a row
    n = orchestrator.cmd_run_loop(object(), run_fn=lambda store, **k: next(seq),
                                  sleep_fn=lambda s: None)
    assert n == 5                                   # ran to convergence; breaker never tripped
    assert modemod.read_mode() == "auto"            # idle convergence, not a ceiling
