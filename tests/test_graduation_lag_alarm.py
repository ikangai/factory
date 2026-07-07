"""Shift-end graduation-lag alarm (blindspot fix 2026-07-07): the 105-commit stall had
zero signal because the only reporting lived inside `_graduate_after_shift`, which runs
only on real+shipped autopilot shifts. `_warn_graduation_lag` is a PASSIVE check that
runs at shift end in EVERY mode — hermetic here via injected `lag_fn`/`file_fn`, with
`config.get_adapter`/`config.target_config` monkeypatched so a config miss can't make
these tests flaky (the real resolution only feeds the injected lag_fn's kwargs).

T6b: the alarm watches TWO edges (real-world evidence, same day: origin/<base> edge
read 0 — pushes current — while origin/main sat 105 commits behind because nothing
promotes the pushed base branch to the target's DEFAULT branch). The injected lag_fn
dispatches on the `base` kwarg so each edge's answer is scripted independently."""
from factory.orchestrator import orchestrator as orch


class _Adapter:
    def entry(self):
        return ("/tmp/fake-root", "/tmp/fake-root/clive.py")


def _fake_config(monkeypatch, target=None):
    monkeypatch.setattr(orch.config, "get_adapter", lambda: _Adapter())
    monkeypatch.setattr(orch.config, "target_config",
                        lambda: target if target is not None else {"base_branch": "basebr"})


def _dispatch(table):
    """A lag_fn scripted per edge: answers by the `base` kwarg, records the call order."""
    seen = []

    def lag_fn(**kw):
        seen.append(kw["base"])
        return table[kw["base"]]
    lag_fn.seen = seen
    return lag_fn


# -- base edge (push pipeline) ------------------------------------------------
def test_lag_alarm_prints_and_files_above_threshold(capsys, store, monkeypatch):
    _fake_config(monkeypatch)
    calls = []
    lag_fn = _dispatch({"basebr": {"ahead": 105}, "main": {"ahead": 0}})
    res = orch._warn_graduation_lag(
        store, threshold=12, lag_fn=lag_fn,
        file_fn=lambda s, err: calls.append(err))
    out = capsys.readouterr().out
    assert "graduation lag: 105" in out and "factory graduate" in out
    assert len(calls) == 1 and "105" in calls[0]           # base edge only — publication is current
    assert res == {"ahead": 105, "publication": {"ahead": 0}}


def test_lag_alarm_quiet_below_threshold(capsys, store, monkeypatch):
    _fake_config(monkeypatch)
    calls = []
    lag_fn = _dispatch({"basebr": {"ahead": 3}, "main": {"ahead": 0}})
    res = orch._warn_graduation_lag(
        store, threshold=12, lag_fn=lag_fn,
        file_fn=lambda s, e: calls.append(e))
    out = capsys.readouterr().out
    assert "graduation lag: 3" in out
    assert "⚠" not in out
    assert not calls
    assert res == {"ahead": 3, "publication": {"ahead": 0}}


def test_lag_alarm_unmeasurable_is_silent(capsys, store, monkeypatch):
    _fake_config(monkeypatch)
    calls = []
    lag_fn = _dispatch({"basebr": {"ahead": None, "error": "x"}})
    res = orch._warn_graduation_lag(
        store, threshold=12, lag_fn=lag_fn,
        file_fn=lambda s, e: calls.append(e))
    out = capsys.readouterr().out
    assert out == ""
    assert not calls
    assert res == {"ahead": None, "error": "x"}            # no publication key — base edge aborted
    assert lag_fn.seen == ["basebr"]                       # …and the second edge was never measured


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


# -- publication edge (default-branch promotion) — T6b ------------------------
def test_publication_lag_prints_warns_and_files_above_threshold(capsys, store, monkeypatch):
    """The 2026-07-07 real-world shape: base pushes current (0) but origin/main 105 behind."""
    _fake_config(monkeypatch, target={"base_branch": "chore"})   # release defaults to "main"
    calls = []
    lag_fn = _dispatch({"chore": {"ahead": 0}, "main": {"ahead": 105}})
    res = orch._warn_graduation_lag(
        store, threshold=12, lag_fn=lag_fn,
        file_fn=lambda s, e: calls.append(e))
    out = capsys.readouterr().out
    assert "publication lag: 105" in out and "origin/main" in out
    assert "⚠" in out and "release_branch" in out          # remedy named
    assert len(calls) == 1 and "publication lag" in calls[0] and "105" in calls[0]
    assert res == {"ahead": 0, "publication": {"ahead": 105}}
    assert lag_fn.seen == ["chore", "main"]


def test_publication_lag_zero_is_silent(capsys, store, monkeypatch):
    _fake_config(monkeypatch, target={"base_branch": "chore"})
    calls = []
    lag_fn = _dispatch({"chore": {"ahead": 0}, "main": {"ahead": 0}})
    res = orch._warn_graduation_lag(
        store, threshold=12, lag_fn=lag_fn,
        file_fn=lambda s, e: calls.append(e))
    out = capsys.readouterr().out
    assert "graduation lag: 0" in out                      # base edge still reports
    assert "publication lag" not in out                    # a current default branch is not news
    assert not calls
    assert res == {"ahead": 0, "publication": {"ahead": 0}}


def test_publication_edge_skipped_when_release_equals_base(capsys, store, monkeypatch):
    _fake_config(monkeypatch, target={"base_branch": "main", "release_branch": "main"})
    calls = []
    lag_fn = _dispatch({"main": {"ahead": 5}})
    res = orch._warn_graduation_lag(
        store, threshold=12, lag_fn=lag_fn,
        file_fn=lambda s, e: calls.append(e))
    out = capsys.readouterr().out
    assert "graduation lag: 5" in out
    assert "publication lag" not in out
    assert not calls
    assert res == {"ahead": 5}                             # no publication key — one edge, one call
    assert lag_fn.seen == ["main"]
