"""Live fleet 'mission control' — a tiny localhost server behind `factory viz --serve`.

Serves the animated loop page (dashboard/static/fleet.html) and `/api/fleet`, the JSON
state the page polls every ~2s. Read-only, bound to localhost, opens its OWN blackboard
connection per request so the data is always fresh while a run is in flight. No write
actions at all (unlike the promotion board)."""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from ..common import mode as modemod
from ..common.store import Blackboard
from ..reporting import fleet_viz

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def fleet_state() -> dict:
    with Blackboard() as store:
        store.init_db()   # idempotent — a pre-init board shows empty, not a 500
        return fleet_viz.fleet_json(store)


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
        if path == "/api/fleet":
            try:
                body = json.dumps(fleet_state(), default=str).encode("utf-8")
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
        # The ONE write action: toggle the autonomy mode (auto/shift).
        if urlparse(self.path).path != "/api/mode":
            return self._send(404, b'{"error":"the only write action is /api/mode"}', "application/json")
        if not self._local_origin():
            return self._send(403, b'{"error":"cross-origin refused (CSRF guard)"}', "application/json")
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            m = modemod.set_mode(payload.get("mode", ""))
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
