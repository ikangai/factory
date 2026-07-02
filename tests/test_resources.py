"""Resources view (reporting/resources.py) + the board's runtime-knob / workforce write endpoints
(Task 6.2). Hermetic — the gather is a pure read; the endpoints are driven on a stub server."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from factory.reporting import resources


def test_resources_gathers_roles_profiles_caps(store):
    store.add_profile("python-dev", description="py", model="standard", overlay="x")
    r = resources.resources(store)
    roles = {x["name"]: x for x in r["roles"]}
    assert roles["conductor"]["transport"] == "super" and roles["conductor"]["wired"] is True
    assert roles["developer"]["model_tier"] == "per-profile"
    assert roles["scope_check"]["transport"] == "isolated" and roles["scope_check"]["wired"] is True
    assert "python-dev" in {p["name"] for p in r["profiles"]}
    # caps carry value + overridden flag; require_test comes from config.yaml (not overridden)
    assert r["caps"]["require_test"]["overridden"] is False
    assert "developer" not in r["legacy"]                # a live role is not also legacy


def test_resources_caps_reflect_a_store_override(store):
    store.set_setting("super_worker.max_parallel", "1")
    caps = resources.resources(store)["caps"]
    assert caps["max_parallel"] == {"value": 1, "source": "override", "overridden": True}


def _serve(monkeypatch=None):
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


def test_post_settings_whitelist_and_type_validation(monkeypatch, tmp_path):
    from factory.dashboard import fleet_server
    from factory.common.store import Blackboard
    monkeypatch.setattr(fleet_server, "Blackboard", lambda: Blackboard(str(tmp_path / "bb.db")))
    httpd, port = _serve()
    try:
        code, info = _post(port, "/api/settings", {"key": "super_worker.max_parallel", "value": "2"})
        assert code == 200 and info["applied_at"] == "next shift"
        with Blackboard(str(tmp_path / "bb.db")) as s:
            assert s.get_setting("super_worker.max_parallel") == "2"
        # a non-whitelisted key → 400
        assert _post(port, "/api/settings", {"key": "super_worker.user", "value": "root"})[0] == 400
        # a bad int → 400
        assert _post(port, "/api/settings", {"key": "super_worker.max_parallel", "value": "-1"})[0] == 400
        # a bad bool → 400
        assert _post(port, "/api/settings", {"key": "super_worker.require_test", "value": "maybe"})[0] == 400
    finally:
        httpd.shutdown()


def test_post_worker_add_retire_shares_cli_guardrails(monkeypatch, tmp_path):
    from factory.dashboard import fleet_server
    from factory.common.store import Blackboard
    monkeypatch.setattr(fleet_server, "Blackboard", lambda: Blackboard(str(tmp_path / "bb.db")))
    httpd, port = _serve()
    try:
        code, info = _post(port, "/api/worker", {"action": "add", "name": "ml-expert",
                                                 "description": "ml", "model": "frontier"})
        assert code == 200 and info["name"] == "ml-expert"
        # bad tier → 400 (same worker_admin guard as the CLI)
        assert _post(port, "/api/worker", {"action": "add", "name": "x", "model": "turbo"})[0] == 400
        # generalist unretireable → 400
        assert _post(port, "/api/worker", {"action": "retire", "name": "generalist"})[0] == 400
        code, _ = _post(port, "/api/worker", {"action": "retire", "name": "ml-expert"})
        assert code == 200
        with Blackboard(str(tmp_path / "bb.db")) as s:
            assert s.get_profile("ml-expert")["active"] == 0
    finally:
        httpd.shutdown()
