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


# -- Theme 6: outcome-based suppression of counterproductive lessons ----------
# A wrong auto-written lesson is injected into every card forever. The factory already
# attributes each surfaced lesson's downstream outcome (merged_after / blocked_after). Once a
# lesson has enough evidence (n >= EFFECTIVENESS_MIN_N) and a poor merge share, it is dropped
# from the card — data preserved (still in `learn list`), reversible, pin overrides.

def test_memory_card_suppresses_counterproductive_learnings(tmp_path):
    with _store(tmp_path) as s:
        factory_memory.record_learning(s, "developer", "narrow briefs to one landable slice")
        bad = s.add_learning("developer", "always refactor the whole module first")
        s.bump_learning_outcomes([bad], merged=True)              # 1 merge …
        for _ in range(14):
            s.bump_learning_outcomes([bad], merged=False)         # … 14 blocks → n=15, share≈0.07
        card = factory_memory.memory_card(s, "developer")
        assert "narrow briefs" in card                            # healthy lesson kept
        assert "refactor the whole module" not in card            # proven-bad lesson suppressed


def test_memory_card_keeps_bad_ratio_below_evidence_floor(tmp_path):
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "prefer sibling-site helper reuse")
        s.bump_learning_outcomes([lid], merged=False)             # 0/1 — n < EFFECTIVENESS_MIN_N
        card = factory_memory.memory_card(s, "developer")
        assert "sibling-site helper" in card                      # too little evidence → kept


def test_memory_card_keeps_pinned_even_when_counterproductive(tmp_path):
    with _store(tmp_path) as s:
        bad = s.add_learning("developer", "rewrite the tests from scratch every time")
        s.bump_learning_outcomes([bad], merged=True)
        for _ in range(14):
            s.bump_learning_outcomes([bad], merged=False)
        s.pin_learning(bad)                                        # operator override
        card = factory_memory.memory_card(s, "developer")
        assert "rewrite the tests from scratch" in card           # pin wins over auto-suppression


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


# ============================================================================
# Task 1.3 (learnings hygiene): `learn retire` + deterministic staleness verify.
# retire = the operator's correction handle (must exist BEFORE any LLM authors
# lessons); verify = zero-token regex cite-check, advisory only — flags stale,
# never deletes, never archives on its own.
# ============================================================================

# -- store: archived column ---------------------------------------------------
def test_archive_learning_hides_from_role_learnings(tmp_path):
    with _store(tmp_path) as s:
        a = s.add_learning("developer", "outdated advice")
        b = s.add_learning("developer", "current advice")
        s.archive_learning(a)
        assert [r["id"] for r in s.learnings_for_role("developer")] == [b]
        assert (s.get_learning(a) or {}).get("archived") == 1    # row survives, just hidden


def test_migrate_adds_archived_and_stale_to_old_db(tmp_path):
    import sqlite3
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)                       # a pre-Task-1.3 learnings table
    conn.execute("""CREATE TABLE learnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL,
        agent TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'general',
        content TEXT NOT NULL, shift_id INTEGER,
        uses INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""")
    conn.commit()
    conn.close()
    with Blackboard(db) as s:
        s.init_db()
        cols = {r[1] for r in s.conn.execute("PRAGMA table_info(learnings)").fetchall()}
        assert "archived" in cols and "stale" in cols


def test_set_learning_stale_roundtrip(tmp_path):
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "see gone.py:5")
        assert s.get_learning(lid)["stale"] == 0
        s.set_learning_stale(lid, True)
        assert s.get_learning(lid)["stale"] == 1
        s.set_learning_stale(lid, False)
        assert s.get_learning(lid)["stale"] == 0


# -- CLI: factory learn retire (exact-id discipline) ---------------------------
def test_cmd_learn_retire_archives_exact_id(tmp_path, capsys):
    with _store(tmp_path) as s:
        lid = s.add_learning("factory", "retire me")
        orch.cmd_learn(s, "retire", learning_id=str(lid))
        assert s.get_learning(lid)["archived"] == 1
        assert "retired" in capsys.readouterr().out


def test_cmd_learn_retire_refuses_unknown_id(tmp_path, capsys):
    with _store(tmp_path) as s:
        lid = s.add_learning("factory", "keep me")
        orch.cmd_learn(s, "retire", learning_id="9999")
        assert "0 rows" in capsys.readouterr().out           # explicit refusal, never silent
        assert s.get_learning(lid)["archived"] == 0


def test_cmd_learn_retire_refuses_non_integer_id(tmp_path, capsys):
    with _store(tmp_path) as s:
        lid = s.add_learning("factory", "keep me too")
        orch.cmd_learn(s, "retire", learning_id="task-abc123")
        assert "0 rows" in capsys.readouterr().out
        assert s.get_learning(lid)["archived"] == 0


def test_retired_learning_leaves_memory_card(tmp_path):
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "bad advice that got retired")
        s.archive_learning(lid)
        assert factory_memory.memory_card(s, "developer") == ""


# -- retire must be durable against re-recording (dedup sees archived rows) ----
def test_learnings_for_role_include_archived_is_the_dedup_window(tmp_path):
    with _store(tmp_path) as s:
        a = s.add_learning("developer", "retired advice")
        b = s.add_learning("developer", "live advice")
        s.archive_learning(a)
        assert [r["id"] for r in s.learnings_for_role("developer")] == [b]
        assert [r["id"] for r in
                s.learnings_for_role("developer", include_archived=True)] == [b, a]


def test_retired_learning_absorbs_rereport_without_resurrecting(tmp_path):
    """The factory auto-records templated lessons on recurring failures (lesson_for_block),
    so the EXACT lesson an operator retires WILL be reported again verbatim. Dedup must
    still match the archived row — bump hits, return created=False — instead of creating
    a fresh live row that silently undoes the operator's `learn retire`."""
    with _store(tmp_path) as s:
        lid, created = factory_memory.record_learning(s, "factory", "retire me forever")
        assert created
        s.archive_learning(lid)
        again = factory_memory.record_learning(s, "factory", "retire me forever")
        assert again == (lid, False)                    # deduped onto the RETIRED row
        row = s.get_learning(lid)
        assert row["archived"] == 1                     # stays hidden
        assert row["hits"] == 2                         # recurrence still counted
        assert s.learnings_for_role("factory") == []    # the advice does NOT come back
        assert factory_memory.memory_card(s, "factory") == ""


# -- extract_cites (regex, zero tokens) ----------------------------------------
def test_extract_cites_paths_lines_and_bare_basenames():
    cites = factory_memory.extract_cites(
        "the retry helper lives in llm.py:262, reuse it — judges in reporting/scope_check.py")
    assert ("llm.py", 262) in cites
    assert ("reporting/scope_check.py", None) in cites


def test_extract_cites_ignores_plain_prose():
    assert factory_memory.extract_cites("narrow the brief to one landable slice") == []
    assert factory_memory.extract_cites("") == []


# -- verify_learnings (deterministic staleness) ---------------------------------
def test_verify_flags_missing_file_as_stale(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "the helper lives in gone.py:10")
        report = factory_memory.verify_learnings(s, roots={"developer": str(target)})
        assert s.get_learning(lid)["stale"] == 1
        assert any(e["id"] == lid and e["stale"] for e in report)


def test_verify_flags_line_beyond_eof_as_stale(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "session.py").write_text("line1\nline2\n")
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "see session.py:278 for the retry")
        factory_memory.verify_learnings(s, roots={"developer": str(target)})
        assert s.get_learning(lid)["stale"] == 1


def test_verify_unique_basename_rglob_fallback(tmp_path):
    """Live cites are mostly bare basenames (session.py:278) — a path-prefix
    resolve would no-op; a UNIQUE basename anywhere in the tree must resolve."""
    target = tmp_path / "target"
    (target / "pkg" / "sub").mkdir(parents=True)
    (target / "pkg" / "sub" / "session.py").write_text("\n".join(["x"] * 300) + "\n")
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "the retry helper is at session.py:278")
        factory_memory.verify_learnings(s, roots={"developer": str(target)})
        assert s.get_learning(lid)["stale"] == 0


def test_verify_ambiguous_basename_is_not_stale(tmp_path):
    """Two files share the cited basename → can't know which; an advisory tool
    must not false-positive, so ambiguity is NOT stale evidence."""
    target = tmp_path / "target"
    (target / "a").mkdir(parents=True)
    (target / "b").mkdir(parents=True)
    (target / "a" / "session.py").write_text("x\n")
    (target / "b" / "session.py").write_text("x\n")
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "see session.py:278")
        factory_memory.verify_learnings(s, roots={"developer": str(target)})
        assert s.get_learning(lid)["stale"] == 0


def test_verify_resolves_cites_per_role(tmp_path):
    """factory-role cites check against the factory repo; developer/conductor
    cites check against the TARGET checkout (a live row cites reporting/scope_check.py)."""
    fac = tmp_path / "fac"
    (fac / "reporting").mkdir(parents=True)
    (fac / "reporting" / "scope_check.py").write_text("x\n")
    target = tmp_path / "target"
    target.mkdir()
    with _store(tmp_path) as s:
        f = s.add_learning("factory", "judges live in reporting/scope_check.py")
        d = s.add_learning("developer", "judges live in reporting/scope_check.py")
        factory_memory.verify_learnings(
            s, roots={"factory": str(fac), "developer": str(target)})
        assert s.get_learning(f)["stale"] == 0
        assert s.get_learning(d)["stale"] == 1


def test_verify_clears_stale_when_cite_resolves_again(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "see helper.py:1")
        factory_memory.verify_learnings(s, roots={"developer": str(target)})
        assert s.get_learning(lid)["stale"] == 1
        (target / "helper.py").write_text("x\n")
        factory_memory.verify_learnings(s, roots={"developer": str(target)})
        assert s.get_learning(lid)["stale"] == 0


def test_verify_is_advisory_never_deletes_or_archives(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "see gone.py:9")
        factory_memory.verify_learnings(s, roots={"developer": str(target)})
        row = s.get_learning(lid)
        assert row is not None and row["archived"] == 0 and row["stale"] == 1
        assert [r["id"] for r in s.learnings_for_role("developer")] == [lid]


def test_verify_skips_archived_rows(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "see gone.py:9")
        s.archive_learning(lid)
        report = factory_memory.verify_learnings(s, roots={"developer": str(target)})
        assert all(e["id"] != lid for e in report)
        assert s.get_learning(lid)["stale"] == 0             # untouched


# -- stale suffix in the memory card -------------------------------------------
def test_memory_card_flags_stale_rows(tmp_path):
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "the retry helper is at session.py:278")
        s.set_learning_stale(lid, True)
        card = factory_memory.memory_card(s, "developer")
        assert "session.py:278 (may be stale — cited file moved)" in card


def test_memory_card_no_stale_suffix_when_fresh(tmp_path):
    with _store(tmp_path) as s:
        s.add_learning("developer", "a perfectly fresh lesson")
        assert "may be stale" not in factory_memory.memory_card(s, "developer")


# -- CLI: factory learn verify ---------------------------------------------------
def test_cmd_learn_verify_prints_summary(tmp_path, capsys, monkeypatch):
    target = tmp_path / "t"
    target.mkdir()
    monkeypatch.setattr(factory_memory, "_role_root", lambda role: str(target))
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "see gone.py:9")
        s.add_learning("developer", "no cites here at all")
        orch.cmd_learn(s, "verify")
        out = capsys.readouterr().out
        assert "stale" in out and f"#{lid}" in out
        assert s.get_learning(lid)["stale"] == 1


def test_cli_learn_retire_wired_through_main(tmp_path, monkeypatch, capsys):
    """`factory learn retire <id>` end-to-end: the argparse arm carries the positional
    id into cmd_learn (hermetic store — same pattern as the viz --selfcheck test)."""
    db = str(tmp_path / "f.db")
    with Blackboard(db) as s:
        s.init_db()
        lid = s.add_learning("factory", "wired lesson")
    monkeypatch.setattr(orch, "Blackboard", lambda *a, **k: Blackboard(db))
    orch.main(["learn", "retire", str(lid)])
    assert "retired" in capsys.readouterr().out
    with Blackboard(db) as s:
        assert s.get_learning(lid)["archived"] == 1


# ============================================================================
# Task 1.4: consult-telemetry + per-task relevant memory card. The worker card
# is scoped by keyword overlap with the task's own title+detail (an old but
# on-point lesson resurfaces instead of aging out), and the surfaced ids get
# outcome attribution (merged_after/blocked_after) at close-out — signal, not
# proof, so the ratio is SUPPRESSED below a minimum denominator.
# ============================================================================

# -- store: merged_after/blocked_after columns ---------------------------------
def test_learning_starts_with_zero_outcome_counters(tmp_path):
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "fresh, never attributed")
        row = s.get_learning(lid)
        assert row["merged_after"] == 0 and row["blocked_after"] == 0


def test_migrate_adds_outcome_columns_to_old_db(tmp_path):
    import sqlite3
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)                       # a pre-Task-1.4 learnings table
    conn.execute("""CREATE TABLE learnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL,
        agent TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'general',
        content TEXT NOT NULL, shift_id INTEGER,
        uses INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""")
    conn.execute("INSERT INTO learnings(role, content, created_at) VALUES ('factory','pre','t')")
    conn.commit()
    conn.close()
    with Blackboard(db) as s:
        s.init_db()
        row = s.learnings_for_role("factory")[0]
        assert row["merged_after"] == 0 and row["blocked_after"] == 0  # backfilled default


def test_migrate_adds_pinned_to_old_db(tmp_path):
    import sqlite3
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)                       # a pre-Task-4.2 learnings table
    conn.execute("""CREATE TABLE learnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL,
        agent TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'general',
        content TEXT NOT NULL, shift_id INTEGER,
        uses INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""")
    conn.execute("INSERT INTO learnings(role, content, created_at) VALUES ('factory','pre','t')")
    conn.commit()
    conn.close()
    with Blackboard(db) as s:
        s.init_db()
        row = s.learnings_for_role("factory")[0]
        assert row["pinned"] == 0                        # backfilled default: not pinned
        s.pin_learning(row["id"])                         # the migrated column is writable
        assert s.pinned_for_role("factory")[0]["id"] == row["id"]


def test_bump_learning_outcomes_batched_and_scoped(tmp_path):
    with _store(tmp_path) as s:
        a = s.add_learning("developer", "A")
        b = s.add_learning("developer", "B")
        c = s.add_learning("developer", "C")
        s.bump_learning_outcomes([a, b], merged=True)
        s.bump_learning_outcomes([a], merged=False)
        assert (s.get_learning(a)["merged_after"], s.get_learning(a)["blocked_after"]) == (1, 1)
        assert (s.get_learning(b)["merged_after"], s.get_learning(b)["blocked_after"]) == (1, 0)
        assert (s.get_learning(c)["merged_after"], s.get_learning(c)["blocked_after"]) == (0, 0)


def test_bump_learning_outcomes_empty_is_noop(tmp_path):
    with _store(tmp_path) as s:
        s.bump_learning_outcomes([], merged=True)           # must not raise


# -- module: memory_card_with_ids ----------------------------------------------
def test_memory_card_is_thin_wrapper_over_with_ids(tmp_path):
    with _store(tmp_path) as s:
        s.add_learning("developer", "dev lesson")
        s.add_learning("factory", "factory lesson")
        text, ids = factory_memory.memory_card_with_ids(s, "developer")
        assert factory_memory.memory_card(s, "developer") == text
        assert "dev lesson" in text and "factory lesson" in text


def test_memory_card_with_ids_returns_surfaced_ids(tmp_path):
    with _store(tmp_path) as s:
        d = s.add_learning("developer", "dev lesson")
        f = s.add_learning("factory", "factory lesson")
        _, ids = factory_memory.memory_card_with_ids(s, "developer")
        assert sorted(ids) == sorted([d, f])


def test_memory_card_with_ids_empty(tmp_path):
    with _store(tmp_path) as s:
        assert factory_memory.memory_card_with_ids(s, "developer") == ("", [])


def test_memory_card_with_ids_topic_pulls_relevant_old_row(tmp_path):
    """An OLD lesson sharing keywords with the task's brief must surface via the
    relevance leg even after it aged out of the newest-N window."""
    with _store(tmp_path) as s:
        old = s.add_learning("developer", "the tokenizer retry helper lives in llm.py")
        for i in range(9):
            s.add_learning("developer", f"unrelated filler lesson number {i}")
        no_topic, _ = factory_memory.memory_card_with_ids(s, "developer")
        assert "tokenizer retry helper" not in no_topic     # aged out of newest-8
        card, ids = factory_memory.memory_card_with_ids(
            s, "developer", topic="fix the tokenizer retry logic in llm.py")
        assert "tokenizer retry helper" in card
        assert old in ids


def test_memory_card_with_ids_topic_keeps_newest_rows(tmp_path):
    """The newest lessons ride along even with ZERO topic overlap — a fresh lesson
    matters regardless of the task at hand."""
    with _store(tmp_path) as s:
        newest = s.add_learning("developer", "brand new off-topic wisdom")
        card, ids = factory_memory.memory_card_with_ids(
            s, "developer", topic="completely disjoint subject matter")
        assert "brand new off-topic wisdom" in card
        assert newest in ids


def test_memory_card_with_ids_topic_caps_relevant_at_four(tmp_path):
    """top-4 relevant + newest-4: six equally relevant old rows → only the four
    newest of them make the relevance leg."""
    with _store(tmp_path) as s:
        words = ["one", "two", "three", "four", "five", "six"]
        for w in words:
            s.add_learning("developer", f"tokenizer retry variant {w}")
        for i in range(4):
            s.add_learning("developer", f"filler lesson {i}")
        card, ids = factory_memory.memory_card_with_ids(
            s, "developer", topic="handle the tokenizer retry")
        assert "variant one" not in card and "variant two" not in card   # the 2 oldest lose
        for w in words[2:]:
            assert f"variant {w}" in card
        assert len(ids) == 8                                # 4 relevant + 4 newest


def test_memory_card_with_ids_topic_scopes_factory_rows_too(tmp_path):
    with _store(tmp_path) as s:
        old_f = s.add_learning("factory", "graduation diverges when llm.py churns")
        for i in range(9):
            s.add_learning("factory", f"factory filler {i}")
        card, ids = factory_memory.memory_card_with_ids(
            s, "developer", topic="refactor llm.py graduation flow")
        assert "graduation diverges" in card
        assert old_f in ids


def test_memory_card_with_ids_bumps_uses(tmp_path):
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "count my surfacing")
        factory_memory.memory_card_with_ids(s, "developer", topic="count surfacing")
        assert s.get_learning(lid)["uses"] == 1


# -- execute: per-task card replaces the shift-wide dev_card --------------------
def test_execute_builds_per_task_relevant_card(tmp_path):
    """Each dispatched task gets its OWN card scoped to its title+detail — an old
    on-point lesson reaches exactly the task it is relevant to."""
    from factory.orchestrator import develop as dev
    with _store(tmp_path) as s:
        s.add_learning("developer", "alpha subsystem quirk: tokenizer needs utf8 guard")
        s.add_learning("developer", "beta subsystem quirk: scheduler drops idle workers")
        for i in range(4):                                  # age both out of the newest-4 leg
            s.add_learning("developer", f"filler lesson {i}")
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-aaaa0014", "fix tokenizer", detail="utf8 guard in the alpha subsystem",
                   source="human")
        s.add_task("task-bbbb0014", "fix scheduler", detail="idle workers in the beta subsystem",
                   source="human")
        s.set_task_status("task-aaaa0014", "in_progress", shift_id=sh)
        s.set_task_status("task-bbbb0014", "in_progress", shift_id=sh)

        seen = {}

        def fake(text, **k):
            seen[text.split(":")[0]] = k.get("memory", "")
            return {"action": "no_candidate"}

        dev.execute_claimed_tasks(s, sh, develop_fn=fake)
        assert "tokenizer needs utf8 guard" in seen["fix tokenizer"]
        assert "scheduler drops idle" not in seen["fix tokenizer"]
        assert "scheduler drops idle" in seen["fix scheduler"]
        assert "tokenizer needs utf8 guard" not in seen["fix scheduler"]


def test_execute_bumps_merged_after_on_surfaced_ids(tmp_path):
    from factory.orchestrator import develop as dev
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "always run the target tests first")
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-cccc0014", "run the target tests", source="human")
        s.set_task_status("task-cccc0014", "in_progress", shift_id=sh)
        dev.execute_claimed_tasks(
            s, sh, develop_fn=lambda text, **k: {"action": "merged", "merge_sha": "abc123"})
        row = s.get_learning(lid)
        assert row["merged_after"] == 1 and row["blocked_after"] == 0


def test_execute_bumps_blocked_after_on_surfaced_ids(tmp_path):
    from factory.orchestrator import develop as dev
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "always run the target tests first")
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-dddd0014", "run the target tests", source="human")
        s.set_task_status("task-dddd0014", "in_progress", shift_id=sh)
        dev.execute_claimed_tasks(
            s, sh, develop_fn=lambda text, **k: {"action": "no_candidate"})
        row = s.get_learning(lid)
        assert row["merged_after"] == 0 and row["blocked_after"] == 1


def test_execute_halted_attributes_no_outcome(tmp_path):
    from factory.orchestrator import develop as dev
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "always run the target tests first")
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-eeee0014", "run the target tests", source="human")
        s.set_task_status("task-eeee0014", "in_progress", shift_id=sh)
        dev.execute_claimed_tasks(s, sh, develop_fn=lambda text, **k: {"action": "halted"})
        row = s.get_learning(lid)
        assert row["merged_after"] == 0 and row["blocked_after"] == 0


# -- Fix 1.4b: attribute outcomes ONLY when a model consulted the card ----------
# An infrastructural failure (transport outage, Guest-House chown, a pre-dispatch
# blow-up) never ran the worker — no model saw the memory card, so bumping
# blocked_after would poison the effectiveness ratio for the newest/most-relevant
# lessons during every outage. Refusal/timeout/worker_failed DID consume the brief.
def _attribution_run(tmp_path, result):
    """Dispatch one task through execute_claimed_tasks with a canned worker result;
    return the surfaced learning row's outcome counters."""
    from factory.orchestrator import develop as dev
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "always run the target tests first")
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-ffff0014", "run the target tests", source="human")
        s.set_task_status("task-ffff0014", "in_progress", shift_id=sh)
        if callable(result):
            dev.execute_claimed_tasks(s, sh, develop_fn=result)
        else:
            dev.execute_claimed_tasks(s, sh, develop_fn=lambda text, **k: dict(result))
        return s.get_learning(lid)


def test_execute_transport_error_attributes_no_outcome(tmp_path):
    """error(stage='transport') = claude binary unavailable — the worker never ran."""
    row = _attribution_run(tmp_path, {"action": "error", "stage": "transport",
                                      "error": "[claude -p unavailable: No such file]"})
    assert row["merged_after"] == 0 and row["blocked_after"] == 0


def test_execute_predispatch_error_attributes_no_outcome(tmp_path):
    """work()'s except handler yields a bare {'action':'error'} with NO stage —
    a pre-dispatch blow-up (e.g. clone failed) never showed the model the card."""
    def boom(text, **k):
        raise RuntimeError("clone failed before dispatch")
    row = _attribution_run(tmp_path, boom)
    assert row["merged_after"] == 0 and row["blocked_after"] == 0


def test_execute_chown_discard_attributes_no_outcome(tmp_path):
    """discarded(stage='chown') happens BEFORE develop_candidate — same class."""
    row = _attribution_run(tmp_path, {"action": "discarded", "stage": "chown",
                                      "error": "sudo chown failed"})
    assert row["merged_after"] == 0 and row["blocked_after"] == 0


def test_execute_refusal_still_bumps_blocked_after(tmp_path):
    """A refusal consumed the brief — the model read the card and declined."""
    row = _attribution_run(tmp_path, {"action": "error", "stage": "refusal",
                                      "error": "I can't help with that"})
    assert row["merged_after"] == 0 and row["blocked_after"] == 1


def test_execute_timeout_still_bumps_blocked_after(tmp_path):
    row = _attribution_run(tmp_path, {"action": "error", "stage": "timeout",
                                      "error": "[claude -p timed out after 1800s]"})
    assert row["merged_after"] == 0 and row["blocked_after"] == 1


def test_execute_worker_failed_still_bumps_blocked_after(tmp_path):
    row = _attribution_run(tmp_path, {"action": "error", "stage": "worker_failed",
                                      "error": "[claude -p rc=1]"})
    assert row["merged_after"] == 0 and row["blocked_after"] == 1


def test_execute_merged_still_bumps_merged_after(tmp_path):
    row = _attribution_run(tmp_path, {"action": "merged", "merge_sha": "abc123"})
    assert row["merged_after"] == 1 and row["blocked_after"] == 0


# -- effectiveness ratio: suppressed below the minimum denominator --------------
def test_effectiveness_none_below_min_denominator(tmp_path):
    row = {"merged_after": 6, "blocked_after": 3}           # n=9 < 10 → noise, suppressed
    assert factory_memory.effectiveness(row) is None


def test_effectiveness_ratio_at_min_denominator(tmp_path):
    row = {"merged_after": 7, "blocked_after": 3}           # n=10 → shown
    share, n = factory_memory.effectiveness(row)
    assert n == 10 and abs(share - 0.7) < 1e-9


def test_cmd_learn_list_shows_ratio_only_above_min_denominator(tmp_path, capsys):
    with _store(tmp_path) as s:
        strong = s.add_learning("developer", "well-attributed lesson")
        weak = s.add_learning("developer", "barely-attributed lesson")
        for _ in range(7):
            s.bump_learning_outcomes([strong], merged=True)
        for _ in range(3):
            s.bump_learning_outcomes([strong], merged=False)
        s.bump_learning_outcomes([weak], merged=True)       # n=1 → suppressed
        orch.cmd_learn(s, "list", role="developer")
        out = capsys.readouterr().out
        strong_line = next(ln for ln in out.splitlines() if "well-attributed" in ln)
        weak_line = next(ln for ln in out.splitlines() if "barely-attributed" in ln)
        assert "70%" in strong_line and "10" in strong_line
        assert "%" not in weak_line


# ============================================================================
# Fix 1.3b — adversarial-review hardening of Task 1.3 (commit 9ad8291).
# FINDING A: the polymorphic `learn` positional bound `learn add "<text>"` to the
# retire id slot and silently DROPPED the lesson — the documented task-add
# content-drop bug class (operator memory). FINDING B: _CITE_RE extracted URL
# path segments as file cites, so a docs link false-flagged a learning stale.
# ============================================================================

# -- FINDING A: `factory learn add "<text>"` must record the text ---------------
def test_cli_learn_add_positional_text_records_through_main(tmp_path, monkeypatch, capsys):
    """`factory learn add "some lesson text"` end-to-end: the positional is the
    learning TEXT for the add action — it must be recorded, never swallowed by
    the retire id slot (hermetic store, same pattern as the retire main test)."""
    db = str(tmp_path / "f.db")
    monkeypatch.setattr(orch, "Blackboard", lambda *a, **k: Blackboard(db))
    orch.main(["learn", "add", "some lesson text"])
    out = capsys.readouterr().out
    assert "recorded" in out and "not recorded" not in out
    with Blackboard(db) as s:
        rows = s.learnings_for_role("factory")              # add defaults to factory role
        assert [r["content"] for r in rows] == ["some lesson text"]


def test_cli_learn_add_content_flag_still_records_through_main(tmp_path, monkeypatch, capsys):
    """The documented `--content` spelling keeps working alongside the positional."""
    db = str(tmp_path / "f.db")
    monkeypatch.setattr(orch, "Blackboard", lambda *a, **k: Blackboard(db))
    orch.main(["learn", "add", "--role", "developer", "--content", "flagged lesson"])
    assert "recorded" in capsys.readouterr().out
    with Blackboard(db) as s:
        assert [r["content"] for r in s.learnings_for_role("developer")] == ["flagged lesson"]


def test_cli_learn_retire_still_takes_positional_id_through_main(tmp_path, monkeypatch, capsys):
    """Fixing add must not regress retire: the positional stays the exact id there."""
    db = str(tmp_path / "f.db")
    with Blackboard(db) as s:
        s.init_db()
        lid = s.add_learning("factory", "retire me via main")
    monkeypatch.setattr(orch, "Blackboard", lambda *a, **k: Blackboard(db))
    orch.main(["learn", "retire", str(lid)])
    assert "retired" in capsys.readouterr().out
    with Blackboard(db) as s:
        assert s.get_learning(lid)["archived"] == 1


# -- FINDING B: URL path segments are not file cites -----------------------------
def test_extract_cites_ignores_url_path_segments():
    """'https://docs.python.org/3/library/re.html' must yield NO cite — a URL path
    can never resolve against a repo root and would false-flag the row stale."""
    assert factory_memory.extract_cites(
        "see https://docs.python.org/3/library/re.html for the syntax") == []


def test_extract_cites_ignores_github_blob_url():
    assert factory_memory.extract_cites(
        "upstream: https://github.com/acme/clive/blob/main/execution/runtime.py") == []


def test_extract_cites_keeps_real_cite_alongside_url():
    """Skipping URL spans must not eat a genuine file cite in the same lesson."""
    cites = factory_memory.extract_cites(
        "reuse the retry in llm.py:12 — background: https://docs.python.org/3/library/re.html")
    assert cites == [("llm.py", 12)]


def test_verify_url_only_learning_never_flagged_stale(tmp_path):
    """A learning whose only 'cite' is inside a URL is cite-free to verify: it is
    skipped entirely (no report entry) and never flagged stale."""
    target = tmp_path / "target"
    target.mkdir()                                          # empty repo — nothing resolves
    with _store(tmp_path) as s:
        lid = s.add_learning("developer",
                             "regex syntax: https://docs.python.org/3/library/re.html")
        report = factory_memory.verify_learnings(s, roots={"developer": str(target)})
        assert all(e["id"] != lid for e in report)
        assert s.get_learning(lid)["stale"] == 0


# ============================================================================
# Task 4.1 (P6 stages 2-3): post-shift investigator for blocked tasks. After
# close-out, up to 3 blocked-this-shift tasks WITH a task_evidence row, scoped
# to discarded(tests) and error stages only, get ONE isolated claude_p at
# STANDARD tier → {cause, lesson, followup?}; the lesson is recorded
# scope='investigated', spend ledgered notes='investigate' WITH shift_id,
# killswitch checked FIRST, fail-open to the canned lesson. NO task spawned.
# ============================================================================

def _blocked_with_evidence(s, sh, tid, *, action, stage, title=None, spec=None,
                           tests_report="r", reply_head="h"):
    """Helper: a task blocked THIS shift carrying one task_evidence row (the
    investigator's precondition — a blocked task with recoverable evidence)."""
    s.add_task(tid, title or tid, source="human", spec=spec)
    s.set_task_status(tid, "blocked", result="blocked reason", shift_id=sh)
    s.add_task_evidence(tid, shift_id=sh, action=action, stage=stage,
                        tests_report=tests_report, reply_head=reply_head)


def test_investigate_blocked_records_investigated_lesson_at_standard_tier(tmp_path):
    """The happy path: a discarded(tests) blocked task → a case-specific investigated
    lesson recorded scope='investigated' at the STANDARD tier (NOT frontier — P10)."""
    from factory.common import config
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=100000)
        _blocked_with_evidence(s, sh, "task-inv00001", action="discarded", stage="tests",
                               tests_report="FAILED tests/test_a.py::test_b - assert 1 == 2",
                               reply_head="wrote the fix but one assertion stayed red")
        seen = {}

        def fake(prompt, *, model="", **k):
            seen["model"] = model
            seen["prompt"] = prompt
            return ('{"cause":"asserted the wrong constant","lesson":"pin the fixture seed '
                    'before asserting the derived value"}', 12, 0.002)

        out = factory_memory.investigate_blocked(s, sh, claude_fn=fake)
        assert seen["model"] == config.resolve_model("standard") != ""    # standard, not frontier
        assert "FAILED tests/test_a.py::test_b" in seen["prompt"]          # evidence reached it
        rows = [r for r in s.learnings_for_role("factory") if r["scope"] == "investigated"]
        assert rows and "pin the fixture seed" in rows[0]["content"]
        assert len(out) == 1 and out[0]["task_id"] == "task-inv00001"


def test_investigate_blocked_ledgers_spend_with_shift_id(tmp_path):
    """Spend is ledgered notes='investigate' WITH the shift_id, so it folds into the
    loop token brake (shift_spend)."""
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=100000)
        _blocked_with_evidence(s, sh, "task-invled01", action="error", stage="timeout")

        def fake(prompt, **k):
            return ('{"cause":"c","lesson":"a specific investigated timeout lesson"}', 42, 0.005)

        factory_memory.investigate_blocked(s, sh, claude_fn=fake)
        rows = [r for r in s.ledger_rows(shift_id=sh) if r["notes"] == "investigate"]
        assert rows and rows[0]["tokens"] == 42
        assert s.shift_spend(sh)["tokens"] == 42


def test_investigate_blocked_stop_vetoes_all_spend(tmp_path, monkeypatch):
    """killswitch.is_halted() is checked FIRST — STOP vetoes even read-only
    investigation spend: no claude call, no ledger row, empty report."""
    from factory.common import killswitch
    monkeypatch.setattr(killswitch, "is_halted", lambda: True)
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=100000)
        _blocked_with_evidence(s, sh, "task-invstop1", action="discarded", stage="tests")
        called = {"n": 0}

        def fake(prompt, **k):
            called["n"] += 1
            return ('{"lesson":"x"}', 1, 0.0)

        out = factory_memory.investigate_blocked(s, sh, claude_fn=fake)
        assert out == [] and called["n"] == 0
        assert s.shift_spend(sh)["tokens"] == 0


def test_investigate_blocked_scope_is_tests_discard_and_errors_only(tmp_path):
    """Scope: discarded(tests) and error(*) only. Skip no_candidate (auto-decompose
    already gave a second opinion) and discarded(frozen/no_test/acceptance) (canned
    lessons already state the cause)."""
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=100000)
        cases = [
            ("task-inv-a", "discarded", "tests", True),
            ("task-inv-b", "error", "timeout", True),
            ("task-inv-c", "error", "merge", True),
            ("task-inv-d", "no_candidate", "", False),
            ("task-inv-e", "discarded", "frozen", False),
            ("task-inv-f", "discarded", "no_test", False),
            ("task-inv-g", "discarded", "acceptance", False),
        ]
        for tid, action, stage, _ in cases:
            _blocked_with_evidence(s, sh, tid, action=action, stage=stage)
        prompts = []

        def fake(prompt, **k):
            prompts.append(prompt)
            return ('{"cause":"c","lesson":"L"}', 1, 0.0)

        factory_memory.investigate_blocked(s, sh, claude_fn=fake)
        seen = {tid for tid, _, _, _ in cases if any(tid in p for p in prompts)}
        assert seen == {tid for tid, _, _, ok in cases if ok}


def test_investigate_blocked_caps_at_three(tmp_path):
    """At most 3 blocked tasks investigated per shift (cost cap)."""
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=100000)
        for i in range(5):
            _blocked_with_evidence(s, sh, f"task-invc{i:04d}", action="discarded", stage="tests")
        calls = {"n": 0}

        def fake(prompt, **k):
            calls["n"] += 1
            return ('{"cause":"c","lesson":"lesson %d"}' % calls["n"], 1, 0.0)

        factory_memory.investigate_blocked(s, sh, claude_fn=fake)
        assert calls["n"] == 3


def test_investigate_blocked_skips_tasks_without_evidence(tmp_path):
    """A blocked task with NO task_evidence row is not investigatable (nothing to
    read); it is skipped, no spend."""
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=100000)
        s.add_task("task-invnoev1", "no evidence", source="human")
        s.set_task_status("task-invnoev1", "blocked", result="x", shift_id=sh)
        called = {"n": 0}

        def fake(prompt, **k):
            called["n"] += 1
            return ('{"lesson":"x"}', 1, 0.0)

        assert factory_memory.investigate_blocked(s, sh, claude_fn=fake) == []
        assert called["n"] == 0


def test_investigate_blocked_only_this_shift(tmp_path):
    """Only tasks blocked in THIS shift are investigated — a prior shift's blocked
    task with evidence is ignored."""
    with _store(tmp_path) as s:
        sh_prev = s.start_shift(token_budget=100000)
        _blocked_with_evidence(s, sh_prev, "task-invprev1", action="discarded", stage="tests")
        sh = s.start_shift(token_budget=100000)
        _blocked_with_evidence(s, sh, "task-invnow01", action="discarded", stage="tests")
        prompts = []

        def fake(prompt, **k):
            prompts.append(prompt)
            return ('{"cause":"c","lesson":"L"}', 1, 0.0)

        factory_memory.investigate_blocked(s, sh, claude_fn=fake)
        assert any("task-invnow01" in p for p in prompts)
        assert not any("task-invprev1" in p for p in prompts)


def test_investigate_blocked_fails_open_to_canned_lesson(tmp_path):
    """A transport/parse failure fails open to the canned lesson_for_block — the
    close-out failure-memory floor still lands, recorded scope='investigated'."""
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=100000)
        _blocked_with_evidence(s, sh, "task-invfo001", action="discarded", stage="tests")

        def fake(prompt, **k):
            return ("[claude -p unavailable: boom]", 0, 0.0)     # transport sentinel, unparseable

        factory_memory.investigate_blocked(s, sh, claude_fn=fake)
        canned = factory_memory.lesson_for_block("discarded", "tests")
        rows = [r for r in s.learnings_for_role("factory") if r["scope"] == "investigated"]
        assert rows and rows[0]["content"] == canned


def test_investigate_blocked_stores_followup_without_spawning_task(tmp_path):
    """A returned followup_title/followup_detail is STORED onto the investigated lesson
    — NOT spawned as a task (that is an explicit follow-up, not built now)."""
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=100000)
        _blocked_with_evidence(s, sh, "task-invfu001", action="discarded", stage="tests",
                               title="big multi-surface task")
        n_before = len(s.list_tasks())

        def fake(prompt, **k):
            return ('{"cause":"too broad","lesson":"split the parser change from the CLI change",'
                    '"followup_title":"add just the parser helper",'
                    '"followup_detail":"create parse_ref() with one focused test"}', 5, 0.0)

        out = factory_memory.investigate_blocked(s, sh, claude_fn=fake)
        assert len(s.list_tasks()) == n_before                    # no task spawned
        rows = [r for r in s.learnings_for_role("factory") if r["scope"] == "investigated"]
        assert rows and "add just the parser helper" in rows[0]["content"]
        assert out[0]["followup_title"] == "add just the parser helper"
        assert out[0]["followup_detail"] == "create parse_ref() with one focused test"


def test_investigate_blocked_gate_off_by_default_and_board_toggleable():
    """The gate is an operator trial DIAL (not a brake) → in SETTINGS_SPEC and OFF in
    config.yaml."""
    from factory.common.config import SETTINGS_SPEC, load_config
    assert SETTINGS_SPEC.get("super_worker.investigate_blocked") is bool
    assert (load_config().get("super_worker") or {}).get("investigate_blocked") is False


def test_investigator_prompt_has_required_placeholders():
    import os
    from factory.common import paths
    p = os.path.join(paths.ROLES_DIR, "investigator", "prompt.md")
    with open(p, encoding="utf-8") as fh:
        txt = fh.read()
    for ph in ("{TITLE}", "{DETAIL}", "{SPEC}", "{ACTION}", "{STAGE}",
               "{TESTS_REPORT}", "{REPLY_HEAD}"):
        assert ph in txt, f"investigator prompt missing {ph}"


def test_execute_investigates_blocked_when_gate_on(tmp_path, monkeypatch):
    """End-to-end: execute_claimed_tasks with investigate_blocked=True runs the
    post-close-out investigator over a discarded(tests) task via the real claude_p seam."""
    from factory.orchestrator import develop as dev
    from factory.roles import common as roles_common
    calls = {"n": 0}

    def fake_claude_p(prompt, **k):
        calls["n"] += 1
        return ('{"cause":"c","lesson":"a very specific investigated lesson about seed X"}', 10, 0.001)

    monkeypatch.setattr(roles_common, "claude_p", fake_claude_p)
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=100000)
        tid = "task-invexe01"
        s.add_task(tid, "flaky task", source="human")
        s.set_task_status(tid, "in_progress", shift_id=sh)

        def fake(text, **k):
            return {"action": "discarded", "stage": "tests",
                    "tests_report": "FAILED tests/test_a.py::test_b",
                    "reply_head": "one assertion stayed red"}

        dev.execute_claimed_tasks(s, sh, develop_fn=fake, investigate_blocked=True)
        assert calls["n"] == 1
        assert any(r["scope"] == "investigated" and "seed X" in r["content"]
                   for r in s.learnings_for_role("factory"))


def test_execute_no_investigation_when_gate_off(tmp_path, monkeypatch):
    """Default OFF — a blocked task closes out with only its canned lesson; the
    investigator's claude_p is never touched."""
    from factory.orchestrator import develop as dev
    from factory.roles import common as roles_common
    called = {"n": 0}

    def fake_claude_p(prompt, **k):
        called["n"] += 1
        return ('{"lesson":"x"}', 1, 0.0)

    monkeypatch.setattr(roles_common, "claude_p", fake_claude_p)
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=100000)
        tid = "task-invoff01"
        s.add_task(tid, "t", source="human")
        s.set_task_status(tid, "in_progress", shift_id=sh)

        def fake(text, **k):
            return {"action": "discarded", "stage": "tests", "tests_report": "r",
                    "reply_head": "h"}

        dev.execute_claimed_tasks(s, sh, develop_fn=fake)     # investigate_blocked defaults OFF
        assert called["n"] == 0


# ============================================================================
# Task 4.2 (P6 stage 4 + P8): pinned card ranking + `factory learn distill`.
# Slice 1 (deterministic): a `pinned` row renders FIRST in the memory_card and
# never ages out, CAPPED at ~6 per role. Slice 2: `factory learn distill
# --role R [--apply]` — dry-run DEFAULT, ONE isolated claude_p at STANDARD tier
# proposes <=5 general rules citing source ids; --apply inserts scope='distilled'
# pinned=1 and archives the sources; the re-insert dedup INCLUDES archived rows;
# killswitch checked FIRST; spend ledgered notes='distill'; fail-open.
# ============================================================================

# -- Slice 1: pinned card ranking --------------------------------------------

def test_pin_learning_and_pinned_for_role(tmp_path):
    with _store(tmp_path) as s:
        a = s.add_learning("developer", "pin me")
        b = s.add_learning("developer", "leave me")
        s.pin_learning(a)
        assert [r["id"] for r in s.pinned_for_role("developer")] == [a]
        assert s.get_learning(a)["pinned"] == 1 and s.get_learning(b)["pinned"] == 0


def test_pinned_for_role_excludes_archived(tmp_path):
    """A pinned row that is later retired never surfaces via the pinned leg."""
    with _store(tmp_path) as s:
        a = s.add_learning("developer", "pinned but retired")
        s.pin_learning(a)
        s.archive_learning(a)
        assert s.pinned_for_role("developer") == []


def test_pinned_learning_renders_first_and_never_ages_out(tmp_path):
    """A pinned OLD row surfaces at the TOP of the card even though it fell out of
    the newest-`limit` window."""
    with _store(tmp_path) as s:
        ids = [s.add_learning("developer", f"lesson {i}") for i in range(10)]
        s.pin_learning(ids[0])                              # the OLDEST — normally aged out
        card = factory_memory.memory_card(s, "developer")   # default limit=8
        assert "lesson 0" in card                           # never ages out
        assert card.index("lesson 0") < card.index("lesson 9")   # renders FIRST


def test_pinned_rows_capped_per_role(tmp_path):
    """At most _PINNED_CAP pinned rows enter via the pinned leg — unbounded pins
    must not regrow the card the phase shrinks."""
    with _store(tmp_path) as s:
        pins = [s.add_learning("developer", f"pinned {i}") for i in range(8)]
        for p in pins:
            s.pin_learning(p)
        for i in range(8):                                   # push the pins out of newest-8
            s.add_learning("developer", f"fresh {i}")
        card = factory_memory.memory_card(s, "developer")
        shown = sum(1 for i in range(8) if f"pinned {i}" in card)
        assert shown == factory_memory._PINNED_CAP           # exactly the cap, not all 8
        assert "pinned 0" not in card and "pinned 1" not in card   # 2 OLDEST pins dropped


# -- Slice 2: `factory learn distill` ----------------------------------------

def _seed_distillables(s, role="developer"):
    return [
        s.add_learning(role, "narrow the brief to one landable slice", scope="no_candidate"),
        s.add_learning(role, "encode the acceptance as a focused test first", scope="tests"),
        s.add_learning(role, "keep changes off frozen files entirely", scope="frozen"),
    ]


def test_distill_dry_run_is_default_and_writes_no_rows(tmp_path):
    """No --apply → propose only: no distilled row, nothing archived — but the LLM
    spend IS ledgered notes='distill' (a dry-run still calls the model to propose)."""
    import json
    with _store(tmp_path) as s:
        ids = _seed_distillables(s)

        def fake(prompt, *, model="", **k):
            return (json.dumps({"rules": [
                {"rule": "write the failing test first, then the minimal change",
                 "sources": ids}]}), 30, 0.004)

        rep = factory_memory.distill_learnings(s, "developer", claude_fn=fake)
        assert rep["applied"] is False
        assert rep["proposed"][0]["rule"].startswith("write the failing test")
        allrows = s.learnings_for_role("developer", include_archived=True)
        assert [r for r in allrows if r["scope"] == "distilled"] == []   # nothing inserted
        assert all(r["archived"] == 0 for r in allrows)                  # nothing archived
        row = s.conn.execute(
            "SELECT notes, tokens FROM budget_ledger WHERE notes='distill'").fetchone()
        assert row is not None and row["tokens"] == 30


def test_distill_runs_at_standard_tier(tmp_path):
    """The model is the STANDARD tier (the P10 promise — judgment, never frontier)."""
    import json
    from factory.common import config
    with _store(tmp_path) as s:
        _seed_distillables(s)
        seen = {}

        def fake(prompt, *, model="", **k):
            seen["model"] = model
            return (json.dumps({"rules": []}), 5, 0.0)

        factory_memory.distill_learnings(s, "developer", claude_fn=fake)
        assert seen["model"] == config.resolve_model("standard") != ""


def test_distill_apply_inserts_pinned_distilled_and_archives_sources(tmp_path):
    """--apply inserts scope='distilled', pinned=1 (renders first, never ages out) and
    archives the cited sources."""
    import json
    with _store(tmp_path) as s:
        ids = _seed_distillables(s)

        def fake(prompt, *, model="", **k):
            return (json.dumps({"rules": [
                {"rule": "TDD first: one failing test, then the minimal change on a clean surface",
                 "sources": ids}]}), 20, 0.003)

        rep = factory_memory.distill_learnings(s, "developer", apply=True, claude_fn=fake)
        assert rep["applied"] is True
        distilled = [r for r in s.learnings_for_role("developer")
                     if r["scope"] == "distilled"]
        assert len(distilled) == 1
        assert distilled[0]["pinned"] == 1
        # the sources are now archived (hidden from prompts)
        for sid in ids:
            assert s.get_learning(sid)["archived"] == 1
        # and the distilled rule pins to the TOP of the card
        card = factory_memory.memory_card(s, "developer")
        assert "TDD first" in card


def test_distill_includes_existing_pinned_distilled_as_candidates(tmp_path):
    """Existing pinned/distilled rows are CONSOLIDATION candidates — else repeat runs
    accumulate. The prompt must surface them."""
    import json
    with _store(tmp_path) as s:
        _seed_distillables(s)
        d = s.add_learning("developer", "an already-distilled pinned rule", scope="distilled")
        s.pin_learning(d)
        seen = {}

        def fake(prompt, *, model="", **k):
            seen["prompt"] = prompt
            return (json.dumps({"rules": []}), 5, 0.0)

        factory_memory.distill_learnings(s, "developer", claude_fn=fake)
        assert "an already-distilled pinned rule" in seen["prompt"]


def test_distill_apply_dedups_reinsert_against_archived_rows(tmp_path):
    """A distilled rule matching a PREVIOUSLY-ARCHIVED lesson must dedup onto the
    archived row (hits bumped, stays hidden), NOT re-enter as a fresh live row."""
    import json
    with _store(tmp_path) as s:
        _seed_distillables(s)
        gone = s.add_learning("developer", "retired lesson: always pin the fixture seed")
        s.archive_learning(gone)

        def fake(prompt, *, model="", **k):
            return (json.dumps({"rules": [
                {"rule": "retired lesson: always pin the fixture seed", "sources": []}]}),
                10, 0.0)

        factory_memory.distill_learnings(s, "developer", apply=True, claude_fn=fake)
        matches = [r for r in s.learnings_for_role("developer", include_archived=True)
                   if "always pin the fixture seed" in r["content"]]
        assert len(matches) == 1 and matches[0]["id"] == gone   # deduped onto the archived row
        assert matches[0]["archived"] == 1                       # stays hidden, not resurrected


def test_distill_caps_at_five_rules(tmp_path):
    """<=5 rules are ever proposed/applied even if the model returns more."""
    import json
    with _store(tmp_path) as s:
        _seed_distillables(s)

        def fake(prompt, *, model="", **k):
            return (json.dumps({"rules": [
                {"rule": f"rule number {i}", "sources": []} for i in range(9)]}), 5, 0.0)

        rep = factory_memory.distill_learnings(s, "developer", claude_fn=fake)
        assert len(rep["proposed"]) == 5


def test_distill_stop_vetoes_all_spend(tmp_path, monkeypatch):
    """killswitch.is_halted() is checked FIRST — STOP vetoes even read-only distill
    spend: no claude call, no ledger row, nothing applied."""
    import json
    from factory.common import killswitch
    monkeypatch.setattr(killswitch, "is_halted", lambda: True)
    with _store(tmp_path) as s:
        _seed_distillables(s)
        called = {"n": 0}

        def fake(prompt, **k):
            called["n"] += 1
            return (json.dumps({"rules": []}), 1, 0.0)

        rep = factory_memory.distill_learnings(s, "developer", apply=True, claude_fn=fake)
        assert called["n"] == 0 and rep["applied"] is False
        assert s.conn.execute(
            "SELECT COUNT(*) c FROM budget_ledger WHERE notes='distill'").fetchone()["c"] == 0


def test_distill_fails_open_on_unparseable_reply(tmp_path):
    """A garbage (non-JSON) reply → nothing proposed, nothing applied, no crash; the
    spend still ledgers (the call happened)."""
    with _store(tmp_path) as s:
        _seed_distillables(s)

        def fake(prompt, *, model="", **k):
            return ("sorry, I could not do that", 7, 0.0)

        rep = factory_memory.distill_learnings(s, "developer", apply=True, claude_fn=fake)
        assert rep["proposed"] == [] and rep["applied"] is False
        assert [r for r in s.learnings_for_role("developer", include_archived=True)
                if r["scope"] == "distilled"] == []


def test_cli_learn_distill_dry_run_through_main(tmp_path, monkeypatch, capsys):
    """`factory learn distill --role developer` wires through main as a DRY-RUN by
    default (no --apply): proposals printed, nothing written."""
    import json
    from factory.roles import common as roles_common
    db = str(tmp_path / "f.db")
    with Blackboard(db) as s:
        s.init_db()
        _seed_distillables(s)

    def fake(prompt, *, model="", **k):
        return (json.dumps({"rules": [
            {"rule": "consolidated rule from the dry run", "sources": []}]}), 8, 0.0)

    monkeypatch.setattr(roles_common, "claude_p", fake)
    monkeypatch.setattr(orch, "Blackboard", lambda *a, **k: Blackboard(db))
    orch.main(["learn", "distill", "--role", "developer"])
    out = capsys.readouterr().out
    assert "consolidated rule from the dry run" in out and "dry-run" in out
    with Blackboard(db) as s:
        assert [r for r in s.learnings_for_role("developer", include_archived=True)
                if r["scope"] == "distilled"] == []
