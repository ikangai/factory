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


def test_fleet_json_cost_kpi_and_per_shift_cost(tmp_path, monkeypatch):
    """Task 0.7: USD cost KPI, TRUE shift/token totals (beyond the last-30 window), and a
    per-shift cost — all from the shift-attributed ledger."""
    import pytest
    monkeypatch.setattr(fleet_viz, "live_workers", lambda: [])
    with _store(tmp_path) as s:
        s.set_mission("m", target_repo="r")
        a = s.start_shift(token_budget=1, mission_id=s.active_mission()["id"])
        s.add_budget("conductor", 100, 0.01, shift_id=a)
        s.add_budget("developer:t1", 400, 0.04, shift_id=a)
        s.end_shift(a, status="completed", tokens_used=500)
        b = s.start_shift(token_budget=1, mission_id=s.active_mission()["id"])
        s.add_budget("conductor", 200, 0.02, shift_id=b)
        s.end_shift(b, status="completed", tokens_used=200)

        j = fleet_viz.fleet_json(s)
    assert j["kpi"]["shifts"] == 2                                # true count
    assert j["kpi"]["total_cost_usd"] == pytest.approx(0.07)      # ledger-wide USD
    assert j["kpi"]["total_tokens"] >= 700
    costs = {sh["id"]: sh["cost"] for sh in j["shifts"]}
    assert costs[a] == pytest.approx(0.05) and costs[b] == pytest.approx(0.02)


def test_fleet_json_total_tokens_counts_shifts_beyond_the_window(tmp_path, monkeypatch):
    """kpi.total_tokens must sum ALL shifts' tokens_used, not just the last-30 view window — the
    old window-sum undercounts ~2x once history grows past N. 31 shifts > the 30-shift window, so
    a window-sum would read 30000 while the true lifetime total is 31000."""
    monkeypatch.setattr(fleet_viz, "live_workers", lambda: [])
    with _store(tmp_path) as s:
        s.set_mission("m", target_repo="r")
        for _ in range(31):                                      # legacy shape: tokens on the shift row,
            sh = s.start_shift(token_budget=1, mission_id=s.active_mission()["id"])   # no ledger rows
            s.end_shift(sh, status="completed", tokens_used=1000)
        j = fleet_viz.fleet_json(s)
    assert j["kpi"]["shifts"] == 31                              # true count (list is capped at 30)
    assert j["kpi"]["total_tokens"] == 31000                     # ALL shifts, not the 30-shift window


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
        # the BACKLOG & plan — active (non-done) tasks only; done excluded
        assert [x["id"] for x in j["backlog"]] == ["b"] and j["backlog"][0]["status"] == "blocked"
        # mission momentum
        assert "Advancing" in j["momentum"]["verdict"] and j["momentum"]["merges_series"] == [1]
        assert j["research"]["working"] is True


def test_fleet_server_mode_toggle(monkeypatch, tmp_path):
    """The dashboard's one write action: POST /api/mode toggles AUTO/SHIFT; bad value → 400."""
    import json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from factory.common import mode as modemod
    from factory.dashboard import fleet_server
    from factory.orchestrator import autopilot

    monkeypatch.setattr(modemod, "_mode_path", lambda: str(tmp_path / ".factory-mode"))
    monkeypatch.setattr(fleet_server, "fleet_state", lambda: {"mode": modemod.read_mode()})
    # HERMETIC: POST mode=auto reaches autopilot.start_runner(), which in prod spawns a REAL
    # detached `factory run --loop --real` subprocess reading the REAL STOP. Stub it so the
    # suite never launches a live runner (the fixture's tmp STOP doesn't reach a subprocess).
    started = {"n": 0}
    monkeypatch.setattr(autopilot, "start_runner",
                        lambda *a, **k: started.update(n=started["n"] + 1) or {"started": True})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), fleet_server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def post(body):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/mode", data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            return urllib.request.urlopen(req)

        out = json.loads(post(b'{"mode":"auto"}').read())
        assert out["mode"] == "auto" and modemod.read_mode() == "auto"     # toggled + persisted
        assert started["n"] == 1                                           # via the STUB, not a real spawn
        try:
            post(b'{"mode":"nonsense"}')
            assert False, "bad mode should 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400                                            # rejected
    finally:
        httpd.shutdown()


def test_fleet_json_includes_the_plan(tmp_path, monkeypatch):
    """Task 2.5: fleet_json carries the plan (milestones + progress) for the Plan tab."""
    monkeypatch.setattr(fleet_viz, "live_workers", lambda: [])
    with _store(tmp_path) as s:
        m = s.set_mission("m", target_repo="r")
        mid = s.add_milestone("M1: recovery", mission_id=m, deliverable="corpus green",
                              budget_tokens=800_000, planned_order=1)
        s.add_task("t1", "slice", source="research")
        s.set_task_milestone("t1", mid)
        s.set_task_status("t1", "done", result="abc")
        j = fleet_viz.fleet_json(s)
    plan = j["plan"]
    assert len(plan) == 1
    assert plan[0]["title"] == "M1: recovery" and plan[0]["status"] == "planned"
    assert plan[0]["budget_tokens"] == 800_000 and plan[0]["deliverable"] == "corpus green"
    assert plan[0]["progress"] == {"done": 1, "total": 1}


def test_fleet_server_plan_endpoint(monkeypatch):
    """Task 2.5: GET /api/plan serves the milestone list standalone (lazy-polled by the tab)."""
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from factory.dashboard import fleet_server

    monkeypatch.setattr(fleet_server, "plan_state",
                        lambda: [{"id": 1, "title": "M1", "status": "active", "progress": {"done": 0, "total": 2}}])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), fleet_server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        out = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/plan").read())
        assert out[0]["title"] == "M1" and out[0]["progress"]["total"] == 2
    finally:
        httpd.shutdown()


def test_fleet_server_timesheets_endpoint(monkeypatch):
    """Task 3.2: GET /api/timesheets → {rows, by_agent}."""
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from factory.dashboard import fleet_server

    monkeypatch.setattr(fleet_server, "timesheets_state",
                        lambda: {"rows": [{"agent": "conductor", "tokens": 100}],
                                 "by_agent": [{"role": "conductor", "tokens": 100}]})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), fleet_server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        out = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/timesheets").read())
        assert out["rows"][0]["agent"] == "conductor" and out["by_agent"][0]["role"] == "conductor"
    finally:
        httpd.shutdown()


def test_fleet_json_carries_the_worker_bench(tmp_path):
    """Task 5.7: fleet_json exposes a compact profiles list (bench + per-profile outcomes) for
    the Resources tab."""
    with _store(tmp_path) as s:
        sid = s.start_shift(token_budget=1)
        s.add_profile("python-dev", description="py", model="standard", overlay="x")
        s.add_task("t1", "slice", source="research")
        s.add_budget("developer:t1", 400, 0.04, shift_id=sid, notes="merged", profile="python-dev")
        j = fleet_viz.fleet_json(s)
    prof = {p["name"]: p for p in j["profiles"]}
    assert prof["python-dev"]["model"] == "standard" and prof["python-dev"]["active"] is True
    assert prof["python-dev"]["engagements"] == 1 and prof["python-dev"]["merged"] == 1


def test_fleet_server_evm_endpoint(monkeypatch):
    """Task 4.2: GET /api/evm serves evm(store) — the totals + per-milestone breakdown."""
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from factory.dashboard import fleet_server

    monkeypatch.setattr(fleet_server, "evm_state",
                        lambda: {"pv": 300_000, "ev": 200_000, "ac_tokens": 70_000,
                                 "cpi": 2.857, "milestones": [{"id": 1, "title": "M1"}]})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), fleet_server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        out = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/evm").read())
        assert out["pv"] == 300_000 and out["milestones"][0]["title"] == "M1"
    finally:
        httpd.shutdown()


def test_fleet_server_research_endpoint(monkeypatch):
    """Task 7.5: GET /api/research → {briefs:[...]} (reuses summary.gather_research_briefs)."""
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from factory.dashboard import fleet_server

    monkeypatch.setattr(fleet_server, "research_state",
                        lambda: {"briefs": [{"title": "retry loop", "technique": "backoff",
                                             "citation": "arxiv:1"}]})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), fleet_server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        out = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/research").read())
        assert out["briefs"][0]["title"] == "retry loop"
    finally:
        httpd.shutdown()


def test_fleet_server_mission_editor(monkeypatch, tmp_path):
    """Task 1.2: POST /api/mission validates (1..2000 chars) and applies via _set_mission
    (which rewrites MISSION.md + sets the store mission). Empty/oversize → 400."""
    import json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from factory.dashboard import fleet_server

    applied = {}
    monkeypatch.setattr(fleet_server, "_set_mission",
                        lambda s: applied.update(statement=s) or {"ok": True, "statement": s})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), fleet_server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def post(body):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/mission", data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            return urllib.request.urlopen(req)

        out = json.loads(post(b'{"statement":"make clive bulletproof"}').read())
        assert out["ok"] is True and applied["statement"] == "make clive bulletproof"
        # empty/oversize → 400; and a valid-JSON non-object body or non-string statement must
        # return 400, not crash the handler thread (dropped connection) with an AttributeError.
        for bad in (b'{"statement":""}', b'{"statement":"' + b"x" * 2001 + b'"}',
                    b'"just a string"', b'[1,2,3]', b'{"statement":123}', b'{"statement":["a"]}'):
            try:
                post(bad)
                assert False, "invalid body should 400"
            except urllib.error.HTTPError as e:
                assert e.code == 400
    finally:
        httpd.shutdown()


def test_set_mission_writes_store_before_file_so_a_busy_store_doesnt_steer(monkeypatch):
    """#8: _set_mission must set the store BEFORE rewriting MISSION.md — so a store-busy failure
    (the 503 case) leaves MISSION.md untouched. Otherwise the 'failed, retry' steer would still
    durably re-steer the loop at the next run start while the board shows the old mission."""
    import sqlite3
    import pytest
    from factory.dashboard import fleet_server
    from factory.research import focus

    wrote = {"n": 0}
    monkeypatch.setattr(focus, "write_mission", lambda p, s: wrote.update(n=wrote["n"] + 1))

    class BusyStore:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def init_db(self): pass
        def set_mission(self, statement): raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(fleet_server, "Blackboard", lambda *a, **k: BusyStore())
    with pytest.raises(sqlite3.OperationalError):
        fleet_server._set_mission("new steer")
    assert wrote["n"] == 0                         # MISSION.md untouched → no phantom steer


def test_upstream_issues_parses_into_structured_rows(monkeypatch):
    """The dashboard's issue feed: gh issue lines → {number,title,labels}, cached."""
    from factory.common import config
    from factory.roles import research_feed
    monkeypatch.setattr(config, "target_repo_slug", lambda: "ikangai/clive")
    monkeypatch.setattr(research_feed, "fetch_issues",
                        lambda repo, **k: "- #41: Self-learning tool discovery  [enhancement]\n"
                                          "- #38: Messaging CLIs")
    try:
        fleet_viz._refresh_issues()                 # synchronous refresh into the cache
        d = fleet_viz._ISSUE_CACHE["data"]
        assert d["repo"] == "ikangai/clive" and d["count"] == 2
        assert d["issues"][0] == {"number": 41, "title": "Self-learning tool discovery", "labels": "enhancement"}
        assert d["issues"][1] == {"number": 38, "title": "Messaging CLIs", "labels": ""}
    finally:
        fleet_viz._ISSUE_CACHE.update(t=-1e9, data={"repo": "", "count": 0, "issues": []}, fetching=False)


def test_dashboard_escapes_dynamic_fields_xss_guard():
    """XSS guard: task titles, conductor reports, and GH-issue-derived digests are
    attacker-influenceable, so every dynamic value that goes into innerHTML must be esc()'d."""
    page = os.path.join(os.path.dirname(__file__), "..", "dashboard", "static", "fleet.html")
    with open(page, encoding="utf-8") as fh:
        body = fh.read()
    assert "const esc=" in body                                          # the escaper exists
    for sink in ("esc(b.title)", "esc(t.title)", "esc(s.report", "esc(x)", "esc(w.role)"):
        assert sink in body, f"un-escaped dashboard field (XSS risk): {sink}"


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


# --------------------------------------------------------------------------- #
# the Work Queue — derived operator actions (PMO redesign)                     #
# --------------------------------------------------------------------------- #
def _calm_payload(**over):
    """A payload where nothing needs the operator (the all-clear baseline)."""
    p = {"halted": False, "mission": "m", "mode": "shift", "autopilot": {"running": False},
         "status": "advancing", "backlog": [], "collab": {"messages": []},
         "briefs_staged": 0, "phase": "develop", "running_shift": 3}
    p.update(over)
    return p


def test_derive_queue_orders_operator_actions_by_severity():
    """The operator starts from required actions (CompassAI grammar): red before amber
    before blue, and every action names the owning tab."""
    payload = _calm_payload(
        halted=True, mode="auto", status="blocked", phase="idle", running_shift=None,
        backlog=[{"id": "t1", "title": "a huge bundled brief", "status": "blocked",
                  "result": "no_candidate"},
                 {"id": "t2", "title": "fine", "status": "open", "result": ""}],
        collab={"messages": [{"sender": "ada", "to": ["human"], "body": "need a decision",
                              "kind": "chat", "ts": "2026-07-03T08:00:00"}]},
        briefs_staged=2)
    q = fleet_viz.derive_queue(payload)
    ids = [a["id"] for a in q]
    assert ids[0] == "halted"                                        # STOP outranks everything
    rank = {"red": 0, "amber": 1, "blue": 2, "green": 3}
    sev = [rank[a["severity"]] for a in q]
    assert sev == sorted(sev)                                        # severity-ordered
    assert all(a["tab"] for a in q)                                  # each opens an owning surface
    assert any(a["id"] == "blocked:t1" and a["tab"] == "plan" for a in q)
    assert any(a["id"] == "autopilot" for a in q)                    # auto mode, runner idle
    assert any(a["id"] == "escalation" and a["tab"] == "execution" for a in q)
    assert any(a["id"] == "briefs" and a["tab"] == "research" for a in q)
    assert not any(a["id"] == "blocked:t2" for a in q)               # open ≠ blocked


def test_derive_queue_all_clear_blocked_cap_and_no_mission():
    q = fleet_viz.derive_queue(_calm_payload())
    assert [a["severity"] for a in q] == ["green"] and q[0]["id"] == "all_clear"
    many = _calm_payload(backlog=[{"id": f"t{i}", "title": f"task {i}", "status": "blocked",
                                   "result": "no_candidate"} for i in range(9)])
    q2 = fleet_viz.derive_queue(many)
    blocked = [a for a in q2 if a["id"].startswith("blocked:")]
    assert len(blocked) == 5                                         # capped, not a wall
    assert any(a["id"] == "blocked_more" for a in q2)                # …but the rest is counted
    q3 = fleet_viz.derive_queue(_calm_payload(mission=None))
    assert any(a["id"] == "mission" and a["severity"] == "red" for a in q3)
    parked = fleet_viz.derive_queue(_calm_payload(phase="idle", running_shift=None))
    assert any(a["id"] == "parked" and a["severity"] == "blue" for a in parked)


def test_fleet_json_carries_queue_and_resume_note(tmp_path, monkeypatch):
    """/api/fleet feeds the Work Queue home tab: the derived actions ride the payload,
    plus the latest shift's resume note (the Report tab's 'next steps')."""
    monkeypatch.setattr(fleet_viz, "live_workers", lambda: [])
    with _store(tmp_path) as s:
        s.set_mission("m", target_repo="r")
        sh = s.start_shift(token_budget=1, mission_id=s.active_mission()["id"])
        s.end_shift(sh, status="completed", report="done", resume_note="next: ship the queue")
        j = fleet_viz.fleet_json(s)
    assert isinstance(j["queue"], list) and j["queue"]               # never empty (all-clear floor)
    assert all({"id", "title", "sub", "severity", "tab"} <= set(a) for a in j["queue"])
    assert j["resume_note"] == "next: ship the queue"
    assert isinstance(j["briefs_staged"], int)


# --------------------------------------------------------------------------- #
# Task 0.6 — deterministic dashboard self-check (checks/visual_check.py)       #
# The operator-memory lesson made executable: a JS syntax error in the inline  #
# <script> silently freezes the board while the server stays green.            #
# --------------------------------------------------------------------------- #
def test_visual_check_extracts_inline_scripts_only():
    """extract_scripts: every inline <script> body with its 1-indexed html line;
    external <script src=…> blocks are not ours to syntax-check."""
    from factory.checks import visual_check
    html = ('<html><head><script src="cdn.js"></script></head><body>\n'
            '<script>\nconst a=1;\n</script>\n'
            '<p>x</p>\n<script type="text/javascript">let b=2;</script></body></html>')
    blocks = visual_check.extract_scripts(html)
    assert len(blocks) == 2                                          # src= block skipped
    assert blocks[0][0] == 2 and blocks[0][1].strip() == "const a=1;"
    assert blocks[1][0] == 6 and blocks[1][1].strip() == "let b=2;"


def test_visual_check_node_flags_a_js_syntax_error(tmp_path):
    """The recorded failure class: broken inline JS must FAIL the check, with the
    offending <script>'s html line named in the error."""
    import shutil as _shutil

    import pytest
    from factory.checks import visual_check
    if _shutil.which("node") is None:
        pytest.skip("node not installed — JS syntax gate not exercised")
    page = tmp_path / "board.html"
    sections = "".join(f'<section id="{s}"></section>' for s in visual_check.REQUIRED_SECTIONS)
    page.write_text("<html><body>" + sections +
                    "<script>\nfunction broken({ ,\n</script></body></html>", encoding="utf-8")
    rep = visual_check.check_dashboard(str(page))
    assert rep["ok"] is False and rep["node_available"] is True
    assert rep["js_errors"] and "line" in rep["js_errors"][0]        # names where it broke
    assert rep["placeholders"] == [] and rep["missing_sections"] == []


def test_visual_check_flags_raw_placeholders_and_missing_sections(tmp_path, monkeypatch):
    """Deterministic scans run even without node (reported skip, not a crash): raw
    {PLACEHOLDER} braces and missing named sections each fail the check; JS template
    literals like `${LIVE_OK}` are legitimate and must NOT be flagged."""
    from factory.checks import visual_check
    monkeypatch.setattr(visual_check.shutil, "which", lambda _b: None)   # no node anywhere
    page = tmp_path / "board.html"
    page.write_text('<html><body><section id="tab-queue"></section>'
                    '<div>{MISSION}</div><script>const s=`${LIVE_OK}`;</script>'
                    '</body></html>', encoding="utf-8")
    rep = visual_check.check_dashboard(str(page))
    assert rep["ok"] is False
    assert rep["placeholders"] == ["{MISSION}"]                      # ${LIVE_OK} not flagged
    assert "tab-plan" in rep["missing_sections"]
    assert "tab-queue" not in rep["missing_sections"]
    assert rep["node_available"] is False and rep["js_errors"] == [] # skip reported, not failed


def test_visual_check_passes_on_the_live_dashboard():
    """THE gate: the shipped dashboard/static/fleet.html must always pass — this is the
    `node --check` operator habit as a permanent zero-token test."""
    import pytest
    from factory.checks import visual_check
    rep = visual_check.check_dashboard()                             # default = the live page
    assert rep["path"].endswith(os.path.join("dashboard", "static", "fleet.html"))
    assert rep["scripts"] >= 1                                       # the board HAS inline JS
    assert rep["placeholders"] == [] and rep["missing_sections"] == []
    if not rep["node_available"]:
        pytest.skip("node not installed — placeholder/section scans passed; JS syntax unverified")
    assert rep["js_errors"] == [] and rep["ok"] is True


def test_cmd_viz_selfcheck_runs_the_gate_and_returns_the_report(tmp_path, monkeypatch, capsys):
    """CLI arm `factory viz --selfcheck`: cmd_viz runs the checker, prints the report, and
    returns the dict (never opens a browser / writes a snapshot on this path)."""
    from factory.checks import visual_check
    from factory.orchestrator import orchestrator
    from factory.reporting import fleet_viz as fv
    monkeypatch.setattr(fv, "generate_fleet_html",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("snapshot path taken")))
    monkeypatch.setattr(visual_check, "check_dashboard",
                        lambda *a, **k: {"ok": False, "path": "p", "scripts": 1,
                                         "node_available": True,
                                         "js_errors": ["<script> at html line 2: SyntaxError: Unexpected token ','"],
                                         "placeholders": [], "missing_sections": []})
    with _store(tmp_path) as s:
        out = orchestrator.cmd_viz(s, open_browser=True, selfcheck=True)
    assert isinstance(out, dict) and out["ok"] is False              # the report rides out
    printed = capsys.readouterr().out
    assert "selfcheck" in printed and "SyntaxError" in printed and "FAIL" in printed


def test_factory_viz_selfcheck_exit_code(tmp_path, monkeypatch, capsys):
    """`factory viz --selfcheck` is an executable GATE: exit 1 on failure, 0 on pass."""
    from factory.checks import visual_check
    from factory.orchestrator import orchestrator
    monkeypatch.setattr(orchestrator, "Blackboard",
                        lambda *a, **k: Blackboard(str(tmp_path / "f.db")))   # hermetic store
    bad = {"ok": False, "path": "p", "scripts": 1, "node_available": False,
           "js_errors": [], "placeholders": ["{MISSION}"], "missing_sections": []}
    monkeypatch.setattr(visual_check, "check_dashboard", lambda *a, **k: dict(bad))
    assert orchestrator.main(["viz", "--selfcheck"]) == 1
    monkeypatch.setattr(visual_check, "check_dashboard",
                        lambda *a, **k: dict(bad, ok=True, placeholders=[]))
    assert orchestrator.main(["viz", "--selfcheck"]) == 0
    capsys.readouterr()                                              # swallow the report prints
