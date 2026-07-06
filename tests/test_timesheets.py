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


def test_by_profile_does_not_count_halted_as_blocked(store):
    """Review #2: a STOP-braked ('halted') developer round is a brake artifact (the task is
    requeued), not a failure — it counts toward engagements + spend but NOT as blocked, so a
    mid-round STOP can't phantom-fail a healthy profile."""
    sid = store.start_shift(token_budget=1)
    store.add_task("task-h", "h", source="research")
    store.add_budget("developer:task-h", 400, 0.0, shift_id=sid, notes="halted", profile="python-dev")
    store.add_budget("developer:task-h", 600, 0.0, shift_id=sid, notes="merged", profile="python-dev")

    p = {r["profile"]: r for r in timesheets.by_profile(store)}["python-dev"]
    assert p["merged"] == 1 and p["blocked"] == 0        # halted excluded from failures
    assert p["engagements"] == 2 and p["tokens"] == 1000  # …but its spend still counts


def test_est_accuracy_skips_tasks_split_across_profiles(store):
    """Review #6: a task reassigned across profiles has PARTIAL per-profile actuals but the FULL
    task est on each row — so it's excluded from est_accuracy (ambiguous). A sole-worker task
    still yields a ratio."""
    sid = store.start_shift(token_budget=1)
    store.add_task("split", "s", source="research")
    store.set_task_estimate("split", 1000)
    store.add_budget("developer:split", 400, 0.0, shift_id=sid, notes="halted", profile="python-dev")
    store.add_budget("developer:split", 600, 0.0, shift_id=sid, notes="merged", profile="ts-dev")
    store.add_task("solo", "x", source="research")
    store.set_task_estimate("solo", 100)
    store.add_budget("developer:solo", 200, 0.0, shift_id=sid, notes="merged", profile="ts-dev")

    roll = {r["profile"]: r for r in timesheets.by_profile(store)}
    assert roll["python-dev"]["est_accuracy"] is None      # split task → ambiguous, skipped
    assert roll["ts-dev"]["est_accuracy"] == 2.0           # only the sole-worker 'solo' counts (200/100)


def test_by_agent_rolls_up_the_whole_ledger_incl_legacy(store):
    a = store.start_shift(token_budget=1)
    store.add_budget("conductor", 100, 0.01, shift_id=a)
    store.add_budget("developer:task-a", 400, 0.04, shift_id=a)
    store.add_budget("researcher", 50, 0.005)          # legacy old-loop row (no shift)

    roll = {x["role"]: x for x in timesheets.by_agent(store)}
    assert roll["developer"]["tokens"] == 400 and roll["developer"]["engagements"] == 1
    assert roll["conductor"]["engagements"] == 1
    assert roll["researcher"]["tokens"] == 50          # legacy IS in the all-time rollup


# -- clocktime: per-shift WALL-CLOCK duration, the time counterpart of per-shift token spend ------

def test_duration_seconds_is_wall_clock_and_none_on_missing_or_bad():
    """The canonical clocktime metric: seconds between two store.now_iso() timestamps, None when
    either is missing/unparseable (a still-running or crashed shift has no ended_at), clamped >= 0."""
    d = timesheets.duration_seconds
    assert d("2026-07-06T10:00:00.000000Z", "2026-07-06T10:05:00.000000Z") == 300.0
    assert d("2026-07-06T10:00:00.000000Z", None) is None       # still running / crashed → no ended_at
    assert d("2026-07-06T10:00:00.000000Z", "") is None
    assert d("not-a-timestamp", "2026-07-06T10:05:00.000000Z") is None
    assert d(None, None) is None
    # clock skew / reversed order clamps at 0 — the metric is never negative
    assert d("2026-07-06T10:05:00.000000Z", "2026-07-06T10:00:00.000000Z") == 0.0


def test_shift_clock_reports_per_shift_wall_time_newest_first(store):
    """shift_clock rolls each shift's started_at→ended_at wall-clock, newest-first (mirrors
    list_shifts). A shift with no ended_at (running/crashed) → seconds None, running True."""
    a = store.start_shift(token_budget=1)
    store.end_shift(a, status="completed", tokens_used=100)
    store._exec("UPDATE shifts SET started_at=?, ended_at=? WHERE id=?",
                ("2026-07-06T10:00:00.000000Z", "2026-07-06T10:05:00.000000Z", a))
    b = store.start_shift(token_budget=1)                        # still running: no ended_at
    store._exec("UPDATE shifts SET started_at=? WHERE id=?",
                ("2026-07-06T11:00:00.000000Z", b))

    rows = timesheets.shift_clock(store)
    assert [r["shift"] for r in rows] == [b, a]                  # newest-first
    running, done = rows[0], rows[1]
    assert running["running"] is True and running["seconds"] is None
    assert done["running"] is False and done["seconds"] == 300.0 and done["status"] == "completed"


def test_cmd_timesheet_prints_per_shift_clock(store, capsys):
    """The CLI surfaces per-shift wall-clock time alongside the per-engagement rollup."""
    from factory.orchestrator.orchestrator import cmd_timesheet
    a = store.start_shift(token_budget=1)
    store.add_budget("conductor", 10, 0.0, shift_id=a, seconds=5, notes="shift lead")
    store.end_shift(a, status="completed", tokens_used=10)
    store._exec("UPDATE shifts SET started_at=?, ended_at=? WHERE id=?",
                ("2026-07-06T10:00:00.000000Z", "2026-07-06T10:05:00.000000Z", a))

    cmd_timesheet(store)
    out = capsys.readouterr().out
    assert "per-shift clock" in out
    assert "5m 0s" in out                                        # the shift's wall-clock duration
