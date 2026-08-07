"""F3 (round-2 integration fix, docs/plans/2026-08-06-publication-broker-design.md):
`publication_broker: true` + `push_approval: false` was a silent bypass — the broker's
whole envelope/pin mechanism only ever runs from `reporting.approvals.execute_approval`,
which only exists because `push_approval` filed an approval row in the first place. With
the gate off, `_graduate_after_shift`/`cmd_graduate` push for REAL, unconditionally,
regardless of whether the broker is armed. Both must now refuse the contradictory
combination loudly instead of silently pushing directly. Hermetic: config monkeypatched,
graduate_fn injected, no real git/gh/network.
"""
import types

from factory.common.store import Blackboard
from factory.orchestrator import orchestrator as orch
from factory.reporting import issue_sync


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


# -- _broker_armed_without_approval_gate: the pure predicate --------------------------------
def test_predicate_true_only_for_armed_plus_gate_off():
    assert orch._broker_armed_without_approval_gate(
        {"publication_broker": True, "push_approval": False}) is True


def test_predicate_false_when_not_armed():
    assert orch._broker_armed_without_approval_gate(
        {"publication_broker": False, "push_approval": False}) is False


def test_predicate_false_when_gate_on():
    assert orch._broker_armed_without_approval_gate(
        {"publication_broker": True, "push_approval": True}) is False


def test_predicate_false_by_default_empty_config():
    # publication_broker defaults False, push_approval defaults True — never the
    # forbidden combination on an unconfigured autonomy block.
    assert orch._broker_armed_without_approval_gate({}) is False


# -- _graduate_after_shift ------------------------------------------------------------------
def _gate(monkeypatch, *, publication_broker: bool, push_approval: bool, failure_tasks=False):
    monkeypatch.setattr(orch.config, "load_config",
                        lambda: {"autonomy": {"push_approval": push_approval,
                                              "publication_broker": publication_broker,
                                              "failure_tasks": failure_tasks}})


def test_graduate_after_shift_refuses_the_forbidden_combination(tmp_path, monkeypatch):
    with _store(tmp_path) as s:
        _gate(monkeypatch, publication_broker=True, push_approval=False)
        calls = []

        def graduate_fn(**kw):
            calls.append(kw)
            return {"action": "synced", "n_commits": 1}

        res = orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=graduate_fn,
                                         repo="o/r", root="/x", base="base")
        assert res == {"action": "skip", "reason": "broker-armed-push-approval-off"}
        assert calls == []                          # the real push NEVER ran


def test_graduate_after_shift_refuses_before_touching_the_repo_lock(tmp_path, monkeypatch):
    """The refusal fires BEFORE the push lock / graduate_fn — no partial side effect."""
    with _store(tmp_path) as s:
        _gate(monkeypatch, publication_broker=True, push_approval=False)

        def boom(**kw):
            raise AssertionError("graduate_fn must never be called")

        res = orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=boom,
                                         repo="o/r", root="/x", base="base")
        assert res["action"] == "skip"


def test_graduate_after_shift_unarmed_gate_off_still_pushes_for_real(tmp_path, monkeypatch):
    """Regression safety: publication_broker OFF (the default) must be COMPLETELY
    unaffected — gate-off still pushes for real exactly as before this fix."""
    with _store(tmp_path) as s:
        _gate(monkeypatch, publication_broker=False, push_approval=False)
        calls = []

        def graduate_fn(**kw):
            calls.append(kw)
            return {"action": "synced", "n_commits": 1}

        res = orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=graduate_fn,
                                         repo="o/r", root="/x", base="base")
        assert res["action"] == "synced"
        assert len(calls) == 1


def test_graduate_after_shift_armed_with_gate_on_proposes_normally(tmp_path, monkeypatch):
    """The LEGITIMATE armed configuration (both true) must be unaffected."""
    with _store(tmp_path) as s:
        _gate(monkeypatch, publication_broker=True, push_approval=True)

        def graduate_fn(**kw):
            return {"action": "dry_run", "range": "a..b", "n_commits": 2,
                   "base_sha": "b0", "tip_sha": "t0", "synced": []}

        res = orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=graduate_fn,
                                         repo="o/r", root="/x", base="base")
        assert res["action"] == "proposed"


def test_graduate_after_shift_refusal_escalates_when_failure_tasks_on(tmp_path, monkeypatch, capsys):
    with _store(tmp_path) as s:
        _gate(monkeypatch, publication_broker=True, push_approval=False, failure_tasks=True)
        orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=lambda **kw: {},
                                   repo="o/r", root="/x", base="base")
        tasks = s.list_tasks(status="open")
        assert len(tasks) == 1
        assert "push_approval" in tasks[0]["title"] or "push_approval" in tasks[0]["detail"]
        assert "refusing to push directly" in capsys.readouterr().out


# -- cmd_graduate -----------------------------------------------------------------------------
class _CmdAd:
    def entry(self):
        return ("/troot", "/troot/clive.py")

    def run_tests(self, cwd, **k):
        return (True, "ok")


def _cmd_gate(monkeypatch, *, publication_broker: bool, push_approval: bool):
    monkeypatch.setattr(orch.config, "target_repo_slug", lambda: "o/r")
    monkeypatch.setattr(orch.config, "target_config", lambda: {"base_branch": "basebr"})
    monkeypatch.setattr(orch.config, "get_adapter", lambda: _CmdAd())
    monkeypatch.setattr(orch.config, "load_config",
                        lambda: {"autonomy": {"push_approval": push_approval,
                                              "publication_broker": publication_broker}})


def test_cmd_graduate_refuses_the_forbidden_combination(tmp_path, monkeypatch, capsys):
    with _store(tmp_path) as s:
        _cmd_gate(monkeypatch, publication_broker=True, push_approval=False)

        def boom(**kw):
            raise AssertionError("graduate_and_push must never be called")

        monkeypatch.setattr(issue_sync, "graduate_and_push", boom)
        res = orch.cmd_graduate(s)                       # no --dry-run
        assert res == {"action": "skip", "reason": "broker-armed-push-approval-off"}
        assert "refusing to push directly" in capsys.readouterr().out


def test_cmd_graduate_explicit_dry_run_still_previews_despite_the_contradiction(tmp_path, monkeypatch):
    """--dry-run mutates nothing, so it stays a legitimate diagnostic even when the
    config itself is in the forbidden combination — the operator needs to be ABLE to
    preview in order to notice/fix the misconfiguration."""
    with _store(tmp_path) as s:
        _cmd_gate(monkeypatch, publication_broker=True, push_approval=False)
        calls = []

        def fake_grad(**kw):
            calls.append(kw)
            return {"action": "dry_run", "range": "a..b", "n_commits": 2, "synced": []}

        monkeypatch.setattr(issue_sync, "graduate_and_push", fake_grad)
        res = orch.cmd_graduate(s, dry_run=True)
        assert res["action"] == "dry_run"
        assert len(calls) == 1 and calls[0]["dry_run"] is True


def test_cmd_graduate_unarmed_gate_off_still_pushes_for_real(tmp_path, monkeypatch):
    with _store(tmp_path) as s:
        _cmd_gate(monkeypatch, publication_broker=False, push_approval=False)
        calls = []

        def fake_grad(**kw):
            calls.append(kw)
            return {"action": "synced", "n_commits": 2, "range": "a..b", "synced": []}

        monkeypatch.setattr(issue_sync, "graduate_and_push", fake_grad)
        res = orch.cmd_graduate(s)                       # no --dry-run
        assert res["action"] == "synced"
        assert len(calls) == 1 and calls[0]["dry_run"] is False
