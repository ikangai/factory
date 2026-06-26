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
