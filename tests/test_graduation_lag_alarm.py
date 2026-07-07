"""Shift-end graduation-lag alarm (blindspot fix 2026-07-07): the 105-commit stall had
zero signal because the only reporting lived inside `_graduate_after_shift`, which runs
only on real+shipped autopilot shifts. `_warn_graduation_lag` is a PASSIVE check that
runs at shift end in EVERY mode — hermetic here via injected `lag_fn`/`file_fn`, with
`config.get_adapter`/`config.target_config` monkeypatched so a config miss can't make
these tests flaky (the real resolution only feeds the injected lag_fn's kwargs)."""
from factory.orchestrator import orchestrator as orch


class _Adapter:
    def entry(self):
        return ("/tmp/fake-root", "/tmp/fake-root/clive.py")


def _fake_config(monkeypatch):
    monkeypatch.setattr(orch.config, "get_adapter", lambda: _Adapter())
    monkeypatch.setattr(orch.config, "target_config", lambda: {"base_branch": "basebr"})


def test_lag_alarm_prints_and_files_above_threshold(capsys, store, monkeypatch):
    _fake_config(monkeypatch)
    calls = []
    res = orch._warn_graduation_lag(
        store, threshold=12,
        lag_fn=lambda **kw: {"ahead": 105},
        file_fn=lambda s, err: calls.append(err))
    out = capsys.readouterr().out
    assert "graduation lag: 105" in out and "factory graduate" in out
    assert calls and "105" in calls[0]
    assert res == {"ahead": 105}


def test_lag_alarm_quiet_below_threshold(capsys, store, monkeypatch):
    _fake_config(monkeypatch)
    calls = []
    res = orch._warn_graduation_lag(
        store, threshold=12,
        lag_fn=lambda **kw: {"ahead": 3},
        file_fn=lambda s, e: calls.append(e))
    out = capsys.readouterr().out
    assert "graduation lag: 3" in out
    assert "⚠" not in out
    assert not calls
    assert res == {"ahead": 3}


def test_lag_alarm_unmeasurable_is_silent(capsys, store, monkeypatch):
    _fake_config(monkeypatch)
    calls = []
    res = orch._warn_graduation_lag(
        store, threshold=12,
        lag_fn=lambda **kw: {"ahead": None, "error": "x"},
        file_fn=lambda s, e: calls.append(e))
    out = capsys.readouterr().out
    assert out == ""
    assert not calls
    assert res == {"ahead": None, "error": "x"}


def test_lag_alarm_never_raises(capsys, store, monkeypatch):
    _fake_config(monkeypatch)
    calls = []
    res = orch._warn_graduation_lag(
        store, threshold=12,
        lag_fn=lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        file_fn=lambda s, e: calls.append(e))
    out = capsys.readouterr().out
    assert res is None
    assert out == ""
    assert not calls
