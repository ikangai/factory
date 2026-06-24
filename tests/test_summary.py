"""Executive-summary generator (presentation layer).

Hermetic — no LLM, no network, no subprocess: we monkeypatch `claude_p` on the
roles.common module (the generator calls it through that module, so monkeypatching
there is honoured) and isolate the staging dirs to tmp. A real temp-file Blackboard
backs the candidate/brief/budget state the generator reads.

Asserts:
  * the generator GATHERS awaiting_gate candidates + staged research briefs into
    the data it passes to the LLM (the prompt is captured and inspected);
  * with a usable canned reply it returns that markdown unchanged;
  * it FALLS BACK to a deterministic templated summary (with the three required
    sections, the awaiting_gate candidate, and the brief) when claude_p errors;
  * read-only: it never promotes (a promotion spy trips if it ever does).
"""
import os

import pytest
import yaml

from factory.common.store import Blackboard
from factory.roles import common as roles_common
from factory.reporting import summary as summ

CHAMP = "champion"

CANNED = """## Discoveries
A new research brief on completion-checking landed.

## Decisions
cand-X cleared the gate and awaits the human. Nothing was promoted automatically.

## Proposed next steps
- Review cand-X at the board.
"""


@pytest.fixture()
def store(tmp_path, monkeypatch):
    # Isolate BOTH staging dirs to tmp so gathering reads test fixtures, not the repo.
    rstage = tmp_path / "research_staging"
    sstage = tmp_path / "scenario_staging"
    rstage.mkdir()
    sstage.mkdir()
    monkeypatch.setattr("factory.common.paths.RESEARCH_STAGING_DIR", str(rstage))
    monkeypatch.setattr("factory.common.paths.STAGING_DIR", str(sstage))

    db = str(tmp_path / "factory.db")
    with Blackboard(db) as bb:
        bb.init_db()
        bb.add_candidate(CHAMP, "champion", "champion.yaml",
                         change_summary="(baseline)", stage="promoted")
        bb.set_champion(CHAMP, "champion.yaml", scores={"working_set": 0.72})
        bb.upsert_scenario("s1", cls="single", partition="working",
                           source="seed", spec_path="s1.yaml", goal="do a thing")
        # A candidate that CLEARED the rule and now awaits the human (a live DECISION).
        bb.add_candidate("cand-X", CHAMP, "cand-X.yaml",
                         change_summary="add a completion-receipt convention",
                         stage="awaiting_gate")
        bb.set_candidate_scores("cand-X", {"working_set": 0.8,
                                           "divergence": {"working_delta": 0.07,
                                                          "alarm": False},
                                           "digest_path": "logs/runs/cand-X.digest.md"})
        # One that FAILED the gate.
        bb.add_candidate("cand-Y", CHAMP, "cand-Y.yaml",
                         change_summary="tried X, regressed", stage="rejected")
        # A run, so the run tally has shape.
        bb.add_run("r1", "cand-X", "s1", "claude-cli", "pass",
                   partition="working", budget_used=10)
        bb.add_budget("proposer", 200, 0.01, notes="seed")
        # A staged research brief = a DISCOVERY from a paper.
        (rstage / "rb-aaa.yaml").write_text(yaml.safe_dump({
            "id": "rb-aaa",
            "title": "SHERLOC: Structured Diagnostic Localization",
            "technique": "hypothesis-driven fault localization before editing",
            "suggested_change": "add a locate-before-edit skill",
            "applies_to": "skills",
            "arxiv_id": "2606.24820v1",
            "url": "http://arxiv.org/abs/2606.24820v1",
        }))
        # A staged mined scenario = a DISCOVERY for the corpus.
        (sstage / "mined-foo.yaml").write_text(yaml.safe_dump({
            "id": "mined-foo", "class": "single",
            "goal": "count the headlines in news.html", "check": "count.txt equals 4",
        }))
        yield bb


def _install_promotion_spy(monkeypatch, store):
    real_set_champion = store.set_champion

    def guarded_set_champion(id, spec_path, scores=None):
        if id != CHAMP:
            raise AssertionError(f"PROMOTION DETECTED: set_champion to {id!r}")
        real_set_champion(id, spec_path, scores)

    monkeypatch.setattr(store, "set_champion", guarded_set_champion)
    monkeypatch.setattr(store, "set_stage",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("WRITE DETECTED: set_stage in a read-only report")))
    monkeypatch.setattr(store, "add_promotion",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("PROMOTION DETECTED: add_promotion called")))


# ---------------------------------------------------------------------------
# (1) gathers awaiting_gate + research briefs into the data/prompt
# ---------------------------------------------------------------------------
def test_gathers_awaiting_gate_and_research_into_data(store):
    data = summ.gather_summary_data(store, since="2026-06-24", mission="improve clive")

    gate_ids = [c["id"] for c in data["awaiting_gate"]]
    assert gate_ids == ["cand-X"]
    assert data["awaiting_gate"][0]["change_summary"] == "add a completion-receipt convention"
    assert data["awaiting_gate"][0]["deltas"]["working_delta"] == 0.07

    titles = [b["title"] for b in data["discoveries"]["research_briefs"]]
    assert any("SHERLOC" in t for t in titles)
    brief = data["discoveries"]["research_briefs"][0]
    assert brief["citation"] == "http://arxiv.org/abs/2606.24820v1"

    mined = [s["id"] for s in data["discoveries"]["mined_scenarios"]]
    assert mined == ["mined-foo"]

    assert data["recent_decisions"]["failed_gate"][0]["id"] == "cand-Y"
    assert data["budget"]["tokens"] == 200


def test_prompt_passed_to_llm_contains_gathered_data(store, monkeypatch):
    captured = {}

    def fake_claude_p(prompt, **k):
        captured["prompt"] = prompt
        return CANNED, 5, 0.0

    monkeypatch.setattr(roles_common, "claude_p", fake_claude_p)
    summ.generate_executive_summary(store, since="2026-06-24", mission="improve clive")

    p = captured["prompt"]
    assert "cand-X" in p                      # the awaiting_gate decision
    assert "SHERLOC" in p                      # the research discovery
    assert "2606.24820v1" in p                 # its citation
    assert "improve clive" in p                # the mission
    # Grounded ONLY in data: the presenter prompt must tell it not to invent.
    assert "Use ONLY the gathered data" in p


# ---------------------------------------------------------------------------
# (2) returns the LLM markdown when it has all three sections
# ---------------------------------------------------------------------------
def test_returns_llm_markdown_with_three_sections(store, monkeypatch):
    _install_promotion_spy(monkeypatch, store)
    monkeypatch.setattr(roles_common, "claude_p", lambda prompt, **k: (CANNED, 5, 0.0))

    out = summ.generate_executive_summary(store, mission="improve clive")
    assert out.strip() == CANNED.strip()
    for sec in ("## Discoveries", "## Decisions", "## Proposed next steps"):
        assert sec in out


# ---------------------------------------------------------------------------
# (3) deterministic fallback when claude_p errors / returns unusable text
# ---------------------------------------------------------------------------
def _assert_deterministic(out):
    for sec in ("## Discoveries", "## Decisions", "## Proposed next steps"):
        assert sec in out
    assert "cand-X" in out                          # the awaiting_gate decision
    assert "SHERLOC" in out                          # the discovery
    assert "Nothing was promoted automatically" in out


def test_falls_back_when_claude_p_raises(store, monkeypatch):
    _install_promotion_spy(monkeypatch, store)

    def boom(prompt, **k):
        raise RuntimeError("claude exploded")

    monkeypatch.setattr(roles_common, "claude_p", boom)
    _assert_deterministic(summ.generate_executive_summary(store, mission="improve clive"))


def test_falls_back_when_claude_p_returns_error_sentinel(store, monkeypatch):
    monkeypatch.setattr(roles_common, "claude_p",
                        lambda prompt, **k: ("[claude -p unavailable: boom]", 0, 0.0))
    _assert_deterministic(summ.generate_executive_summary(store))


def test_falls_back_when_required_section_missing(store, monkeypatch):
    # Reply has two of three sections → not usable → deterministic fallback.
    partial = "## Discoveries\nstuff\n\n## Decisions\nmore stuff\n"
    monkeypatch.setattr(roles_common, "claude_p", lambda prompt, **k: (partial, 1, 0.0))
    _assert_deterministic(summ.generate_executive_summary(store))


# ---------------------------------------------------------------------------
# (4) empty store still yields a valid three-section summary (no crash)
# ---------------------------------------------------------------------------
def test_empty_store_yields_valid_summary(tmp_path, monkeypatch):
    monkeypatch.setattr("factory.common.paths.RESEARCH_STAGING_DIR",
                        str(tmp_path / "r"))
    monkeypatch.setattr("factory.common.paths.STAGING_DIR", str(tmp_path / "s"))
    os.makedirs(tmp_path / "r"); os.makedirs(tmp_path / "s")
    monkeypatch.setattr(roles_common, "claude_p",
                        lambda prompt, **k: ("[claude -p unavailable]", 0, 0.0))
    with Blackboard(str(tmp_path / "db.db")) as bb:
        bb.init_db()
        out = summ.generate_executive_summary(bb)
    for sec in ("## Discoveries", "## Decisions", "## Proposed next steps"):
        assert sec in out
    assert "No new research briefs" in out
