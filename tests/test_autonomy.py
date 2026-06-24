"""Autonomy harness (mission axis C): the unattended loop is a SEQUENCER over the
existing roles, and it NEVER promotes.

Hermetic — no LLM, no network, no subprocess: we monkeypatch the four round steps
on the orchestrator module (cmd_research / cmd_baseline / cmd_propose / cmd_round)
so the loop only exercises its own sequencing + stop logic. A real temp-file
Blackboard backs the candidate/budget state the loop reads.

A "promotion spy" wraps the store's champion-setter and the candidate stage-setter
to FAIL the test if the loop ever promotes (set the champion to a candidate, or
move a candidate to stage='promoted'). Baseline legitimately re-stamps the
champion's OWN scores (same id, same spec) — that is not a promotion and is
allowed; promoting a *candidate* is what the spy forbids.
"""
import os

import pytest

from factory.common.store import Blackboard
from factory.orchestrator import autonomy
from factory.orchestrator import orchestrator as orch

CHAMP = orch.CHAMPION_ID


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def store(tmp_path, monkeypatch):
    # Isolate the research staging dir to tmp so the loop's brief-counting (and the
    # fake research step's marker files) never touch the real repo.
    staging = tmp_path / "research_staging"
    staging.mkdir()
    monkeypatch.setattr("factory.common.paths.RESEARCH_STAGING_DIR", str(staging))
    db = str(tmp_path / "factory.db")
    with Blackboard(db) as bb:
        bb.init_db()
        # Minimal champion + one working scenario so the loop has shape.
        bb.add_candidate(CHAMP, "champion", "champion.yaml",
                         change_summary="(baseline)", stage="promoted")
        bb.set_champion(CHAMP, "champion.yaml", scores={})
        bb.upsert_scenario("s1", cls="single", partition="working",
                           source="seed", spec_path="s1.yaml", goal="do a thing")
        yield bb


def _install_promotion_spy(monkeypatch, store):
    """Fail loudly if the loop ever promotes: (a) sets the champion to a NON-champion
    id or a DIFFERENT spec_path, or (b) moves any candidate to stage='promoted'."""
    real_set_champion = store.set_champion
    real_set_stage = store.set_stage

    def guarded_set_champion(id, spec_path, scores=None):
        if id != CHAMP:
            raise AssertionError(f"PROMOTION DETECTED: set_champion to candidate {id!r}")
        # baseline re-stamps the champion's own scores (same id) — allowed.
        real_set_champion(id, spec_path, scores)

    def guarded_set_stage(id, stage):
        if stage == "promoted" and id != CHAMP:
            raise AssertionError(
                f"PROMOTION DETECTED: candidate {id!r} -> stage 'promoted'")
        real_set_stage(id, stage)

    monkeypatch.setattr(store, "set_champion", guarded_set_champion)
    monkeypatch.setattr(store, "set_stage", guarded_set_stage)
    # There is no promote()/add_promotion() in the loop path; spy on it too so a
    # regression that recorded a promotion decision would also trip the test.
    monkeypatch.setattr(store, "add_promotion",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("PROMOTION DETECTED: add_promotion called")))


def _patch_steps(monkeypatch, *, propose_returns, gate_clears, calls,
                 research_stages=True):
    """Replace the four round steps. `propose_returns` / `gate_clears` are lists
    indexed by round (1-based); missing entries default to None / no clear."""
    def fake_research(s, query=None, **k):
        calls.append(("research", query))
        if research_stages:
            # simulate a brief landing on disk by adding a budget row + file marker;
            # the loop counts disk files, so write one into the staging dir.
            from factory.common import paths
            os.makedirs(paths.RESEARCH_STAGING_DIR, exist_ok=True)
            with open(os.path.join(paths.RESEARCH_STAGING_DIR,
                                   f"rb-{len(calls)}.yaml"), "w") as fh:
                fh.write("id: rb\nstatus: staged\n")

    def fake_baseline(s, *a, **k):
        calls.append(("baseline",))
        s.add_budget("baseline", 100, 0.0, notes="test")

    def fake_propose(s, *a, **k):
        calls.append(("propose",))
        rnd = sum(1 for c in calls if c[0] == "propose")
        cid = propose_returns[rnd - 1] if rnd - 1 < len(propose_returns) else None
        if cid:
            s.add_candidate(cid, CHAMP, f"{cid}.yaml",
                            change_summary="test", stage="proposed")
            s.add_budget("proposer", 200, 0.0, notes="test")
        return cid

    def fake_round(s, cid, *a, **k):
        calls.append(("round", cid))
        rnd = sum(1 for c in calls if c[0] == "round")
        clears = gate_clears[rnd - 1] if rnd - 1 < len(gate_clears) else False
        # Use the store's stage-setter (the spy wraps it) exactly as cmd_round does.
        s.set_stage(cid, "awaiting_gate" if clears else "rejected")
        s.add_budget("reporter", 50, 0.0, notes="test")

    monkeypatch.setattr(orch, "cmd_research", fake_research)
    monkeypatch.setattr(orch, "cmd_baseline", fake_baseline)
    monkeypatch.setattr(orch, "cmd_propose", fake_propose)
    monkeypatch.setattr(orch, "cmd_round", fake_round)


def _arm_governor(store, monkeypatch):
    """Force the gain governor to ARM every round so the loop reaches propose."""
    monkeypatch.setattr(autonomy.triggers, "should_propose",
                        lambda s, cfg: (True, 99, 3))


def _hold_governor(monkeypatch):
    """Force the gain governor to never arm (no champion failures)."""
    monkeypatch.setattr(autonomy.triggers, "should_propose",
                        lambda s, cfg: (False, 0, 3))


# ---------------------------------------------------------------------------
# (a) loops up to max_rounds
# ---------------------------------------------------------------------------
def test_loops_up_to_max_rounds_and_never_promotes(store, monkeypatch):
    _install_promotion_spy(monkeypatch, store)
    _arm_governor(store, monkeypatch)
    calls: list = []
    # Each round proposes a fresh candidate that CLEARS the gate (so no-improvement
    # never trips) → the only stop is max_rounds.
    _patch_steps(monkeypatch, propose_returns=[f"cand-{i}" for i in range(10)],
                 gate_clears=[True] * 10, calls=calls)

    summary = autonomy.cmd_autonomous(store, "improve clive shell driving",
                                      max_rounds=3, do_research=True)

    assert summary["rounds_run"] == 3
    assert summary["stop_reason"] == "max_rounds reached"
    # all three candidates cleared → all sit at the gate for the human
    assert sorted(summary["awaiting_gate"]) == ["cand-0", "cand-1", "cand-2"]
    # we baselined + proposed + rounded each of the 3 rounds
    assert sum(1 for c in calls if c[0] == "baseline") == 3
    assert sum(1 for c in calls if c[0] == "round") == 3


# ---------------------------------------------------------------------------
# (b) early stop: no-improvement
# ---------------------------------------------------------------------------
def test_stops_early_on_no_improvement(store, monkeypatch):
    _install_promotion_spy(monkeypatch, store)
    _arm_governor(store, monkeypatch)
    calls: list = []
    # Governor armed every round, candidate proposed every round, but NONE clears
    # the gate → after no_improvement_rounds (config: 3) consecutive misses it stops.
    _patch_steps(monkeypatch, propose_returns=[f"cand-{i}" for i in range(10)],
                 gate_clears=[False] * 10, calls=calls)

    summary = autonomy.cmd_autonomous(store, "mission", max_rounds=10,
                                      do_research=True)

    assert summary["rounds_run"] == 3  # == no_improvement_rounds from config
    assert "no improvement" in summary["stop_reason"]
    assert summary["awaiting_gate"] == []  # nothing cleared


# ---------------------------------------------------------------------------
# (b) early stop: no-work (governor holds AND research stages nothing new)
# ---------------------------------------------------------------------------
def test_stops_early_on_no_work(store, monkeypatch):
    _install_promotion_spy(monkeypatch, store)
    _hold_governor(monkeypatch)
    calls: list = []
    _patch_steps(monkeypatch, propose_returns=[], gate_clears=[],
                 calls=calls, research_stages=False)

    summary = autonomy.cmd_autonomous(store, "mission", max_rounds=10,
                                      do_research=True)

    assert summary["rounds_run"] == 1
    assert "no work" in summary["stop_reason"]
    assert summary["awaiting_gate"] == []


# ---------------------------------------------------------------------------
# (b) early stop: token_budget
# ---------------------------------------------------------------------------
def test_stops_early_on_token_budget(store, monkeypatch):
    _install_promotion_spy(monkeypatch, store)
    _arm_governor(store, monkeypatch)
    calls: list = []
    # Clears every round (so no-improvement won't fire); each round burns
    # 100(baseline)+200(propose)+50(reporter)=350 tokens. Budget 300 => round 1
    # starts at 0 (< 300), burns to 350, and the end-of-round check (350 >= 300)
    # stops the loop after exactly one round.
    _patch_steps(monkeypatch, propose_returns=[f"cand-{i}" for i in range(10)],
                 gate_clears=[True] * 10, calls=calls)

    summary = autonomy.cmd_autonomous(store, "mission", max_rounds=10,
                                      token_budget=300, do_research=False)

    assert summary["rounds_run"] == 1
    assert "token_budget" in summary["stop_reason"]
    assert summary["tokens_spent"] >= 300


# ---------------------------------------------------------------------------
# (c) the spy itself: a loop that DID promote must be caught
# ---------------------------------------------------------------------------
def test_promotion_spy_actually_catches_promotion(store, monkeypatch):
    _install_promotion_spy(monkeypatch, store)
    # Sanity: the spy trips if anything promotes a candidate. Proves (c) is a real
    # assertion, not a no-op — were the loop to promote, the test suite would fail.
    with pytest.raises(AssertionError, match="PROMOTION DETECTED"):
        store.set_stage("cand-x", "promoted")
    with pytest.raises(AssertionError, match="PROMOTION DETECTED"):
        store.set_champion("cand-x", "cand-x.yaml", scores={})


# ---------------------------------------------------------------------------
# (d) candidates awaiting_gate are reported
# ---------------------------------------------------------------------------
def test_reports_awaiting_gate_candidates(store, monkeypatch, capsys):
    _install_promotion_spy(monkeypatch, store)
    _arm_governor(store, monkeypatch)
    calls: list = []
    # round 1 clears, round 2 does not → cand-0 stays at the gate; loop ends at
    # max_rounds=2. The final summary must list cand-0.
    _patch_steps(monkeypatch, propose_returns=["cand-0", "cand-1"],
                 gate_clears=[True, False], calls=calls)

    summary = autonomy.cmd_autonomous(store, "mission", max_rounds=2,
                                      do_research=False)

    assert summary["awaiting_gate"] == ["cand-0"]
    out = capsys.readouterr().out
    assert "AWAITING HUMAN PROMOTION at the gate" in out
    assert "cand-0" in out
    assert "Nothing was promoted automatically" in out


# ---------------------------------------------------------------------------
# dry-run: prints the plan, invokes NOTHING, spends NOTHING
# ---------------------------------------------------------------------------
def test_dry_run_invokes_no_steps_and_spends_nothing(store, monkeypatch, capsys):
    _install_promotion_spy(monkeypatch, store)
    calls: list = []
    _patch_steps(monkeypatch, propose_returns=["cand-0"], gate_clears=[True],
                 calls=calls)

    summary = autonomy.cmd_autonomous(store, "improve clive shell driving",
                                      max_rounds=2, dry_run=True)

    assert calls == []  # NO role step was invoked
    assert summary["tokens_spent"] == 0
    assert summary["rounds_run"] == 2
    out = capsys.readouterr().out
    assert "PLAN (dry-run" in out
    assert store.budget_totals()["tokens"] == 0  # nothing spent
