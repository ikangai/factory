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
