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
from ..reporting import fleet_viz

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

MAX_MISSION_CHARS = 2000


def fleet_state() -> dict:
    with Blackboard() as store:
        store.init_db()   # idempotent — a pre-init board shows empty, not a 500
        return fleet_viz.fleet_json(store)


def plan_state() -> list:
    with Blackboard() as store:
        store.init_db()
        return fleet_viz.plan_list(store)


def timesheets_state() -> dict:
    from ..reporting import timesheets
    with Blackboard() as store:
        store.init_db()
        return {"rows": timesheets.timesheet(store), "by_agent": timesheets.by_agent(store)}


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
               "/api/timesheets": timesheets_state}
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
        if path not in ("/api/mode", "/api/stop", "/api/resume", "/api/mission"):
            return self._send(404, b'{"error":"unknown write action"}', "application/json")
        if not self._local_origin():
            return self._send(403, b'{"error":"cross-origin refused (CSRF guard)"}', "application/json")
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
