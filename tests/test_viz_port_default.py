"""`factory viz --serve`'s port default is DERIVED from config (dashboard.port+1), not a
second remembered constant — the multi-instance installer assigns each instance its own
dashboard.port, and the fleet port must follow it by construction (multi-instance runbook:
docs/runbooks/multi-instance-install.md, Ports)."""
from factory.orchestrator.orchestrator import cmd_viz, fleet_viz_default_port


def test_default_derives_from_dashboard_port():
    assert fleet_viz_default_port({"dashboard": {"port": 9111}}) == 9112


def test_default_falls_back_to_the_stock_8788():
    # empty/missing dashboard block -> 8787+1, the historical `--port` default
    assert fleet_viz_default_port({}) == 8788
    assert fleet_viz_default_port({"dashboard": {}}) == 8788


def test_cmd_viz_serve_resolves_a_none_port_via_the_derivation(monkeypatch):
    seen = {}

    def fake_serve(*, port, open_browser):
        seen["port"] = port
        return 0

    from factory.dashboard import fleet_server
    monkeypatch.setattr(fleet_server, "serve", fake_serve)
    import factory.orchestrator.orchestrator as orch
    monkeypatch.setattr(orch, "fleet_viz_default_port", lambda cfg=None: 9556)

    cmd_viz(None, serve=True, port=None, open_browser=False)
    assert seen["port"] == 9556

    cmd_viz(None, serve=True, port=7000, open_browser=False)  # explicit --port still wins
    assert seen["port"] == 7000
