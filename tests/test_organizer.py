"""The organizer (design: docs/plans/2026-07-09-self-organizing-factory-design.md §3;
impl plan Phase 2): an isolated, FRONTIER-tier `claude -p` call that proposes an org
chart from the live backlog + bench + fit evidence; validate_chart (never the
organizer's own claim) decides whether it applies. Also carries the Phase-1 integrator
review's two additions: validate_chart's bench-entry/self-containment checks (addition A)
and the {BOUNDS} seam's content contract (addition B) — both landed here per the plan's
explicit instruction (existing test files are off-limits).
"""
from factory.common.store import Blackboard
from factory.orchestrator import org


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


def _one_class_chart(**overrides):
    """A minimal one-class, self-contained chart (bench supplies the class's own profile)
    for a single-violation test — override one field. Mirrors tests/test_org_resolver.py's
    helper of the same name/shape (kept local: that file is off-limits to edit)."""
    cls = {"name": "mechanical-fix", "match": {"any": ["typo"]},
          "stages": {"scope_check": False}, "tiers": {"worker": "fast"},
          "profile": "python-dev"}
    cls.update(overrides.pop("class_overrides", {}))
    bench = overrides.pop("bench", [
        {"name": "python-dev", "model": "fast", "overlay": "", "description": "py specialist"}])
    chart = {"classes": [cls], "default_class": "mechanical-fix", "bench": bench, "retire": []}
    chart.update(overrides)
    return chart


# -- addition A: validate_chart bench-entry + self-containment checks -------------------
def test_validate_chart_rejects_bad_bench_entry_slug():
    chart = _one_class_chart(bench=[{"name": "Not A Slug!", "model": "fast", "description": "d"}],
                             class_overrides={"profile": ""})
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is False
    assert any("slug" in r.lower() for r in reasons)


def test_validate_chart_rejects_bench_entry_model_not_in_palette():
    chart = _one_class_chart(bench=[{"name": "python-dev", "model": "turbo", "description": "d"}])
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is False
    assert any("turbo" in r for r in reasons)


def test_validate_chart_rejects_bench_entry_missing_description():
    chart = _one_class_chart(bench=[{"name": "python-dev", "model": "fast", "description": ""}])
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is False
    assert any("description" in r for r in reasons)


def test_validate_chart_rejects_bench_entry_non_string_description():
    chart = _one_class_chart(bench=[{"name": "python-dev", "model": "fast", "description": None}])
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is False
    assert any("description" in r for r in reasons)


def test_validate_chart_rejects_non_string_retire_entry():
    chart = _one_class_chart(retire=[42])
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is False
    assert any("retire" in r.lower() and "string" in r.lower() for r in reasons)


def test_validate_chart_accepts_string_retire_entries():
    chart = _one_class_chart(retire=["stale-a", "stale-b"])
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is True and reasons == []


def test_validate_chart_rejects_class_profile_not_self_contained():
    """A class naming a profile that is neither in the chart's own bench nor among the
    (unset here) active profiles is a dangling reference — reject."""
    chart = _one_class_chart(bench=[], class_overrides={"profile": "nowhere-defined"})
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is False
    assert any("nowhere-defined" in r and "self-contained" in r for r in reasons)


def test_validate_chart_accepts_class_profile_matching_bench_entry():
    chart = _one_class_chart()   # profile 'python-dev' IS in the default bench
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is True and reasons == []


def test_validate_chart_accepts_class_profile_matching_active_profile():
    """The self-containment check also honors profiles that already exist in the store
    (passed in via `active_profiles`) — not just the chart's own bench."""
    chart = _one_class_chart(bench=[], class_overrides={"profile": "already-active"})
    ok, reasons = org.validate_chart(chart, max_profiles=10, active_profiles={"already-active"})
    assert ok is True and reasons == []


def test_validate_chart_accepts_blank_profile_without_self_containment():
    """'' always means generalist — never a dangling reference, bench or no bench."""
    chart = _one_class_chart(bench=[], class_overrides={"profile": ""})
    ok, reasons = org.validate_chart(chart, max_profiles=10)
    assert ok is True and reasons == []


# -- addition B: the {BOUNDS} seam's content contract ------------------------------------
# The organizer's authority line is enforced in CODE (validate_chart); {BOUNDS} is what
# tells the MODEL where that line is, verbatim-clear, so a well-intentioned chart doesn't
# waste a plan reaching past it. Every fact below is asserted literally — this seam is
# read by an LLM, not executed, so its only "test" is: does the text actually say it.
def test_bounds_text_lists_every_org_bool_key():
    text = org._bounds_text(6)
    for key in org.ORG_BOOL_KEYS:
        assert key in text


def test_bounds_text_states_scope_check_and_auto_decompose_are_narrow_only():
    text = org._bounds_text(6)
    assert "scope_check" in text and "auto_decompose" in text
    assert "narrow" in text.lower()
    assert "callable" in text.lower()   # the "won't exist" mechanism, named honestly


def test_bounds_text_states_the_other_six_work_both_ways():
    text = org._bounds_text(6)
    both_ways = org.ORG_BOOL_KEYS - {"scope_check", "auto_decompose"}
    assert len(both_ways) == 6
    for key in both_ways:
        assert key in text
    assert "both ways" in text.lower()


def test_bounds_text_states_tier_palette_and_synonyms():
    text = org._bounds_text(6)
    for tier in org.TIER_PALETTE:
        assert (tier or "''") in text or tier in text
    assert "frontier" in text.lower() and "synonym" in text.lower()


def test_bounds_text_states_max_profiles_verbatim():
    text = org._bounds_text(9)
    assert "9" in text and "max_profiles" in text


def test_bounds_text_states_brakes_budgets_capacity_frozen_human_out_of_reach():
    text = org._bounds_text(6)
    low = text.lower()
    assert "out of reach" in low or "permanently" in low
    assert "rejected wholesale" in low or "reject" in low
    assert "frozen" in low
    assert "human" in low
    assert "budget" in low or "brake" in low
    assert "max_parallel" in text   # a named capacity int, out of reach


# -- build_organizer_prompt: the seams actually get filled -------------------------------
def test_build_organizer_prompt_fills_every_seam(tmp_path):
    with _store(tmp_path) as s:
        m = s.set_mission("ship it", target_repo="acme/widget")
        s.add_task("t1", "fix a typo in the README", source="issue")
        s.add_profile("python-dev", description="py specialist", model="fast", overlay="")
        p = org.build_organizer_prompt(s, mission=s.active_mission(), max_profiles=6)
        assert "ship it" in p
        assert "t1: fix a typo" in p
        assert "python-dev" in p
        assert "no evidence yet" in p.lower()            # empty fit table
        assert "max_profiles" in p
        for seam in ("{MISSION}", "{BACKLOG}", "{BENCH}", "{FIT}", "{MEMORY}", "{BOUNDS}"):
            assert seam not in p


def test_build_organizer_prompt_with_no_mission_is_a_standing_prompt(tmp_path):
    with _store(tmp_path) as s:
        p = org.build_organizer_prompt(s, mission=None, max_profiles=6)
        assert "{MISSION}" not in p
        assert "no active mission" in p.lower() or "standing" in p.lower()


# -- plan_org: propose / validate / apply / supersede, fail-closed -----------------------
_VALID_REPLY_CHART = {
    "classes": [
        {"name": "mechanical-fix", "match": {"any": ["typo"]},
         "stages": {"scope_check": False}, "tiers": {"worker": "fast"},
         "profile": "python-dev"},
        {"name": "standard-dev", "match": {"any": ["*"]}, "stages": {}, "tiers": {},
         "profile": ""},
    ],
    "default_class": "standard-dev",
    "bench": [{"name": "python-dev", "model": "fast", "overlay": "o", "description": "d"}],
    "retire": ["old-hand"],
    "rationale": "mechanical-fix/fast: no evidence yet — judgment.",
}


def _fake_claude(reply_obj):
    """The repo's established fake-claude_p pattern (mirrors
    tests/test_factory_memory.py's investigate_blocked fakes): returns
    (text, tokens, cost) and records what it was called with."""
    import json as _json
    seen = {}

    def fake(prompt, *, model="", **k):
        seen["prompt"] = prompt
        seen["model"] = model
        seen["n"] = seen.get("n", 0) + 1
        return _json.dumps(reply_obj), 50, 0.01

    fake.seen = seen
    return fake


def test_plan_org_happy_path_applies_chart_bench_and_classification(tmp_path):
    with _store(tmp_path) as s:
        m = s.set_mission("ship it")
        # a PRIOR active chart, to prove supersession
        prior_id = s.add_org_chart(m, {"classes": [{"name": "old", "match": {"any": ["x"]},
                                                     "stages": {}, "tiers": {}, "profile": ""}],
                                        "default_class": "old", "bench": [], "retire": []})
        s.add_profile("old-hand", description="d", model="standard", overlay="")
        s.add_task("t1", "fix a typo in the docs", source="issue")
        s.add_task("t2", "totally unrelated work", source="issue")

        fake = _fake_claude(_VALID_REPLY_CHART)
        chart = org.plan_org(s, force=True, claude_fn=fake)

        assert chart is not None and fake.seen["n"] == 1
        assert fake.seen["model"] == org.config.resolve_model("")   # frontier tier
        row = s.get_active_org_chart(m)
        assert row is not None and row["chart"]["default_class"] == "standard-dev"
        assert s._one("SELECT status FROM org_charts WHERE id = ?", (prior_id,))["status"] == "superseded"
        assert s.get_profile("python-dev") is not None               # bench upserted (new)
        assert s.get_profile("old-hand")["active"] == 0               # retired one deactivated
        assert s.get_task("t1")["org_class"] == "mechanical-fix"
        assert s.get_task("t1")["profile"] == "python-dev"
        assert s.get_task("t2")["org_class"] == "standard-dev"        # default fallthrough
        assert s.get_task("t2")["profile"] == ""
        # ledger_rows() filters WHERE shift_id IS NOT NULL (the timesheet view) — a bare
        # CLI-invoked plan (no shift) is NULL-shift_id by design, so query the table directly.
        ledger = s._all("SELECT * FROM budget_ledger WHERE notes = 'organizer'")
        assert ledger and ledger[0]["tokens"] == 50


def test_plan_org_invalid_json_records_learning_and_no_chart_change(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("ship it")

        def fake(prompt, **k):
            return "not json at all", 10, 0.001

        result = org.plan_org(s, claude_fn=fake)
        assert not result
        assert s.get_active_org_chart(None) is None and s._all("SELECT * FROM org_charts") == []
        rows = [r for r in s.learnings_for_role("factory") if r["scope"] == "organizer"]
        assert rows and "unparseable" in rows[0]["content"].lower()


def test_plan_org_validation_failure_stores_rejected_chart_and_learning(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        bad = {"classes": [{"name": "x", "match": {"any": ["y"]}, "stages": {"max_parallel": True},
                            "tiers": {}, "profile": ""}], "default_class": "x", "bench": [], "retire": []}
        fake = _fake_claude(bad)
        result = org.plan_org(s, claude_fn=fake)
        assert not result
        rows = s._all("SELECT * FROM org_charts")
        assert len(rows) == 1 and rows[0]["status"] == "rejected"
        assert s.get_active_org_chart(None) is None                  # never applied
        learn = [r for r in s.learnings_for_role("factory") if r["scope"] == "organizer"]
        assert learn and "validation" in learn[0]["content"].lower()


def test_plan_org_stop_engaged_no_claude_call(tmp_path, monkeypatch):
    from factory.common import killswitch
    monkeypatch.setattr(killswitch, "is_halted", lambda: True)
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        fake = _fake_claude(_VALID_REPLY_CHART)
        result = org.plan_org(s, claude_fn=fake)
        assert not result and fake.seen == {}
        assert s._all("SELECT * FROM org_charts") == [] and s._all("SELECT * FROM budget_ledger") == []


def test_plan_org_refuses_without_force_when_a_chart_already_exists(tmp_path):
    with _store(tmp_path) as s:
        m = s.set_mission("ship it")
        s.add_org_chart(m, _VALID_REPLY_CHART)
        fake = _fake_claude(_VALID_REPLY_CHART)
        result = org.plan_org(s, claude_fn=fake)             # force defaults False
        assert not result and fake.seen == {}                 # never even called
        assert len(s._all("SELECT * FROM org_charts")) == 1   # unchanged


def test_plan_org_ledgers_spend_with_shift_id_from_the_hook(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        sh = s.start_shift(token_budget=1000)
        fake = _fake_claude(_VALID_REPLY_CHART)
        org.plan_org(s, claude_fn=fake, shift_id=sh)
        rows = [r for r in s.ledger_rows(shift_id=sh) if r["notes"] == "organizer"]
        assert rows and rows[0]["tokens"] == 50
        assert s.shift_spend(sh)["tokens"] == 50


def test_plan_org_ledgers_spend_without_shift_id_when_cli_invoked(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        fake = _fake_claude(_VALID_REPLY_CHART)
        org.plan_org(s, claude_fn=fake)
        rows = s._all("SELECT * FROM budget_ledger WHERE notes = 'organizer'")
        assert rows and rows[0]["shift_id"] is None


def test_plan_org_works_with_no_active_mission_standing_chart(tmp_path):
    """A bare `factory org plan` with no mission steers a STANDING chart (mission_id
    NULL) — the design's own fallback for a mission-less dev/test run."""
    with _store(tmp_path) as s:
        fake = _fake_claude(_VALID_REPLY_CHART)
        chart = org.plan_org(s, claude_fn=fake)
        assert chart is not None
        row = s.get_active_org_chart(None)
        assert row is not None and row["mission_id"] is None
