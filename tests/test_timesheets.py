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
    # ORDERING is the contract: the LAST shift-attributed row inserted (developer:task-a) comes
    # first (newest-first, id DESC breaks a same-timestamp tie). Pins it against a dropped/flipped
    # ORDER BY — a dict-by-agent collapse would silently lose this.
    agent_order = [r["agent"] for r in rows]
    assert agent_order == ["developer:task-a", "conductor"]   # newest first; legacy row excluded
    agents = {r["agent"]: r for r in rows}
    d = agents["developer:task-a"]
    assert d["role"] == "developer" and d["task_title"] == "add retry"
    assert d["profile"] == "python-dev" and d["verdict"] == "merged"
    assert d["shift"] == a and d["seconds"] == 30 and d["tokens"] == 400


def test_timesheet_shift_filter_is_applied_in_query_not_after_limit(store):
    """--shift must filter in the query, not after LIMIT: an older shift's rows survive even when
    newer shifts have pushed them past the limit window."""
    old = store.start_shift(token_budget=1)
    store.add_budget("conductor", 10, 0.0, shift_id=old, seconds=1, notes="shift lead")
    new = store.start_shift(token_budget=1)
    for _ in range(5):
        store.add_budget("conductor", 20, 0.0, shift_id=new, seconds=1, notes="shift lead")

    # limit=3 would drop the single old-shift row entirely with a post-filter (all newest are `new`).
    rows = timesheets.timesheet(store, limit=3, shift_id=old)
    assert [r["shift"] for r in rows] == [old] and rows[0]["tokens"] == 10


def test_by_profile_rolls_up_outcomes_and_est_accuracy(store):
    """Task 5.7: per-profile outcome rollup — engagements, merged/blocked, tokens, and est_accuracy
    (median actual/est). Only developer rows with a profile count; conductor/legacy are excluded."""
    sid = store.start_shift(token_budget=1)
    store.add_task("task-a", "a", source="research")
    store.set_task_estimate("task-a", 100)
    store.add_budget("developer:task-a", 200, 0.02, shift_id=sid, notes="merged", profile="python-dev")
    store.add_task("task-b", "b", source="research")
    store.add_budget("developer:task-b", 50, 0.0, shift_id=sid, notes="no_candidate", profile="python-dev")
    store.add_budget("conductor", 10, 0.0, shift_id=sid, profile="")     # not developer → excluded

    roll = {r["profile"]: r for r in timesheets.by_profile(store)}
    assert set(roll) == {"python-dev"}
    p = roll["python-dev"]
    assert p["engagements"] == 2 and p["merged"] == 1 and p["blocked"] == 1
    assert p["tokens"] == 250
    assert p["est_accuracy"] == 2.0                                      # task-a actual 200 / est 100


def test_by_agent_rolls_up_the_whole_ledger_incl_legacy(store):
    a = store.start_shift(token_budget=1)
    store.add_budget("conductor", 100, 0.01, shift_id=a)
    store.add_budget("developer:task-a", 400, 0.04, shift_id=a)
    store.add_budget("researcher", 50, 0.005)          # legacy old-loop row (no shift)

    roll = {x["role"]: x for x in timesheets.by_agent(store)}
    assert roll["developer"]["tokens"] == 400 and roll["developer"]["engagements"] == 1
    assert roll["conductor"]["engagements"] == 1
    assert roll["researcher"]["tokens"] == 50          # legacy IS in the all-time rollup
