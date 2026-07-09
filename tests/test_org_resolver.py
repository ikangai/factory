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


def test_validate_chart_rejects_blank_string_in_match_any():
    """Fix 2a: a blank/whitespace-only keyword in match.any is a `"" in text` substring
    test that matches EVERY task — an accidental catch-all."""
    chart = _one_class_chart(class_overrides={"match": {"any": ["typo", "   "]}})
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is False
    assert any("match.any" in r for r in reasons)


def test_validate_chart_rejects_pure_empty_string_in_match_any():
    chart = _one_class_chart(class_overrides={"match": {"any": [""]}})
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is False
    assert any("match.any" in r for r in reasons)


def test_validate_chart_rejects_duplicate_class_names():
    """Fix 2b: classify() matches the FIRST same-named class, but apply's classes_by_name
    dict keys on name and applies the LAST — a duplicate name means two different things
    depending which code path reads it."""
    chart = {"classes": [
        {"name": "dup", "match": {"any": ["a"]}, "stages": {}, "tiers": {}, "profile": ""},
        {"name": "dup", "match": {"any": ["b"]}, "stages": {}, "tiers": {}, "profile": ""},
    ], "default_class": "dup", "bench": [], "retire": []}
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is False
    assert any("dup" in r and "duplicate" in r.lower() for r in reasons)


def test_validate_chart_rejects_more_than_max_classes():
    """Fix 2c: MAX_CLASSES caps a chart at a handful of reusable buckets, not one class
    per task."""
    classes = [{"name": f"class-{i}", "match": {"any": ["x"]}, "stages": {}, "tiers": {},
               "profile": ""} for i in range(org.MAX_CLASSES + 1)]
    chart = {"classes": classes, "default_class": "class-0", "bench": [], "retire": []}
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is False
    assert any(str(org.MAX_CLASSES) in r for r in reasons)


def test_validate_chart_accepts_exactly_max_classes():
    classes = [{"name": f"class-{i}", "match": {"any": ["x"]}, "stages": {}, "tiers": {},
               "profile": ""} for i in range(org.MAX_CLASSES)]
    chart = {"classes": classes, "default_class": "class-0", "bench": [], "retire": []}
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is True and reasons == []


def test_validate_chart_stage_key_rejects_milestone_verify_and_investigate_blocked():
    """Fix 2f: these two ARE legal SETTINGS_SPEC booleans (ORG_BOOL_KEYS) but have no live
    per-task consult site — validate_chart's `stages` whitelist is ORG_WIRED_KEYS, the
    narrower set, so naming either is now a hard rejection, not a silent no-op."""
    for key in ("milestone_verify", "investigate_blocked"):
        chart = _one_class_chart(class_overrides={"stages": {key: True}})
        ok, reasons = org.validate_chart(chart, max_profiles=10)
        assert ok is False
        assert any(key in r for r in reasons)


def test_org_wired_keys_is_a_subset_of_org_bool_keys():
    """Drift guard: ORG_WIRED_KEYS must never name something ORG_BOOL_KEYS (the derived
    SETTINGS_SPEC boolean set) doesn't even recognize as a legal boolean leaf."""
    assert org.ORG_WIRED_KEYS <= org.ORG_BOOL_KEYS


def test_validate_chart_bench_cap_counts_resulting_active_not_raw_bench_list():
    """Fix 2d: cap arithmetic is EXISTING active (minus retires) plus bench adds, generalist
    excluded — not a raw len(bench) count. Two bench entries that REPLACE two already-active
    profiles (no retires) must not overflow a cap of 2."""
    chart = _one_class_chart(bench=[
        {"name": "python-dev", "model": "fast", "description": "d"},
        {"name": "core-surgeon", "model": "standard", "description": "d"},
    ], class_overrides={"profile": "python-dev"})
    ok, reasons = org.validate_chart(chart, max_profiles=2,
                                     active_profiles={"python-dev", "core-surgeon"})
    assert ok is True and reasons == []


def test_validate_chart_bench_cap_counts_retires_as_freeing_room():
    chart = _one_class_chart(bench=[{"name": "new-hand", "model": "fast", "description": "d"}],
                             retire=["stale-hand"], class_overrides={"profile": "new-hand"})
    ok, reasons = org.validate_chart(chart, max_profiles=2,
                                     active_profiles={"stale-hand", "other-hand"})
    assert ok is True and reasons == []      # stale-hand retires → room for new-hand


def test_validate_chart_bench_cap_rejects_when_resulting_count_exceeds_cap():
    chart = _one_class_chart(bench=[{"name": "new-hand", "model": "fast", "description": "d"}],
                             class_overrides={"profile": "new-hand"})
    ok, reasons = org.validate_chart(chart, max_profiles=1,
                                     active_profiles={"other-hand"})
    assert ok is False
    assert any("max_profiles" in r for r in reasons)


def test_validate_chart_bench_cap_never_counts_generalist():
    chart = _one_class_chart(bench=[{"name": "new-hand", "model": "fast", "description": "d"}],
                             class_overrides={"profile": "new-hand"})
    ok, reasons = org.validate_chart(chart, max_profiles=1, active_profiles={"generalist"})
    assert ok is True and reasons == []


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


def test_task_params_always_classifies_fresh_ignoring_a_stale_stamp(tmp_path):
    """AMENDED (Fix 5a, self-organizing-factory adversarial review — staleness):
    task_params no longer trusts a task's STAMPED org_class as an input — it's a WRITTEN
    RECORD of a past classify() call, never a cache to read back (both the stamp and this
    call are classify() outputs over the same chart; recomputing is strictly fresher). Here
    the task is STAMPED 'mechanical-fix' but its title no longer matches that class's own
    rules (staleness, e.g. after a `task reopen` or a replan that redefined the same class
    name) — task_params must re-classify fresh and fall through to default_class, NOT
    blindly honor the stale stamp. Supersedes this test's prior name/body, which pinned the
    old (stale-trusting) behavior."""
    with _store(tmp_path) as s:
        m = s.set_mission("x")
        s.add_org_chart(m, VALID_CHART)
        s.add_task("t1", "irrelevant title", source="issue")
        s.set_task_org_class("t1", "mechanical-fix")   # a stale/wrong stamp
        t = s.get_task("t1")
        params = org.task_params(s, t)
        assert params.org_class == "standard-dev"       # fresh classify() wins, not the stamp
        assert params.profile == ""


def test_task_params_fresh_classify_agrees_with_an_accurate_stamp(tmp_path):
    """A task whose title genuinely matches its stamped class still resolves correctly —
    fresh classification and an accurate stamp simply agree (the stamp was never consulted,
    but classify() lands on the same answer either way)."""
    with _store(tmp_path) as s:
        m = s.set_mission("x")
        s.add_org_chart(m, VALID_CHART)
        s.add_task("t1", "fix a typo in the README", source="issue")
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


# -- chart scoping correctness (Fix 4b) ---------------------------------------
def test_active_mission_without_its_own_chart_is_chartless_not_inherited(tmp_path):
    """Fix 4b (self-organizing-factory adversarial review): mission B becoming active must
    NOT inherit mission A's still-active chart — an active mission without its own chart is
    CHARTLESS, full stop (the design's own YAGNI list rules out cross-mission standing
    orgs). RED before the fix: org.get_active_chart used to fall back to
    get_active_org_chart(None), which (pre-4a) returned "the latest active chart of ANY
    mission" — mission A's chart would leak into mission B."""
    with _store(tmp_path) as s:
        mission_a = s.set_mission("mission A")
        s.add_org_chart(mission_a, VALID_CHART)
        s.set_mission("mission B")             # switches the active mission; steps A down
        assert org.get_active_chart(s) is None   # chartless — mission A's chart NOT inherited


def test_standing_chart_still_applies_with_no_active_mission(tmp_path):
    """The standing-chart fallback survives Fix 4b — it just narrows to "no active mission
    at all", not "this mission happens to have none"."""
    with _store(tmp_path) as s:
        s.add_org_chart(None, VALID_CHART)
        assert org.get_active_chart(s) == VALID_CHART


# -- class_summary (Fix 7 simplification): shared by cmd_org show + fleet_viz -----------
def test_class_summary_renders_stages_and_tiers_kv_with_frontier_default():
    c = {"name": "mechanical-fix", "profile": "python-dev",
        "stages": {"scope_check": False}, "tiers": {"worker": "fast", "reviewer": ""}}
    cs = org.class_summary(c)
    assert cs["name"] == "mechanical-fix" and cs["profile"] == "python-dev"
    assert cs["stages_kv"] == "scope_check=False"
    assert "worker=fast" in cs["tiers_kv"] and "reviewer=frontier" in cs["tiers_kv"]


def test_class_summary_defaults_for_a_blank_class():
    cs = org.class_summary({})
    assert cs["name"] == "" and cs["profile"] == "(none)"
    assert cs["stages_kv"] == "(none)" and cs["tiers_kv"] == "(none)"


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
