"""Researcher role — generic focus, dual sources (arXiv + GitHub), human material.

Hermetic: we monkeypatch search_arxiv / search_repos / claude_p so no network or
LLM is touched, and assert the deterministic behaviour (focus derivation, dual
staging + dual-citation provenance guard, MISSION.md material classification, and
graceful degradation on retrieval failure)."""
import yaml

from factory.roles import common
from factory.research.arxiv import Paper
from factory.research.git_repos import Repo
from factory.research import focus as focus_mod
from factory.research import ingest


# --------------------------------------------------------------------------- #
# (a) focus comes from MISSION.md when query is None                          #
# --------------------------------------------------------------------------- #
def _mission(tmp_path, body):
    p = tmp_path / "MISSION.md"
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_focus_from_research_focus_section(tmp_path):
    mp = _mission(tmp_path,
                  "# M\n\n## Mission\n\ngeneral text\n\n"
                  "## Research focus (overrides the default query)\n\n"
                  "self-healing distributed databases\n\n"
                  "## Material from the human\n\n")
    assert focus_mod.read_research_focus(mp) == "self-healing distributed databases"


def test_focus_falls_back_to_mission_then_default(tmp_path):
    # No Research focus section → falls back to Mission body.
    mp = _mission(tmp_path, "# M\n\n## Mission\n\noptimise robot grasping\n")
    assert focus_mod.read_research_focus(mp) == "optimise robot grasping"
    # No usable section at all → None (caller uses DEFAULT_QUERY).
    mp2 = _mission(tmp_path, "# M\n\nno sections here\n")
    assert focus_mod.read_research_focus(mp2) is None


def test_research_cli_agents_derives_focus_from_mission(tmp_path, monkeypatch):
    mp = _mission(tmp_path,
                  "# M\n\n## Research focus\n\nquantum error correction\n")
    captured = {}

    def _arxiv(query, **k):
        captured["arxiv_q"] = query
        return []

    def _repos(query, **k):
        captured["repos_q"] = query
        return []

    monkeypatch.setattr("factory.research.arxiv.search_arxiv", _arxiv)
    monkeypatch.setattr("factory.research.git_repos.search_repos", _repos)
    monkeypatch.setattr("factory.common.paths.RESEARCH_STAGING_DIR", str(tmp_path / "st"))

    class _Store:
        def add_budget(self, *a, **k):
            pass

    # query=None → focus must be derived from MISSION.md
    out = common.research_cli_agents(_Store(), query=None, mission_file=mp)
    assert out == []  # no material → nothing staged, but focus was used:
    assert captured["arxiv_q"] == "quantum error correction"
    assert captured["repos_q"] == "quantum error correction"


# --------------------------------------------------------------------------- #
# (b) both paper AND repo briefs stage; provenance accepts paper+repo, flags  #
#     an invented citation                                                    #
# --------------------------------------------------------------------------- #
def test_dual_source_staging_and_provenance_guard(tmp_path, monkeypatch):
    papers = [Paper(arxiv_id="2606.24820v1", title="SHERLOC", summary="abs",
                    url="http://arxiv.org/abs/2606.24820v1", published="2026-06-23",
                    authors=["A. Researcher"])]
    repos = [Repo(full_name="acme/clidriver", description="drives a CLI",
                  url="https://github.com/acme/clidriver", stars=42,
                  language="Python", topics=["agent"], pushed_at="2026-06-01")]
    monkeypatch.setattr("factory.research.arxiv.search_arxiv", lambda *a, **k: papers)
    monkeypatch.setattr("factory.research.git_repos.search_repos", lambda *a, **k: repos)

    reply = """```yaml
briefs:
  - arxiv_id: "2606.24820v1"          # grounded paper citation
    title: "SHERLOC"
    applies_to: skills
    suggested_change: "add a locate-before-edit skill"
    rationale: "grounded in the paper"
  - repo: "acme/clidriver"            # grounded repo citation
    title: "clidriver"
    applies_to: command_affordances
    suggested_change: "adopt its retry-on-nonzero-exit affordance"
    rationale: "the repo implements this"
  - arxiv_id: "9999.99999"            # invented → must be flagged
    title: "Invented"
    applies_to: system_prompt
    suggested_change: "do a thing"
    rationale: "ungrounded"
```"""
    monkeypatch.setattr(common, "claude_p", lambda prompt, **k: (reply, 10, 0.0))
    monkeypatch.setattr("factory.common.paths.RESEARCH_STAGING_DIR", str(tmp_path))

    class _Store:
        def add_budget(self, *a, **k):
            pass

    written = common.research_cli_agents(_Store(), query="x", max_papers=1, max_repos=1)
    assert len(written) == 3
    briefs = [yaml.safe_load(open(p)) for p in written]
    by_paper = next(b for b in briefs if b.get("arxiv_id") == "2606.24820v1")
    by_repo = next(b for b in briefs if b.get("repo") == "acme/clidriver")
    invented = next(b for b in briefs if b.get("arxiv_id") == "9999.99999")
    assert "provenance_warning" not in by_paper      # grounded paper → clean
    assert "provenance_warning" not in by_repo       # grounded repo → clean
    assert "provenance_warning" in invented          # invented cite → flagged
    assert all(b["status"] == "staged" for b in briefs)


def test_repo_citation_as_url_is_grounded(tmp_path, monkeypatch):
    repos = [Repo(full_name="acme/clidriver", description="x",
                  url="https://github.com/acme/clidriver", stars=1,
                  language="Go", topics=[], pushed_at="2026-06-01")]
    monkeypatch.setattr("factory.research.arxiv.search_arxiv", lambda *a, **k: [])
    monkeypatch.setattr("factory.research.git_repos.search_repos", lambda *a, **k: repos)
    reply = """```yaml
briefs:
  - repo: "https://github.com/acme/clidriver"   # cited by URL → still grounded
    title: "clidriver"
    applies_to: skills
    suggested_change: "x"
    rationale: "y"
```"""
    monkeypatch.setattr(common, "claude_p", lambda prompt, **k: (reply, 1, 0.0))
    monkeypatch.setattr("factory.common.paths.RESEARCH_STAGING_DIR", str(tmp_path))

    class _Store:
        def add_budget(self, *a, **k):
            pass

    written = common.research_cli_agents(_Store(), query="x", max_papers=0, max_repos=1)
    assert len(written) == 1
    assert "provenance_warning" not in yaml.safe_load(open(written[0]))


# --------------------------------------------------------------------------- #
# (c) parse_material classifies arxiv id / arxiv URL / github URL, marks the  #
#     bare non-matching line unfetched (and never SSRFs)                       #
# --------------------------------------------------------------------------- #
def test_parse_material_classification(tmp_path, monkeypatch):
    body = (
        "# M\n\n## Material from the human\n\n"
        "- 2606.24820\n"                                  # bare arxiv id
        "- https://arxiv.org/abs/2601.00001v2\n"          # arxiv URL
        "- https://github.com/bytedance/deer-flow\n"      # github repo URL
        "- https://evil.example.com/secret\n"             # NOT fetchable → unfetched
        "\n## next\n")
    mp = _mission(tmp_path, body)

    fetched_arxiv = []
    fetched_repo = []

    def _arxiv(query, **k):
        fetched_arxiv.append(query)
        aid = query.split(":", 1)[-1]
        return [Paper(arxiv_id=aid, title="t", summary="s",
                      url=f"http://arxiv.org/abs/{aid}", published="2026",
                      authors=[])]

    def _repos(query, **k):
        fetched_repo.append(query)
        full = query.split(":", 1)[-1]
        return [Repo(full_name=full, description="d", url=f"https://github.com/{full}",
                     stars=1, language="Py", topics=[], pushed_at="2026")]

    monkeypatch.setattr("factory.research.arxiv.search_arxiv", _arxiv)
    monkeypatch.setattr("factory.research.git_repos.search_repos", _repos)

    items = ingest.parse_material(mp)
    kinds = [i["kind"] for i in items]
    assert kinds == ["arxiv", "arxiv", "repo", "unfetched"]
    assert items[0]["arxiv_id"] == "2606.24820"
    assert items[1]["arxiv_id"] == "2601.00001v2"
    assert items[2]["full_name"] == "bytedance/deer-flow"
    # the non-arxiv/non-github URL was classified unfetched and NEVER dereferenced
    assert "evil.example.com" not in " ".join(fetched_arxiv + fetched_repo)
    assert items[3]["kind"] == "unfetched"
    # the github query went through the safe `repo:` form
    assert fetched_repo == ["repo:bytedance/deer-flow"]


def test_material_feeds_high_priority_section(tmp_path, monkeypatch):
    body = ("# M\n\n## Material from the human\n\n"
            "- https://github.com/acme/star\n")
    mp = _mission(tmp_path, body)
    monkeypatch.setattr("factory.research.arxiv.search_arxiv", lambda *a, **k: [])
    monkeypatch.setattr(
        "factory.research.git_repos.search_repos",
        lambda *a, **k: [Repo(full_name="acme/star", description="d",
                              url="https://github.com/acme/star", stars=9,
                              language="Py", topics=[], pushed_at="2026")])

    seen = {}

    def _claude(prompt, **k):
        seen["prompt"] = prompt
        return ("```yaml\nbriefs:\n  - repo: \"acme/star\"\n    title: t\n"
                "    applies_to: skills\n    suggested_change: c\n    rationale: r\n```", 1, 0.0)

    monkeypatch.setattr(common, "claude_p", _claude)
    monkeypatch.setattr("factory.common.paths.RESEARCH_STAGING_DIR", str(tmp_path / "st"))

    class _Store:
        def add_budget(self, *a, **k):
            pass

    written = common.research_cli_agents(_Store(), query="x", max_papers=0, max_repos=0,
                                         mission_file=mp)
    assert "MATERIAL THE HUMAN ASKED YOU TO READ" in seen["prompt"]
    assert "acme/star" in seen["prompt"]
    # human material grounds the citation even though max_repos=0 (no general search)
    assert len(written) == 1
    assert "provenance_warning" not in yaml.safe_load(open(written[0]))


# --------------------------------------------------------------------------- #
# (d) retrieval failure → degrades, no crash                                  #
# --------------------------------------------------------------------------- #
def test_both_sources_fail_degrades_to_empty(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr("factory.research.arxiv.search_arxiv", _boom)
    monkeypatch.setattr("factory.research.git_repos.search_repos", _boom)

    class _Store:
        def add_budget(self, *a, **k):
            pass

    # No mission_file, both sources raise → empty, never crashes the loop.
    assert common.research_cli_agents(_Store(), query="x") == []


def test_material_fetch_failure_marks_unfetched(tmp_path, monkeypatch):
    body = ("# M\n\n## Material from the human\n\n- 2606.24820\n")
    mp = _mission(tmp_path, body)

    def _boom(*a, **k):
        raise RuntimeError("arxiv down")

    monkeypatch.setattr("factory.research.arxiv.search_arxiv", _boom)
    monkeypatch.setattr("factory.research.git_repos.search_repos", lambda *a, **k: [])
    items = ingest.parse_material(mp)
    assert len(items) == 1
    assert items[0]["kind"] == "arxiv"
    assert items[0]["paper"] is None
    assert "error" in items[0]  # recorded for the human, no crash
