"""F15 (round-2 integration fix, docs/plans/2026-08-06-publication-broker-design.md):
`dashboard.fleet_server._apply_setting` enforced only `n >= 0` for int knobs, not
`harness_surface.SANE_BOUNDS` — so a board click could set
`super_worker.claim_lease_minutes=0`, making every shift start reclaim EVERY in-flight
task (including one a parallel worker is still actively building). Hermetic: Blackboard
monkeypatched to a tmp db (mirrors tests/test_fleet_queue_endpoints.py's own idiom — a
new file, that one untouched).
"""
import pytest

from factory.common.store import Blackboard
from factory.dashboard import fleet_server


def _store(tmp_path, monkeypatch):
    db = str(tmp_path / "f.db")
    monkeypatch.setattr(fleet_server, "Blackboard", lambda: Blackboard(db))
    with Blackboard(db) as s:
        s.init_db()
    return db


def test_claim_lease_minutes_zero_is_rejected(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="between 10 and 1440"):
        fleet_server._apply_setting("super_worker.claim_lease_minutes", "0")


def test_claim_lease_minutes_within_bounds_is_accepted(tmp_path, monkeypatch):
    db = _store(tmp_path, monkeypatch)
    out = fleet_server._apply_setting("super_worker.claim_lease_minutes", "240")
    assert out["value"] == "240"
    with Blackboard(db) as s:
        assert s.get_setting("super_worker.claim_lease_minutes") == "240"


def test_claim_lease_minutes_above_max_is_rejected(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="between 10 and 1440"):
        fleet_server._apply_setting("super_worker.claim_lease_minutes", "5000")


def test_max_parallel_above_bound_is_rejected(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="between 1 and 8"):
        fleet_server._apply_setting("super_worker.max_parallel", "100")


def test_max_parallel_within_bound_is_accepted(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    out = fleet_server._apply_setting("super_worker.max_parallel", "4")
    assert out["value"] == "4"


def test_a_bool_setting_is_unaffected_by_bounds_enforcement(tmp_path, monkeypatch):
    """Regression safety: bounds only apply to int knobs — bool knobs keep their existing
    true/false validation untouched."""
    _store(tmp_path, monkeypatch)
    out = fleet_server._apply_setting("super_worker.require_test", "true")
    assert out["value"] == "true"


def test_still_rejects_a_negative_int_the_same_as_before(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        fleet_server._apply_setting("super_worker.max_parallel", "-1")
