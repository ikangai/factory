"""Blog-post generator (presentation layer).

The factory turns its ongoing autonomous work into an accessible, Ars-Technica-style
article for a broad-but-tech-curious audience. Mirrors `reporting/summary.py`: gather
deterministic state → isolated `claude_p` (the Blogger role) → validate → a
deterministic templated article that never crashes and never invents.

Hermetic — no LLM: `claude_p` is monkeypatched on roles.common; staging dirs in tmp.
"""
import pytest
import yaml

from factory.common.store import Blackboard
from factory.reporting import blog
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


# ---------------------------------------------------------------------------
# (1) LLM path: parse slug, return the article (with its H1 title) as body
# ---------------------------------------------------------------------------
def test_returns_llm_post_and_slug(store, monkeypatch):
    post = ("slug: teaching-an-agent-to-leave-a-receipt\n\n"
            "# Teaching an AI agent to leave a receipt\n\n"
            "Imagine hiring an assistant who finishes the job but never tells you. "
            "That is the small problem the factory chewed on this week, and the fix "
            "turned out to be a single sentence added to the agent's instructions. "
            "Here is how the machine figured it out on its own, and why a human still "
            "gets the final say before anything ships.")
    monkeypatch.setattr(roles_common, "claude_p", lambda prompt, **k: (post, 50, 0.0))

    slug, body = blog.generate_blog_post(store, since="2026-06-25",
                                         mission="improve clive shell driving")
    assert slug == "teaching-an-agent-to-leave-a-receipt"
    assert body.splitlines()[0].startswith("# ")    # the article keeps its H1 title
    assert "slug:" not in body                        # marker stripped
    assert "leave a receipt" in body


def test_prompt_is_grounded_and_voiced(store, monkeypatch):
    captured = {}
    monkeypatch.setattr(roles_common, "claude_p",
                        lambda prompt, **k: captured.__setitem__("p", prompt) or
                        ("slug: x\n\n# T\n\n" + "word " * 120, 1, 0.0))
    blog.generate_blog_post(store, since="2026-06-25", mission="improve clive")
    p = captured["p"]
    assert "cand-X" in p            # grounded in the real run data
    assert "SHERLOC" in p
    assert "improve clive" in p     # the mission
    assert "ONLY" in p or "only" in p   # told to ground only in the data (no invention)


# ---------------------------------------------------------------------------
# (2) deterministic fallback — a real, grounded article (title + length), no invention
# ---------------------------------------------------------------------------
def test_falls_back_to_deterministic_article(store, monkeypatch):
    monkeypatch.setattr(roles_common, "claude_p",
                        lambda prompt, **k: ("[claude -p unavailable: boom]", 0, 0.0))
    slug, body = blog.generate_blog_post(store, since="2026-06-25",
                                         mission="improve clive shell driving")
    assert slug
    assert body.lstrip().startswith("# ")           # has an H1 title
    assert len(body) > 400                            # a real article, not a stub
    assert "improve clive shell driving" in body     # grounded in the mission
    assert "promot" in body.lower()                  # explains the human gate
    # short reply with no title is ALSO unusable → fallback
    monkeypatch.setattr(roles_common, "claude_p", lambda prompt, **k: ("too short", 1, 0.0))
    _, body2 = blog.generate_blog_post(store, mission="m")
    assert body2.lstrip().startswith("# ")


def test_never_crashes_when_claude_p_raises(store, monkeypatch):
    def boom(prompt, **k):
        raise RuntimeError("claude exploded")
    monkeypatch.setattr(roles_common, "claude_p", boom)
    slug, body = blog.generate_blog_post(store, mission="m")
    assert slug and body.strip()


def test_read_only_never_promotes(store, monkeypatch):
    monkeypatch.setattr(store, "set_stage",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("WRITE")))
    monkeypatch.setattr(store, "add_promotion",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("PROMOTE")))
    monkeypatch.setattr(roles_common, "claude_p",
                        lambda prompt, **k: ("[claude -p unavailable]", 0, 0.0))
    blog.generate_blog_post(store, mission="m")  # must not raise
