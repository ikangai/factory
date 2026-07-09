"""Live fleet 'mission control' — a tiny localhost server behind `factory viz --serve`.

Serves the animated loop page (dashboard/static/fleet.html) and `/api/fleet`, the JSON
state the page polls every ~2s. Bound to localhost, opens its OWN blackboard connection
per request so the data is always fresh while a run is in flight. Mostly read-only; the
one store-writing action is the mission editor (POST /api/mission, CSRF-guarded, per-request
connection, store-busy → 503) — this server is the FIRST second process to write the live
blackboard, so every write handler opens a fresh Blackboard and never shares it across the
ThreadingHTTPServer's threads. STOP/resume/mode toggles write the killswitch/mode files."""
from __future__ import annotations

import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from ..common import mode as modemod
from ..common.store import Blackboard
from ..orchestrator import autopilot
from ..reporting import fleet_viz, human_queue

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

MAX_MISSION_CHARS = 2000


def fleet_state() -> dict:
    autopilot.restart_if_auto()   # brake-respecting self-heal: revive a crashed runner in AUTO (Task 5.3)
    with Blackboard() as store:
        store.init_db()   # idempotent — a pre-init board shows empty, not a 500
        state = fleet_viz.fleet_json(store)
        # The Queue tab's data (Task 6, design §2). derive_human_queue already degrades each
        # of its three sections to [] on its own failure (see reporting/human_queue.py's
        # module docstring) — this try/except is a second, outer belt: a future change to
        # that contract, or a raise from a section this module doesn't yet wrap, must still
        # leave the REST of the dashboard rendering rather than 500 the whole poll.
        try:
            state["human_queue"] = human_queue.derive_human_queue(store)
        except Exception as e:  # noqa: BLE001
            print(f"[queue] human_queue unavailable: {e}")
            state["human_queue"] = {"items": [], "counts": {}}
        return state


def plan_state() -> list:
    with Blackboard() as store:
        store.init_db()
        return fleet_viz.plan_list(store)


def timesheets_state() -> dict:
    from ..reporting import timesheets
    with Blackboard() as store:
        store.init_db()
        return {"rows": timesheets.timesheet(store), "by_agent": timesheets.by_agent(store),
                "by_profile": timesheets.by_profile(store)}


def research_state() -> dict:
    """Staged research briefs for the board's Research tab (Task 7.5). Filesystem read-only —
    reuses reporting.summary.gather_research_briefs rather than duplicating the gather."""
    from ..reporting.summary import gather_research_briefs
    return {"briefs": gather_research_briefs()}


def evm_state() -> dict:
    from ..reporting import evm
    with Blackboard() as store:
        store.init_db()
        return evm.evm(store)


def resources_state() -> dict:
    from ..reporting import resources
    with Blackboard() as store:
        store.init_db()
        return resources.resources(store)


def _apply_setting(key: str, value) -> dict:
    """Validate + persist a whitelisted runtime override (Task 6.2). Raises ValueError (→400) on a
    bad key/value, sqlite3.OperationalError (→503) on a busy store. Takes effect at the NEXT shift
    (that's when cmd_run resolves knobs), so we say so honestly."""
    from ..common import config
    if key not in config.SETTINGS_SPEC:
        raise ValueError(f"unknown setting {key!r}")
    kind = config.SETTINGS_SPEC[key]
    if kind is bool:
        low = str(value).strip().lower()
        if low not in ("true", "false"):
            raise ValueError("bool setting must be 'true' or 'false'")
        stored = low
    else:                                             # int knob: non-negative integers only
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValueError("int setting must be an integer")
        if n < 0:
            raise ValueError("int setting must be >= 0")
        stored = str(n)
    with Blackboard() as store:
        store.init_db()
        store.set_setting(key, stored)
    return {"key": key, "value": stored, "applied_at": "next shift"}


def _apply_worker(action: str, name: str, *, description: str = "", overlay: str = "",
                  model: str = "") -> dict:
    """add/retire a worker profile via the SAME guardrails as the CLI (worker_admin — one policy).
    Raises ValueError (→400) on a guard failure, sqlite3.OperationalError (→503) on a busy store."""
    from ..reporting import worker_admin
    with Blackboard() as store:
        store.init_db()
        if action == "add":
            err = worker_admin.validate_add(name, model, overlay) or worker_admin.cap_error(store, name)
            if err:
                raise ValueError(err)
            store.add_profile(name, description=description, overlay=overlay, model=model,
                              created_by="operator")
            return {"action": "add", "name": name}
        if action == "retire":
            err = worker_admin.retire_error(store, name)
            if err:
                raise ValueError(err)
            store.retire_profile(name)
            return {"action": "retire", "name": name}
        raise ValueError(f"unknown worker action {action!r}")


def _queue_answer(payload: dict) -> dict:
    """POST /api/queue/answer: reply to bus escalation `id` as the operator (Task 6, design
    §2). `common.bus.answer` NEVER raises (see common/bus.py's module docstring) — a bus
    outage returns ok:False rather than a 500. Only a SUCCESSFUL reply is audited: a phantom
    audit row for a reply nobody actually received (because the bus was down) would be
    worse than no row at all."""
    from ..common import bus
    msg_id = payload.get("id")
    if msg_id is None:
        raise ValueError("id required")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text required")
    ok = bus.answer(msg_id, text)
    if ok:
        with Blackboard() as store:
            store.init_db()
            store.record_operator_action("answer", f"bus-{msg_id}", text[:200])
    return {"ok": ok}


def _queue_task(payload: dict) -> dict:
    """POST /api/queue/task: reframe/retry/drop an existing backlog task, or add a new
    operator-authored one (Task 6, design §2). reframe/retry/drop require an existing
    `task_id` — an unknown id is a ValueError (-> 400), the same guard-rejection convention
    _apply_setting/_apply_worker use above. Every branch records an operator_action on
    success only."""
    from ..reporting import scope_check
    import uuid
    op = payload.get("op")
    if op not in ("reframe", "retry", "drop", "add"):
        raise ValueError(f"unknown op {op!r}")
    with Blackboard() as store:
        store.init_db()
        if op == "add":
            title = (payload.get("title") or "").strip()
            if not title:
                raise ValueError("title required")
            # Born spec-complete, like a research-fed task (roles/research_feed.py):
            # optional target_surface/acceptance/out_of_scope fold into detail via the same
            # helper the scope check reads.
            spec = {"target_surface": str(payload.get("target_surface") or ""),
                    "acceptance": str(payload.get("acceptance") or ""),
                    "out_of_scope": str(payload.get("out_of_scope") or "")}
            detail = str(payload.get("detail") or "") + scope_check.spec_detail_suffix(spec)
            tid = f"task-{uuid.uuid4().hex[:8]}"
            # source='human': store/schema.sql's tasks.source CHECK reserves this value for
            # exactly this case — operator-authored, distinct from 'research'/'worker'/
            # 'issue'/'mission'.
            store.add_task(tid, title, source="human", detail=detail, spec=spec)
            store.record_operator_action("add", tid, title[:200])
            return {"ok": True, "task_id": tid}
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id required")
        if store.get_task(task_id) is None:
            raise ValueError(f"unknown task_id {task_id!r}")
        if op == "reframe":
            title = payload.get("title")
            detail = payload.get("detail")
            title = title if isinstance(title, str) else None
            detail = detail if isinstance(detail, str) else None
            if title is None and detail is None:
                raise ValueError("reframe needs a title and/or detail")
            store.reframe_task(task_id, title=title, detail=detail)
            store.record_operator_action(
                "reframe", task_id, title[:200] if title is not None else "(detail only)")
            return {"ok": True, "task_id": task_id}
        if op == "retry":
            # result is NOT NULL (schema default '') — clear it via '' per set_task_status's
            # own convention (it only touches result when the kwarg is not None).
            store.set_task_status(task_id, "open", result="")
            store.record_operator_action("retry", task_id)
            return {"ok": True, "task_id": task_id}
        store.set_task_status(task_id, "dropped")   # op == "drop"
        store.record_operator_action("drop", task_id)
        return {"ok": True, "task_id": task_id}


def _queue_approval(payload: dict) -> dict:
    """POST /api/queue/approval: approve/reject a pending_approvals row (Task 6, design §2/3).
    `execute_approval` already claims/verifies/resolves/audits internally on EVERY outcome
    (approve / approve-failed / approve-stale-refreshed — see reporting/approvals.py) — this
    must NOT add a second audit row on approve. `reject_approval` audits itself too. Both
    results pass through VERBATIM: a stale-preview approve returns
    {"ok": False, "error": "preview-stale", "fresh": ...} and the Queue tab needs that exact
    shape to re-render the fresh card."""
    from ..reporting import approvals
    try:
        approval_id = int(payload.get("approval_id"))
    except (TypeError, ValueError):
        raise ValueError("approval_id required")
    op = payload.get("op")
    if op not in ("approve", "reject"):
        raise ValueError(f"unknown op {op!r}")
    with Blackboard() as store:
        store.init_db()
        if op == "approve":
            return approvals.execute_approval(store, approval_id)
        note = payload.get("note")
        return approvals.reject_approval(store, approval_id, note if isinstance(note, str) else "")


def _set_mission(statement: str) -> dict:
    """Apply a mission steer from the board: set the active mission in a FRESH per-request store
    (ground rule 2 — never share a store across handler threads), THEN rewrite MISSION.md's
    ## Mission (durable, so it survives the next run-start sync). Store-first so a store-busy 503
    leaves MISSION.md untouched — otherwise a 'failed, retry' would still durably steer the loop
    at the next run start while the board kept showing the old mission. Returns the applied state."""
    from ..common import paths
    from ..research.focus import write_mission
    with Blackboard() as store:
        store.init_db()
        store.set_mission(statement)
    write_mission(paths.factory("MISSION.md"), statement)
    return {"ok": True, "statement": statement}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/fleet", "/index.html"):
            page = os.path.join(STATIC, "fleet.html")
            with open(page, "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        api = {"/api/fleet": fleet_state, "/api/plan": plan_state,
               "/api/timesheets": timesheets_state, "/api/evm": evm_state,
               "/api/resources": resources_state, "/api/research": research_state}
        if path in api:
            try:
                body = json.dumps(api[path](), default=str).encode("utf-8")
            except Exception as e:  # noqa: BLE001
                return self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            return self._send(200, body, "application/json")
        return self._send(404, b'{"error":"not found"}', "application/json")

    def _local_origin(self) -> bool:
        """CSRF guard — the board binds localhost but is browser-reachable; allow only
        same-origin (no Origin) or an explicit localhost Origin for the one write action."""
        origin = self.headers.get("Origin") or self.headers.get("Referer")
        if not origin:
            return True
        try:
            return urlparse(origin).hostname in ("127.0.0.1", "localhost", "::1")
        except Exception:  # noqa: BLE001
            return False

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in ("/api/mode", "/api/stop", "/api/resume", "/api/mission",
                        "/api/settings", "/api/worker",
                        "/api/queue/answer", "/api/queue/task", "/api/queue/approval"):
            return self._send(404, b'{"error":"unknown write action"}', "application/json")
        if not self._local_origin():
            return self._send(403, b'{"error":"cross-origin refused (CSRF guard)"}', "application/json")
        if path in ("/api/settings", "/api/worker"):   # runtime knobs + workforce (Task 6.2)
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, ValueError) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            if not isinstance(payload, dict):
                return self._send(400, b'{"error":"body must be a JSON object"}', "application/json")
            try:
                if path == "/api/settings":
                    # str()-coerce the key (like the worker path) — an unhashable JSON list/object
                    # key must become an unknown-key 400, not a `key not in dict` TypeError crash.
                    info = _apply_setting(str(payload.get("key", "")), payload.get("value"))
                else:
                    info = _apply_worker(str(payload.get("action", "")),
                                         str(payload.get("name", "")),
                                         description=str(payload.get("description", "")),
                                         overlay=str(payload.get("overlay", "")),
                                         model=str(payload.get("model", "")))
            except ValueError as e:                    # a guard rejection → 400
                return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            except sqlite3.OperationalError:           # store busy (WAL cross-process contention)
                return self._send(503, b'{"error":"store busy, retry"}', "application/json")
            return self._send(200, json.dumps(info).encode(), "application/json")
        if path.startswith("/api/queue/"):   # human queue actions (Task 6, design §2)
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, ValueError) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            if not isinstance(payload, dict):
                return self._send(400, b'{"error":"body must be a JSON object"}', "application/json")
            try:
                if path == "/api/queue/answer":
                    info = _queue_answer(payload)
                elif path == "/api/queue/task":
                    info = _queue_task(payload)
                else:                                       # /api/queue/approval
                    info = _queue_approval(payload)
            except ValueError as e:                    # a validation rejection → 400
                return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            except sqlite3.OperationalError:           # store busy (WAL cross-process contention)
                return self._send(503, b'{"error":"store busy, retry"}', "application/json")
            # info can carry a nested "fresh" preview dict (approve's stale-preview shape) —
            # default=str keeps json.dumps robust the same way the GET /api/fleet path does.
            return self._send(200, json.dumps(info, default=str).encode(), "application/json")
        if path == "/api/mission":            # the human's steering wheel, live on the board
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, ValueError) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            if not isinstance(payload, dict):         # a valid-JSON non-object (list/str/number)
                return self._send(400, b'{"error":"body must be a JSON object"}', "application/json")
            raw = payload.get("statement")
            statement = (raw if isinstance(raw, str) else "").strip()
            if not statement or len(statement) > MAX_MISSION_CHARS:
                return self._send(400, json.dumps(
                    {"error": f"statement must be 1..{MAX_MISSION_CHARS} chars"}).encode(),
                    "application/json")
            try:
                info = _set_mission(statement)
            except sqlite3.OperationalError:  # store busy (WAL cross-process contention)
                return self._send(503, b'{"error":"store busy, retry"}', "application/json")
            return self._send(200, json.dumps(info).encode(), "application/json")
        from ..common import killswitch
        if path == "/api/stop":               # HALT the fleet now — the board's emergency brake
            killswitch.engage("dashboard")
            return self._send(200, b'{"halted": true}', "application/json")
        if path == "/api/resume":
            killswitch.release()
            return self._send(200, b'{"halted": false}', "application/json")
        # /api/mode — toggle the autonomy mode (auto/shift)
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            mode = payload.get("mode", "") if isinstance(payload, dict) else ""
            m = modemod.set_mode(mode)
        except (json.JSONDecodeError, ValueError) as e:
            return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
        info = {"mode": m}
        if m == modemod.AUTO:                 # toggling AUTO actually STARTS the autopilot runner
            from ..orchestrator import autopilot
            try:
                info["autopilot"] = autopilot.start_runner()
            except Exception as e:  # noqa: BLE001 — surface the failure, don't 500 the toggle
                info["autopilot"] = {"started": False, "error": str(e)}
        return self._send(200, json.dumps(info).encode(), "application/json")


def serve(host: str = "127.0.0.1", port: int = 8788, *, open_browser: bool = True) -> int:
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"[viz] fleet mission-control on {url}  (Ctrl-C to stop)")
    if open_browser:
        import subprocess
        try:
            subprocess.run(["open", url], check=False, capture_output=True)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[viz] stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
