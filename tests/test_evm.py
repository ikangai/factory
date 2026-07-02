"""Agent-adapted EVM (reporting/evm.py): value unit = planned tokens. PV = Σ budget over
non-dropped milestones; EV = delivered budgets + partial credit for active ones; AC = ledgered
developer spend attributed via tasks.milestone_id; conductor/research spend = overhead (never
smeared across milestones); ac_tokens + overhead = the whole ledger (conservation)."""
import pytest

from factory.reporting import evm


def test_evm_pv_ev_ac_cpi_over_milestones(store):
    sid = store.start_shift(token_budget=1)

    # Milestone A: delivered, one linked done task with 40k ledgered.
    a = store.add_milestone("A: recovery", budget_tokens=100_000, planned_order=1)
    store.add_task("task-a1", "slice a1", source="research")
    store.set_task_milestone("task-a1", a)
    store.set_task_status("task-a1", "done", result="sha1")
    store.add_budget("developer:task-a1", 40_000, 0.40, shift_id=sid, notes="merged")
    store.set_milestone_status(a, "delivered")

    # Milestone B: active, 1 of 2 linked tasks done, 30k ledgered (no est → done/total credit).
    b = store.add_milestone("B: eval", budget_tokens=200_000, planned_order=2)
    store.set_milestone_status(b, "active")
    store.add_task("task-b1", "slice b1", source="research")
    store.add_task("task-b2", "slice b2", source="research")
    store.set_task_milestone("task-b1", b)
    store.set_task_milestone("task-b2", b)
    store.set_task_status("task-b1", "done", result="sha2")
    store.add_budget("developer:task-b1", 30_000, 0.30, shift_id=sid, notes="merged")

    # Conductor overhead — attributed to no milestone.
    store.add_budget("conductor", 10_000, 0.10, shift_id=sid, notes="shift lead")

    e = evm.evm(store)
    assert e["pv"] == 300_000
    assert e["ev"] == 200_000                       # 100k delivered + 200k * (1/2)
    assert e["ac_tokens"] == 70_000
    assert e["overhead_tokens"] == 10_000           # 80k ledger - 70k attributed
    assert e["ac_tokens"] + e["overhead_tokens"] == 80_000      # conservation
    assert e["cpi"] == pytest.approx(200_000 / 70_000, rel=1e-3)
    assert e["percent_complete"] == pytest.approx(200_000 / 300_000, rel=1e-3)

    ms = {m["id"]: m for m in e["milestones"]}
    assert ms[a]["pv"] == 100_000 and ms[a]["ev"] == 100_000 and ms[a]["ac_tokens"] == 40_000
    assert ms[b]["pv"] == 200_000 and ms[b]["ev"] == 100_000 and ms[b]["ac_tokens"] == 30_000
    assert ms[b]["progress"] == {"done": 1, "total": 2}

    # Cumulative spend-per-shift series (all 80k landed in the one shift).
    assert e["series"]["shift_ids"] == [sid]
    assert e["series"]["ac_cumulative"] == [80_000]


def test_evm_est_weighted_partial_credit(store):
    sid = store.start_shift(token_budget=1)
    c = store.add_milestone("C: mixed", budget_tokens=100_000)
    store.set_milestone_status(c, "active")
    store.add_task("task-c1", "big slice", source="research")
    store.add_task("task-c2", "small slice", source="research")
    store.set_task_milestone("task-c1", c)
    store.set_task_milestone("task-c2", c)
    store.set_task_estimate("task-c1", 30_000)
    store.set_task_estimate("task-c2", 10_000)
    store.set_task_status("task-c1", "done", result="x")
    store.add_budget("developer:task-c1", 25_000, 0.0, shift_id=sid, notes="merged")

    e = evm.evm(store)
    m = {x["id"]: x for x in e["milestones"]}[c]
    # est-weighted: done-est 30k / all-est 40k = 0.75 → EV = 100k * 0.75, NOT done/total (0.5).
    assert m["ev"] == 75_000

    est = {r["task"]: r for r in e["estimates"]}
    assert est["task-c1"] == {"task": "task-c1", "title": "big slice", "est": 30_000, "actual": 25_000}
    assert "task-c2" not in est          # no actual yet → not an est-vs-actual data point


def test_evm_partial_estimate_coverage_falls_back_to_done_total(store):
    """Review #1: est-weighting must require EVERY linked task to be estimated. A milestone where
    only some tasks carry an estimate uses plain done/total — else one estimated+done task would
    dominate the fraction and report inflated EV."""
    sid = store.start_shift(token_budget=1)
    m = store.add_milestone("mixed coverage", budget_tokens=100_000)
    store.set_milestone_status(m, "active")
    for tid, est, status in [("p1", 30_000, "done"), ("p2", 0, "open"), ("p3", 0, "open")]:
        store.add_task(tid, tid, source="research")
        store.set_task_milestone(tid, m)
        if est:
            store.set_task_estimate(tid, est)
        if status == "done":
            store.set_task_status(tid, "done", result="x")

    row = {x["id"]: x for x in evm.evm(store)["milestones"]}[m]
    # 1 of 3 done, partial estimate coverage → done/total (0.33), NOT est-weighted 100%
    assert row["ev"] == round(100_000 * (1 / 3))


def test_evm_series_covers_all_shifts_beyond_the_50_window(store):
    """Review #3: the cumulative burn series must span the WHOLE history, not list_shifts()'s
    last-50 window (which would reset the running total to 0 at the window edge)."""
    for _ in range(52):
        sid = store.start_shift(token_budget=1)
        store.add_budget("conductor", 100, 0.0, shift_id=sid)
    series = evm.evm(store)["series"]
    assert len(series["shift_ids"]) == 52
    assert series["ac_cumulative"][0] == 100 and series["ac_cumulative"][-1] == 5200


def test_evm_dropped_milestone_excluded_from_baseline(store):
    sid = store.start_shift(token_budget=1)
    d = store.add_milestone("D: abandoned", budget_tokens=500_000)
    store.set_milestone_status(d, "dropped")
    store.add_task("task-d1", "abandoned slice", source="research")
    store.set_task_milestone("task-d1", d)
    store.add_budget("developer:task-d1", 20_000, 0.0, shift_id=sid, notes="merged")

    e = evm.evm(store)
    assert e["pv"] == 0                              # dropped milestone is off the baseline
    assert [m["id"] for m in e["milestones"]] == []  # not rendered
    assert e["ac_tokens"] == 0                       # its spend is not milestone-attributed
    assert e["overhead_tokens"] == 20_000           # it falls into overhead (conservation holds)
    assert e["cpi"] is None                          # EV/AC undefined with no attributed spend
