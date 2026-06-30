"""Spec-shaped tasks at authorship (GSD integration #5, design:
docs/plans/2026-06-27-gsd-spec-driven-integration.md): producers emit a target_surface +
acceptance, the CLI stops dropping detail, and spec-completeness is visible."""
from factory.common.store import Blackboard
from factory.orchestrator import orchestrator as orch
from factory.reporting import scope_check


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


# -- spec helpers ------------------------------------------------------------
def test_is_spec_complete():
    assert scope_check.is_spec_complete({"target_surface": "llm.py", "acceptance": "test passes"})
    assert not scope_check.is_spec_complete({"target_surface": "llm.py"})   # no acceptance
    assert not scope_check.is_spec_complete({})
    assert not scope_check.is_spec_complete("x")


def test_spec_detail_suffix_renders_fields():
    s = scope_check.spec_detail_suffix({"target_surface": "llm.py", "acceptance": "retry test passes"})
    assert "llm.py" in s and "retry test passes" in s and "SPEC" in s


def test_spec_detail_suffix_empty_when_no_spec():
    assert scope_check.spec_detail_suffix({}) == ""
    assert scope_check.spec_detail_suffix("x") == ""


# -- cmd_task add --detail (the long-standing detail-drop fix) ----------------
def test_cmd_task_add_persists_detail(tmp_path):
    with _store(tmp_path) as s:
        orch.cmd_task(s, "add", rest="do x", source="human", detail="a bounded spec")
        t = [t for t in s.list_tasks() if t["title"] == "do x"][0]
        assert t["detail"] == "a bounded spec"


def test_cmd_task_add_without_detail_still_works(tmp_path):
    with _store(tmp_path) as s:
        orch.cmd_task(s, "add", rest="do y", source="human")
        t = [t for t in s.list_tasks() if t["title"] == "do y"][0]
        assert t["detail"] == ""


# -- researcher folds the emitted spec into the stored detail ----------------
def test_propose_directions_folds_spec_into_detail(tmp_path, monkeypatch):
    from factory.roles import research_feed, common
    with _store(tmp_path) as s:
        s.set_mission("make clive reliable", target_repo="o/r")
        monkeypatch.setattr(research_feed, "fetch_issues", lambda *a, **k: "(none)")
        reply = ('```json\n{"directions":[{"title":"add retry","detail":"do x",'
                 '"target_surface":"llm.py","acceptance":"retry test passes"}]}\n```')
        monkeypatch.setattr(common, "claude_super", lambda *a, **k: (reply, 0, 0.0))

        class _Ad:
            def entry(self):
                return ("/x", "/x/clive.py")

        monkeypatch.setattr(research_feed.config, "get_adapter", lambda: _Ad())
        research_feed.propose_directions(s)
        t = [t for t in s.list_tasks() if t["title"] == "add retry"][0]
        assert "llm.py" in t["detail"] and "retry test passes" in t["detail"]
