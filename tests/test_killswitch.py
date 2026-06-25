"""Kill switch — the human's emergency brake for the full-auto factory (design:
docs/plans/2026-06-25-autonomous-code-factory.md). Dropping a STOP flag halts the
fleet immediately; removing it resumes. Deterministic, file-based, no daemon."""
from factory.common import killswitch as ks


def test_not_halted_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr("factory.common.paths.FACTORY_ROOT", str(tmp_path))
    assert not ks.is_halted()


def test_engage_halts_and_records_reason(tmp_path, monkeypatch):
    monkeypatch.setattr("factory.common.paths.FACTORY_ROOT", str(tmp_path))
    path = ks.engage("operator pulled the brake")
    assert ks.is_halted()
    with open(path) as fh:
        assert "operator pulled the brake" in fh.read()


def test_release_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr("factory.common.paths.FACTORY_ROOT", str(tmp_path))
    ks.engage()
    assert ks.is_halted()
    ks.release()
    assert not ks.is_halted()
    ks.release()  # idempotent — releasing when not halted is a no-op
