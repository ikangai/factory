"""Regression tests for the xhigh review of the GSD integration (e69cea6..HEAD)."""
from factory.common.store import Blackboard
from factory.reporting import scope_check, acceptance


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


# -- finding 1/6/11: prefilter must NOT clobber a durable authored spec ------
def test_prefilter_pass_without_spec_keeps_the_tasks_authored_spec(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-1", "do x", source="research",
                   spec={"target_surface": "llm.py", "acceptance": "retry test"})
        s.set_task_status("task-1", "in_progress", shift_id=sh)
        task = s.tasks_in_flight(sh)[0]                    # carries the persisted spec
        judge = lambda t: {"decision": "pass"}            # passes WITHOUT re-emitting a spec
        keep = scope_check.prefilter(s, [task], shift_id=sh, judge=judge)
        assert keep[0]["spec"]["target_surface"] == "llm.py"   # authored spec preserved, not {}


def test_prefilter_pass_with_spec_prefers_the_judge_spec(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        judge = lambda t: {"decision": "pass", "spec": {"target_surface": "fresh.py", "acceptance": "a"}}
        keep = scope_check.prefilter(s, [{"id": "t", "title": "x", "detail": "",
                                          "spec": {"target_surface": "old.py"}}],
                                     shift_id=sh, judge=judge)
        assert keep[0]["spec"]["target_surface"] == "fresh.py"


# -- finding 2/8: a source module named test_*.py is NOT a test --------------
def test_is_test_excludes_a_src_module_named_test_prefix():
    assert acceptance._is_test("tests/test_llm.py") is True
    assert acceptance._is_test("src/clive/tests/test_x.py") is True
    assert acceptance._is_test("src/clive/test_harness.py") is False   # production module, not a test
    ok, _ = acceptance.acceptance_ok(["src/clive/feature.py", "src/clive/test_harness.py"])
    assert ok is False                                    # untested source no longer slips through


# -- finding 3/9: _within_surface must not substring-match --------------------
def test_within_surface_rejects_substring_siblings():
    assert scope_check._within_surface("src/clive/llm.py", "llm.py") is True
    assert scope_check._within_surface("src/clive/rapid_api.py", "api.py") is False  # not a substring win
    assert scope_check._within_surface("src/clive/llm_utils.py", "llm") is False
    ok, _ = scope_check.spec_fulfillment({"target_surface": "api.py"},
                                         ["src/clive/api.py", "src/clive/rapid_api.py"])
    assert ok is False                                    # the real stray is now detected


# -- finding 5: add_subtasks robust to a non-list, strips, drops empty spec ---
def test_add_subtasks_non_list_subtasks_is_zero_not_chars(tmp_path):
    with _store(tmp_path) as s:
        assert scope_check.add_subtasks(s, "do x then y") == 0    # a string is not iterated char-by-char
        assert s.list_tasks() == []


def test_add_subtasks_strips_detail_and_drops_empty_spec(tmp_path):
    with _store(tmp_path) as s:
        n = scope_check.add_subtasks(s, [{"title": "do x", "detail": "  spaced  "}])
        assert n == 1
        t = s.list_tasks()[0]
        assert t["detail"] == "spaced"                    # stripped
        assert t["spec"] == {}                            # no surface/acceptance -> empty, not {"":""}
