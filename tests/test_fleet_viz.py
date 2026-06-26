"""The fleet visualization (reporting/fleet_viz.py): a self-contained HTML view of the
factory's worker instances + activities. Hermetic — build_fleet_state + render_fleet_html
are pure (the store is seeded; the live-worker list is injected, no pgrep)."""
import os

from factory.common.store import Blackboard
from factory.reporting import fleet_viz


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


def test_build_state_groups_tasks_by_shift_and_status(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("make clive reliable", target_repo="ikangai/clive")
        sh = s.start_shift(token_budget=500000, mission_id=s.active_mission()["id"])
        s.add_task("t1", "fix dead-pane detection", source="research")
        s.set_task_status("t1", "done", result="c742f9d3abcd", shift_id=sh)
        s.add_task("t2", "add retry", source="issue")
        s.set_task_status("t2", "blocked", result="no_candidate", shift_id=sh)
        s.add_task("t3", "an open one", source="worker")     # never worked → no shift_id
        s.end_shift(sh, status="completed", report="planned + shipped 1", tokens_used=46177)

        state = fleet_viz.build_fleet_state(s)
        assert state["shifts"][0]["id"] == sh
        assert {t["id"] for t in state["shifts"][0]["tasks"]} == {"t1", "t2"}   # worked this shift
        assert [t["id"] for t in state["tasks_by_status"]["open"]] == ["t3"]    # backlog board
        assert [t["id"] for t in state["tasks_by_status"]["done"]] == ["t1"]


def test_render_shows_workers_activities_and_live(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("make clive reliable", target_repo="ikangai/clive")
        sh = s.start_shift(token_budget=1, mission_id=s.active_mission()["id"])
        s.add_task("t1", "fix dead-pane detection", source="research")
        s.set_task_status("t1", "done", result="c742f9d3abcd", shift_id=sh)
        s.add_task("t2", "add retry", source="issue")
        s.set_task_status("t2", "blocked", result="no_candidate", shift_id=sh)
        s.end_shift(sh, status="completed", report="shipped one", tokens_used=46177)
        s.record_mission_status(shift_id=sh, status="advancing", rationale="1 shipped", metrics={})
        s.add_digest(shift_id=sh, shipped=["t1"], summary="shipped the dead-pane fix")

        doc = fleet_viz.render_fleet_html(
            fleet_viz.build_fleet_state(s),
            live=[{"pid": "90311", "role": "developer worker", "where": "/tmp/cf-dev-x/clone"}],
            generated_at="just now")

    assert "<html" in doc and "make clive reliable" in doc           # mission
    assert f"shift {sh}" in doc                                       # conductor instance
    assert "fix dead-pane detection" in doc and "c742f9d3" in doc     # a shipped dispatch + sha
    assert "blocked" in doc and "no_candidate" in doc                 # a blocked dispatch + reason
    assert "shipped the dead-pane fix" in doc                         # research digest
    assert "developer worker" in doc and "90311" in doc               # the LIVE worker
    assert "advancing" in doc                                         # mission-status timeline


def test_render_handles_an_empty_factory(tmp_path):
    with _store(tmp_path) as s:
        doc = fleet_viz.render_fleet_html(fleet_viz.build_fleet_state(s), live=[],
                                          generated_at="now")
    assert "no mission set" in doc and "no shifts yet" in doc         # graceful, not a crash


def test_live_workers_filters_shells_and_echoes(monkeypatch):
    """Only real super-workers (claude -p … --add-dir …) count — not shells/greps that
    merely MENTION 'claude -p' in their text (the 'pid: echo' junk seen on the live board)."""
    import types
    canned = (
        '4011 /Users/x/.local/bin/claude -p --add-dir /tmp/cf-dev-abc/clone --max-turns 24 --allowedTools Read Write Edit Bash Grep WebSearch\n'
        '5123 /bin/zsh -c echo "checking claude -p workers"\n'
        '6001 claude -p --setting-sources user --add-dir /Users/x/factory --max-turns 60 --allowedTools Read Bash Grep WebSearch\n'
        '7001 claude -p --setting-sources user --add-dir /Users/x/Development --max-turns 40 --allowedTools Read Grep Glob WebSearch WebFetch\n')
    monkeypatch.setattr(fleet_viz.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout=canned))
    ws = fleet_viz.live_workers()
    assert [w["pid"] for w in ws] == ["4011", "6001", "7001"]   # the zsh/echo line dropped
    # classified by toolset signature: clone→developer, Bash-not-in-clone→conductor, web+no-Bash→researcher
    assert [w["role"] for w in ws] == ["developer worker", "conductor", "researcher"]


def test_fleet_json_derives_phase_and_summary(tmp_path, monkeypatch):
    """The --serve data layer: the current LOOP PHASE is derived from live workers + the
    running shift, and the summary counts drive the progress visuals."""
    with _store(tmp_path) as s:
        s.set_mission("inter-clive comms", target_repo="ikangai/clive")
        sh = s.start_shift(token_budget=1, mission_id=s.active_mission()["id"])   # a RUNNING shift
        s.add_task("a", "x", source="issue"); s.set_task_status("a", "done", result="sha", shift_id=sh)
        s.add_task("b", "y", source="research")                                    # open
        s.add_task("c", "z", source="issue"); s.set_task_status("c", "blocked", result="no_candidate", shift_id=sh)
        s.record_mission_status(shift_id=sh, status="advancing", rationale="1 shipped", metrics={})

        monkeypatch.setattr(fleet_viz, "live_workers",
                            lambda: [{"pid": "1", "role": "developer worker", "where": "/tmp/cf-dev-x"}])
        j = fleet_viz.fleet_json(s)
        assert j["phase"] == "develop" and j["running_shift"] == sh        # worker live → develop
        assert j["mission"] == "inter-clive comms" and j["status"] == "advancing"
        assert j["summary"] == {"shifts": 1, "shipped": 1, "open": 1, "in_progress": 0, "blocked": 1}

        monkeypatch.setattr(fleet_viz, "live_workers", lambda: [])
        assert fleet_viz.fleet_json(s)["phase"] == "plan"                  # running shift, no worker → plan
        s.end_shift(sh, status="completed")
        assert fleet_viz.fleet_json(s)["phase"] == "idle"                  # nothing running → idle


def test_fleet_json_has_ceo_kpis_built_ledger_and_momentum(tmp_path, monkeypatch):
    """The CEO view: KPIs (shipped/shifts/tokens/exec/workers/research), the built ledger
    (what was shipped, with shas), and a mission-momentum verdict."""
    with _store(tmp_path) as s:
        s.set_mission("inter-clive comms", target_repo="r")
        sh = s.start_shift(token_budget=1, mission_id=s.active_mission()["id"])
        s.add_task("a", "verify-before-done", source="research")
        s.set_task_status("a", "done", result="247cebcAAA1", shift_id=sh)
        s.add_task("b", "planner verify", source="issue")
        s.set_task_status("b", "blocked", result="no_candidate", shift_id=sh)
        s.end_shift(sh, status="completed", report="shipped 1", tokens_used=46000)
        s.record_mission_status(shift_id=sh, status="advancing", rationale="1 shipped", metrics={})
        monkeypatch.setattr(fleet_viz, "live_workers", lambda: [])

        j = fleet_viz.fleet_json(s)
        k = j["kpi"]
        assert k["shipped"] == 1 and k["shifts"] == 1
        assert k["total_tokens"] == 46000 and k["tokens_per_merge"] == 46000      # efficiency
        assert k["workers_total"] == 2                                            # 1 done + 1 blocked dispatched
        assert k["research_proposed"] == 1 and k["research_shipped"] == 1         # is research working?
        assert k["exec_seconds"] >= 0                                             # the ended shift has a duration
        # the BUILT ledger — what's actually been built
        assert [b["title"] for b in j["built"]] == ["verify-before-done"]
        assert j["built"][0]["sha"] == "247cebcAAA" and j["built"][0]["source"] == "research"
        # mission momentum
        assert "Advancing" in j["momentum"]["verdict"] and j["momentum"]["merges_series"] == [1]
        assert j["research"]["working"] is True


def test_fleet_server_serves_the_live_page_and_api(monkeypatch):
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from factory.dashboard import fleet_server

    monkeypatch.setattr(fleet_server, "fleet_state", lambda: {
        "mission": "m", "phase": "develop", "status": "advancing", "running_shift": 3,
        "summary": {"shifts": 1, "shipped": 1, "open": 0, "in_progress": 0, "blocked": 0},
        "live": [], "shifts": [], "mission_status": [], "digests": []})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), fleet_server.Handler)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        api = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/fleet").read())
    finally:
        httpd.shutdown()
    assert "HARNESS FACTORY" in page and "/api/fleet" in page             # the live page
    assert api["phase"] == "develop" and api["summary"]["shipped"] == 1   # the polled state


def test_generate_writes_a_self_contained_file(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("m")
        out = str(tmp_path / "fleet.html")
        path = fleet_viz.generate_fleet_html(s, out_path=out, generated_at="t")
    assert path == out and os.path.exists(out)
    with open(out, encoding="utf-8") as fh:
        body = fh.read()
    assert body.startswith("<!doctype html") and "Harness Factory" in body
