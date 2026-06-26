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
