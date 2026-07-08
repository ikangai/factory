"""dashboard/fleet_server.py: the human-queue write endpoints (Task 6, docs/plans/2026-07-08-
factory-owned-bus-human-queue.md; design: …-design.md §2) plus /api/fleet's human_queue key.

Hermetic — mirrors tests/test_resources.py's fleet-server harness exactly: a real
ThreadingHTTPServer bound to an ephemeral port, `fleet_server.Blackboard` monkeypatched to a
tmp-file store so every handler thread's fresh-connection-per-request pattern still lands on
an isolated db. Bus writes (`answer`) and approval execution (`execute_approval`/
`reject_approval` internals) are monkeypatched at the module-attribute level
(factory.common.bus.answer / factory.reporting.approvals.execute_approval) — those modules'
OWN semantics are proven by tests/test_bus.py and tests/test_approvals.py; this file proves
the fleet-server dispatch, validation, passthrough and audit-row wiring around them.
`reject_approval` is exercised for REAL (it's a thin store-only helper, no subprocess/git),
matching test_human_queue.py's "don't fake what's already cheap and real" bias."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from factory.common import bus as common_bus
from factory.common.store import Blackboard
from factory.reporting import approvals


def _serve():
    from factory.dashboard import fleet_server
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), fleet_server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def _post(port, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        return 200, json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(port, path):
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}{path}").read())


def _wire_store(monkeypatch, tmp_path):
    """Point the fleet server at a tmp-file store (test_resources.py's idiom) and neutralize
    the watchdog (test_autopilot.py's idiom) so a GET /api/fleet in this file never touches
    the real mode file / spawns a real process."""
    from factory.dashboard import fleet_server
    db = str(tmp_path / "bb.db")
    monkeypatch.setattr(fleet_server, "Blackboard", lambda: Blackboard(db))
    monkeypatch.setattr(fleet_server.autopilot, "restart_if_auto", lambda: None)
    return db


# -- /api/fleet carries human_queue ---------------------------------------------------------

def test_fleet_payload_includes_human_queue(monkeypatch, tmp_path):
    db = _wire_store(monkeypatch, tmp_path)
    # No real bus reachable from this test — derive_human_queue's escalations section must
    # see an empty/absent bus, not whatever the dev machine's REAL .groupchat happens to hold.
    monkeypatch.setenv("AGORA_DIR", str(tmp_path / "no_bus"))
    with Blackboard(db) as s:
        s.init_db()
    httpd, port = _serve()
    try:
        state = _get(port, "/api/fleet")
        assert state["human_queue"]["counts"] == {"escalations": 0, "approvals": 0,
                                                    "blocked": 0, "total": 0}
        assert state["human_queue"]["items"] == []
    finally:
        httpd.shutdown()


def test_fleet_payload_degrades_human_queue_on_failure(monkeypatch, tmp_path):
    """A raise anywhere in derive_human_queue must not blank the rest of the dashboard."""
    from factory.dashboard import fleet_server
    db = _wire_store(monkeypatch, tmp_path)
    with Blackboard(db) as s:
        s.init_db()

    def _boom(store):
        raise RuntimeError("boom")

    monkeypatch.setattr(fleet_server.human_queue, "derive_human_queue", _boom)
    httpd, port = _serve()
    try:
        state = _get(port, "/api/fleet")
        assert state["human_queue"] == {"items": [], "counts": {}}
        assert "mission" in state   # the REST of the payload still rendered
    finally:
        httpd.shutdown()


# -- POST /api/queue/answer ------------------------------------------------------------------

def test_queue_answer_happy_path_calls_bus_and_audits(monkeypatch, tmp_path):
    db = _wire_store(monkeypatch, tmp_path)
    calls = []

    def fake_answer(msg_id, text, **kw):
        calls.append((msg_id, text, kw))
        return True

    monkeypatch.setattr(common_bus, "answer", fake_answer)
    with Blackboard(db) as s:
        s.init_db()
    httpd, port = _serve()
    try:
        code, info = _post(port, "/api/queue/answer", {"id": 42, "text": "on it"})
        assert code == 200 and info == {"ok": True}
        assert calls == [(42, "on it", {})]
        with Blackboard(db) as s:
            actions = s.recent_operator_actions()
            assert len(actions) == 1
            assert actions[0]["action"] == "answer"
            assert actions[0]["item_ref"] == "bus-42"
            assert actions[0]["detail"] == "on it"
    finally:
        httpd.shutdown()


def test_queue_answer_bus_failure_returns_ok_false_and_no_audit(monkeypatch, tmp_path):
    db = _wire_store(monkeypatch, tmp_path)
    monkeypatch.setattr(common_bus, "answer", lambda *a, **k: False)
    with Blackboard(db) as s:
        s.init_db()
    httpd, port = _serve()
    try:
        code, info = _post(port, "/api/queue/answer", {"id": 1, "text": "hi"})
        assert code == 200 and info == {"ok": False}
        with Blackboard(db) as s:
            assert s.recent_operator_actions() == []   # a reply nobody received isn't audited
    finally:
        httpd.shutdown()


def test_queue_answer_validation_failures_400(monkeypatch, tmp_path):
    db = _wire_store(monkeypatch, tmp_path)
    with Blackboard(db) as s:
        s.init_db()
    httpd, port = _serve()
    try:
        assert _post(port, "/api/queue/answer", {"text": "hi"})[0] == 400          # no id
        assert _post(port, "/api/queue/answer", {"id": 1})[0] == 400               # no text
        assert _post(port, "/api/queue/answer", {"id": 1, "text": "   "})[0] == 400  # blank text
    finally:
        httpd.shutdown()


# -- POST /api/queue/task ---------------------------------------------------------------------

def test_queue_task_reframe_reopens_clears_result_and_audits(monkeypatch, tmp_path):
    db = _wire_store(monkeypatch, tmp_path)
    with Blackboard(db) as s:
        s.init_db()
        s.add_task("task-x", "old title", source="worker", detail="old detail")
        s.set_task_status("task-x", "blocked", result="boom")
    httpd, port = _serve()
    try:
        code, info = _post(port, "/api/queue/task",
                           {"op": "reframe", "task_id": "task-x", "title": "new title",
                            "detail": "new detail"})
        assert code == 200 and info == {"ok": True, "task_id": "task-x"}
        with Blackboard(db) as s:
            t = s.get_task("task-x")
            assert t["title"] == "new title" and t["detail"] == "new detail"
            assert t["status"] == "open" and t["result"] == ""
            actions = s.recent_operator_actions()
            assert actions[0]["action"] == "reframe" and actions[0]["item_ref"] == "task-x"
    finally:
        httpd.shutdown()


def test_queue_task_reframe_detail_only_is_allowed(monkeypatch, tmp_path):
    db = _wire_store(monkeypatch, tmp_path)
    with Blackboard(db) as s:
        s.init_db()
        s.add_task("task-x", "keep title", source="worker")
        s.set_task_status("task-x", "blocked", result="boom")
    httpd, port = _serve()
    try:
        code, info = _post(port, "/api/queue/task",
                           {"op": "reframe", "task_id": "task-x", "detail": "narrower brief"})
        assert code == 200 and info["ok"] is True
        with Blackboard(db) as s:
            t = s.get_task("task-x")
            assert t["title"] == "keep title" and t["detail"] == "narrower brief"
            assert t["status"] == "open"
    finally:
        httpd.shutdown()


def test_queue_task_retry_reopens_and_clears_result(monkeypatch, tmp_path):
    db = _wire_store(monkeypatch, tmp_path)
    with Blackboard(db) as s:
        s.init_db()
        s.add_task("task-y", "t", source="worker")
        s.set_task_status("task-y", "blocked", result="boom")
    httpd, port = _serve()
    try:
        code, info = _post(port, "/api/queue/task", {"op": "retry", "task_id": "task-y"})
        assert code == 200 and info == {"ok": True, "task_id": "task-y"}
        with Blackboard(db) as s:
            t = s.get_task("task-y")
            assert t["status"] == "open" and t["result"] == ""
            actions = s.recent_operator_actions()
            assert actions[0]["action"] == "retry" and actions[0]["item_ref"] == "task-y"
    finally:
        httpd.shutdown()


def test_queue_task_drop_sets_status_dropped(monkeypatch, tmp_path):
    db = _wire_store(monkeypatch, tmp_path)
    with Blackboard(db) as s:
        s.init_db()
        s.add_task("task-z", "t", source="worker")
    httpd, port = _serve()
    try:
        code, info = _post(port, "/api/queue/task", {"op": "drop", "task_id": "task-z"})
        assert code == 200 and info == {"ok": True, "task_id": "task-z"}
        with Blackboard(db) as s:
            assert s.get_task("task-z")["status"] == "dropped"
            actions = s.recent_operator_actions()
            assert actions[0]["action"] == "drop" and actions[0]["item_ref"] == "task-z"
    finally:
        httpd.shutdown()


def test_queue_task_add_creates_spec_complete_human_task(monkeypatch, tmp_path):
    db = _wire_store(monkeypatch, tmp_path)
    with Blackboard(db) as s:
        s.init_db()
    httpd, port = _serve()
    try:
        code, info = _post(port, "/api/queue/task",
                           {"op": "add", "title": "new work", "detail": "do the thing",
                            "target_surface": "llm.py", "acceptance": "tests pass"})
        assert code == 200 and info["ok"] is True
        tid = info["task_id"]
        assert tid.startswith("task-")
        with Blackboard(db) as s:
            t = s.get_task(tid)
            assert t["source"] == "human"
            assert t["title"] == "new work"
            assert t["status"] == "open"
            assert "do the thing" in t["detail"]
            assert "Target surface: llm.py" in t["detail"]
            assert "Acceptance: tests pass" in t["detail"]
            assert t["spec"] == {"target_surface": "llm.py", "acceptance": "tests pass",
                                 "out_of_scope": ""}
            actions = s.recent_operator_actions()
            assert actions[0]["action"] == "add" and actions[0]["item_ref"] == tid
    finally:
        httpd.shutdown()


def test_queue_task_validation_failures_400(monkeypatch, tmp_path):
    db = _wire_store(monkeypatch, tmp_path)
    with Blackboard(db) as s:
        s.init_db()
        s.add_task("task-real", "t", source="worker")
    httpd, port = _serve()
    try:
        assert _post(port, "/api/queue/task", {"op": "bogus"})[0] == 400
        assert _post(port, "/api/queue/task", {"op": "add"})[0] == 400              # no title
        assert _post(port, "/api/queue/task", {"op": "retry"})[0] == 400            # no task_id
        assert _post(port, "/api/queue/task",
                    {"op": "retry", "task_id": "task-nope"})[0] == 400              # unknown id
        assert _post(port, "/api/queue/task",
                    {"op": "reframe", "task_id": "task-real"})[0] == 400            # no title/detail
    finally:
        httpd.shutdown()


# -- POST /api/queue/approval -----------------------------------------------------------------

def test_queue_approval_approve_passthrough_no_double_audit(monkeypatch, tmp_path):
    db = _wire_store(monkeypatch, tmp_path)
    calls = []

    def fake_execute(store, approval_id):
        calls.append(approval_id)
        # a realistic execute_approval: it audits internally on success (approvals.py)
        store.record_operator_action("approve", f"approval-{approval_id}", "pushed 3 commit(s)")
        return {"ok": True, "result": {"action": "synced", "n_commits": 3}}

    monkeypatch.setattr(approvals, "execute_approval", fake_execute)
    with Blackboard(db) as s:
        s.init_db()
        aid = s.add_pending_approval("graduation", {"n_commits": 3})
    httpd, port = _serve()
    try:
        code, info = _post(port, "/api/queue/approval", {"approval_id": aid, "op": "approve"})
        assert code == 200
        assert info == {"ok": True, "result": {"action": "synced", "n_commits": 3}}
        assert calls == [aid]
        with Blackboard(db) as s:
            actions = s.recent_operator_actions()
            # ONLY the fake's own audit row — the endpoint must not double-audit on approve
            assert len(actions) == 1 and actions[0]["action"] == "approve"
    finally:
        httpd.shutdown()


def test_queue_approval_approve_preview_stale_passthrough(monkeypatch, tmp_path):
    db = _wire_store(monkeypatch, tmp_path)
    fresh = {"n_commits": 5, "range": "a..b"}

    def fake_execute(store, approval_id):
        return {"ok": False, "error": "preview-stale", "fresh": fresh}

    monkeypatch.setattr(approvals, "execute_approval", fake_execute)
    with Blackboard(db) as s:
        s.init_db()
        aid = s.add_pending_approval("graduation", {"n_commits": 3})
    httpd, port = _serve()
    try:
        code, info = _post(port, "/api/queue/approval", {"approval_id": aid, "op": "approve"})
        assert code == 200
        assert info == {"ok": False, "error": "preview-stale", "fresh": fresh}
    finally:
        httpd.shutdown()


def test_queue_approval_reject_records_note_and_audits(monkeypatch, tmp_path):
    db = _wire_store(monkeypatch, tmp_path)
    with Blackboard(db) as s:
        s.init_db()
        aid = s.add_pending_approval("graduation", {"n_commits": 3})
    httpd, port = _serve()
    try:
        code, info = _post(port, "/api/queue/approval",
                           {"approval_id": aid, "op": "reject", "note": "not ready"})
        assert code == 200 and info == {"ok": True}
        with Blackboard(db) as s:
            row = s.get_approval(aid)
            assert row["status"] == "rejected" and row["note"] == "not ready"
            actions = s.recent_operator_actions()
            assert actions[0]["action"] == "reject" and actions[0]["detail"] == "not ready"
    finally:
        httpd.shutdown()


def test_queue_approval_validation_failures_400(monkeypatch, tmp_path):
    db = _wire_store(monkeypatch, tmp_path)
    with Blackboard(db) as s:
        s.init_db()
    httpd, port = _serve()
    try:
        assert _post(port, "/api/queue/approval", {"approval_id": "x", "op": "approve"})[0] == 400
        assert _post(port, "/api/queue/approval", {"approval_id": 1, "op": "bogus"})[0] == 400
        assert _post(port, "/api/queue/approval", {"op": "approve"})[0] == 400   # no approval_id
    finally:
        httpd.shutdown()
