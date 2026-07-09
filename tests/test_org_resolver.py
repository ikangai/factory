"""The org resolver (design: docs/plans/2026-07-09-self-organizing-factory-design.md §2;
impl plan Task 1.2): pure store+config reads, no LLM. validate_chart enforces the
authority line in CODE (never trust the organizer's own claim of compliance); classify
matches a task to a class; task_params is the ONE consult point dispatch threads through,
falling through to today's global resolve_setting when there's no active chart or no
override — so a chartless mission is byte-identical to today.
"""
from factory.common.store import Blackboard
from factory.common import config
from factory.orchestrator import org


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


# The design doc's example (docs/plans/2026-07-09-self-organizing-factory-design.md §1),
# extended with the "standard-dev" default class the excerpt REFERENCES but (being an
# illustrative fragment) never defines — added here so the happy-path fixture is fully
# self-consistent under validate_chart's "default_class names a defined class" rule.
VALID_CHART = {
    "classes": [
        {"name": "mechanical-fix",
         "match": {"any": ["typo", "rename", "docstring", "comment"]},
         "stages": {"scope_check": False, "reviewer": False},
         "tiers": {"worker": "fast", "scope_judge": "fast", "reviewer": "",
                   "decomposer": "standard", "investigator": "standard"},
         "profile": "python-dev"},
        {"name": "risky-core", "match": {"any": ["llm.py", "planner", "concurrency"]},
         "stages": {"reviewer": True, "retry_on_discard": True},
         "tiers": {"worker": "standard", "reviewer": ""},
         "profile": "core-surgeon"},
        {"name": "standard-dev", "match": {"any": ["*"]}, "stages": {}, "tiers": {},
         "profile": ""},
    ],
    "default_class": "standard-dev",
    "bench": [
        {"name": "python-dev", "model": "fast", "overlay": "...", "description": "..."},
        {"name": "core-surgeon", "model": "standard", "overlay": "...", "description": "..."},
    ],
    "retire": ["stale-profile"],
}


def _one_class_chart(**overrides):
    """A minimal one-class chart for a single-violation test — override one field."""
    cls = {"name": "mechanical-fix", "match": {"any": ["typo"]},
          "stages": {"scope_check": False}, "tiers": {"worker": "fast"},
          "profile": "python-dev"}
    cls.update(overrides.pop("class_overrides", {}))
    chart = {"classes": [cls], "default_class": "mechanical-fix", "bench": [], "retire": []}
    chart.update(overrides)
    return chart


# -- ORG_BOOL_KEYS: derived, not hand-listed ---------------------------------
def test_org_bool_keys_is_exactly_the_bool_leaves_of_settings_spec():
    expected = {key.split(".", 1)[1] for key, kind in config.SETTINGS_SPEC.items() if kind is bool}
    assert org.ORG_BOOL_KEYS == expected
    assert "scope_check" in org.ORG_BOOL_KEYS
    assert "max_parallel" not in org.ORG_BOOL_KEYS   # a capacity int, never org-controllable


# -- validate_chart: the authority line enforced in code ---------------------
def test_validate_chart_accepts_the_design_docs_example():
    ok, reasons = org.validate_chart(VALID_CHART, max_profiles=10)
    assert ok is True and reasons == []


def test_validate_chart_rejects_unknown_stage_key():
    chart = _one_class_chart(class_overrides={"stages": {"not_a_real_stage": True}})
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is False
    assert any("not_a_real_stage" in r for r in reasons)


def test_validate_chart_rejects_capacity_int_as_a_stage():
    """max_parallel is int-typed in SETTINGS_SPEC — global load management stays operator-
    owned (design's authority line), so it must never be settable as a per-class stage."""
    chart = _one_class_chart(class_overrides={"stages": {"max_parallel": True}})
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is False
    assert any("max_parallel" in r for r in reasons)


def test_validate_chart_rejects_tier_not_in_palette():
    chart = _one_class_chart(class_overrides={"tiers": {"worker": "turbo"}})
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is False
    assert any("turbo" in r for r in reasons)


def test_validate_chart_rejects_undefined_default_class():
    chart = _one_class_chart(default_class="nope-not-defined")
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is False
    assert any("default_class" in r for r in reasons)


def test_validate_chart_rejects_bench_overflow():
    chart = _one_class_chart(bench=[
        {"name": "a", "model": "fast"}, {"name": "b", "model": "fast"},
        {"name": "c", "model": "fast"}])
    ok, reasons = org.validate_chart(chart, max_profiles=2)
    assert ok is False
    assert any("bench" in r.lower() and "max_profiles" in r for r in reasons)


def test_validate_chart_rejects_empty_match_any():
    chart = _one_class_chart(class_overrides={"match": {"any": []}})
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is False
    assert any("match.any" in r for r in reasons)


def test_validate_chart_rejects_non_list_match_any():
    chart = _one_class_chart(class_overrides={"match": {"any": "typo"}})
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is False
    assert any("match.any" in r for r in reasons)


def test_validate_chart_rejects_non_slug_class_name():
    chart = _one_class_chart(class_overrides={"name": "Not A Slug!"})
    chart["default_class"] = "Not A Slug!"
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is False
    assert any("slug" in r.lower() for r in reasons)


def test_validate_chart_rejects_tier_role_outside_the_authority_line():
    """The authority line names exactly worker/scope_judge/decomposer/reviewer/investigator —
    a tier assigned to any other role (e.g. 'conductor') reaches past it."""
    chart = _one_class_chart(class_overrides={"tiers": {"conductor": "fast"}})
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is False
    assert any("conductor" in r for r in reasons)


# -- classify: first-match over title+detail, else default -------------------
def test_classify_first_match_case_insensitive_over_title_and_detail():
    task = {"title": "Fix a TYPO in the README", "detail": ""}
    assert org.classify(VALID_CHART, task) == "mechanical-fix"

    task2 = {"title": "harden", "detail": "touches llm.py concurrency paths"}
    assert org.classify(VALID_CHART, task2) == "risky-core"


def test_classify_falls_through_to_default_class():
    task = {"title": "totally unrelated work", "detail": "nothing matches"}
    assert org.classify(VALID_CHART, task) == "standard-dev"


# -- task_params: the ONE consult point --------------------------------------
def test_task_params_no_active_chart_is_empty_overrides(tmp_path):
    with _store(tmp_path) as s:
        s.add_task("t1", "fix a typo", source="issue")
        t = s.get_task("t1")
        params = org.task_params(s, t)
        assert params.stages == {} and params.tiers == {} and params.profile == ""


def test_task_params_uses_the_tasks_stamped_org_class(tmp_path):
    with _store(tmp_path) as s:
        m = s.set_mission("x")
        s.add_org_chart(m, VALID_CHART)
        s.add_task("t1", "irrelevant title", source="issue")
        s.set_task_org_class("t1", "mechanical-fix")
        t = s.get_task("t1")
        params = org.task_params(s, t)
        assert params.org_class == "mechanical-fix"
        assert params.stages == {"scope_check": False, "reviewer": False}
        assert params.profile == "python-dev"


def test_task_params_classifies_on_the_fly_when_org_class_is_blank(tmp_path):
    with _store(tmp_path) as s:
        m = s.set_mission("x")
        s.add_org_chart(m, VALID_CHART)
        s.add_task("t1", "fix a typo in docs", source="issue")   # org_class defaults to ''
        t = s.get_task("t1")
        assert t["org_class"] == ""
        params = org.task_params(s, t)
        assert params.org_class == "mechanical-fix"   # classified from title, not persisted here
        assert params.profile == "python-dev"


# -- render_fit_table ---------------------------------------------------------
def test_render_fit_table_empty_says_no_evidence_yet():
    text = org.render_fit_table([])
    assert "no evidence" in text.lower()


def test_render_fit_table_renders_rows():
    rows = [{"org_class": "mechanical-fix", "tier": "fast", "attempts": 5, "done": 4,
            "blocked": 1, "top_stage": "tests", "avg_tokens": 321.0}]
    text = org.render_fit_table(rows)
    assert "mechanical-fix" in text and "fast" in text and "5" in text and "tests" in text


# -- cmd_org: the read-only CLI surface (Task 1.4; plan/replan arrive in Phase 2) --------
def test_cmd_org_show_with_no_active_chart(tmp_path, capsys):
    with _store(tmp_path) as s:
        org.cmd_org(s, "show")
        out = capsys.readouterr().out
        assert "no active org chart" in out.lower()


def test_cmd_org_show_renders_classes_bench_and_rationale(tmp_path, capsys):
    with _store(tmp_path) as s:
        m = s.set_mission("ship it")
        s.add_org_chart(m, VALID_CHART, rationale="cited fit rows for worker tier")
        org.cmd_org(s, "show")
        out = capsys.readouterr().out
        assert "mechanical-fix" in out and "risky-core" in out and "standard-dev" in out
        assert "python-dev" in out and "core-surgeon" in out          # bench
        assert "cited fit rows for worker tier" in out                # rationale
        assert "default_class" in out.lower() and "standard-dev" in out


def test_cmd_org_fit_renders_the_table(tmp_path, capsys):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1)
        s.add_task("t1", "x", source="issue")
        s.add_routing_outcome("t1", shift_id=sh, org_class="mechanical-fix", profile="python-dev",
                              tier="fast", outcome="done", tokens=100)
        org.cmd_org(s, "fit")
        out = capsys.readouterr().out
        assert "mechanical-fix" in out and "fast" in out


def test_cmd_org_fit_with_no_evidence(tmp_path, capsys):
    with _store(tmp_path) as s:
        org.cmd_org(s, "fit")
        out = capsys.readouterr().out
        assert "no evidence" in out.lower()
