"""Factory memory (design: docs/plans/2026-06-27-factory-memory-design.md).

Agents + super-workers store learnings they read back to improve. Hermetic — a tmp
SQLite store per test; any git/gh/process I/O is injected.
"""
from factory.common.store import Blackboard
from factory.orchestrator import orchestrator as orch
from factory.reporting import factory_memory


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


# -- store: learnings CRUD ---------------------------------------------------
def test_add_and_list_learning_roundtrip(tmp_path):
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "narrow briefs to one landable slice",
                             scope="no_candidate")
        assert isinstance(lid, int) and lid > 0
        rows = s.learnings_for_role("developer")
        assert len(rows) == 1
        r = rows[0]
        assert r["content"] == "narrow briefs to one landable slice"
        assert r["role"] == "developer" and r["scope"] == "no_candidate"
        assert r["uses"] == 0


def test_learnings_are_role_isolated(tmp_path):
    with _store(tmp_path) as s:
        s.add_learning("developer", "dev lesson")
        s.add_learning("researcher", "res lesson")
        assert [r["content"] for r in s.learnings_for_role("developer")] == ["dev lesson"]
        assert [r["content"] for r in s.learnings_for_role("researcher")] == ["res lesson"]


def test_learnings_for_role_newest_first_and_limited(tmp_path):
    with _store(tmp_path) as s:
        for i in range(5):
            s.add_learning("conductor", f"lesson {i}")
        rows = s.learnings_for_role("conductor", limit=3)
        assert [r["content"] for r in rows] == ["lesson 4", "lesson 3", "lesson 2"]


def test_bump_learning_uses(tmp_path):
    with _store(tmp_path) as s:
        a = s.add_learning("factory", "A")
        b = s.add_learning("factory", "B")
        s.bump_learning_uses([a])
        uses = {r["id"]: r["uses"] for r in s.learnings_for_role("factory")}
        assert uses[a] == 1 and uses[b] == 0


def test_all_learnings_spans_roles_newest_first(tmp_path):
    with _store(tmp_path) as s:
        s.add_learning("developer", "d1")
        s.add_learning("factory", "f1")
        contents = [r["content"] for r in s.all_learnings()]
        assert contents == ["f1", "d1"]


# -- module: record_learning (dedup) -----------------------------------------
def test_record_learning_stores_and_returns_id(tmp_path):
    with _store(tmp_path) as s:
        lid = factory_memory.record_learning(s, "developer", "always narrow briefs")
        assert lid is not None
        assert s.learnings_for_role("developer")[0]["content"] == "always narrow briefs"


def test_record_learning_dedups_near_duplicates(tmp_path):
    with _store(tmp_path) as s:
        factory_memory.record_learning(s, "developer", "Narrow the brief to one slice")
        dup = factory_memory.record_learning(s, "developer", "narrow the brief to one slice.")
        assert dup is None                                  # case/punctuation-insensitive dup
        assert len(s.learnings_for_role("developer")) == 1


def test_record_learning_dedup_is_role_scoped(tmp_path):
    with _store(tmp_path) as s:
        factory_memory.record_learning(s, "developer", "shared insight")
        other = factory_memory.record_learning(s, "researcher", "shared insight")
        assert other is not None                            # same text, other role → not a dup
        assert len(s.all_learnings()) == 2


def test_record_learning_ignores_empty(tmp_path):
    with _store(tmp_path) as s:
        assert factory_memory.record_learning(s, "developer", "   ") is None
        assert s.learnings_for_role("developer") == []


# -- module: memory_card -----------------------------------------------------
def test_memory_card_empty_when_no_learnings(tmp_path):
    with _store(tmp_path) as s:
        assert factory_memory.memory_card(s, "developer") == ""


def test_memory_card_lists_role_learnings(tmp_path):
    with _store(tmp_path) as s:
        factory_memory.record_learning(s, "developer", "lesson one")
        factory_memory.record_learning(s, "developer", "lesson two")
        card = factory_memory.memory_card(s, "developer")
        assert "lesson one" in card and "lesson two" in card
        assert "developer" in card.lower()


def test_memory_card_includes_factory_lessons_for_other_roles(tmp_path):
    with _store(tmp_path) as s:
        factory_memory.record_learning(s, "factory", "graduate when divergence grows")
        card = factory_memory.memory_card(s, "developer")
        assert "graduate when divergence grows" in card     # factory lessons shared to every role


def test_memory_card_bumps_uses_on_surfaced_rows(tmp_path):
    with _store(tmp_path) as s:
        factory_memory.record_learning(s, "developer", "x")
        factory_memory.memory_card(s, "developer")
        assert s.learnings_for_role("developer")[0]["uses"] == 1


# -- CLI: factory learn ------------------------------------------------------
def test_cmd_learn_add_records(tmp_path):
    with _store(tmp_path) as s:
        orch.cmd_learn(s, "add", role="developer", content="dedupe your briefs")
        assert s.learnings_for_role("developer")[0]["content"] == "dedupe your briefs"


def test_cmd_learn_add_dedups(tmp_path):
    with _store(tmp_path) as s:
        orch.cmd_learn(s, "add", role="developer", content="same lesson")
        orch.cmd_learn(s, "add", role="developer", content="same lesson")
        assert len(s.learnings_for_role("developer")) == 1


def test_cmd_learn_add_ignores_empty(tmp_path):
    with _store(tmp_path) as s:
        orch.cmd_learn(s, "add", role="developer", content="")
        assert s.learnings_for_role("developer") == []


def test_cmd_learn_list_prints_learnings(tmp_path, capsys):
    with _store(tmp_path) as s:
        s.add_learning("developer", "visible lesson")
        orch.cmd_learn(s, "list", role="developer")
        assert "visible lesson" in capsys.readouterr().out


# -- parse_learnings (super-worker reply → learnings) ------------------------
def test_parse_learnings_extracts_bullets():
    reply = (
        "did the work, tests green.\n\n"
        "LEARNINGS:\n"
        "- the retry helper lives in llm.py:262, reuse it\n"
        "- completion.py max_wait is activity-aware now\n")
    assert factory_memory.parse_learnings(reply) == [
        "the retry helper lives in llm.py:262, reuse it",
        "completion.py max_wait is activity-aware now"]


def test_parse_learnings_none_or_missing():
    assert factory_memory.parse_learnings("LEARNINGS: none") == []
    assert factory_memory.parse_learnings("no section here") == []
    assert factory_memory.parse_learnings("") == []


def test_parse_learnings_inline_single():
    assert factory_memory.parse_learnings("LEARNINGS: narrow the briefs") == ["narrow the briefs"]


def test_parse_learnings_stops_at_trailing_prose():
    reply = "LEARNINGS:\n- lesson A\n\nSome trailing prose, not a learning."
    assert factory_memory.parse_learnings(reply) == ["lesson A"]


# -- lesson_for_block (canned factory failure-memory) ------------------------
def test_lesson_for_block_maps_known_actions():
    assert "narrow" in factory_memory.lesson_for_block("no_candidate").lower()
    assert factory_memory.lesson_for_block("discarded")
    assert factory_memory.lesson_for_block("auto_reverted")


def test_lesson_for_block_unknown_is_none():
    assert factory_memory.lesson_for_block("merged") is None
    assert factory_memory.lesson_for_block("halted") is None


# -- wiring: prompt injection + main-thread recording ------------------------
def test_role_prompts_have_memory_placeholder():
    import os
    from factory.common import paths
    for role in ("conductor", "developer", "research_feed"):
        p = os.path.join(paths.ROLES_DIR, role, "prompt.md")
        with open(p, encoding="utf-8") as fh:
            assert "{MEMORY}" in fh.read(), f"{role}/prompt.md lost its {{MEMORY}} seam"


def test_conductor_prompt_injects_memory_card(tmp_path, monkeypatch):
    from factory.roles import conductor, research_feed
    monkeypatch.setattr(research_feed, "fetch_issues", lambda *a, **k: "(none)")
    with _store(tmp_path) as s:
        s.add_learning("conductor", "claim pristine-file tasks for clean merges")
        prompt = conductor.build_conductor_prompt(
            s, {"statement": "m", "target_repo": "o/r"}, shift_id=1, token_budget=1000)
        assert "{MEMORY}" not in prompt
        assert "claim pristine-file tasks for clean merges" in prompt


def test_research_prompt_injects_memory_card(tmp_path):
    from factory.roles import research_feed
    with _store(tmp_path) as s:
        s.add_learning("researcher", "arxiv is a good source for tmux-agent papers")
        prompt = research_feed.build_research_prompt(
            s, {"statement": "m", "target_repo": "o/r"}, limit=5, issues="(none)")
        assert "{MEMORY}" not in prompt
        assert "arxiv is a good source for tmux-agent papers" in prompt


def test_execute_records_developer_and_factory_learnings(tmp_path):
    from factory.orchestrator import develop as dev
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        tid = "task-aaaa1111"
        s.add_task(tid, "do a thing", source="human")
        s.set_task_status(tid, "in_progress", shift_id=sh)

        def fake(text, **k):                            # a developer that left a lesson but no branch
            return {"action": "no_candidate", "learnings": ["reuse the retry helper in llm.py"]}

        dev.execute_claimed_tasks(s, sh, develop_fn=fake)
        assert any("reuse the retry helper" in r["content"]
                   for r in s.learnings_for_role("developer"))         # developer's emitted lesson
        assert any("no_candidate" in r["content"]
                   for r in s.learnings_for_role("factory"))           # canned factory failure-memory
