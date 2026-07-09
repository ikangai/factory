"""Shift-end graduation-lag alarm (blindspot fix 2026-07-07): the 105-commit stall had
zero signal because the only reporting lived inside `_graduate_after_shift`, which runs
only on real+shipped autopilot shifts. `_warn_graduation_lag` is a PASSIVE check that
runs at shift end in EVERY mode — hermetic here via injected `lag_fn`/`file_fn`, with
`config.get_adapter`/`config.target_config` monkeypatched so a config miss can't make
these tests flaky (the real resolution only feeds the injected lag_fn's kwargs).

T6b: the alarm watches TWO edges (real-world evidence, same day: origin/<base> edge
read 0 — pushes current — while origin/main sat 105 commits behind because nothing
promotes the pushed base branch to the target's DEFAULT branch). The injected lag_fn
dispatches on the `base` kwarg so each edge's answer is scripted independently.

Hardening: each edge files under its OWN dedup ref (graduation:lag-base /
graduation:lag-publication) so an open base-edge task can't swallow the publication
edge's escalation; the outer except prints instead of silently disabling the alarm."""
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


def _collector(calls):
    """A file_fn that records (message, dedup ref) — the alarm must scope dedup per edge."""
    return lambda s, e, ref=None: calls.append((e, ref))


# -- base edge (push pipeline) ------------------------------------------------
def test_lag_alarm_prints_and_files_above_threshold(capsys, store, monkeypatch):
    _fake_config(monkeypatch)
    calls = []
    lag_fn = _dispatch({"basebr": {"ahead": 105}, "main": {"ahead": 0}})
    res = orch._warn_graduation_lag(
        store, threshold=12, lag_fn=lag_fn, file_fn=_collector(calls))
    out = capsys.readouterr().out
    assert "graduation lag: 105" in out and "factory graduate" in out
    assert len(calls) == 1 and "105" in calls[0][0]        # base edge only — publication is current
    assert calls[0][1] == "graduation:lag-base"            # edge-specific dedup ref
    assert res == {"ahead": 105, "publication": {"ahead": 0}}


def test_lag_alarm_quiet_below_threshold(capsys, store, monkeypatch):
    _fake_config(monkeypatch)
    calls = []
    lag_fn = _dispatch({"basebr": {"ahead": 3}, "main": {"ahead": 0}})
    res = orch._warn_graduation_lag(
        store, threshold=12, lag_fn=lag_fn, file_fn=_collector(calls))
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
        store, threshold=12, lag_fn=lag_fn, file_fn=_collector(calls))
    out = capsys.readouterr().out
    assert out == ""
    assert not calls
    assert res == {"ahead": None, "error": "x"}            # no publication key — base edge aborted
    assert lag_fn.seen == ["basebr"]                       # …and the second edge was never measured


def test_lag_alarm_never_raises(capsys, store, monkeypatch):
    """Never raises — but never SILENT about it either: a persistent config bug that
    swallowed the alarm forever would recreate the exact blindspot it exists to close."""
    _fake_config(monkeypatch)
    calls = []
    res = orch._warn_graduation_lag(
        store, threshold=12,
        lag_fn=lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        file_fn=_collector(calls))
    out = capsys.readouterr().out
    assert res is None
    assert "lag alarm skipped" in out and "boom" in out    # traceable, not silent
    assert not calls


# -- publication edge (default-branch promotion) — T6b ------------------------
def test_publication_lag_prints_warns_and_files_above_threshold(capsys, store, monkeypatch):
    """The 2026-07-07 real-world shape: base pushes current (0) but origin/main 105 behind."""
    _fake_config(monkeypatch, target={"base_branch": "chore"})   # release defaults to "main"
    # push_approval pinned OFF: this test is about the print/file alarm, not the Task 5.5
    # approval-proposal side effect (covered separately, gate-on, below) — pinning also
    # decouples it from config.yaml's live push_approval default (ON as of 2026-07-08).
    monkeypatch.setattr(orch.config, "load_config", lambda: {"autonomy": {"push_approval": False}})
    calls = []
    lag_fn = _dispatch({"chore": {"ahead": 0}, "main": {"ahead": 105}})
    res = orch._warn_graduation_lag(
        store, threshold=12, lag_fn=lag_fn, file_fn=_collector(calls))
    out = capsys.readouterr().out
    assert "publication lag: 105" in out and "origin/main" in out
    assert "⚠" in out and "release_branch" in out          # remedy named
    assert len(calls) == 1 and "publication lag" in calls[0][0] and "105" in calls[0][0]
    assert calls[0][1] == "graduation:lag-publication"     # its own dedup ref, not the base edge's
    assert res == {"ahead": 0, "publication": {"ahead": 105}}
    assert lag_fn.seen == ["chore", "main"]
    assert store.pending_approvals() == []                 # gate OFF — no proposal filed


def test_publication_lag_above_threshold_files_an_approval_when_gate_on(capsys, store, monkeypatch):
    """Task 5.5: the SAME above-threshold publication lag, but with autonomy.push_approval
    ON — files a pending 'publication' approval alongside the existing failure-task alarm,
    so the operator can promote base→release from the dashboard's Queue tab."""
    _fake_config(monkeypatch, target={"base_branch": "chore"})
    monkeypatch.setattr(orch.config, "load_config", lambda: {"autonomy": {"push_approval": True}})
    lag_fn = _dispatch({"chore": {"ahead": 0}, "main": {"ahead": 105}})
    orch._warn_graduation_lag(store, threshold=12, lag_fn=lag_fn, file_fn=_collector([]))
    rows = store.pending_approvals()
    assert len(rows) == 1
    assert rows[0]["kind"] == "publication" and rows[0]["status"] == "pending"
    assert rows[0]["payload"] == {"ahead": 105, "release": "main"}
    # a second alarm (lag persists) supersedes rather than piling up a second live row
    orch._warn_graduation_lag(store, threshold=12, lag_fn=lag_fn, file_fn=_collector([]))
    assert len(store.pending_approvals()) == 1
    assert len(store.pending_approvals(status="superseded")) == 1


def test_publication_lag_suppressed_when_operator_rejected_it_unchanged(capsys, store, monkeypatch):
    """Fix 3b (final whole-branch review): once the operator rejects a publication proposal,
    the next shift's alarm must NOT re-file the identical card (same ahead + release) — it
    would just nag with what the operator already declined. A changed lag proposes normally."""
    _fake_config(monkeypatch, target={"base_branch": "chore"})
    monkeypatch.setattr(orch.config, "load_config", lambda: {"autonomy": {"push_approval": True}})
    rej = store.add_pending_approval("publication", {"ahead": 105, "release": "main"})
    store.resolve_approval(rej, "rejected", note="promote manually this week")
    lag_fn = _dispatch({"chore": {"ahead": 0}, "main": {"ahead": 105}})
    orch._warn_graduation_lag(store, threshold=12, lag_fn=lag_fn, file_fn=_collector([]))
    assert store.pending_approvals() == []                  # identical → not re-proposed
    assert "publication unchanged since operator rejection" in capsys.readouterr().out
    # the lag GROWS → a genuinely new proposal is filed
    lag_fn2 = _dispatch({"chore": {"ahead": 0}, "main": {"ahead": 200}})
    orch._warn_graduation_lag(store, threshold=12, lag_fn=lag_fn2, file_fn=_collector([]))
    rows = store.pending_approvals()
    assert len(rows) == 1 and rows[0]["payload"] == {"ahead": 200, "release": "main"}


def test_publication_lag_zero_is_silent(capsys, store, monkeypatch):
    _fake_config(monkeypatch, target={"base_branch": "chore"})
    calls = []
    lag_fn = _dispatch({"chore": {"ahead": 0}, "main": {"ahead": 0}})
    res = orch._warn_graduation_lag(
        store, threshold=12, lag_fn=lag_fn, file_fn=_collector(calls))
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
        store, threshold=12, lag_fn=lag_fn, file_fn=_collector(calls))
    out = capsys.readouterr().out
    assert "graduation lag: 5" in out
    assert "publication lag" not in out
    assert not calls
    assert res == {"ahead": 5}                             # no publication key — one edge, one call
    assert lag_fn.seen == ["main"]


# -- end-to-end: the REAL failure seam, both edges, cross-edge dedup ----------
def test_both_edges_file_distinct_tasks_and_dedup_within_edge(capsys, store, monkeypatch):
    """No injected file_fn: the alarm routes through the real _maybe_file_graduation_failure
    → factory_memory.record_graduation_failure (gate forced ON). Both edges above threshold
    must file TWO distinct open tasks (edge-specific source_refs — an open base-edge task
    must not swallow the publication escalation); a second run files none (per-edge dedup)."""
    _fake_config(monkeypatch, target={"base_branch": "chore"})
    # push_approval pinned OFF: this test is scoped to the failure-task escalation seam,
    # not the Task 5.5 approval-proposal side effect (covered separately).
    monkeypatch.setattr(orch.config, "load_config",
                        lambda: {"autonomy": {"failure_tasks": True, "push_approval": False}})
    lag_fn = _dispatch({"chore": {"ahead": 100}, "main": {"ahead": 105}})
    orch._warn_graduation_lag(store, threshold=12, lag_fn=lag_fn)
    refs = sorted(t.get("source_ref") for t in store.list_tasks(status="open"))
    assert refs == ["graduation:lag-base", "graduation:lag-publication"]
    orch._warn_graduation_lag(store, threshold=12, lag_fn=lag_fn)   # same lags, next shift
    assert len(store.list_tasks(status="open")) == 2               # deduped within each edge
