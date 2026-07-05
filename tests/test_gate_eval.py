"""Golden-case eval loop for the LLM gates (`factory eval-gates` — roadmap Task 2.1,
P12: eval the improvement layer itself). Hermetic — tmp store, injected/monkeypatched
judge; the suite NEVER hits a live LLM (the real run is an operator act)."""
import json

import pytest

from factory.common.store import Blackboard
from factory.reporting import gate_eval, scope_check


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


def _fixtures(tmp_path, cases):
    p = tmp_path / "goldens.jsonl"
    p.write_text("\n".join(json.dumps(c) for c in cases) + "\n", encoding="utf-8")
    return str(p)


# One realistic expected-pass golden + one adversarial (expected as a SET — LLM
# nondeterminism means split vs reject on an over-bundled brief are both correct).
_TWO = [
    {"id": "g-pass", "title": "Fall back to trim on an empty compressor summary",
     "detail": "one bounded change in context_compress.py", "expected": ["pass"]},
    {"id": "g-adv", "title": "Add retries, parallelize startup, and surface a metric",
     "detail": "three independent changes on three surfaces",
     "expected": ["split", "reject"]},
]


# -- the shipped goldens (scenarios/gates/scope.jsonl) ------------------------
def test_shipped_scope_goldens_are_valid_and_probing():
    """The hand-authored fixture file is well-formed, capped, majority expected-pass,
    and probes BOTH failure hypotheses: over-bundled briefs (split/reject) and clive's
    frozen surface (execution/ + runtime.py → reject)."""
    cases = gate_eval.load_fixtures()               # the real scenarios/gates/scope.jsonl
    assert 10 <= len(cases) <= gate_eval.MAX_CASES
    ids = [c["id"] for c in cases]
    assert len(set(ids)) == len(ids)                # unique ids (flip detection keys on them)
    for c in cases:
        assert c["title"].strip() and (c.get("detail") or "").strip()
        assert c["expected"] and set(c["expected"]) <= set(scope_check.DECISIONS)
    adversarial = [c for c in cases if "pass" not in c["expected"]]
    passing = [c for c in cases if c["expected"] == ["pass"]]
    assert 3 <= len(adversarial) <= 5               # the never-rejects hypothesis is probed
    assert len(passing) > len(adversarial)          # majority realistic expected-pass
    frozen = [c for c in adversarial
              if "runtime.py" in (c["title"] + c.get("detail", ""))
              or "execution/" in (c["title"] + c.get("detail", ""))]
    assert any("reject" in c["expected"] for c in frozen)


def test_load_fixtures_caps_the_count(tmp_path):
    """~20-case spend ceiling: a runaway fixture file can't turn one eval run into an
    unbounded LLM bill."""
    many = [{"id": f"c{i}", "title": f"t{i}", "detail": "d", "expected": ["pass"]}
            for i in range(30)]
    cases = gate_eval.load_fixtures(_fixtures(tmp_path, many))
    assert len(cases) == gate_eval.MAX_CASES


def test_load_fixtures_refuses_malformed_cases(tmp_path):
    """A broken golden file fails LOUDLY — silently shrinking the eval would fake green."""
    bad = [{"id": "a", "title": "t", "expected": ["weird-verdict"]}]
    with pytest.raises(ValueError):
        gate_eval.load_fixtures(_fixtures(tmp_path, bad))
    with pytest.raises(ValueError):
        gate_eval.load_fixtures(_fixtures(tmp_path, [{"id": "a", "expected": ["pass"]}]))


# -- the runner ---------------------------------------------------------------
def test_run_scorecard_expected_sets_absorb_nondeterminism(tmp_path, capsys):
    verdicts = {"g-pass": {"decision": "pass"},
                "g-adv": {"decision": "reject", "reason": "bundles three changes"}}
    with _store(tmp_path) as s:
        rep = gate_eval.run_gate_eval(s, judge=lambda t: verdicts[t["id"]],
                                      path=_fixtures(tmp_path, _TWO))
    assert rep["total"] == 2 and rep["ok"] == 2 and rep["failed"] == 0
    assert rep["ok_all"] is True
    assert "2/2" in capsys.readouterr().out         # aggregate scorecard printed


def test_run_flags_pass_bias_when_adversarial_cases_sail_through(tmp_path, capsys):
    """THE hypothesis these goldens probe: a judge that waves everything through as
    'pass' (it has never rejected/split in production) must show up as pass-bias."""
    with _store(tmp_path) as s:
        rep = gate_eval.run_gate_eval(s, judge=lambda t: {"decision": "pass"},
                                      path=_fixtures(tmp_path, _TWO))
    assert rep["failed"] == 1 and rep["pass_bias"] == 1 and rep["ok_all"] is False
    assert "miscalibrated toward pass" in capsys.readouterr().out


def test_run_normalizes_garbage_like_production(tmp_path):
    """A crashing/garbled judge normalizes to 'pass' via normalize_verdict — exactly
    prefilter's fail-open — so an adversarial golden FAILS instead of erroring out."""
    def judge(t):
        raise RuntimeError("llm down")

    with _store(tmp_path) as s:
        rep = gate_eval.run_gate_eval(s, judge=judge, path=_fixtures(tmp_path, _TWO))
    by_id = {c["id"]: c for c in rep["cases"]}
    assert by_id["g-pass"]["ok"] and by_id["g-pass"]["verdict"] == "pass"
    assert not by_id["g-adv"]["ok"]                 # fail-open never fakes an adversarial win


def test_default_judge_is_the_live_scope_judge(tmp_path, monkeypatch):
    """No injected judge → the LIVE production scope_judge seam runs (monkeypatched
    here — the point of the eval is to grade the real prompt+judge, not a stub)."""
    seen = []
    monkeypatch.setattr(scope_check, "scope_judge",
                        lambda task, **k: seen.append(task["id"]) or {"decision": "pass"})
    with _store(tmp_path) as s:
        gate_eval.run_gate_eval(s, path=_fixtures(tmp_path, _TWO))
    assert seen == ["g-pass", "g-adv"]


# -- spend: ledgered, brake-visible --------------------------------------------
def test_run_ledgers_spend_under_gate_eval_with_shift(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        judge = lambda t: {"decision": "pass", "_spend": {"tokens": 100, "cost": 0.01}}
        gate_eval.run_gate_eval(s, judge=judge, shift_id=sh, path=_fixtures(tmp_path, _TWO))
        rows = [e for e in s.budget_entries() if e["role_or_run"] == "gate_eval"]
        assert len(rows) == 2 and all(r["shift_id"] == sh for r in rows)
        assert s.shift_spend(sh)["tokens"] == 200   # folds into the loop token brake


def test_run_ledgers_standalone_without_a_shift(tmp_path):
    """Standalone CLI run (no shift context): spend still ledgers, shift_id NULL —
    add_budget's shift_id is Optional, so nothing is dropped. A stub judge without a
    _spend key ledgers nothing (mirrors _ledger_judge_spend)."""
    def judge(t):
        if t["id"] == "g-pass":
            return {"decision": "pass", "_spend": {"tokens": 50, "cost": 0.0}}
        return {"decision": "reject"}               # no _spend → no row

    with _store(tmp_path) as s:
        gate_eval.run_gate_eval(s, judge=judge, path=_fixtures(tmp_path, _TWO))
        rows = [e for e in s.budget_entries() if e["role_or_run"] == "gate_eval"]
        assert len(rows) == 1 and rows[0]["shift_id"] is None


def test_run_refuses_under_stop_before_any_judge_call(tmp_path, monkeypatch, capsys):
    """STOP vetoes even 'read-only' eval spend: zero judge calls, zero ledger rows."""
    from factory.common import killswitch
    monkeypatch.setattr(killswitch, "is_halted", lambda: True)
    calls = []
    with _store(tmp_path) as s:
        rep = gate_eval.run_gate_eval(
            s, judge=lambda t: calls.append(t) or {"decision": "pass"},
            path=_fixtures(tmp_path, _TWO))
        assert rep["halted"] is True and rep["ok_all"] is False and rep["cases"] == []
        assert calls == []
        assert [e for e in s.budget_entries() if e["role_or_run"] == "gate_eval"] == []
    assert "STOP" in capsys.readouterr().out


# -- persistence + flip detection ----------------------------------------------
def test_per_case_outcomes_persist_latest_per_case(tmp_path):
    with _store(tmp_path) as s:
        fx = _fixtures(tmp_path, _TWO)
        gate_eval.run_gate_eval(s, judge=lambda t: {"decision": "pass"}, path=fx)
        gate_eval.run_gate_eval(s, judge=lambda t: {"decision": "reject", "reason": "r"},
                                path=fx)
        latest = {r["case_id"]: r for r in s.latest_gate_eval_results("scope")}
        assert set(latest) == {"g-pass", "g-adv"}
        assert latest["g-pass"]["ok"] == 0          # run 2: reject vs expected {pass}
        assert latest["g-adv"]["ok"] == 1 and latest["g-adv"]["verdict"] == "reject"


def test_flip_ok_to_fail_records_a_factory_learning(tmp_path):
    """A golden that was ok last run and fails now = a REGRESSION in the gate itself
    (e.g. a prompt edit) — recorded as a factory learning. fail→fail is not a flip."""
    good = {"g-pass": {"decision": "pass"},
            "g-adv": {"decision": "split", "subtasks": [{"title": "a"}]}}
    with _store(tmp_path) as s:
        fx = _fixtures(tmp_path, _TWO)
        rep1 = gate_eval.run_gate_eval(s, judge=lambda t: good[t["id"]], path=fx)
        assert rep1["ok_all"] is True and rep1["flips"] == []
        assert not any(r["scope"] == "gate_eval" for r in s.learnings_for_role("factory"))
        rep2 = gate_eval.run_gate_eval(s, judge=lambda t: {"decision": "pass"}, path=fx)
        assert rep2["flips"] == ["g-adv"]
        rows = [r for r in s.learnings_for_role("factory") if r["scope"] == "gate_eval"]
        assert len(rows) == 1 and "g-adv" in rows[0]["content"]
        rep3 = gate_eval.run_gate_eval(s, judge=lambda t: {"decision": "pass"}, path=fx)
        assert rep3["flips"] == []                  # still failing ≠ a new flip


# -- CLI wiring -----------------------------------------------------------------
def test_factory_eval_gates_cli_routes_and_gates(tmp_path, monkeypatch, capsys):
    """`factory eval-gates` routes to the runner and is a GATE: exit 1 on any failing
    golden (or STOP), 0 when all goldens hold."""
    from factory.orchestrator import orchestrator
    from factory.reporting import gate_eval as ge
    monkeypatch.setattr(orchestrator, "Blackboard",
                        lambda *a, **k: Blackboard(str(tmp_path / "cli.db")))
    calls = {}

    def fake_run(store, **kw):
        calls.update(kw)
        return {"gate": "scope", "halted": False, "ok_all": True, "total": 2, "ok": 2,
                "failed": 0, "cases": [], "flips": [], "pass_bias": 0}

    monkeypatch.setattr(ge, "run_gate_eval", fake_run)
    assert orchestrator.main(["eval-gates"]) == 0
    assert calls["gate"] == "scope" and "shift_id" in calls
    monkeypatch.setattr(ge, "run_gate_eval",
                        lambda store, **kw: {"ok_all": False, "halted": False})
    assert orchestrator.main(["eval-gates"]) == 1
    capsys.readouterr()


def test_cmd_eval_gates_attributes_the_running_shift(tmp_path, monkeypatch):
    """Invoked inside a shift context, the CLI hands the RUNNING shift's id to the
    runner so the spend folds into that shift's brake accounting."""
    from factory.orchestrator import orchestrator
    from factory.reporting import gate_eval as ge
    calls = {}
    monkeypatch.setattr(ge, "run_gate_eval",
                        lambda store, **kw: calls.update(kw) or {"ok_all": True})
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=100)
        orchestrator.cmd_eval_gates(s)
    assert calls["shift_id"] == sh
