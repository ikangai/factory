"""The store must run in WAL mode by construction, not by inherited DB-header luck
(blindspot fix: crash/copy-safety of the single blackboard file)."""
from factory.common.store import Blackboard


def test_fresh_store_is_wal(tmp_path):
    bb = Blackboard(db_path=str(tmp_path / "bb.db"))
    mode = bb.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
