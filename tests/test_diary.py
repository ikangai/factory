"""Development-diary generator (presentation layer).

The factory narrates its own autonomous work as a first-person dev-diary entry, in
the diary skill's voice (past tense, chronological prose — NO headers, NO bullets),
so the work of all its `claude -p` role instances is captured the way a developer
would write it up. Mirrors `reporting/summary.py`: gather deterministic state →
isolated `claude_p` (the Diarist role) → validate → deterministic fallback that
never crashes and never invents.

Hermetic — no LLM: we monkeypatch `claude_p` on roles.common (the generator calls
it through that module) and isolate staging dirs to tmp.
"""
import os

import pytest
import yaml

from factory.common.store import Blackboard
from factory.reporting import diary
from factory.roles import common as roles_common

CHAMP = "champion"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    rstage = tmp_path / "r"
    sstage = tmp_path / "s"
    rstage.mkdir()
    sstage.mkdir()
    monkeypatch.setattr("factory.common.paths.RESEARCH_STAGING_DIR", str(rstage))
    monkeypatch.setattr("factory.common.paths.STAGING_DIR", str(sstage))
    with Blackboard(str(tmp_path / "f.db")) as bb:
        bb.init_db()
        bb.add_candidate(CHAMP, "champion", "champion.yaml",
                         change_summary="(baseline)", stage="promoted")
        bb.set_champion(CHAMP, "champion.yaml", scores={"working_set": 0.72})
        bb.upsert_scenario("s1", cls="single", partition="working",
                           source="seed", spec_path="s1.yaml", goal="do a thing")
        bb.add_candidate("cand-X", CHAMP, "cand-X.yaml",
                         change_summary="add a completion-receipt convention",
                         stage="awaiting_gate")
        bb.add_run("r1", "cand-X", "s1", "claude-cli", "pass",
                   partition="working", budget_used=10)
        bb.add_budget("proposer", 200, 0.01, notes="seed")
        (rstage / "rb-aaa.yaml").write_text(yaml.safe_dump({
            "id": "rb-aaa", "title": "SHERLOC",
            "technique": "locate before edit", "suggested_change": "add a skill",
            "applies_to": "skills", "arxiv_id": "2606.24820v1"}))
        yield bb


def _no_headers_or_bullets(body: str):
    for ln in body.splitlines():
        assert not ln.lstrip().startswith("#"), f"diary voice: no headers — {ln!r}"
        assert not ln.lstrip()[:2] in ("- ", "* "), f"diary voice: no bullets — {ln!r}"


# ---------------------------------------------------------------------------
# (1) LLM path: returns the entry + a parsed slug; strips the slug marker line
# ---------------------------------------------------------------------------
def test_returns_llm_entry_and_parsed_slug(store, monkeypatch):
    reply = ("slug: research-driven-proposal-round\n\n"
             "I ran an autonomous session toward sharper clive shell driving. "
             "I staged a SHERLOC brief and proposed cand-X, which cleared the rule "
             "and now waits for the human. Grounding the change in a paper felt "
             "like the loop finally using its research instead of padding a summary.")
    monkeypatch.setattr(roles_common, "claude_p", lambda prompt, **k: (reply, 9, 0.0))

    slug, body = diary.generate_diary_entry(store, since="2026-06-25",
                                            mission="improve clive shell driving")
    assert slug == "research-driven-proposal-round"
    assert "slug:" not in body.splitlines()[0]      # marker stripped from the body
    assert "I ran an autonomous session" in body


def test_prompt_is_grounded_in_gathered_data(store, monkeypatch):
    captured = {}
    monkeypatch.setattr(roles_common, "claude_p",
                        lambda prompt, **k: captured.__setitem__("p", prompt) or ("slug: x\n\nI did things.", 1, 0.0))
    diary.generate_diary_entry(store, since="2026-06-25", mission="improve clive")
    p = captured["p"]
    assert "cand-X" in p          # the awaiting_gate decision is in the data
    assert "SHERLOC" in p         # the research discovery is in the data
    assert "improve clive" in p   # the mission


# ---------------------------------------------------------------------------
# (2) deterministic fallback — obeys the diary VOICE (no headers/bullets, 1st person)
# ---------------------------------------------------------------------------
def test_falls_back_to_deterministic_diary_voice(store, monkeypatch):
    monkeypatch.setattr(roles_common, "claude_p",
                        lambda prompt, **k: ("[claude -p unavailable: boom]", 0, 0.0))
    slug, body = diary.generate_diary_entry(store, since="2026-06-25",
                                            mission="improve clive shell driving")
    assert slug                                   # a non-empty slug
    assert "I " in body                           # first person
    assert "improve clive shell driving" in body  # grounded in the mission
    assert "promoted" in body.lower()             # mentions the human gate
    _no_headers_or_bullets(body)                  # diary voice: prose, not a report


def test_llm_reply_with_headers_or_bullets_is_rejected(store, monkeypatch):
    """The diary VOICE forbids headers/bullets. A misbehaving LLM reply that emits
    them must be rejected → fall back to the (voice-compliant) deterministic entry,
    so a factory diary entry always passes the diary skill's linter."""
    bad = ("slug: x\n\n## A header\n\n- a bullet point\n\nI did some things.")
    monkeypatch.setattr(roles_common, "claude_p", lambda prompt, **k: (bad, 3, 0.0))
    _, body = diary.generate_diary_entry(store, since="2026-06-25", mission="m")
    _no_headers_or_bullets(body)   # got the deterministic fallback, not the bad reply


def test_slug_is_length_bounded(store, monkeypatch):
    """A separator-free LLM slug must not produce an over-length filename component."""
    monkeypatch.setattr(roles_common, "claude_p",
                        lambda prompt, **k: (f"slug: {'z' * 500}\n\nI did things.", 1, 0.0))
    slug, _ = diary.generate_diary_entry(store, mission="m")
    assert 0 < len(slug) <= 80


def test_never_crashes_when_claude_p_raises(store, monkeypatch):
    def boom(prompt, **k):
        raise RuntimeError("claude exploded")
    monkeypatch.setattr(roles_common, "claude_p", boom)
    slug, body = diary.generate_diary_entry(store, mission="m")
    assert slug and body.strip()


def test_read_only_never_promotes(store, monkeypatch):
    monkeypatch.setattr(store, "set_stage",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("WRITE")))
    monkeypatch.setattr(store, "add_promotion",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("PROMOTE")))
    monkeypatch.setattr(roles_common, "claude_p", lambda prompt, **k: ("slug: y\n\nI did things.", 1, 0.0))
    diary.generate_diary_entry(store, mission="m")  # must not raise
