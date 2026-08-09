"""dashboard/auth.py + the fleet server's write gate.

The hole this closes, found while reviewing the Phase 3 design: `fleet_server.do_POST`
served nine state-changing routes on 127.0.0.1 with no authentication. Its only guard was
a CSRF check whose first branch is `if not origin: return True` — so a request that simply
sends no Origin header (every `curl`, every script) passed. Localhost is not an identity
boundary: every local account reaches it, including the workers and graders Phase 3 exists
to isolate. Any local process could approve a publication, clear the killswitch, flip a
merge gate or re-steer the mission.

These tests drive a REAL server over a REAL socket. A test that monkeypatched the handler
would not have caught the original bug, because the bug was that the handler never asked.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from factory.dashboard import auth, fleet_server


@pytest.fixture()
def token_root(tmp_path, monkeypatch):
    """Point the token at a tmp dir — never the real FACTORY_ROOT."""
    # Patch auth's own resolution only — never the shared paths.FACTORY_ROOT, which
    # every other consumer (static assets, logs, the store) also reads.
    monkeypatch.setattr(auth, "token_path",
                        lambda root=None: str(tmp_path / ".dashboard-token"))
    return str(tmp_path)


@pytest.fixture()
def server(token_root):
    """A real ThreadingHTTPServer on an ephemeral port, torn down after the test."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), fleet_server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(httpd, path, body=None, *, token=None, origin=None, query=""):
    host, port = httpd.server_address[0], httpd.server_address[1]
    req = urllib.request.Request(
        f"http://{host}:{port}{path}{query}",
        data=json.dumps(body or {}).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    if token is not None:
        req.add_header(auth.HEADER, token)
    if origin is not None:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# -- the token itself ----------------------------------------------------------------

def test_ensure_token_creates_a_0600_secret_and_is_idempotent(token_root):
    first = auth.ensure_token()
    assert first and len(first) >= 32
    mode = os.stat(auth.token_path()).st_mode & 0o777
    assert mode == 0o600, "the board key must not be readable by other local accounts"
    assert auth.ensure_token() == first, "a second call must not mint a new secret"


def test_verify_fails_closed_when_there_is_no_token_file(token_root):
    """A server that cannot read its own secret has no way to tell an operator from a
    worker, so it must refuse writes rather than allow them."""
    assert auth.verify("anything") is False
    assert auth.verify("") is False
    assert auth.verify(None) is False


def test_verify_rejects_a_wrong_token(token_root):
    auth.ensure_token()
    assert auth.verify("not-the-token") is False


def test_load_token_never_creates_one(token_root):
    assert auth.load_token() == ""
    assert not os.path.exists(auth.token_path())


# -- the write gate, over a real socket ----------------------------------------------

@pytest.mark.parametrize("path", [
    "/api/mode", "/api/stop", "/api/resume", "/api/mission", "/api/settings",
    "/api/worker", "/api/queue/answer", "/api/queue/task", "/api/queue/approval",
])
def test_every_write_route_refuses_an_unauthenticated_post(server, path):
    """The original bug, one test per route: no Origin header (so the CSRF guard passes)
    and no key. This is exactly what `curl` sends by default."""
    auth.ensure_token()
    status, body = _post(server, path, {"statement": "x"})
    assert status == 403, f"{path} accepted an unauthenticated write"
    assert b"board key" in body


def test_a_wrong_key_is_refused(server):
    auth.ensure_token()
    status, _ = _post(server, "/api/mission", {"statement": "x"}, token="wrong")
    assert status == 403


def test_the_correct_key_is_accepted_via_header(server, monkeypatch):
    """Proves the gate is a gate, not a wall — the operator's board still works."""
    token = auth.ensure_token()
    seen = {}
    monkeypatch.setattr(fleet_server, "_set_mission",
                        lambda text: seen.setdefault("mission", text) or {"ok": True})
    status, _ = _post(server, "/api/mission", {"statement": "ship it"}, token=token)
    assert status == 200
    assert seen["mission"] == "ship it"


def test_the_correct_key_is_accepted_via_query_on_first_load(server, monkeypatch):
    """`?k=` exists so the very first page load can hand the page its key."""
    token = auth.ensure_token()
    monkeypatch.setattr(fleet_server, "_set_mission", lambda text: {"ok": True})
    status, _ = _post(server, "/api/mission", {"statement": "x"},
                      query=f"?{auth.QUERY_KEY}={token}")
    assert status == 200


def test_reads_are_not_gated(server):
    """Only writes need the key. Gating reads would break every existing poll and buys
    nothing against the threat this closes (forging an approval, not observing state)."""
    host, port = server.server_address[0], server.server_address[1]
    with urllib.request.urlopen(f"http://{host}:{port}/api/plan", timeout=5) as resp:
        assert resp.status == 200


def test_an_unknown_write_route_is_still_a_404_not_a_403(server):
    """Route existence is decided before authentication, so the gate cannot be used to
    probe which endpoints exist."""
    auth.ensure_token()
    status, _ = _post(server, "/api/not-a-real-route", {})
    assert status == 404


def test_the_csrf_guard_still_runs_ahead_of_the_key_check(server):
    """The CSRF guard is not a substitute for authentication, but it is not redundant
    either — a browser on another origin must still be refused before anything else."""
    auth.ensure_token()
    status, body = _post(server, "/api/mission", {"statement": "x"},
                         origin="http://evil.example")
    assert status == 403
    assert b"cross-origin" in body


# -- the legacy board (dashboard/server.py) shares the gap and the fix -----------------

def test_legacy_promote_route_refuses_an_unauthenticated_post(tmp_path, monkeypatch):
    """`/api/promote` on the older board had the identical guard — a CSRF check that passes
    when no Origin header is sent — and promoting a champion is a real state change."""
    from factory.dashboard import server as legacy

    monkeypatch.setattr(auth, "token_path",
                        lambda root=None: str(tmp_path / ".dashboard-token"))
    auth.ensure_token()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), legacy.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, body = _post(httpd, "/api/promote", {"candidate_id": "c1"})
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == 403
    assert b"board key" in body
