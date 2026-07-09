"""Org-chart store substrate (design: docs/plans/2026-07-09-self-organizing-factory-design.md
§1/§4; impl plan Task 1.1): org_charts (the validated chart document, versioned per mission),
routing_outcomes (the fit-table evidence, one row per dispatched task at close-out), and
tasks.org_class (the per-task class assignment). Hermetic — a tmp SQLite db per test, same
convention as test_conductor_store.py."""
from factory.common.store import Blackboard


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


CHART = {
    "classes": [{"name": "mechanical-fix", "match": {"any": ["typo"]},
                "stages": {"scope_check": False}, "tiers": {"worker": "fast"},
                "profile": "python-dev"}],
    "default_class": "mechanical-fix",
    "bench": [],
    "retire": [],
}


def test_add_and_get_active_org_chart_round_trips_and_latest_active_wins(tmp_path):
    with _store(tmp_path) as s:
        assert s.get_active_org_chart() is None            # nothing yet
        cid1 = s.add_org_chart(1, CHART, rationale="first cut")
        got = s.get_active_org_chart(1)
        assert got["id"] == cid1
        assert got["chart"] == CHART                        # round-trips as a dict, not a string
        assert got["rationale"] == "first cut"
        assert got["version"] == 1

        chart2 = {**CHART, "default_class": "standard-dev"}
        cid2 = s.add_org_chart(1, chart2, rationale="replan")
        got2 = s.get_active_org_chart(1)
        assert got2["id"] == cid2 and got2["chart"]["default_class"] == "standard-dev"
        assert got2["version"] == 2                          # bumped, not reset


def test_get_active_org_chart_with_no_mission_id_returns_only_standing_charts(tmp_path):
    """AMENDED (Fix 4a, self-organizing-factory adversarial review): mission_id=None must
    select ONLY standing charts (mission_id IS NULL) — NOT "the latest active chart of ANY
    mission", which is what this test used to pin. That old behavior is exactly what let a
    newly-active mission with no chart of its own "inherit" an unrelated mission's chart
    via the fallback in orchestrator/org.py:_active_row (Fix 4b) — a real bug, not a
    feature. Supersedes this test's prior name/body."""
    with _store(tmp_path) as s:
        s.add_org_chart(1, CHART)
        s.add_org_chart(2, CHART)
        assert s.get_active_org_chart() is None               # no STANDING chart exists yet
        cid_standing = s.add_org_chart(None, CHART)            # a genuine standing chart
        latest = s.get_active_org_chart()
        assert latest["id"] == cid_standing


def test_supersede_org_charts_flips_actives_to_superseded(tmp_path):
    with _store(tmp_path) as s:
        cid = s.add_org_chart(1, CHART)
        s.supersede_org_charts(1)
        assert s.get_active_org_chart(1) is None              # no longer active
        row = s._one("SELECT status FROM org_charts WHERE id = ?", (cid,))
        assert row["status"] == "superseded"


def test_supersede_org_charts_scopes_to_the_mission(tmp_path):
    """Superseding mission 1's chart must not touch mission 2's active chart."""
    with _store(tmp_path) as s:
        s.add_org_chart(1, CHART)
        cid2 = s.add_org_chart(2, CHART)
        s.supersede_org_charts(1)
        assert s.get_active_org_chart(2)["id"] == cid2         # untouched


def test_supersede_org_charts_except_id_spares_the_named_row(tmp_path):
    """Fix 3a: the apply-path reorder inserts the NEW chart (active) first, then supersedes
    every OTHER active chart for the mission — `except_id` must spare the new row itself so
    a mission is never left with zero active charts, even transiently."""
    with _store(tmp_path) as s:
        old1 = s.add_org_chart(1, CHART)
        new = s.add_org_chart(1, CHART)          # both 'active' at this point (pre-supersede)
        s.supersede_org_charts(1, except_id=new)
        assert s._one("SELECT status FROM org_charts WHERE id = ?", (old1,))["status"] == "superseded"
        assert s._one("SELECT status FROM org_charts WHERE id = ?", (new,))["status"] == "active"
        assert s.get_active_org_chart(1)["id"] == new


def test_set_task_org_class_and_legacy_default(tmp_path):
    with _store(tmp_path) as s:
        s.add_task("t1", "fix typo", source="issue")
        assert s.get_task("t1")["org_class"] == ""             # additive migration default
        s.set_task_org_class("t1", "mechanical-fix")
        assert s.get_task("t1")["org_class"] == "mechanical-fix"


def test_org_class_column_exists_on_a_fresh_db_via_migrate(tmp_path):
    """Exercises the _migrate guarded-ALTER pattern directly (common/store.py:82-100 style):
    a fresh init_db() must produce the column via the CREATE+migrate path."""
    with _store(tmp_path) as s:
        cols = {r[1] for r in s.conn.execute("PRAGMA table_info(tasks)").fetchall()}
        assert "org_class" in cols


def test_rejected_chart_is_stored_but_never_returned_as_active(tmp_path):
    with _store(tmp_path) as s:
        cid = s.add_org_chart(1, CHART, status="rejected", rationale="bad tier")
        assert s.get_active_org_chart(1) is None
        row = s._one("SELECT * FROM org_charts WHERE id = ?", (cid,))
        assert row["status"] == "rejected"


def test_add_routing_outcome_and_fit_rows_aggregate(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1)
        s.add_task("t1", "x", source="issue")
        s.add_task("t2", "y", source="issue")
        s.add_task("t3", "z", source="issue")
        s.add_routing_outcome("t1", shift_id=sh, org_class="mechanical-fix", profile="python-dev",
                              tier="fast", outcome="done", tokens=100)
        s.add_routing_outcome("t2", shift_id=sh, org_class="mechanical-fix", profile="python-dev",
                              tier="fast", outcome="done", tokens=200)
        s.add_routing_outcome("t3", shift_id=sh, org_class="mechanical-fix", profile="python-dev",
                              tier="fast", outcome="blocked", stage="tests", tokens=300)
        rows = s.fit_rows()
        assert len(rows) == 1
        r = rows[0]
        assert r["org_class"] == "mechanical-fix" and r["tier"] == "fast"
        assert r["attempts"] == 3 and r["done"] == 2 and r["blocked"] == 1
        assert r["top_stage"] == "tests"
        assert r["avg_tokens"] == 200


def test_fit_rows_empty_is_an_empty_list(tmp_path):
    with _store(tmp_path) as s:
        assert s.fit_rows() == []
