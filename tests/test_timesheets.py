"""Agent timesheets (reporting/timesheets.py): who worked when, how long, at what spend, to
what verdict — pure reads over the shift-attributed budget_ledger the rail writes (Phase 0)."""
from factory.reporting import timesheets


def test_timesheet_shapes_engagements_newest_first(store):
    a = store.start_shift(token_budget=1)
    store.add_task("task-a", "add retry", source="research")
    store.add_budget("conductor", 100, 0.01, shift_id=a, seconds=5, notes="shift lead")
    store.add_budget("developer:task-a", 400, 0.04, shift_id=a, seconds=30,
                     notes="merged", profile="python-dev")
    store.add_budget("researcher", 50, 0.005)          # legacy: no shift — excluded from timesheet

    rows = timesheets.timesheet(store)
    agents = {r["agent"]: r for r in rows}
    assert "developer:task-a" in agents and "conductor" in agents
    assert "researcher" not in agents                  # shift_id IS NULL → not a timesheet row
    d = agents["developer:task-a"]
    assert d["role"] == "developer" and d["task_title"] == "add retry"
    assert d["profile"] == "python-dev" and d["verdict"] == "merged"
    assert d["shift"] == a and d["seconds"] == 30 and d["tokens"] == 400


def test_by_agent_rolls_up_the_whole_ledger_incl_legacy(store):
    a = store.start_shift(token_budget=1)
    store.add_budget("conductor", 100, 0.01, shift_id=a)
    store.add_budget("developer:task-a", 400, 0.04, shift_id=a)
    store.add_budget("researcher", 50, 0.005)          # legacy old-loop row (no shift)

    roll = {x["role"]: x for x in timesheets.by_agent(store)}
    assert roll["developer"]["tokens"] == 400 and roll["developer"]["engagements"] == 1
    assert roll["conductor"]["engagements"] == 1
    assert roll["researcher"]["tokens"] == 50          # legacy IS in the all-time rollup
