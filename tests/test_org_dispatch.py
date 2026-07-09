"""The pipeline consult + outcome recording (impl plan Task 1.3): with an ACTIVE org chart,
execute_claimed_tasks (orchestrator/develop.py) classifies + profile-assigns each claimed
task on the MAIN thread, threads per-task stage overrides through the existing knob
plumbing, and close-out records ONE routing_outcomes row per dispatched (done/blocked)
task — the fit table's raw material. Hermetic: fake develop_fn/scope_judge, hand-seeded
store, same convention as test_develop_glue.py.

Point (d) of the task (no new assert needed): with NO active chart, execute_claimed_tasks's
task-status/ledger/close-out behavior is UNCHANGED — proven by the pre-existing
test_develop_glue.py / test_org_store.py / test_org_resolver.py suites staying green
alongside this file (routing_outcomes rows are written regardless of chart presence — that
IS the evidence loop's point, building fit-table data even before an organizer exists — but
no EXISTING assertion about task status, ledger rows, or dispatch args changes)."""
from factory.orchestrator import develop, org
from factory.common.store import Blackboard


CHART = {
    "classes": [
        {"name": "mechanical-fix", "match": {"any": ["typo"]},
         "stages": {"scope_check": False}, "tiers": {}, "profile": "python-dev"},
        {"name": "risky-core", "match": {"any": ["concurrency"]},
         "stages": {}, "tiers": {}, "profile": ""},
    ],
    "default_class": "risky-core",
    "bench": [],
    "retire": [],
}


def _chart_store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    m = s.set_mission("ship it")
    s.add_org_chart(m, CHART)
    s.add_profile("python-dev", description="d", overlay="", model="fast")
    return s


def test_unclassified_task_gets_org_class_persisted_and_profile_applied(tmp_path):
    """(a): an unclassified open task (org_class == '', profile == '') gets classified AND
    profile-assigned at dispatch, on the main thread, from the class the chart names."""
    s = _chart_store(tmp_path)
    sh = s.start_shift(token_budget=1)
    s.add_task("t1", "fix a typo in the README", source="issue")
    s.set_task_status("t1", "in_progress", shift_id=sh)
    assert s.get_task("t1")["org_class"] == "" and s.get_task("t1")["profile"] == ""

    develop.execute_claimed_tasks(
        s, sh, develop_fn=lambda text, **k: {"action": "merged", "merge_sha": "x"})

    t = s.get_task("t1")
    assert t["org_class"] == "mechanical-fix"
    assert t["profile"] == "python-dev"        # the class's profile applied (task had none)
    s.close()


def test_task_with_a_named_profile_keeps_it(tmp_path):
    """A task that ALREADY names a profile is never overridden by the chart's class profile
    (only tasks with none get the class's assignment)."""
    s = _chart_store(tmp_path)
    s.add_profile("hand-picked", description="d", overlay="", model="standard")
    sh = s.start_shift(token_budget=1)
    s.add_task("t1", "fix a typo", source="issue")
    s.set_task_profile("t1", "hand-picked")
    s.set_task_status("t1", "in_progress", shift_id=sh)

    develop.execute_claimed_tasks(
        s, sh, develop_fn=lambda text, **k: {"action": "merged", "merge_sha": "x"})

    assert s.get_task("t1")["profile"] == "hand-picked"
    s.close()


def test_class_scope_check_false_skips_the_judge_for_that_task_only(tmp_path):
    """(b): a class overriding stages.scope_check=false bypasses the injected scope judge
    for tasks of that class, while a task of another class (no override) still gets judged."""
    s = _chart_store(tmp_path)
    sh = s.start_shift(token_budget=1)
    s.add_task("skip", "fix a typo somewhere", source="issue")          # mechanical-fix: skip
    s.set_task_status("skip", "in_progress", shift_id=sh)
    s.add_task("judged", "touch the concurrency code", source="issue")  # risky-core: judged
    s.set_task_status("judged", "in_progress", shift_id=sh)

    judged_ids = []

    def scope_fake(task):
        judged_ids.append(task["id"])
        return {"decision": "pass"}

    develop.execute_claimed_tasks(
        s, sh, develop_fn=lambda text, **k: {"action": "merged", "merge_sha": "x"},
        scope_judge=scope_fake)

    assert judged_ids == ["judged"]              # 'skip' bypassed the judge entirely
    assert s.get_task("skip")["status"] == "done"      # bypass still dispatches (no reject/split)
    assert s.get_task("judged")["status"] == "done"
    s.close()


def test_close_out_records_one_routing_outcome_per_dispatched_task(tmp_path):
    """(c): a merged task records outcome='done', stage=''; a blocked task records
    outcome='blocked', stage=<the gate stage>. tier = the PROFILE's alias; tokens = the
    same value the developer ledger row gets."""
    s = _chart_store(tmp_path)
    sh = s.start_shift(token_budget=1000)
    s.add_task("ok", "fix a typo cleanly", source="issue")
    s.set_task_status("ok", "in_progress", shift_id=sh)
    s.add_task("bad", "fix a typo badly", source="issue")
    s.set_task_status("bad", "in_progress", shift_id=sh)

    def fake(text, **k):
        if "cleanly" in text:
            return {"action": "merged", "merge_sha": "sha1", "tokens": 111}
        return {"action": "discarded", "stage": "tests", "tokens": 222}

    develop.execute_claimed_tasks(s, sh, develop_fn=fake)

    rows = {r["task_id"]: r for r in s._all("SELECT * FROM routing_outcomes")}
    assert set(rows) == {"ok", "bad"}
    assert rows["ok"]["outcome"] == "done" and rows["ok"]["stage"] == ""
    assert rows["ok"]["tier"] == "fast" and rows["ok"]["tokens"] == 111
    assert rows["ok"]["org_class"] == "mechanical-fix"
    assert rows["bad"]["outcome"] == "blocked" and rows["bad"]["stage"] == "tests"
    assert rows["bad"]["tier"] == "fast" and rows["bad"]["tokens"] == 222
    s.close()


def test_no_active_chart_writes_routing_outcomes_with_blank_org_class(tmp_path):
    """No chart at all: task_params falls through to empty overrides (byte-identical
    dispatch), but routing_outcomes STILL records the attempt (org_class='') — the
    evidence loop builds fit-table data even before an organizer exists."""
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=1)
    s.add_task("t1", "some task", source="issue")
    s.set_task_status("t1", "in_progress", shift_id=sh)

    develop.execute_claimed_tasks(
        s, sh, develop_fn=lambda text, **k: {"action": "merged", "merge_sha": "x", "tokens": 5})

    rows = s._all("SELECT * FROM routing_outcomes WHERE task_id = 't1'")
    assert len(rows) == 1
    assert rows[0]["org_class"] == "" and rows[0]["outcome"] == "done" and rows[0]["tokens"] == 5
    s.close()


def test_halted_task_records_no_routing_outcome(tmp_path):
    """A STOP-braked run never completed — no outcome to attribute."""
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=1)
    s.add_task("t1", "x", source="issue")
    s.set_task_status("t1", "in_progress", shift_id=sh)

    develop.execute_claimed_tasks(s, sh, develop_fn=lambda text, **k: {"action": "halted"})

    assert s._all("SELECT * FROM routing_outcomes") == []
    s.close()


def test_scope_judge_accepts_an_explicit_model_override(monkeypatch):
    """scope_check.py's scope_judge gains an optional `model` kwarg (Task 1.3): when given
    (even ''), it is used directly instead of today's config-derived scope_check_tier read."""
    from factory.reporting import scope_check
    from factory.roles import common as roles_common

    seen = {}

    def fake_claude_super(prompt, **k):
        seen["model"] = k.get("model")
        return ('{"decision": "pass"}', 10, 0.001)

    monkeypatch.setattr(roles_common, "claude_super", fake_claude_super)
    scope_check.scope_judge({"title": "t", "detail": ""}, model="fast")
    assert seen["model"] == org.config.resolve_model("fast")


def test_scope_judge_default_model_is_unchanged_when_omitted(monkeypatch):
    """Omitting `model` preserves today's config-derived read — byte-identical."""
    from factory.reporting import scope_check
    from factory.roles import common as roles_common

    seen = {}

    def fake_claude_super(prompt, **k):
        seen["model"] = k.get("model")
        return ('{"decision": "pass"}', 10, 0.001)

    monkeypatch.setattr(roles_common, "claude_super", fake_claude_super)
    scope_check.scope_judge({"title": "t", "detail": ""})
    assert seen["model"] == org.config.resolve_model("")
