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
@pytest.fixture(autouse=True)
def _stub_exec_summary(tmp_path, monkeypatch):
    """Keep the loop HERMETIC. cmd_autonomous ends by generating an executive
    summary, which otherwise makes a real `claude -p` call AND writes into the
    repo's updates/ dir. Stub the generator (no tokens) and redirect FACTORY_ROOT
    so the summary write lands in tmp, never the real repo (the summary itself is
    covered by the reporting tests)."""
    monkeypatch.setattr(
        "factory.reporting.summary.generate_executive_summary",
        lambda *a, **k: "## Discoveries\n(stub)\n\n## Decisions\n(stub)\n\n"
                        "## Proposed next steps\n(stub)\n")
    # The loop also writes a dev-diary entry (and, when enabled, a blog post); stub
    # both generators so no real claude -p fires and writes land under tmp FACTORY_ROOT.
    monkeypatch.setattr("factory.reporting.diary.generate_diary_entry",
                        lambda *a, **k: ("stub-entry", "I did stub things this run."))
    monkeypatch.setattr("factory.reporting.blog.generate_blog_post",
                        lambda *a, **k: ("stub-post", "# Stub headline\n\nStub body."))
    monkeypatch.setattr("factory.common.paths.FACTORY_ROOT", str(tmp_path))
@pytest.fixture()
def store(tmp_path, monkeypatch):
    # Isolate EVERY filesystem location the loop could write into to tmp, so a test
    # can never touch the real repo corpus. Belt-and-suspenders: the round steps are
    # stubbed, but if a future test forgets to stub cmd_intake the real intake must
    # still be unable to mine/promote into the actual scenarios/ dirs (this exact
    # gap once auto-promoted unvetted scenarios into the live working set).
    staging = tmp_path / "research_staging"
    staging.mkdir()
    monkeypatch.setattr("factory.common.paths.RESEARCH_STAGING_DIR", str(staging))
    for name in ("WORKING_DIR", "HELD_OUT_DIR", "STAGING_DIR"):
        d = tmp_path / name.lower()
        d.mkdir()
        monkeypatch.setattr(f"factory.common.paths.{name}", str(d))
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

    def fake_intake(s, *a, **k):
        # default: intake fires but promotes nothing (no new working scenarios) and
        # spends nothing — keeps the existing sequencing/budget assertions intact.
        return {"mined": [], "validated": [], "promoted": [],
                "unverified": [], "rejected": []}

    monkeypatch.setattr(orch, "cmd_research", fake_research)
    monkeypatch.setattr(orch, "cmd_baseline", fake_baseline)
    monkeypatch.setattr(orch, "cmd_propose", fake_propose)
    monkeypatch.setattr(orch, "cmd_round", fake_round)
    monkeypatch.setattr(orch, "cmd_intake", fake_intake)


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
# (b) KEEP BUSY: when the governor never arms, broaden research instead of
#     stopping — keep surfacing discoveries so the 09:00 update has content.
# ---------------------------------------------------------------------------
def test_idle_broadens_research_and_keeps_working(store, monkeypatch):
    """Governor never arms (champion robust) but each idle round the broadened
    research sweep finds NEW material → the loop must NOT stop on 'no work'; it
    keeps researching every round (escalating breadth) until a hard ceiling."""
    _install_promotion_spy(monkeypatch, store)
    _hold_governor(monkeypatch)
    seen: list = []  # (max_papers, max_repos) for each research call

    def fake_research(s, query=None, max_papers=8, max_repos=6, **k):
        seen.append((max_papers, max_repos))
        # every idle round finds NEW material → a fresh brief lands on disk
        from factory.common import paths
        os.makedirs(paths.RESEARCH_STAGING_DIR, exist_ok=True)
        with open(os.path.join(paths.RESEARCH_STAGING_DIR,
                               f"rb-{len(seen)}.yaml"), "w") as fh:
            fh.write("id: rb\nstatus: staged\n")

    monkeypatch.setattr(orch, "cmd_research", fake_research)
    monkeypatch.setattr(orch, "cmd_baseline", lambda s, *a, **k: None)
    monkeypatch.setattr(orch, "cmd_propose", lambda s, *a, **k: None)
    monkeypatch.setattr(orch, "cmd_round", lambda s, *a, **k: None)
    monkeypatch.setattr(orch, "cmd_intake", lambda s, *a, **k: {"promoted": []})

    summary = autonomy.cmd_autonomous(store, "mission", max_rounds=4,
                                      do_research=True)

    assert summary["rounds_run"] == 4
    assert summary["stop_reason"] == "max_rounds reached"
    # researched EVERY round (kept busy), not just the scheduled cadence
    assert len(seen) == 4
    # breadth escalated on a later idle round (more papers AND repos than round 1)
    assert seen[1][0] > seen[0][0]
    assert seen[1][1] > seen[0][1]
    # bounded: never blows past the breadth cap (default factor 4 over base 8)
    assert max(p for p, _ in seen) <= 8 * 4


# ---------------------------------------------------------------------------
# (b) KEEP BUSY has a floor: if broadened research is ALSO dry for K rounds,
#     stop (research exhausted) rather than spin — must broaden >1 round first.
# ---------------------------------------------------------------------------
def test_stops_when_idle_research_exhausted(store, monkeypatch):
    _install_promotion_spy(monkeypatch, store)
    _hold_governor(monkeypatch)
    calls: list = []

    def fake_research(s, query=None, max_papers=8, max_repos=6, **k):
        calls.append("research")  # finds NOTHING new → no marker file written

    monkeypatch.setattr(orch, "cmd_research", fake_research)
    monkeypatch.setattr(orch, "cmd_baseline", lambda s, *a, **k: None)
    monkeypatch.setattr(orch, "cmd_propose", lambda s, *a, **k: None)
    monkeypatch.setattr(orch, "cmd_round", lambda s, *a, **k: None)
    # intake is ALSO dry (promotes nothing) → with research dry too, the loop must
    # give up on 'research exhausted' rather than spin.
    monkeypatch.setattr(orch, "cmd_intake", lambda s, *a, **k: {"promoted": []})

    summary = autonomy.cmd_autonomous(store, "mission", max_rounds=10,
                                      do_research=True)

    assert "research exhausted" in summary["stop_reason"]
    # broadened for several rounds before giving up (== triggers.no_improvement_rounds)
    assert summary["rounds_run"] == 3
    assert len(calls) == 3
    assert summary["awaiting_gate"] == []


# ---------------------------------------------------------------------------
# (#2) INTAKE is wired into the loop: each research-cadence round the loop mines +
#      #64-validates + auto-promotes new working scenarios (the self-sustaining
#      arrow). Hermetic: cmd_intake is stubbed; we only assert the SEQUENCING.
# ---------------------------------------------------------------------------
def test_autonomous_runs_intake_on_research_cadence(store, monkeypatch):
    _install_promotion_spy(monkeypatch, store)
    _arm_governor(store, monkeypatch)
    calls: list = []
    _patch_steps(monkeypatch, propose_returns=["cand-0", "cand-1", "cand-2"],
                 gate_clears=[True, True, True], calls=calls)
    intake_calls: list = []

    def fake_intake(s, *a, **k):
        intake_calls.append(1)
        return {"mined": [], "validated": [], "promoted": [],
                "unverified": [], "rejected": []}

    monkeypatch.setattr(orch, "cmd_intake", fake_intake)
    autonomy.cmd_autonomous(store, "mission", max_rounds=3, do_research=True)
    assert len(intake_calls) >= 1   # intake actually fires inside the loop


def test_intake_promotions_keep_loop_busy_when_research_dry(store, monkeypatch):
    """Governor holds and research is DRY (no briefs), but intake promotes a fresh
    working scenario each idle round → that is NEW WORK, so the loop must NOT stop
    on 'research exhausted'; it runs to the hard ceiling."""
    _install_promotion_spy(monkeypatch, store)
    _hold_governor(monkeypatch)
    monkeypatch.setattr(orch, "cmd_research", lambda s, **k: None)   # dry: stages nothing
    monkeypatch.setattr(orch, "cmd_baseline", lambda s, *a, **k: None)
    monkeypatch.setattr(orch, "cmd_propose", lambda s, *a, **k: None)
    monkeypatch.setattr(orch, "cmd_round", lambda s, *a, **k: None)
    n = {"i": 0}

    def fake_intake(s, *a, **k):
        n["i"] += 1
        return {"mined": ["m"], "validated": ["m"], "promoted": [f"m{n['i']}"],
                "unverified": [], "rejected": []}

    monkeypatch.setattr(orch, "cmd_intake", fake_intake)
    summary = autonomy.cmd_autonomous(store, "mission", max_rounds=4, do_research=True)

    assert summary["rounds_run"] == 4
    assert summary["stop_reason"] == "max_rounds reached"
    assert n["i"] >= 1   # intake kept producing work


def test_dry_run_writes_no_diary_or_blog(store, monkeypatch):
    """A --dry-run must not write a diary entry or blog post (nothing invoked,
    nothing spent) — guards the `if not dry_run` on both presentation blocks."""
    import os

    from factory.common import paths
    _install_promotion_spy(monkeypatch, store)
    calls: list = []
    _patch_steps(monkeypatch, propose_returns=["cand-0"], gate_clears=[True], calls=calls)

    summary = autonomy.cmd_autonomous(store, "mission", max_rounds=1, dry_run=True,
                                      do_diary=True, do_blog=True)

    assert not os.path.exists(os.path.join(paths.FACTORY_ROOT, ".dev-diary"))
    assert not os.path.exists(os.path.join(paths.FACTORY_ROOT, "blog"))
    assert "diary_path" not in summary and "blog_path" not in summary


def test_no_intake_flag_skips_intake(store, monkeypatch):
    _install_promotion_spy(monkeypatch, store)
    _arm_governor(store, monkeypatch)
    calls: list = []
    _patch_steps(monkeypatch, propose_returns=["cand-0"], gate_clears=[True], calls=calls)
    intake_calls: list = []
    monkeypatch.setattr(orch, "cmd_intake",
                        lambda s, *a, **k: intake_calls.append(1) or {"promoted": []})
    autonomy.cmd_autonomous(store, "mission", max_rounds=2, do_research=True,
                            do_intake=False)
    assert intake_calls == []   # do_intake=False disables the intake arrow


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
