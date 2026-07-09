"""The self-organizing org chart's board surface (impl plan Task 3.1; design
docs/plans/2026-07-09-self-organizing-factory-design.md §5: "the fleet-viz data JSON... a
dedicated tab is follow-up polish"). `reporting.fleet_viz.org_state` is a standalone
section function (mirrors plan_list/profiles_compact — see tests/test_fleet_viz.py), always
returning the SAME keys whether or not a chart is active: a chartless mission is the
explicit "no org chart" state, never a missing key. `_org_section_html` renders the one
compact HTML block; a REJECTED latest chart must stay visible (audit surfacing) even when
an older, unaffected chart is still active. New file (fleet_viz's tests are NOT split per
section — test_fleet_viz.py is one file — but Task 3.1 calls for a fresh file for the org
surface specifically, so the org substrate's test suite stays together: test_org_store.py,
test_org_resolver.py, test_org_dispatch.py, test_organizer.py, test_org_viz.py)."""
import copy

from factory.common.store import Blackboard
from factory.common import config
from factory.reporting import fleet_viz


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


CHART = {
    "classes": [
        {"name": "mechanical-fix", "match": {"any": ["typo"]},
         "stages": {"scope_check": False, "reviewer": False},
         "tiers": {"worker": "fast", "reviewer": ""}, "profile": "python-dev"},
        {"name": "standard-dev", "match": {"any": ["*"]}, "stages": {}, "tiers": {},
         "profile": ""},
    ],
    "default_class": "standard-dev",
    "bench": [],
    "retire": [],
}


def _organizer_on(monkeypatch):
    """super_worker.organizer=true on a deepcopy — the load_config monkeypatch pattern
    tests/test_organizer.py's _config_with_organizer_on uses, so the real lru-cached dict
    is never mutated."""
    cfg = copy.deepcopy(config.load_config())
    cfg.setdefault("super_worker", {})["organizer"] = True
    monkeypatch.setattr(config, "load_config", lambda: cfg)


# --------------------------------------------------------------------------- #
# org_state — the payload section (pure function of the store)                #
# --------------------------------------------------------------------------- #
def test_org_state_is_an_explicit_no_org_chart_state_never_a_missing_key(tmp_path):
    with _store(tmp_path) as s:
        org = fleet_viz.org_state(s)
    assert org["state"] == "no org chart"
    # every key present even chartless — a caller never has to guess from absence
    for key in ("state", "version", "default_class", "classes", "organizer_on", "fit", "latest"):
        assert key in org
    assert org["classes"] == [] and org["fit"] == []
    assert org["latest"] == {"version": 0, "status": ""}
    assert org["organizer_on"] is False


def test_org_state_surfaces_the_active_chart_summary(tmp_path):
    with _store(tmp_path) as s:
        m = s.set_mission("m")
        s.add_org_chart(m, CHART, rationale="first cut")
        org = fleet_viz.org_state(s)
    assert org["state"] == "active"
    assert org["version"] == 1
    assert org["default_class"] == "standard-dev"
    by_name = {c["name"]: c for c in org["classes"]}
    assert set(by_name) == {"mechanical-fix", "standard-dev"}
    mech = by_name["mechanical-fix"]
    assert mech["profile"] == "python-dev"
    assert mech["stages"] == {"scope_check": False, "reviewer": False}
    assert mech["tiers"] == {"worker": "fast", "reviewer": ""}
    assert by_name["standard-dev"]["profile"] == ""


def test_org_state_falls_back_to_a_standing_chart_with_no_active_mission(tmp_path):
    """No active mission -> org_state still surfaces a standing chart (mission_id NULL),
    mirroring orchestrator/org.py's own _active_row fallback."""
    with _store(tmp_path) as s:
        s.add_org_chart(None, CHART)
        org = fleet_viz.org_state(s)
    assert org["state"] == "active" and org["default_class"] == "standard-dev"


def test_org_state_carries_the_fit_table_rows(tmp_path):
    with _store(tmp_path) as s:
        sid = s.start_shift(token_budget=1)
        s.add_task("t1", "x", source="worker")
        s.add_routing_outcome("t1", shift_id=sid, org_class="mechanical-fix",
                              profile="python-dev", tier="fast", outcome="done", tokens=100)
        s.add_task("t2", "y", source="worker")
        s.add_routing_outcome("t2", shift_id=sid, org_class="mechanical-fix",
                              profile="python-dev", tier="fast", outcome="blocked",
                              stage="gate", tokens=200)
        org = fleet_viz.org_state(s)
        expected_fit = s.fit_rows()
    assert org["fit"] == expected_fit
    row = org["fit"][0]
    assert row["org_class"] == "mechanical-fix" and row["attempts"] == 2
    assert row["done"] == 1 and row["blocked"] == 1


def test_org_state_reports_the_organizer_knob(tmp_path, monkeypatch):
    with _store(tmp_path) as s:
        off = fleet_viz.org_state(s)
        _organizer_on(monkeypatch)
        on = fleet_viz.org_state(s)
    assert off["organizer_on"] is False
    assert on["organizer_on"] is True


def test_org_state_surfaces_a_rejected_latest_chart_alongside_an_older_active_one(tmp_path):
    """A rejected replan attempt must stay visible (audit surfacing) even though it never
    applied — the active chart (an older, unaffected version) is reported separately."""
    with _store(tmp_path) as s:
        m = s.set_mission("m")
        s.add_org_chart(m, CHART, status="active")
        bad = {"classes": [{"name": "bad", "match": {"any": ["x"]}, "stages": {"nope": True},
                            "tiers": {}, "profile": ""}], "default_class": "missing"}
        s.add_org_chart(m, bad, status="rejected")
        org = fleet_viz.org_state(s)
    assert org["state"] == "active" and org["version"] == 1     # the active chart is unaffected
    assert org["latest"] == {"version": 2, "status": "rejected"}  # the rejected attempt is visible


def test_org_state_latest_is_empty_when_nothing_was_ever_proposed(tmp_path):
    with _store(tmp_path) as s:
        org = fleet_viz.org_state(s)
    assert org["latest"] == {"version": 0, "status": ""}


def test_org_state_never_raises_on_a_broken_store(monkeypatch, tmp_path):
    """Crash-proof like every other fleet_viz section (module docstring: 'never crashes the
    caller') — a store method blowing up degrades to the chartless state, not an exception."""
    with _store(tmp_path) as s:
        def boom(*a, **k):
            raise RuntimeError("store exploded")
        monkeypatch.setattr(s, "active_mission", boom)
        org = fleet_viz.org_state(s)
    assert org["state"] == "no org chart"


# --------------------------------------------------------------------------- #
# fleet_json — the payload carries "org"                                      #
# --------------------------------------------------------------------------- #
def test_fleet_json_carries_the_org_section(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_viz, "live_workers", lambda: [])
    with _store(tmp_path) as s:
        m = s.set_mission("m")
        s.add_org_chart(m, CHART)
        j = fleet_viz.fleet_json(s)
    assert j["org"]["state"] == "active"
    assert j["org"]["default_class"] == "standard-dev"


def test_fleet_json_org_section_is_chartless_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_viz, "live_workers", lambda: [])
    with _store(tmp_path) as s:
        j = fleet_viz.fleet_json(s)
    assert j["org"]["state"] == "no org chart"


# --------------------------------------------------------------------------- #
# HTML — one compact block, always present                                    #
# --------------------------------------------------------------------------- #
def test_render_fleet_html_renders_no_org_chart_when_chartless(tmp_path):
    with _store(tmp_path) as s:
        doc = fleet_viz.render_fleet_html(fleet_viz.build_fleet_state(s), live=[],
                                          generated_at="now")
    assert "Org chart" in doc and "no org chart" in doc


def test_render_fleet_html_renders_the_active_chart_and_fit_evidence(tmp_path):
    with _store(tmp_path) as s:
        m = s.set_mission("m")
        s.add_org_chart(m, CHART)
        org = fleet_viz.org_state(s)
        doc = fleet_viz.render_fleet_html(fleet_viz.build_fleet_state(s), live=[],
                                          generated_at="now", org=org)
    assert "mechanical-fix" in doc and "python-dev" in doc
    assert "default_class=standard-dev" in doc
    assert "fit evidence: none yet" in doc


def test_render_fleet_html_surfaces_a_rejected_latest_chart_in_the_html(tmp_path):
    with _store(tmp_path) as s:
        m = s.set_mission("m")
        s.add_org_chart(m, CHART, status="active")
        s.add_org_chart(m, {"classes": [], "default_class": ""}, status="rejected")
        org = fleet_viz.org_state(s)
        doc = fleet_viz.render_fleet_html(fleet_viz.build_fleet_state(s), live=[],
                                          generated_at="now", org=org)
    assert "REJECTED" in doc


def test_generate_fleet_html_writes_the_org_section_into_the_file(tmp_path):
    with _store(tmp_path) as s:
        m = s.set_mission("m")
        s.add_org_chart(m, CHART)
        out = str(tmp_path / "fleet.html")
        fleet_viz.generate_fleet_html(s, out_path=out, generated_at="t")
    with open(out, encoding="utf-8") as fh:
        body = fh.read()
    assert "Org chart" in body and "mechanical-fix" in body


# --------------------------------------------------------------------------- #
# store substrate: latest_org_chart (any status, unlike get_active_org_chart) #
# --------------------------------------------------------------------------- #
def test_latest_org_chart_returns_the_most_recent_row_of_any_status(tmp_path):
    with _store(tmp_path) as s:
        assert s.latest_org_chart(1) is None
        s.add_org_chart(1, CHART, status="active")
        cid2 = s.add_org_chart(1, CHART, status="rejected")
        latest = s.latest_org_chart(1)
    assert latest["id"] == cid2 and latest["status"] == "rejected"
    assert latest["chart"] == CHART


def test_latest_org_chart_with_no_mission_id_returns_latest_overall(tmp_path):
    with _store(tmp_path) as s:
        s.add_org_chart(1, CHART)
        cid2 = s.add_org_chart(2, CHART, status="rejected")
        latest = s.latest_org_chart()
    assert latest["id"] == cid2


# --------------------------------------------------------------------------- #
# Fix 8b: `factory task list` surfaces [class/profile] markers when set        #
# --------------------------------------------------------------------------- #
def test_task_list_appends_class_profile_marker_when_set(tmp_path, capsys):
    from factory.orchestrator import orchestrator
    with _store(tmp_path) as s:
        s.add_task("t1", "fix a typo", source="issue")
        s.set_task_org_class("t1", "mechanical-fix")
        s.set_task_profile("t1", "python-dev")
        orchestrator.cmd_task(s, "list")
        out = capsys.readouterr().out
    assert "[mechanical-fix/python-dev]" in out


def test_task_list_omits_the_marker_when_unassigned(tmp_path, capsys):
    """A task with no class/profile renders its line byte-identical to before this fix —
    no trailing bracket noise for the common (chartless) case."""
    from factory.orchestrator import orchestrator
    with _store(tmp_path) as s:
        s.add_task("t1", "fix a typo", source="issue")
        orchestrator.cmd_task(s, "list")
        out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("t1"))
    assert line == "t1\topen\t[issue] fix a typo"
    assert "[" not in line.split("]", 1)[1]   # no SECOND bracket group after "[issue]"


def test_task_list_marker_shows_a_class_with_no_profile(tmp_path, capsys):
    from factory.orchestrator import orchestrator
    with _store(tmp_path) as s:
        s.add_task("t1", "fix a typo", source="issue")
        s.set_task_org_class("t1", "standard-dev")   # class set, profile stays generalist ('')
        orchestrator.cmd_task(s, "list")
        out = capsys.readouterr().out
    assert "[standard-dev/]" in out
