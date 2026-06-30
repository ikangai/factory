"""Spec-fulfillment feedback (GSD integration #6, design:
docs/plans/2026-06-27-gsd-spec-driven-integration.md): record when a delivered diff strays
outside its declared target_surface, so the scope check self-tunes."""
from factory.common.store import Blackboard
from factory.reporting import scope_check


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


# -- spec_fulfillment --------------------------------------------------------
def test_fulfillment_matched_within_surface():
    ok, _ = scope_check.spec_fulfillment({"target_surface": "llm.py"},
                                         ["src/clive/llm.py", "tests/test_llm.py"])
    assert ok is True                                 # source within surface; test ignored


def test_fulfillment_strays_outside_surface():
    ok, reason = scope_check.spec_fulfillment({"target_surface": "llm.py"},
                                              ["src/clive/llm.py", "src/clive/session.py"])
    assert ok is False and "session.py" in reason


def test_fulfillment_no_surface_is_matched():
    assert scope_check.spec_fulfillment({}, ["a.py"])[0] is True
    assert scope_check.spec_fulfillment({"target_surface": ""}, ["a.py"])[0] is True


def test_fulfillment_ignores_tests_and_docs():
    ok, _ = scope_check.spec_fulfillment({"target_surface": "llm.py"},
                                         ["src/clive/llm.py", "tests/test_x.py", "README.md"])
    assert ok is True                                 # only source paths can stray


def test_fulfillment_empty_diff_matched():
    assert scope_check.spec_fulfillment({"target_surface": "llm.py"}, [])[0] is True


# -- wiring: a merged diff that strays records a spec-creep learning ----------
def test_execute_records_spec_creep_on_strayed_merge(tmp_path):
    from factory.orchestrator import develop as dev
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-1", "do x", source="human")
        s.set_task_status("task-1", "in_progress", shift_id=sh)
        scope = lambda t: {"decision": "pass",
                           "spec": {"target_surface": "llm.py", "acceptance": "a test"}}
        dev_fn = lambda text, **k: {"action": "merged", "merge_sha": "abc",
                                    "changed_paths": ["src/clive/llm.py", "src/clive/session.py"]}
        dev.execute_claimed_tasks(s, sh, develop_fn=dev_fn, scope_judge=scope)
        assert s.get_task("task-1")["status"] == "done"
        assert any("target_surface" in r["content"]
                   for r in s.learnings_for_role("factory"))


def test_execute_no_spec_creep_when_diff_stays(tmp_path):
    from factory.orchestrator import develop as dev
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-1", "do x", source="human")
        s.set_task_status("task-1", "in_progress", shift_id=sh)
        scope = lambda t: {"decision": "pass", "spec": {"target_surface": "llm.py", "acceptance": "t"}}
        dev_fn = lambda text, **k: {"action": "merged", "merge_sha": "abc",
                                    "changed_paths": ["src/clive/llm.py", "tests/test_llm.py"]}
        dev.execute_claimed_tasks(s, sh, develop_fn=dev_fn, scope_judge=scope)
        assert not any("target_surface" in r["content"]
                       for r in s.learnings_for_role("factory"))
