"""The research feed (design step 4): a web researcher proposes bounded directions toward
the mission, de-duped against the backlog, landing as source='research' tasks; consuming
the shipped digests closes the research<->dev loop. Hermetic — claude_super injected."""
import os

import pytest
import yaml

from factory.common import paths
from factory.common.store import Blackboard
from factory.research import focus
from factory.roles import common, research_feed


@pytest.fixture(autouse=True)
def _no_real_material(monkeypatch):
    """Keep these tests hermetic: the researcher now reads MISSION.md's ## Material section
    (FACTORY_ROOT is a real path). Default it to empty; the material tests override it."""
    monkeypatch.setattr(focus, "read_material", lambda p: None)


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


def test_propose_directions_appends_human_material(tmp_path, monkeypatch):
    """Task 1.3: the ## Material from the human bullets reach the researcher's prompt."""
    monkeypatch.setattr(focus, "read_material",
                        lambda p: "- arxiv:2401.00001 tmux agents\n- github.com/acme/tool")
    captured = {}

    def fake_super(prompt, **k):
        captured["prompt"] = prompt
        return ('```json\n{"directions":[]}\n```', 1, 0.0)

    monkeypatch.setattr(common, "claude_super", fake_super)
    with _store(tmp_path) as s:
        s.set_mission("x")
        s.start_shift(token_budget=1)
        research_feed.propose_directions(s)
    assert "Material from the human" in captured["prompt"]
    assert "arxiv:2401.00001 tmux agents" in captured["prompt"]
    assert "github.com/acme/tool" in captured["prompt"]


def test_propose_directions_no_material_appends_no_block(tmp_path, monkeypatch):
    captured = {}

    def fake_super(prompt, **k):
        captured["prompt"] = prompt
        return ('```json\n{"directions":[]}\n```', 1, 0.0)

    monkeypatch.setattr(common, "claude_super", fake_super)   # read_material → None via autouse
    with _store(tmp_path) as s:
        s.set_mission("x")
        s.start_shift(token_budget=1)
        research_feed.propose_directions(s)
    assert "Material from the human" not in captured["prompt"]


def test_propose_directions_adds_research_tasks_dedupes_and_consumes_digests(tmp_path, monkeypatch):
    def fake_super(prompt, **k):
        return ('looked into it… ```json\n{"directions":['
                '{"title":"add bounded retry to pane reconnect","detail":"flaky panes drop; retry"},'
                '{"title":"fix dead-pane detection","detail":"dup of an existing task"}]}\n```', 200, 0.01)

    monkeypatch.setattr(common, "claude_super", fake_super)
    with _store(tmp_path) as s:
        s.set_mission("make clive reliable", target_repo="ikangai/clive")
        s.add_task("t1", "fix dead-pane detection", source="issue")   # existing → must dedupe
        s.add_digest(shift_id=None, shipped=["x"], summary="shipped the X fix")

        added = research_feed.propose_directions(s)

        assert [a["title"] for a in added] == ["add bounded retry to pane reconnect"]   # dup dropped
        titles = [t["title"] for t in s.list_tasks(status="open")]
        assert titles.count("fix dead-pane detection") == 1            # not duplicated
        research = [t for t in s.list_tasks(status="open") if t["source"] == "research"]
        assert [t["title"] for t in research] == ["add bounded retry to pane reconnect"]
        assert s.unconsumed_digests() == []                           # the loop closed


def test_propose_directions_ledgers_researcher_spend(tmp_path, monkeypatch):
    """Task 0.5: the research refill records its own tokens/cost against the current shift
    (previously discarded as _tokens/_cost)."""
    monkeypatch.setattr(common, "claude_super",
                        lambda *a, **k: ('```json\n{"directions":[]}\n```', 900, 0.09))
    with _store(tmp_path) as s:
        s.set_mission("x")
        sh = s.start_shift(token_budget=1)
        research_feed.propose_directions(s)
        rows = [e for e in s.budget_entries() if e["role_or_run"] == "researcher"]
    assert len(rows) == 1 and rows[0]["tokens"] == 900 and rows[0]["cost"] == 0.09
    assert rows[0]["shift_id"] == sh


def test_propose_directions_uses_the_web_researcher_toolset(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(common, "claude_super",
                        lambda prompt, **k: captured.update(prompt=prompt, **k)
                        or ('```json\n{"directions":[]}\n```', 1, 0.0))
    with _store(tmp_path) as s:
        s.set_mission("RELIABILITY-FOCUS", target_repo="ikangai/clive")
        s.add_digest(shift_id=None, shipped=[], summary="shipped the reconnect fix")
        research_feed.propose_directions(s)

    assert captured["settings"] == "user"                     # full instance: web + diary
    assert "WebSearch" in captured["allowed_tools"]           # the researcher searches
    assert "Bash" not in captured["allowed_tools"]            # …but doesn't shell or edit code
    assert "RELIABILITY-FOCUS" in captured["prompt"]          # the mission
    assert "shipped the reconnect fix" in captured["prompt"]  # outcome-informed (the digest)


def test_fetch_issues_parses_gh_json_and_is_graceful(monkeypatch):
    import json
    import subprocess
    import types
    canned = json.dumps([
        {"number": 41, "title": "Self-learning tool discovery", "labels": [{"name": "enhancement"}]},
        {"number": 38, "title": "Messaging CLIs in toolset", "labels": []}])
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=canned))
    out = research_feed.fetch_issues("ikangai/clive")
    assert "#41: Self-learning tool discovery  [enhancement]" in out and "#38: Messaging CLIs" in out

    assert research_feed.fetch_issues("") == ""                          # no repo → no fetch
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert research_feed.fetch_issues("x/y") == ""                       # gh missing → graceful


def test_issue_dedup_detects_reissues_not_new_bugs():
    """The unattended bug-filing guard: skip an issue whose title matches an OPEN one
    (case/space-insensitive, either-contains-the-other), but let a genuinely new bug through."""
    from factory.orchestrator.orchestrator import _dup_title
    existing = ("- #41: Self-learning tool discovery  [enhancement]\n"
                "- #38: Messaging CLIs in toolset")
    assert _dup_title("Self-learning tool discovery", existing)              # exact (normalized)
    assert _dup_title("self-learning   TOOL  discovery", existing)           # case/space-insensitive
    assert _dup_title("Add self-learning tool discovery to clive", existing)  # superset contains it
    assert not _dup_title("Fix pane reconnect race on resize", existing)     # genuinely new → file it
    assert not _dup_title("", existing)                                      # blank → not a dup


def test_build_research_prompt_includes_the_open_issues(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("make clive reliable", target_repo="ikangai/clive")
        p = research_feed.build_research_prompt(s, s.active_mission(), limit=5,
                                                issues="- #41: do a thing  [enhancement]")
        assert "#41: do a thing" in p and "OPEN ISSUES" in p             # the researcher sees real issues


def test_propose_directions_no_mission_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "claude_super", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not spawn a researcher with no mission")))
    with _store(tmp_path) as s:
        assert research_feed.propose_directions(s) == []


def test_propose_directions_tolerates_a_junk_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "claude_super", lambda *a, **k: ("no json here", 1, 0.0))
    with _store(tmp_path) as s:
        s.set_mission("m")
        assert research_feed.propose_directions(s) == []          # parses to nothing → no tasks, no crash


# --- Task 5.4: staged-brief → backlog converter -----------------------------

def _write_brief(dir_, name, **fields):
    """Drop a research-staging yaml (hermetic: dir_ is a tmp, never the real staging)."""
    path = os.path.join(dir_, f"{name}.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(fields, fh, sort_keys=False, allow_unicode=True)
    return path


def _read_status(path):
    with open(path, "r", encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("status")


def test_convert_briefs_staged_clean_adds_task_and_flips_yaml(tmp_path, monkeypatch):
    """A staged, grounded brief becomes a source='research' backlog task and its yaml
    flips to 'converted' so a re-run never re-adds it."""
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(paths, "RESEARCH_STAGING_DIR", str(staging))
    p = _write_brief(str(staging), "rb-aaa", id="rb-aaa", status="staged",
                     title="add locate-before-edit skill",
                     suggested_change="hypothesis first", rationale="fewer flails",
                     arxiv_id="2606.24820")

    with _store(tmp_path) as s:
        added = research_feed.convert_briefs(s)
        assert [a["title"] for a in added] == ["add locate-before-edit skill"]
        tasks = [t for t in s.list_tasks(status="open") if t["source"] == "research"]
        assert [t["title"] for t in tasks] == ["add locate-before-edit skill"]
    assert _read_status(p) == "converted"


def test_convert_briefs_provenance_warning_is_skipped_and_untouched(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(paths, "RESEARCH_STAGING_DIR", str(staging))
    p = _write_brief(str(staging), "rb-bad", id="rb-bad", status="staged",
                     title="ungrounded idea", suggested_change="do a thing",
                     provenance_warning="citation not among fetched papers — verify")

    with _store(tmp_path) as s:
        added = research_feed.convert_briefs(s)
        assert added == []
        assert s.list_tasks(status="open") == []
    assert _read_status(p) == "staged"          # untouched — operator must verify first


def test_convert_briefs_already_converted_is_skipped(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(paths, "RESEARCH_STAGING_DIR", str(staging))
    _write_brief(str(staging), "rb-done", id="rb-done", status="converted",
                 title="already promoted", suggested_change="x")

    with _store(tmp_path) as s:
        assert research_feed.convert_briefs(s) == []
        assert s.list_tasks(status="open") == []


def test_convert_briefs_dedupes_against_open_backlog(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(paths, "RESEARCH_STAGING_DIR", str(staging))
    p = _write_brief(str(staging), "rb-dup", id="rb-dup", status="staged",
                     title="Fix Dead-Pane Detection", suggested_change="x")

    with _store(tmp_path) as s:
        s.add_task("t1", "fix dead-pane detection", source="issue")   # already in backlog
        added = research_feed.convert_briefs(s)
        assert added == []                                            # case-insensitive dup dropped
        assert len(s.list_tasks(status="open")) == 1
    assert _read_status(p) == "staged"          # not converted — nothing was added
