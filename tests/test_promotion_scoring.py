"""Promotion eligibility must be LIKE-FOR-LIKE (factory bug #65).

`scoring.evaluate_promotion` decides whether a candidate beats the champion. The
bug: it compared the candidate's score on its (possibly SCOPED) eval set against
the champion's STALE FULL-HISTORY aggregate — a different scenario set, and
pooling the champion's pre-fix runs. A scoped round (candidate on n=5) vs the
champion's whole history (n=11) manufactured a positive `working_delta` and
FALSELY cleared the gate, even when, on the SAME scenarios at their CURRENT
state, candidate == champion (zero real improvement). cand-fb0504f94b sat in
`awaiting_gate` as a false positive because of this.

The comparison must be: candidate vs champion on the SHARED scenario set, each at
its LATEST outcome per (scenario, model) — never the pooled history.
"""
import uuid

import pytest

from factory.common import scoring
from factory.common.store import Blackboard

CFG = {"promotion": {}}  # working_set_min_delta 0.0, regression_tolerance 0.0


@pytest.fixture
def store(tmp_path):
    bb = Blackboard(db_path=str(tmp_path / "bb.db"))
    bb.init_db()
    bb.add_candidate("champion", "champion", "champion.yaml", stage="promoted")
    yield bb
    bb.close()


def _scenario(bb, sid):
    bb.upsert_scenario(sid, cls="single", partition="working",
                       source="seed", spec_path=f"{sid}.yaml")


def _run(bb, candidate_id, sid, outcome, *, model="claude-cli", partition="working"):
    # each add_run commits → monotonically increasing created_at, so repeated
    # (scenario, model) cells resolve their "latest" run deterministically.
    bb.add_run(uuid.uuid4().hex, candidate_id, sid, model, outcome, partition=partition)


def _candidate(bb, cid):
    bb.add_candidate(cid, "champion", f"{cid}.yaml", stage="proposed")


def test_scoped_round_does_not_fake_eligibility_from_stale_aggregate(store):
    """The repro: the champion's pooled history (scenarios the candidate was never
    run on + a stale pre-fix fail) sits below the candidate's scoped rate, but on
    the SHARED scenarios at current state they are equal → NOT eligible."""
    for sid in ("s1", "s2", "s3"):
        _scenario(store, sid)
    # champion CURRENT state on the shared set: s1 pass, s2 fail.
    _run(store, "champion", "s1", "fail")   # stale pre-fix run...
    _run(store, "champion", "s1", "pass")   # ...superseded — latest is pass
    _run(store, "champion", "s2", "fail")
    _run(store, "champion", "s3", "fail")   # champion-only scenario (not in cand set)
    # candidate evaluated ONLY on the shared {s1, s2}: s1 pass, s2 fail.
    _candidate(store, "cand-x")
    _run(store, "cand-x", "s1", "pass")
    _run(store, "cand-x", "s2", "fail")

    promo = scoring.evaluate_promotion(store, "cand-x", "champion", CFG)

    # like-for-like on {s1, s2}: champ 0.5 == cand 0.5 → no real improvement.
    assert promo["working_delta"] == 0.0
    assert promo["beats_working"] is False
    assert promo["eligible"] is False
    # comparison was scoped to the 2 shared scenarios, not the champion's n=4 history.
    assert promo["n_compared"] == 2
    assert promo["panel_deltas"] == {"claude-cli": 0.0}
    assert promo["held_delta"] == 0.0


def test_champion_stale_fail_uses_latest_outcome(store):
    """A scenario the champion has SINCE fixed (fail→pass) must compare on its
    latest (pass), so a candidate that merely matches it isn't credited."""
    _scenario(store, "s1")
    _run(store, "champion", "s1", "fail")   # older
    _run(store, "champion", "s1", "pass")   # newer → current capability
    _candidate(store, "cand-y")
    _run(store, "cand-y", "s1", "pass")

    promo = scoring.evaluate_promotion(store, "cand-y", "champion", CFG)
    assert promo["working_delta"] == 0.0
    assert promo["eligible"] is False


def test_genuine_like_for_like_improvement_is_eligible(store):
    """Guard against over-correction: when the candidate passes a scenario the
    champion's LATEST run still fails, the delta is real → eligible."""
    for sid in ("s1", "s2"):
        _scenario(store, sid)
    _run(store, "champion", "s1", "pass")
    _run(store, "champion", "s2", "fail")   # champion still fails s2
    _candidate(store, "cand-z")
    _run(store, "cand-z", "s1", "pass")
    _run(store, "cand-z", "s2", "pass")     # candidate fixes s2

    promo = scoring.evaluate_promotion(store, "cand-z", "champion", CFG)
    assert promo["working_delta"] == pytest.approx(0.5)
    assert promo["beats_working"] is True
    assert promo["eligible"] is True
    assert promo["n_compared"] == 2
    assert promo["panel_deltas"] == {"claude-cli": pytest.approx(0.5)}
