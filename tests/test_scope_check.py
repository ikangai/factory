"""Spec-driven pre-dispatch scope check (design:
docs/plans/2026-06-27-gsd-spec-driven-integration.md). Hermetic — tmp store, injected judge.
"""
from factory.common.store import Blackboard
from factory.reporting import scope_check


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


def _task(tid, title, detail=""):
    return {"id": tid, "title": title, "detail": detail}


# -- normalize_verdict -------------------------------------------------------
def test_normalize_pass():
    assert scope_check.normalize_verdict({"decision": "pass"})["decision"] == "pass"


def test_normalize_unknown_decision_defaults_pass():
    assert scope_check.normalize_verdict({"decision": "weird"})["decision"] == "pass"


def test_normalize_non_dict_defaults_pass():
    assert scope_check.normalize_verdict("nope")["decision"] == "pass"


def test_normalize_split_without_subtasks_becomes_pass():
    assert scope_check.normalize_verdict({"decision": "split", "subtasks": []})["decision"] == "pass"


def test_normalize_split_keeps_only_titled_subtasks():
    v = scope_check.normalize_verdict(
        {"decision": "split", "subtasks": [{"title": "a"}, {"detail": "no title"}, {"title": "b"}]})
    assert v["decision"] == "split" and [s["title"] for s in v["subtasks"]] == ["a", "b"]


def test_normalize_reject_keeps_reason():
    v = scope_check.normalize_verdict({"decision": "reject", "reason": "touches frozen"})
    assert v["decision"] == "reject" and v["reason"] == "touches frozen"


# -- prefilter ---------------------------------------------------------------
def test_prefilter_pass_dispatches_with_spec(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        judge = lambda t: {"decision": "pass",
                           "spec": {"target_surface": "llm.py", "acceptance": "retry test passes"}}
        keep = scope_check.prefilter(s, [_task("task-1", "do x")], shift_id=sh, judge=judge)
        assert [t["id"] for t in keep] == ["task-1"]
        assert keep[0]["spec"]["target_surface"] == "llm.py"


def test_prefilter_reject_blocks_and_records_learning(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-1", "do x", source="human")
        s.set_task_status("task-1", "in_progress", shift_id=sh)
        judge = lambda t: {"decision": "reject", "reason": "not landable as one change"}
        keep = scope_check.prefilter(s, [_task("task-1", "do x")], shift_id=sh, judge=judge)
        assert keep == []
        assert s.get_task("task-1")["status"] == "blocked"
        assert any("not landable" in r["content"] for r in s.learnings_for_role("factory"))


def test_prefilter_split_adds_subtasks_and_blocks_original(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-1", "do x and y", source="human")
        s.set_task_status("task-1", "in_progress", shift_id=sh)
        judge = lambda t: {"decision": "split", "reason": "two changes",
                           "subtasks": [{"title": "do x", "detail": "x"}, {"title": "do y"}]}
        keep = scope_check.prefilter(s, [_task("task-1", "do x and y")], shift_id=sh, judge=judge)
        assert keep == []
        assert s.get_task("task-1")["status"] == "blocked"
        opens = [t["title"] for t in s.list_tasks(status="open")]
        assert "do x" in opens and "do y" in opens


def test_prefilter_judge_error_is_fail_open(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)

        def judge(t):
            raise RuntimeError("llm down")

        keep = scope_check.prefilter(s, [_task("task-1", "do x")], shift_id=sh, judge=judge)
        assert [t["id"] for t in keep] == ["task-1"]      # a checker hiccup must NOT block work


def test_prefilter_empty_input():
    assert scope_check.prefilter(None, [], shift_id=1, judge=lambda t: {}) == []


# -- Task 0.5: ledger the judge/decomposer spend -----------------------------
def test_prefilter_ledgers_scope_judge_spend(tmp_path):
    """A judge that reports _spend is ledgered as 'scope_check' on the shift."""
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        judge = lambda t: {"decision": "pass", "_spend": {"tokens": 300, "cost": 0.01}}
        scope_check.prefilter(s, [_task("task-1", "do x")], shift_id=sh, judge=judge)
        rows = [e for e in s.budget_entries() if e["role_or_run"] == "scope_check"]
    assert len(rows) == 1 and rows[0]["tokens"] == 300 and rows[0]["cost"] == 0.01
    assert rows[0]["shift_id"] == sh


def test_prefilter_without_spend_key_ledgers_nothing(tmp_path):
    """The existing judge-stub style (plain dict, no _spend) creates no ledger row."""
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        scope_check.prefilter(s, [_task("task-1", "do x")], shift_id=sh,
                              judge=lambda t: {"decision": "pass"})
        assert [e for e in s.budget_entries() if e["role_or_run"] == "scope_check"] == []


def test_decompose_no_candidate_ledgers_decomposer_spend(tmp_path):
    """The decomposer's _spend is ledgered as 'decompose' on the shift."""
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        decomposer = lambda t: {"subtasks": [{"title": "a"}],
                                "_spend": {"tokens": 200, "cost": 0.02}}
        n = scope_check.decompose_no_candidate(s, _task("t", "x"), shift_id=sh, decomposer=decomposer)
        rows = [e for e in s.budget_entries() if e["role_or_run"] == "decompose"]
    assert n == 1 and len(rows) == 1 and rows[0]["tokens"] == 200 and rows[0]["shift_id"] == sh


def test_scope_judge_attaches_its_spend(monkeypatch):
    """The production scope judge embeds (tokens,cost) under _spend for the call site."""
    from factory.roles import common as rcommon
    monkeypatch.setattr(rcommon, "_load_prompt", lambda name: "judge {TASK}")
    monkeypatch.setattr(rcommon, "claude_super",
                        lambda *a, **k: ('```json\n{"decision":"pass"}\n```', 55, 0.005))
    v = scope_check.scope_judge({"title": "x"})
    assert v["decision"] == "pass"
    assert v["_spend"]["tokens"] == 55 and v["_spend"]["cost"] == 0.005
    assert v["_spend"]["seconds"] >= 0            # real duration so timesheets aren't 0-min (#17)


def test_decompose_judge_attaches_its_spend(monkeypatch):
    from factory.roles import common as rcommon
    monkeypatch.setattr(rcommon, "_load_prompt", lambda name: "decompose {TASK}")
    monkeypatch.setattr(rcommon, "claude_super",
                        lambda *a, **k: ('```json\n{"subtasks":[]}\n```', 33, 0.003))
    obj = scope_check.decompose_judge({"title": "x"})
    assert obj["_spend"]["tokens"] == 33 and obj["_spend"]["cost"] == 0.003
    assert obj["_spend"]["seconds"] >= 0          # real duration so timesheets aren't 0-min (#17)


# -- wiring into execute_claimed_tasks ---------------------------------------
def test_execute_runs_scope_prefilter_when_judge_given(tmp_path):
    from factory.orchestrator import develop as dev
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-1", "do x and y", source="human")
        s.set_task_status("task-1", "in_progress", shift_id=sh)
        dispatched = []

        def fake_dev(text, **k):
            dispatched.append(text)
            return {"action": "no_candidate"}

        dev.execute_claimed_tasks(s, sh, develop_fn=fake_dev,
                                  scope_judge=lambda t: {"decision": "reject", "reason": "too broad"})
        assert dispatched == []                            # rejected before any worker spun up
        assert s.get_task("task-1")["status"] == "blocked"
