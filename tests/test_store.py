"""Characterization tests for the SQLite access layer (common/store.py).

These exercise the `Blackboard` CRUD surface against an isolated temp DB. They
never touch the real store — every test goes through the `bb` fixture, which
builds `Blackboard(db_path=tmp_path / "bb.db")` and calls `.init_db()`.

Rows come back as plain dicts (sqlite3.Row -> dict in the access layer), so all
assertions index by column name.
"""
from __future__ import annotations

import sqlite3

import pytest

from factory.common.store import Blackboard


@pytest.fixture()
def bb(tmp_path):
    """An isolated, schema-initialized blackboard backed by a temp-dir DB file."""
    board = Blackboard(db_path=str(tmp_path / "bb.db"))
    board.init_db()
    try:
        yield board
    finally:
        board.close()


# -- small helpers to satisfy FK constraints --------------------------------

def _seed_candidate(bb, id="cand-1", parent="champion",
                    spec_path="specs/candidates/cand-1.yaml", **kw):
    bb.add_candidate(id, parent, spec_path, **kw)
    return id


def _seed_scenario(bb, id="scn-1", *, cls="single", partition="working",
                   source="seed", spec_path="scenarios/working/scn-1.yaml",
                   goal="do the thing"):
    bb.upsert_scenario(id, cls=cls, partition=partition, source=source,
                       spec_path=spec_path, goal=goal)
    return id


def _seed_run(bb, id="run-1", candidate_id="cand-1", scenario_id="scn-1",
              model="panel-a", outcome="pass", **kw):
    bb.add_run(id, candidate_id, scenario_id, model, outcome, **kw)
    return id


# -- candidates -------------------------------------------------------------

def test_add_and_get_candidate_round_trip(bb):
    bb.add_candidate("cand-1", "champion", "specs/candidates/cand-1.yaml",
                     change_summary="tighten the open block",
                     diff={"added": ["x"]})
    row = bb.get_candidate("cand-1")
    assert row is not None
    assert row["id"] == "cand-1"
    assert row["parent"] == "champion"
    assert row["spec_path"] == "specs/candidates/cand-1.yaml"
    assert row["stage"] == "proposed"  # default
    assert row["change_summary"] == "tighten the open block"
    # diff is stored as JSON text
    assert row["diff_json"] == '{"added": ["x"]}'
    # scores default to empty JSON object
    assert row["scores_json"] == "{}"
    assert row["created_at"]  # non-empty ISO timestamp


def test_get_candidate_missing_returns_none(bb):
    assert bb.get_candidate("nope") is None


def test_list_candidates_all_and_by_stage(bb):
    bb.add_candidate("c1", "champion", "p1", stage="proposed")
    bb.add_candidate("c2", "c1", "p2", stage="scored")
    bb.add_candidate("c3", "c2", "p3", stage="scored")

    all_ids = {c["id"] for c in bb.list_candidates()}
    assert all_ids == {"c1", "c2", "c3"}

    scored = bb.list_candidates(stage="scored")
    assert {c["id"] for c in scored} == {"c2", "c3"}

    assert bb.list_candidates(stage="promoted") == []


def test_set_stage_updates_candidate(bb):
    _seed_candidate(bb)
    bb.set_stage("cand-1", "evaluating")
    assert bb.get_candidate("cand-1")["stage"] == "evaluating"


def test_set_candidate_scores_serializes_json(bb):
    _seed_candidate(bb)
    bb.set_candidate_scores("cand-1", {"divergence": 0.5, "n": 3})
    row = bb.get_candidate("cand-1")
    assert row["scores_json"] == '{"divergence": 0.5, "n": 3}'


# -- scenarios --------------------------------------------------------------

def test_upsert_and_get_scenario_round_trip(bb):
    bb.upsert_scenario("scn-1", cls="single", partition="working",
                       source="seed", spec_path="scenarios/working/scn-1.yaml",
                       goal="resolve the bug", snapshot="img:base",
                       check_path="checks/scn-1.py")
    row = bb.get_scenario("scn-1")
    assert row["id"] == "scn-1"
    assert row["class"] == "single"
    assert row["partition"] == "working"
    assert row["source"] == "seed"
    assert row["goal"] == "resolve the bug"
    assert row["snapshot"] == "img:base"
    assert row["check_path"] == "checks/scn-1.py"
    assert row["active"] == 1
    assert row["leakage_count"] == 0


def test_upsert_scenario_preserves_leakage_on_overwrite(bb):
    _seed_scenario(bb, id="scn-1", goal="v1")
    bb.increment_leakage("scn-1", by=3)
    # Re-upsert with a new goal; leakage_count must be carried over, not reset.
    bb.upsert_scenario("scn-1", cls="single", partition="working",
                       source="seed", spec_path="scenarios/working/scn-1.yaml",
                       goal="v2")
    row = bb.get_scenario("scn-1")
    assert row["goal"] == "v2"
    assert row["leakage_count"] == 3


def test_list_scenarios_partition_filter(bb):
    _seed_scenario(bb, id="w1", partition="working")
    _seed_scenario(bb, id="h1", partition="held-out",
                   spec_path="scenarios/held-out/h1.yaml")

    working = bb.list_scenarios(partition="working")
    assert {s["id"] for s in working} == {"w1"}

    held = bb.list_scenarios(partition="held-out")
    assert {s["id"] for s in held} == {"h1"}

    both = bb.list_scenarios()  # no partition filter
    assert {s["id"] for s in both} == {"w1", "h1"}


def test_list_scenarios_active_only_excludes_retired(bb):
    _seed_scenario(bb, id="alive")
    _seed_scenario(bb, id="dead", spec_path="scenarios/working/dead.yaml")
    bb.retire_scenario("dead")

    active = bb.list_scenarios(active_only=True)
    assert {s["id"] for s in active} == {"alive"}

    everything = bb.list_scenarios(active_only=False)
    assert {s["id"] for s in everything} == {"alive", "dead"}
    # retire flips the active flag to 0
    assert bb.get_scenario("dead")["active"] == 0


def test_increment_leakage_accumulates(bb):
    _seed_scenario(bb, id="scn-1")
    bb.increment_leakage("scn-1")          # default by=1
    bb.increment_leakage("scn-1", by=4)
    assert bb.get_scenario("scn-1")["leakage_count"] == 5


def test_scenario_class_check_constraint(bb):
    with pytest.raises(sqlite3.IntegrityError):
        bb.upsert_scenario("bad", cls="not-a-class", partition="working",
                           source="seed", spec_path="x.yaml")


# -- runs -------------------------------------------------------------------

def test_add_and_get_run_round_trip(bb):
    _seed_candidate(bb)
    _seed_scenario(bb)
    bb.add_run("run-1", "cand-1", "scn-1", "panel-a", "pass",
               evidence_path="logs/runs/run-1/", budget_used=120,
               clive_claim="I fixed it", check_json={"ok": True},
               duration_s=2.5)
    row = bb.get_run("run-1")
    assert row["id"] == "run-1"
    assert row["candidate_id"] == "cand-1"
    assert row["scenario_id"] == "scn-1"
    assert row["model"] == "panel-a"
    assert row["outcome"] == "pass"
    assert row["evidence_path"] == "logs/runs/run-1/"
    assert row["budget_used"] == 120
    assert row["partition"] == "working"  # default
    assert row["clive_claim"] == "I fixed it"
    assert row["check_json"] == '{"ok": true}'
    assert row["duration_s"] == 2.5


def test_runs_for_candidate_ordered_by_created_at(bb):
    _seed_candidate(bb)
    _seed_scenario(bb, id="s1")
    _seed_scenario(bb, id="s2", spec_path="scenarios/working/s2.yaml")
    _seed_scenario(bb, id="s3", spec_path="scenarios/working/s3.yaml")
    # Insert out of timestamp order; runs_for_candidate must return ascending.
    bb.add_run("rA", "cand-1", "s1", "m", "pass")
    bb.add_run("rB", "cand-1", "s2", "m", "fail")
    bb.add_run("rC", "cand-1", "s3", "m", "pass")
    runs = bb.runs_for_candidate("cand-1")
    ids = [r["id"] for r in runs]
    # ascending by created_at == insertion order here
    assert ids == ["rA", "rB", "rC"]
    times = [r["created_at"] for r in runs]
    assert times == sorted(times)


def test_runs_for_candidate_only_that_candidate(bb):
    _seed_candidate(bb, id="cand-1")
    _seed_candidate(bb, id="cand-2", spec_path="specs/candidates/cand-2.yaml")
    _seed_scenario(bb)
    bb.add_run("r1", "cand-1", "scn-1", "m", "pass")
    bb.add_run("r2", "cand-2", "scn-1", "m", "pass")
    assert [r["id"] for r in bb.runs_for_candidate("cand-1")] == ["r1"]


def test_all_runs_returns_everything(bb):
    _seed_candidate(bb)
    _seed_scenario(bb)
    bb.add_run("r1", "cand-1", "scn-1", "m", "pass")
    bb.add_run("r2", "cand-1", "scn-1", "m", "fail")
    assert {r["id"] for r in bb.all_runs()} == {"r1", "r2"}


def test_recent_failures_filters_and_joins_goal(bb):
    _seed_candidate(bb)
    _seed_scenario(bb, id="wk", partition="working", goal="working goal")
    _seed_scenario(bb, id="ho", partition="held-out", goal="held-out goal",
                   spec_path="scenarios/held-out/ho.yaml")

    # Should surface: fail/error/blocked on the working partition.
    bb.add_run("f1", "cand-1", "wk", "m", "fail", partition="working")
    bb.add_run("e1", "cand-1", "wk", "m", "error", partition="working")
    bb.add_run("b1", "cand-1", "wk", "m", "blocked", partition="working")
    # Should be excluded: a passing working run.
    bb.add_run("p1", "cand-1", "wk", "m", "pass", partition="working")
    # Should be excluded: budget_exceeded is not in the failure set.
    bb.add_run("x1", "cand-1", "wk", "m", "budget_exceeded", partition="working")
    # Should be excluded: a failing run but on the held-out partition.
    bb.add_run("h1", "cand-1", "ho", "m", "fail", partition="held-out")

    fails = bb.recent_failures()
    ids = {r["id"] for r in fails}
    assert ids == {"f1", "e1", "b1"}
    # join surfaces the scenario goal column
    assert all(r["scenario_goal"] == "working goal" for r in fails)
    # ordered DESC by created_at (most recent first)
    times = [r["created_at"] for r in fails]
    assert times == sorted(times, reverse=True)


def test_recent_failures_respects_limit(bb):
    _seed_candidate(bb)
    _seed_scenario(bb, id="wk", partition="working")
    for i in range(5):
        bb.add_run(f"f{i}", "cand-1", "wk", "m", "fail", partition="working")
    assert len(bb.recent_failures(limit=2)) == 2


# -- champion ---------------------------------------------------------------

def test_set_and_get_champion(bb):
    assert bb.get_champion() is None
    bb.set_champion("champ-1", "specs/champion.yaml", scores={"div": 0.9})
    champ = bb.get_champion()
    assert champ["id"] == "champ-1"
    assert champ["spec_path"] == "specs/champion.yaml"
    assert champ["scores_json"] == '{"div": 0.9}'
    assert champ["promoted_at"]


def test_set_champion_none_scores_defaults_empty(bb):
    bb.set_champion("champ-1", "specs/champion.yaml")
    assert bb.get_champion()["scores_json"] == "{}"


def test_get_champion_returns_most_recent(bb):
    bb.set_champion("champ-1", "specs/champion.yaml")
    bb.set_champion("champ-2", "specs/champion-2.yaml")
    # ordered by promoted_at DESC, LIMIT 1 -> the later insert wins
    assert bb.get_champion()["id"] == "champ-2"


# -- foreign keys -----------------------------------------------------------

def test_run_with_missing_scenario_raises_fk(bb):
    _seed_candidate(bb)  # candidate exists, scenario does not
    with pytest.raises(sqlite3.IntegrityError):
        bb.add_run("run-x", "cand-1", "ghost-scenario", "m", "pass")


def test_run_with_missing_candidate_raises_fk(bb):
    _seed_scenario(bb)  # scenario exists, candidate does not
    with pytest.raises(sqlite3.IntegrityError):
        bb.add_run("run-x", "ghost-candidate", "scn-1", "m", "pass")


def test_run_invalid_outcome_check_constraint(bb):
    _seed_candidate(bb)
    _seed_scenario(bb)
    with pytest.raises(sqlite3.IntegrityError):
        bb.add_run("run-x", "cand-1", "scn-1", "m", "not-an-outcome")


# -- judge notes ------------------------------------------------------------

def test_add_and_get_judge_note(bb):
    _seed_candidate(bb)
    _seed_scenario(bb)
    _seed_run(bb)
    bb.add_judge_note("run-1", {"semantic_pass": True, "notes": "looks good"})
    note = bb.judge_note("run-1")
    assert note["run_id"] == "run-1"
    assert note["flags_json"] == '{"semantic_pass": true, "notes": "looks good"}'
    assert note["created_at"]


def test_judge_note_missing_returns_none(bb):
    assert bb.judge_note("nope") is None


def test_add_judge_note_replaces_existing(bb):
    _seed_candidate(bb)
    _seed_scenario(bb)
    _seed_run(bb)
    bb.add_judge_note("run-1", {"v": 1})
    bb.add_judge_note("run-1", {"v": 2})  # INSERT OR REPLACE on PK run_id
    assert bb.judge_note("run-1")["flags_json"] == '{"v": 2}'


# -- promotions -------------------------------------------------------------

def test_add_and_list_promotions(bb):
    _seed_candidate(bb)
    bb.add_promotion("cand-1", "promote", "operator-1", rationale="ships clean")
    rows = bb.promotions()
    assert len(rows) == 1
    assert rows[0]["candidate_id"] == "cand-1"
    assert rows[0]["decision"] == "promote"
    assert rows[0]["operator"] == "operator-1"
    assert rows[0]["rationale"] == "ships clean"
    assert rows[0]["decided_at"]


def test_promotions_empty_initially(bb):
    assert bb.promotions() == []


# -- budget -----------------------------------------------------------------

def test_budget_totals_empty(bb):
    totals = bb.budget_totals()
    assert totals["tokens"] == 0
    assert totals["cost"] == 0


def test_add_budget_and_totals(bb):
    bb.add_budget("proposer", 100, cost=0.5, notes="draft")
    bb.add_budget("judge", 50, cost=0.25)
    totals = bb.budget_totals()
    assert totals["tokens"] == 150
    assert totals["cost"] == 0.75


def test_budget_entries_ordered_by_at(bb):
    bb.add_budget("proposer", 10)
    bb.add_budget("judge", 20)
    entries = bb.budget_entries()
    assert [e["role_or_run"] for e in entries] == ["proposer", "judge"]
    assert [e["tokens"] for e in entries] == [10, 20]


def test_budget_ledger_shift_attribution(store):
    store.add_budget("conductor", 100, 0.01, shift_id=7, seconds=12.5)
    store.add_budget("developer:task-ab", 400, 0.04, shift_id=7, profile="python-dev")
    store.add_budget("researcher", 50, 0.005)              # no shift — old-loop style still works
    spend = store.shift_spend(7)
    assert spend == {"tokens": 500, "cost": 0.05, "seconds": 12.5}
    assert store.shift_spend(99) == {"tokens": 0, "cost": 0.0, "seconds": 0.0}
    row = store._one("SELECT profile FROM budget_ledger WHERE role_or_run='developer:task-ab'")
    assert row["profile"] == "python-dev"


# -- safety flags -----------------------------------------------------------

def test_add_safety_flag_and_query_for_candidate(bb):
    _seed_candidate(bb)
    _seed_scenario(bb)
    _seed_run(bb)
    bb.add_safety_flag("run-1", "out_of_scope_path", "wrote /etc", "high")
    flags = bb.safety_flags_for_candidate("cand-1")
    assert len(flags) == 1
    assert flags[0]["kind"] == "out_of_scope_path"
    assert flags[0]["detail"] == "wrote /etc"
    assert flags[0]["severity"] == "high"
    # join surfaces run context columns
    assert flags[0]["scenario_id"] == "scn-1"
    assert flags[0]["model"] == "panel-a"


def test_all_safety_flags_includes_candidate_context(bb):
    _seed_candidate(bb)
    _seed_scenario(bb)
    _seed_run(bb)
    bb.add_safety_flag("run-1", "unrequested_port", "opened 8080", "medium")
    rows = bb.all_safety_flags()
    assert len(rows) == 1
    assert rows[0]["candidate_id"] == "cand-1"
    assert rows[0]["kind"] == "unrequested_port"


def test_safety_flag_invalid_severity_check_constraint(bb):
    _seed_candidate(bb)
    _seed_scenario(bb)
    _seed_run(bb)
    with pytest.raises(sqlite3.IntegrityError):
        bb.add_safety_flag("run-1", "kind", "detail", "catastrophic")


# -- context manager --------------------------------------------------------

def test_context_manager_closes_cleanly(tmp_path):
    path = str(tmp_path / "ctx.db")
    with Blackboard(db_path=path) as board:
        board.init_db()
        board.add_candidate("c", "champion", "p")
        assert board.get_candidate("c")["id"] == "c"
    # Re-open the same file: the data persisted past the context exit.
    reopened = Blackboard(db_path=path)
    try:
        assert reopened.get_candidate("c") is not None
    finally:
        reopened.close()
