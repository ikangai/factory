"""Researcher role: distil fetched papers into GROUNDED, staged briefs.

Hermetic — no network and no LLM: we monkeypatch arXiv retrieval and claude_p, and
assert the deterministic part (grounding guard + staging + vetting metadata)."""
import yaml

from factory.common.store import Blackboard
from factory.roles import common
from factory.research.arxiv import Paper


def test_research_stages_briefs_and_flags_ungrounded_citations(tmp_path, monkeypatch):
    papers = [Paper(arxiv_id="2606.24820v1", title="SHERLOC", summary="abstract",
                    url="http://arxiv.org/abs/2606.24820v1", published="2026-06-23",
                    authors=["A. Researcher"])]
    monkeypatch.setattr("factory.research.arxiv.search_arxiv", lambda *a, **k: papers)
    # keep hermetic: the dual-source role also queries GitHub — stub it to empty.
    monkeypatch.setattr("factory.research.git_repos.search_repos", lambda *a, **k: [])

    reply = """```yaml
briefs:
  - arxiv_id: "2606.24820v1"      # cites a fetched paper → grounded
    title: "SHERLOC"
    applies_to: skills
    suggested_change: "add a locate-before-edit skill"
    rationale: "grounded in the paper"
  - arxiv_id: "9999.99999"        # NOT among fetched papers → must be flagged
    title: "Invented"
    applies_to: system_prompt
    suggested_change: "do a thing"
    rationale: "ungrounded"
  - title: "no change field"      # missing suggested_change → dropped
    arxiv_id: "2606.24820v1"
```"""
    monkeypatch.setattr(common, "claude_p", lambda prompt, **k: (reply, 10, 0.0))
    monkeypatch.setattr("factory.common.paths.RESEARCH_STAGING_DIR", str(tmp_path))

    class _Store:
        def add_budget(self, *a, **k):
            pass

        def learnings_for_role(self, *a, **k):
            return []

        def pinned_for_role(self, *a, **k):
            return []

    written = common.research_cli_agents(_Store(), max_papers=1)
    assert len(written) == 2  # the two with a suggested_change; the third is dropped

    briefs = {b["arxiv_id"] + str(i): b for i, b in
              enumerate(yaml.safe_load(open(p)) for p in written)}
    by_id = {b["arxiv_id"]: b for b in (yaml.safe_load(open(p)) for p in written)}
    assert "provenance_warning" not in by_id["2606.24820v1"]   # grounded → clean
    assert "provenance_warning" in by_id["9999.99999"]          # hallucinated cite → flagged
    assert all(b["status"] == "staged" for b in by_id.values())  # vetting required


def test_research_prompt_includes_researcher_learnings(tmp_path, monkeypatch):
    """The paper-distilling researcher reads its durable learnings (the {MEMORY} seam),
    the same way research_feed does — so lessons about HOW to distill (e.g. survey
    triage) reach the researcher that actually produces the staged briefs."""
    papers = [Paper(arxiv_id="2606.24820v1", title="SHERLOC", summary="abstract",
                    url="http://arxiv.org/abs/2606.24820v1", published="2026-06-23",
                    authors=["A. Researcher"])]
    monkeypatch.setattr("factory.research.arxiv.search_arxiv", lambda *a, **k: papers)
    monkeypatch.setattr("factory.research.git_repos.search_repos", lambda *a, **k: [])
    monkeypatch.setattr("factory.common.paths.RESEARCH_STAGING_DIR", str(tmp_path))

    captured = {}
    monkeypatch.setattr(common, "claude_p",
                        lambda prompt, **k: (captured.update(prompt=prompt), ("briefs: []", 1, 0.0))[1])

    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    with s:
        s.add_learning("researcher", "SURVEY TRIAGE: cite the primary paper, never the survey.")
        common.research_cli_agents(s, max_papers=1)

    assert "What you've learned so far (researcher)" in captured["prompt"]   # the memory seam
    assert "cite the primary paper, never the survey" in captured["prompt"]  # the lesson itself


def test_research_degrades_to_empty_on_retrieval_failure(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr("factory.research.arxiv.search_arxiv", _boom)
    monkeypatch.setattr("factory.research.git_repos.search_repos", _boom)

    class _Store:
        def add_budget(self, *a, **k):
            pass

        def learnings_for_role(self, *a, **k):
            return []

        def pinned_for_role(self, *a, **k):
            return []

    assert common.research_cli_agents(_Store()) == []  # never crashes the loop
