"""Pytest config for the standalone factory: put the factory's parent dir on
sys.path so `import factory.<sub>` resolves (mirrors how `python -m factory.*` runs)."""
import os
import sys

_here = os.path.dirname(__file__)
_factory_parent = os.path.abspath(os.path.join(_here, "..", ".."))
if _factory_parent not in sys.path:
    sys.path.insert(0, _factory_parent)

import pytest

from factory.common.store import Blackboard


@pytest.fixture(autouse=True)
def _hermetic_killswitch(tmp_path_factory, monkeypatch):
    """Isolate every test from the REAL kill switch.

    `killswitch.is_halted()` reads `FACTORY_ROOT/STOP` (a hardcoded path), so an
    operator brake engaged for a live shift would leak into the suite — halting the
    develop/code-round/acceptance tests that don't monkeypatch it and depend on STOP
    being absent. Point `stop_flag_path` at a fresh per-test tmp file: `is_halted`
    defaults False, `engage`/`release` still work (test_killswitch routes through the
    same path), and tests that exercise STOP re-monkeypatch `is_halted` and win. The
    real STOP file is never touched, so a live brake stays engaged during test runs."""
    from factory.common import killswitch
    stop = tmp_path_factory.mktemp("ks") / "STOP"
    monkeypatch.setattr(killswitch, "stop_flag_path", lambda: str(stop))


@pytest.fixture
def store(tmp_path):
    """A suite-wide isolated, schema-initialized blackboard on a temp-dir DB.

    Minimal on purpose (just init_db) so any test can request `store`; modules
    that need extra seeding (e.g. a champion candidate) define their own local
    `store` fixture, which shadows this one."""
    board = Blackboard(db_path=str(tmp_path / "bb.db"))
    board.init_db()
    try:
        yield board
    finally:
        board.close()
