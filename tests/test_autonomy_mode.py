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
