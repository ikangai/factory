"""Auto-decomposition on no_candidate (GSD integration #4, design:
docs/plans/2026-06-27-gsd-spec-driven-integration.md): a brief too big to land is split into
a sequenced chain of single-surface sub-tasks instead of just blocked."""
from factory.common.store import Blackboard
from factory.reporting import scope_check


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


def _task(tid="task-1", title="do x and y", detail=""):
    return {"id": tid, "title": title, "detail": detail}


# -- add_subtasks (shared helper) --------------------------------------------
def test_add_subtasks_adds_titled_with_folded_spec(tmp_path):
    with _store(tmp_path) as s:
        n = scope_check.add_subtasks(s, [
            {"title": "do x", "detail": "x", "target_surface": "a.py", "acceptance": "test a"},
            {"detail": "no title"},                       # skipped (no title)
            {"title": "do y"},
        ])
        assert len(n) == 2                                # add_subtasks returns the NEW ids (Task 5.2)
        opens = {t["title"]: t for t in s.list_tasks(status="open")}
        assert set(opens) == {"do x", "do y"}
        assert "a.py" in opens["do x"]["detail"] and "test a" in opens["do x"]["detail"]
        assert opens["do x"]["source"] == "worker"        # satisfies the tasks.source CHECK


def test_add_subtasks_empty_is_zero(tmp_path):
    with _store(tmp_path) as s:
        assert scope_check.add_subtasks(s, []) == []
        assert scope_check.add_subtasks(s, None) == []


# -- decompose_no_candidate --------------------------------------------------
def test_decompose_splits_and_records_learning(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        decomposer = lambda t: {"subtasks": [{"title": "part one"}, {"title": "part two"}]}
        n = scope_check.decompose_no_candidate(s, _task(), shift_id=sh, decomposer=decomposer)
        assert len(n) == 2                                # decompose_no_candidate returns the NEW ids
        assert {"part one", "part two"} <= {t["title"] for t in s.list_tasks(status="open")}
        assert any("decompos" in r["content"].lower() for r in s.learnings_for_role("factory"))


def test_decompose_empty_returns_zero(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        assert scope_check.decompose_no_candidate(s, _task(), shift_id=sh,
                                                  decomposer=lambda t: {"subtasks": []}) == []


def test_decompose_judge_error_returns_zero(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)

        def boom(t):
            raise RuntimeError("llm down")

        assert scope_check.decompose_no_candidate(s, _task(), shift_id=sh, decomposer=boom) == []


# -- wiring into execute_claimed_tasks ---------------------------------------
def test_execute_decomposes_no_candidate_when_decomposer_given(tmp_path):
    from factory.orchestrator import develop as dev
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-1", "do x and y", source="human")
        s.set_task_status("task-1", "in_progress", shift_id=sh)

        def fake_dev(text, **k):
            return {"action": "no_candidate"}

        dev.execute_claimed_tasks(
            s, sh, develop_fn=fake_dev,
            decomposer=lambda t: {"subtasks": [{"title": "x only"}, {"title": "y only"}]})
        assert s.get_task("task-1")["status"] == "blocked"
        assert "decompos" in (s.get_task("task-1")["result"] or "").lower()
        assert {"x only", "y only"} <= {t["title"] for t in s.list_tasks(status="open")}
        # the canned no_candidate factory lesson is NOT recorded (decomposition replaced it)
        assert not any("bundled too much" in r["content"] for r in s.learnings_for_role("factory"))


def test_execute_no_decomposer_keeps_canned_lesson(tmp_path):
    from factory.orchestrator import develop as dev
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-1", "do x and y", source="human")
        s.set_task_status("task-1", "in_progress", shift_id=sh)
        dev.execute_claimed_tasks(s, sh, develop_fn=lambda text, **k: {"action": "no_candidate"})
        assert s.get_task("task-1")["status"] == "blocked"
        assert any("bundled too much" in r["content"] for r in s.learnings_for_role("factory"))
