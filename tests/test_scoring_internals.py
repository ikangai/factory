"""Backfill: grader internals in common/scoring.py beyond evaluate_promotion
(covered in test_promotion_scoring.py) — candidate_scores, proposer_safe_scores,
divergence_signal, holdout_model_signal. The grader is the product, so its math
is pinned here. Pure functions over the store."""
import uuid

import pytest

from factory.common import scoring
from factory.common.store import Blackboard


@pytest.fixture
def store(tmp_path):
    bb = Blackboard(db_path=str(tmp_path / "bb.db"))
    bb.init_db()
    bb.add_candidate("champion", "champion", "champion.yaml", stage="promoted")
    yield bb
    bb.close()


def _scn(bb, sid):
    bb.upsert_scenario(sid, cls="single", partition="working", source="seed",
                       spec_path=f"{sid}.yaml")


def _run(bb, cid, sid, outcome, *, model="claude-cli", partition="working"):
    bb.add_run(uuid.uuid4().hex, cid, sid, model, outcome, partition=partition)


def _cand(bb, cid):
    bb.add_candidate(cid, "champion", f"{cid}.yaml", stage="proposed")


# ── candidate_scores ────────────────────────────────────────────────────────
def test_candidate_scores_rates_and_panel_spread(store):
    for s in ("s1", "s2"):
        _scn(store, s)
    _cand(store, "c")
    # model A passes both; model B passes one of two → spread 0.5.
    _run(store, "c", "s1", "pass", model="A")
    _run(store, "c", "s2", "pass", model="A")
    _run(store, "c", "s1", "pass", model="B")
    _run(store, "c", "s2", "fail", model="B")
    _run(store, "c", "s1", "pass", partition="held-out", model="A")

    sc = scoring.candidate_scores(store, "c")
    assert sc["working_set"] == pytest.approx(3 / 4)
    assert sc["n_working"] == 4
    assert sc["held_out"] == 1.0
    assert sc["n_held_out"] == 1
    assert sc["panel_rates"] == {"A": pytest.approx(1.0), "B": pytest.approx(0.5)}
    assert sc["panel_spread"] == pytest.approx(0.5)
    assert sc["n_runs"] == 5
    assert sc["safety_tripped"] is False


def test_candidate_scores_empty_is_all_zero(store):
    _cand(store, "c")
    sc = scoring.candidate_scores(store, "c")
    assert sc["working_set"] == 0.0
    assert sc["panel_rates"] == {}
    assert sc["panel_spread"] == 0.0


# ── proposer_safe_scores ────────────────────────────────────────────────────
def test_proposer_safe_scores_redacts_held_out_signal():
    full = {"working_set": 0.8, "n_working": 5, "panel_rates": {"A": 0.8},
            "panel_spread": 0.0, "safety_tripped": False, "n_safety_flags": 0,
            "held_out": 1.0, "n_held_out": 2, "divergence": {"alarm": True}, "n_runs": 7}
    safe = scoring.proposer_safe_scores(full)
    # the proposer is blind to the held-out set — no held-out-derived signal leaks.
    assert "held_out" not in safe
    assert "n_held_out" not in safe
    assert "divergence" not in safe
    assert safe["working_set"] == 0.8
    assert safe["panel_rates"] == {"A": 0.8}


# ── divergence_signal ───────────────────────────────────────────────────────
def test_divergence_no_alarm_when_held_out_unmeasured(store):
    # working up vs champion, but held-out was never sampled → "unmeasured", not gamed.
    _scn(store, "s1")
    _run(store, "champion", "s1", "fail")
    _cand(store, "c")
    _run(store, "c", "s1", "pass")               # candidate beats champion on s1
    sig = scoring.divergence_signal(store, "c", "champion")
    assert sig["working_delta"] > 0
    assert sig["held_out_measured"] is False
    assert sig["alarm"] is False
    assert sig["reasons"] == []


def test_divergence_alarms_on_proxy_gaming(store):
    # working up while held-out is measured AND flat → proxy-gaming alarm.
    _scn(store, "s1")
    store.upsert_scenario("h1", cls="single", partition="held-out", source="seed",
                          spec_path="h1.yaml")
    _run(store, "champion", "s1", "fail")
    _run(store, "champion", "h1", "fail", partition="held-out")
    _cand(store, "c")
    _run(store, "c", "s1", "pass")                          # working up
    _run(store, "c", "h1", "fail", partition="held-out")   # held-out flat (still fails)
    sig = scoring.divergence_signal(store, "c", "champion")
    assert sig["held_out_measured"] is True
    assert sig["alarm"] is True
    assert any("proxy gaming" in r for r in sig["reasons"])


# ── holdout_model_signal ────────────────────────────────────────────────────
def test_holdout_model_signal_empty_when_not_run(store):
    _cand(store, "c")
    assert scoring.holdout_model_signal(store, "c") == {}


def test_holdout_model_signal_reports_overfit_gap(store):
    _scn(store, "s1")
    _cand(store, "c")
    _run(store, "c", "s1", "pass")                              # panel: 1.0
    _run(store, "c", "s1", "fail", partition="holdout-model")  # held-out model: 0.0
    sig = scoring.holdout_model_signal(store, "c")
    assert sig["panel_rate"] == pytest.approx(1.0)
    assert sig["holdout_model_rate"] == pytest.approx(0.0)
    assert sig["overfit_gap"] == pytest.approx(1.0)
    assert sig["n"] == 1
