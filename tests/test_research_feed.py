"""The research feed (design step 4): a web researcher proposes bounded directions toward
the mission, de-duped against the backlog, landing as source='research' tasks; consuming
the shipped digests closes the research<->dev loop. Hermetic — claude_super injected."""
from factory.common.store import Blackboard
from factory.roles import common, research_feed


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


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
