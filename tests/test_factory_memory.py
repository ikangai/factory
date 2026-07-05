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
        first_id, _ = factory_memory.record_learning(s, "developer",
                                                     "Narrow the brief to one slice")
        dup = factory_memory.record_learning(s, "developer", "narrow the brief to one slice.")
        assert dup == (first_id, False)                     # case/punctuation-insensitive dup
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


# ============================================================================
# Review-fix regression tests (xhigh review of the feature, 2026-06-27)
# ============================================================================

# -- parse_learnings: numbered lists, prose intro, last-section-wins ----------
def test_parse_learnings_numbered_list():
    assert factory_memory.parse_learnings("LEARNINGS:\n1. first lesson\n2. second lesson") == [
        "first lesson", "second lesson"]


def test_parse_learnings_paren_numbered_list():
    assert factory_memory.parse_learnings("LEARNINGS:\n1) a\n2) b") == ["a", "b"]


def test_parse_learnings_prose_intro_then_bullets():
    assert factory_memory.parse_learnings("LEARNINGS:\nHere are the lessons:\n- A\n- B") == ["A", "B"]


def test_parse_learnings_takes_last_section_ignoring_earlier_prose_learnings_line():
    reply = ("Learnings: I found a bug in the tokenizer\n- not a real lesson\n\n"
             "final summary paragraph.\n\nLEARNINGS:\n- the real durable lesson")
    assert factory_memory.parse_learnings(reply) == ["the real durable lesson"]


# -- _is_dup: length-ratio gate so a short generic doesn't swallow specifics --
def test_is_dup_short_generic_does_not_swallow_long_specific():
    existing = [{"content": "narrow the brief"}]
    assert factory_memory._is_dup(
        "narrow the brief to one file and split the rest into a sequenced follow-up",
        existing) is None


def test_is_dup_exact_and_close_still_dedup():
    assert factory_memory._is_dup("narrow the brief", [{"content": "narrow the brief"}])
    assert factory_memory._is_dup("narrow the briefs", [{"content": "narrow the brief"}])


def test_record_learning_dedups_identical_non_ascii(tmp_path):
    with _store(tmp_path) as s:
        a = factory_memory.record_learning(s, "developer", "日本語のレッスンを学んだ")
        b = factory_memory.record_learning(s, "developer", "日本語のレッスンを学んだ")
        assert a is not None and b == (a[0], False)         # dedup must fire for non-ASCII too


# -- coerce_learnings: researcher JSON shape guard ---------------------------
def test_coerce_learnings_rejects_non_list():
    assert factory_memory.coerce_learnings("narrow the briefs") == []   # a string, not a list
    assert factory_memory.coerce_learnings(None) == []


def test_coerce_learnings_filters_to_nonempty_strings():
    assert factory_memory.coerce_learnings(["a", "", None, "b", 3, "  "]) == ["a", "b"]


# -- lesson_for_block: revert_failed + stage-aware discard -------------------
def test_lesson_for_block_revert_failed_has_a_lesson():
    assert factory_memory.lesson_for_block("revert_failed")


def test_lesson_for_block_discarded_is_stage_aware():
    tests_lesson = factory_memory.lesson_for_block("discarded", "tests")
    generic = factory_memory.lesson_for_block("discarded")
    assert tests_lesson and tests_lesson != generic and "test" in tests_lesson.lower()


# -- store: batched + empty-safe bump ----------------------------------------
def test_bump_learning_uses_empty_is_noop(tmp_path):
    with _store(tmp_path) as s:
        s.bump_learning_uses([])                            # must not raise


# -- CLI: list defaults to ALL roles, add defaults to factory ----------------
def test_cmd_learn_list_default_shows_all_roles(tmp_path, capsys):
    with _store(tmp_path) as s:
        s.add_learning("conductor", "cond lesson")
        s.add_learning("developer", "dev lesson")
        orch.cmd_learn(s, "list")
        out = capsys.readouterr().out
        assert "cond lesson" in out and "dev lesson" in out


def test_cmd_learn_add_defaults_to_factory_role(tmp_path):
    with _store(tmp_path) as s:
        orch.cmd_learn(s, "add", content="a factory-level lesson")
        assert s.learnings_for_role("factory")[0]["content"] == "a factory-level lesson"


# ============================================================================
# Task 0.1 (P11): stage-aware error lessons — a transport failure or a refusal
# must stop being recorded as the false "brief bundled too much" lesson.
# ============================================================================

def test_lesson_for_block_error_transport_is_stage_aware():
    transport = factory_memory.lesson_for_block("error", "transport")
    generic = factory_memory.lesson_for_block("error")
    assert transport and transport != generic and "transport" in transport.lower()
    assert "bundled too much" not in transport


def test_lesson_for_block_error_refusal_is_stage_aware():
    refusal = factory_memory.lesson_for_block("error", "refusal")
    generic = factory_memory.lesson_for_block("error")
    assert refusal and refusal != generic and "refus" in refusal.lower()
    assert "bundled too much" not in refusal


def test_lesson_for_block_error_timeout_falls_back_to_generic():
    """timeout/worker_failed stay decompose-eligible; their canned fallback (when no
    decomposer replaced it) is the generic error lesson — NOT the no_candidate one."""
    generic = factory_memory.lesson_for_block("error")
    assert factory_memory.lesson_for_block("error", "timeout") == generic
    assert factory_memory.lesson_for_block("error", "worker_failed") == generic


def test_execute_transport_error_records_transport_lesson_not_bundled(tmp_path):
    """End-to-end close-out: an error(transport) result records the transport lesson,
    never the false no_candidate 'bundled too much' lesson."""
    from factory.orchestrator import develop as dev
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-cccc3333", "x", source="human")
        s.set_task_status("task-cccc3333", "in_progress", shift_id=sh)

        def fake(text, **k):
            return {"action": "error", "stage": "transport",
                    "error": "[claude -p unavailable: [Errno 2] No such file: 'claude']"}

        dev.execute_claimed_tasks(s, sh, develop_fn=fake)
        lessons = [r["content"] for r in s.learnings_for_role("factory")]
        assert not any("bundled too much" in c for c in lessons)
        assert any("transport" in c.lower() for c in lessons)


# -- execute: don't record learnings for a STOP-halted run -------------------
def test_execute_does_not_record_learnings_for_halted(tmp_path):
    from factory.orchestrator import develop as dev
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        tid = "task-bbbb2222"
        s.add_task(tid, "x", source="human")
        s.set_task_status(tid, "in_progress", shift_id=sh)

        def fake(text, **k):
            return {"action": "halted", "learnings": ["should not be recorded on a halt"]}

        dev.execute_claimed_tasks(s, sh, develop_fn=fake)
        assert s.learnings_for_role("developer") == []


# ============================================================================
# Task 0.4 (P6 stage 1): per-task failure evidence (task_evidence) — the
# factory must be able to RE-READ why a task failed (the full tests_report +
# the worker's reply head), not just the ≤200-char blocked reason string.
# ============================================================================

# -- store: task_evidence CRUD ------------------------------------------------
def test_add_task_evidence_roundtrip(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=100)
        s.add_task("task-eeee0001", "t", source="human")
        eid = s.add_task_evidence("task-eeee0001", shift_id=sh, action="discarded",
                                  stage="tests", tests_report="FAILED test_x - boom",
                                  reply_head="I wrote the fix but one test stayed red")
        assert isinstance(eid, int) and eid > 0
        rows = s.task_evidence("task-eeee0001")
        assert len(rows) == 1
        r = rows[0]
        assert r["task_id"] == "task-eeee0001" and r["shift_id"] == sh
        assert r["action"] == "discarded" and r["stage"] == "tests"
        assert r["tests_report"] == "FAILED test_x - boom"
        assert r["reply_head"].startswith("I wrote the fix")
        assert r["created_at"]


def test_task_evidence_is_task_scoped_and_newest_first(tmp_path):
    with _store(tmp_path) as s:
        s.add_task("task-eeee0002", "a", source="human")
        s.add_task("task-eeee0003", "b", source="human")
        s.add_task_evidence("task-eeee0002", action="no_candidate", reply_head="first")
        s.add_task_evidence("task-eeee0002", action="error", stage="timeout",
                            reply_head="second")
        s.add_task_evidence("task-eeee0003", action="discarded", stage="tests")
        rows = s.task_evidence("task-eeee0002")
        assert [r["reply_head"] for r in rows] == ["second", "first"]   # newest first
        assert len(s.task_evidence("task-eeee0003")) == 1


# -- close-out: one evidence row per blocked task, main thread only -----------
def test_execute_blocked_task_persists_evidence(tmp_path):
    from factory.orchestrator import develop as dev
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-dddd4444", "x", source="human")
        s.set_task_status("task-dddd4444", "in_progress", shift_id=sh)

        def fake(text, **k):
            return {"action": "discarded", "stage": "tests",
                    "tests_report": "FAILED tests/test_y.py::test_z - assert 1 == 2",
                    "reply_head": "attempted the change; one assertion stayed red"}

        dev.execute_claimed_tasks(s, sh, develop_fn=fake)
        rows = s.task_evidence("task-dddd4444")
        assert len(rows) == 1
        r = rows[0]
        assert r["action"] == "discarded" and r["stage"] == "tests" and r["shift_id"] == sh
        assert "tests/test_y.py::test_z" in r["tests_report"]
        assert "assertion stayed red" in r["reply_head"]


def test_execute_evidence_survives_auto_decompose(tmp_path):
    """The evidence insert must land BEFORE the auto-decompose `continue`, or a
    decomposed no_candidate loses its evidence forever."""
    from factory.orchestrator import develop as dev
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        tid = "task-dddd5555"
        s.add_task(tid, "too big", source="human")
        s.set_task_status(tid, "in_progress", shift_id=sh)

        def fake(text, **k):
            return {"action": "no_candidate", "reply_head": "came back empty-handed"}

        dev.execute_claimed_tasks(s, sh, develop_fn=fake,
                                  decomposer=lambda t: {"subtasks": [{"title": "slice 1"}]})
        t = s.get_task(tid)
        assert t["status"] == "blocked" and "decomposed" in t["result"]
        rows = s.task_evidence(tid)
        assert len(rows) == 1
        assert rows[0]["action"] == "no_candidate"
        assert rows[0]["reply_head"] == "came back empty-handed"


def test_execute_error_stage_rides_into_evidence(tmp_path):
    """Task 0.1's new error stages (timeout/worker_failed/transport/refusal) must be
    carried onto the evidence row so failures stay diagnosable after the shift."""
    from factory.orchestrator import develop as dev
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-dddd6666", "x", source="human")
        s.set_task_status("task-dddd6666", "in_progress", shift_id=sh)

        def fake(text, **k):
            return {"action": "error", "stage": "refusal",
                    "error": "I can't help with that",
                    "reply_head": "I can't help with that brief as written."}

        dev.execute_claimed_tasks(s, sh, develop_fn=fake)
        rows = s.task_evidence("task-dddd6666")
        assert len(rows) == 1
        assert rows[0]["action"] == "error" and rows[0]["stage"] == "refusal"
        assert "can't help" in rows[0]["reply_head"]


def test_execute_no_evidence_for_merged_or_halted(tmp_path):
    """Evidence is FAILURE forensics: a merged task and a STOP-halted run (requeued,
    not failed) must write no rows."""
    from factory.orchestrator import develop as dev
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-dddd7777", "m: ship it", source="human")
        s.add_task("task-dddd8888", "h: stopped", source="human")
        for tid in ("task-dddd7777", "task-dddd8888"):
            s.set_task_status(tid, "in_progress", shift_id=sh)

        def fake(text, **k):     # map by title — parallel workers finish in any order
            return ({"action": "merged", "merge_sha": "abc123"} if text.startswith("m:")
                    else {"action": "halted"})

        dev.execute_claimed_tasks(s, sh, develop_fn=fake)
        assert s.task_evidence("task-dddd7777") == []
        assert s.task_evidence("task-dddd8888") == []


# -- develop_and_merge: reply_head + tests_report ride OUT of the round -------
class _EvAdapter:
    """Minimal hermetic adapter for the develop_and_merge evidence-carry tests."""
    def __init__(self, *, has_branch=True, changed=("src/x.py",), tests_passed=True):
        self._has_branch, self._changed, self._tests_passed = has_branch, list(changed), tests_passed

    def clone(self, dest):
        import os
        os.makedirs(dest, exist_ok=True); return dest

    def default_branch(self, repo): return "main"
    def test_command(self): return ["pytest", "-q"]
    def frozen_paths(self): return []
    def branch_exists(self, repo, branch): return self._has_branch
    def changed_paths(self, repo, *refs): return list(self._changed)
    def fetch_candidate(self, repo, clone_dir, branch): return branch
    def add_worktree(self, repo, dest, branch):
        import os
        os.makedirs(dest, exist_ok=True); return dest
    def remove_worktree(self, repo, dest): pass
    def run_tests(self, repo, **k): return (self._tests_passed, "1 failed: test_z red")


def test_develop_and_merge_carries_reply_head_on_no_candidate(monkeypatch, tmp_path):
    from factory.orchestrator import develop
    from factory.roles import common
    reply = "analysed the brief in depth; " + "x" * 3000   # long honest reply, no branch
    monkeypatch.setattr(common, "develop_candidate",
                        lambda clone_dir, **k: {"branch": k["branch"], "reply": reply})
    ad = _EvAdapter(has_branch=False)
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"), task="t",
                                    champion_scores={"working": 0, "held_out": 0},
                                    grade_fn=lambda r: {})
    assert res["action"] == "no_candidate"
    assert res["reply_head"] == reply[:2000]                # capped at 2000 chars


def test_develop_and_merge_carries_reply_head_and_tests_report_on_red_tests(monkeypatch, tmp_path):
    from factory.orchestrator import develop
    from factory.roles import common
    monkeypatch.setattr(common, "develop_candidate",
                        lambda clone_dir, **k: {"branch": k["branch"], "reply": "did the work"})
    ad = _EvAdapter(tests_passed=False)
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"), task="t",
                                    champion_scores={"working": 0, "held_out": 0},
                                    grade_fn=lambda r: {}, require_test=False)
    assert res["action"] == "discarded" and res["stage"] == "tests"
    assert res["tests_report"] == "1 failed: test_z red"    # already rode out of run_code_round
    assert res["reply_head"] == "did the work"              # now rides alongside it


# ============================================================================
# Task 0.5: count recurrence on dedup-hit (`hits` column) — a deduped report
# must BUMP the matched learning's counter, not vanish. The frequency signal
# is the factory's cheapest severity ranking.
# ============================================================================

# -- store: hits column + bump + exact-id read --------------------------------
def test_add_learning_starts_with_hits_one(tmp_path):
    with _store(tmp_path) as s:
        lid = s.add_learning("factory", "first sighting")
        assert s.learnings_for_role("factory")[0]["hits"] == 1
        assert s.get_learning(lid)["hits"] == 1


def test_bump_learning_hits_increments_one_row(tmp_path):
    with _store(tmp_path) as s:
        a = s.add_learning("factory", "A")
        b = s.add_learning("factory", "B")
        s.bump_learning_hits(a)
        s.bump_learning_hits(a)
        assert s.get_learning(a)["hits"] == 3
        assert s.get_learning(b)["hits"] == 1               # only the matched row bumps


def test_get_learning_unknown_id_is_none(tmp_path):
    with _store(tmp_path) as s:
        assert s.get_learning(999) is None


def test_migrate_adds_hits_to_predating_db(tmp_path):
    """A DB created before the column existed gains `hits` via _migrate (CREATE TABLE
    IF NOT EXISTS alone won't alter the existing table)."""
    import sqlite3
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE learnings (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, "
        "agent TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'general', "
        "content TEXT NOT NULL, shift_id INTEGER, uses INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO learnings(role, content, created_at) VALUES ('factory','pre','t')")
    conn.commit()
    conn.close()
    with Blackboard(db) as s:
        s.init_db()
        rows = s.learnings_for_role("factory")
        assert rows and rows[0]["hits"] == 1                # backfilled default
        lid = s.add_learning("factory", "post-migration lesson")
        assert s.get_learning(lid)["hits"] == 1


# -- module: _is_dup returns the matched ROW ----------------------------------
def test_is_dup_returns_matched_row(tmp_path):
    existing = [{"id": 7, "content": "narrow the brief"}]
    hit = factory_memory._is_dup("narrow the brief", existing)
    assert hit is existing[0]                               # the row, not a bare bool


# -- module: record_learning returns (id, created) + bumps on dup -------------
def test_record_learning_new_returns_created_true(tmp_path):
    with _store(tmp_path) as s:
        rec = factory_memory.record_learning(s, "developer", "fresh lesson")
        assert isinstance(rec, tuple)
        lid, created = rec
        assert isinstance(lid, int) and created is True


def test_record_learning_dup_bumps_hits_and_returns_created_false(tmp_path):
    with _store(tmp_path) as s:
        first_id, _ = factory_memory.record_learning(s, "factory", "graduate when divergence grows")
        rec = factory_memory.record_learning(s, "factory", "graduate when divergence grows")
        assert rec == (first_id, False)                     # same id, not created
        assert len(s.learnings_for_role("factory")) == 1    # still one row
        assert s.get_learning(first_id)["hits"] == 2        # recurrence counted


# -- module: memory_card surfaces the recurrence signal -----------------------
def test_memory_card_marks_recurring_at_three_hits(tmp_path):
    with _store(tmp_path) as s:
        for _ in range(3):
            factory_memory.record_learning(s, "developer", "the flaky gate strikes again")
        card = factory_memory.memory_card(s, "developer")
        assert "the flaky gate strikes again (recurring x3)" in card


def test_memory_card_no_recurring_marker_below_three_hits(tmp_path):
    with _store(tmp_path) as s:
        factory_memory.record_learning(s, "developer", "seen twice only")
        factory_memory.record_learning(s, "developer", "seen twice only")
        card = factory_memory.memory_card(s, "developer")
        assert "seen twice only" in card and "recurring" not in card


def test_memory_card_marks_recurring_factory_rows_too(tmp_path):
    with _store(tmp_path) as s:
        for _ in range(4):
            factory_memory.record_learning(s, "factory", "shared recurring hazard")
        card = factory_memory.memory_card(s, "developer")
        assert "shared recurring hazard (recurring x4)" in card


# -- CLI: reinforced print + hits in list -------------------------------------
def test_cmd_learn_add_dup_prints_reinforced_with_count(tmp_path, capsys):
    with _store(tmp_path) as s:
        orch.cmd_learn(s, "add", role="developer", content="same lesson")
        capsys.readouterr()
        lid = orch.cmd_learn(s, "add", role="developer", content="same lesson")
        out = capsys.readouterr().out
        assert "reinforced" in out and f"#{lid}" in out and "(x2)" in out
        assert len(s.learnings_for_role("developer")) == 1


def test_cmd_learn_list_shows_hits(tmp_path, capsys):
    with _store(tmp_path) as s:
        factory_memory.record_learning(s, "developer", "counted lesson")
        factory_memory.record_learning(s, "developer", "counted lesson")
        orch.cmd_learn(s, "list", role="developer")
        out = capsys.readouterr().out
        assert "hits 2" in out
