"""The 09:00 daily update: `factory daily` (a bounded autonomous run that emits
the executive summary) and the launchd schedule that fires it.

Hermetic: cmd_daily is exercised with cmd_autonomous monkeypatched (so no LLM /
subprocess), and the launchd plist is generated + parsed in-memory (no launchctl).
"""
import plistlib

from factory.orchestrator import orchestrator as orch
from factory.orchestrator import scheduling


# --------------------------------------------------------------------------- #
# `factory daily` reads MISSION.md and runs a LARGER bounded autonomous session #
# --------------------------------------------------------------------------- #
def test_cmd_daily_reads_mission_and_runs_bounded_autonomous(tmp_path, monkeypatch):
    mission_file = tmp_path / "MISSION.md"
    mission_file.write_text(
        "# Factory mission\n\n## Mission\n\noptimise the widget pipeline\n\n"
        "## Research focus\n\nwidgets\n", encoding="utf-8")
    # route paths.factory("MISSION.md") at our temp file
    monkeypatch.setattr("factory.common.paths.factory",
                        lambda *p: str(tmp_path.joinpath(*p)))

    captured = {}

    def fake_auto(store, mission, *, max_rounds, token_budget=None,
                  do_research=True, **k):
        captured.update(mission=mission, max_rounds=max_rounds,
                        token_budget=token_budget, do_research=do_research)
        return {"rounds_run": max_rounds, "report_path": str(tmp_path / "r.md")}

    monkeypatch.setattr("factory.orchestrator.autonomy.cmd_autonomous", fake_auto)

    summary = orch.cmd_daily(object())  # store is unused by the fake

    assert captured["mission"] == "optimise the widget pipeline"
    assert captured["do_research"] is True
    # "Larger" daily budget: several rounds + a non-None token ceiling
    assert captured["max_rounds"] == orch.DAILY_MAX_ROUNDS
    assert captured["token_budget"] == orch.DAILY_TOKEN_BUDGET
    assert orch.DAILY_MAX_ROUNDS >= 4
    assert orch.DAILY_TOKEN_BUDGET is not None
    assert summary["report_path"].endswith("r.md")


def test_cmd_daily_falls_back_to_default_mission(tmp_path, monkeypatch):
    # No MISSION.md → cmd_daily must still run with a non-empty default mission.
    monkeypatch.setattr("factory.common.paths.factory",
                        lambda *p: str(tmp_path.joinpath(*p)))
    captured = {}
    monkeypatch.setattr("factory.orchestrator.autonomy.cmd_autonomous",
                        lambda store, mission, **k: captured.update(mission=mission) or {})
    orch.cmd_daily(object())
    assert isinstance(captured["mission"], str) and captured["mission"].strip()


# --------------------------------------------------------------------------- #
# launchd schedule fires `factory daily` at 09:00 daily                        #
# --------------------------------------------------------------------------- #
def test_launchd_plist_runs_factory_daily_at_0900():
    xml = scheduling.launchd_plist("/opt/factory", "/usr/bin/python3")
    pl = plistlib.loads(xml.encode("utf-8"))
    assert pl["Label"] == scheduling.PLIST_LABEL
    assert pl["StartCalendarInterval"]["Hour"] == 9
    assert pl["StartCalendarInterval"]["Minute"] == 0
    args = pl["ProgramArguments"]
    assert args[-1] == "daily"
    assert any(a.endswith("bin/factory") for a in args)
    assert pl["WorkingDirectory"] == "/opt/factory"
    # the chosen interpreter is propagated so the agent runs under the right python
    assert pl["EnvironmentVariables"]["FACTORY_PYTHON"] == "/usr/bin/python3"


def test_launchd_plist_can_schedule_the_conductor_loop():
    xml = scheduling.launchd_plist("/opt/factory", "/usr/bin/python3",
                                   command=("run",), label=scheduling.RUN_LABEL)
    pl = plistlib.loads(xml.encode("utf-8"))
    assert pl["ProgramArguments"][-1] == "run"        # schedules `factory run`, not daily
    assert pl["Label"] == scheduling.RUN_LABEL        # a distinct agent (coexists with daily)
    assert "run-launchd" in pl["StandardOutPath"]     # separate logs


def test_plist_path_is_in_user_launchagents():
    p = scheduling.plist_path()
    assert p.endswith(f"/Library/LaunchAgents/{scheduling.PLIST_LABEL}.plist")
