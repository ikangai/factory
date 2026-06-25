"""The code-candidate ROUND — the orchestration that wires the full-auto cores into one
flow (design: docs/plans/2026-06-25-autonomous-code-factory.md):

  kill-switch → frozen-check → tests → scenario-eval → auto-merge gate →
  merge-or-discard → re-baseline → auto-revert self-heal

Live execution (git + the target's tests + the scenario eval) is INJECTED via a fake
adapter and a `grade_fn`, so the DECISION FLOW is tested without running the target.
"""
from factory.common import killswitch
from factory.orchestrator import code_round


class FakeAdapter:
    def __init__(self, *, frozen=(), tests_passed=True):
        self._frozen = list(frozen)
        self._tests_passed = tests_passed
        self.calls = []

    def frozen_paths(self):
        return self._frozen

    def run_tests(self, repo, **k):
        self.calls.append("run_tests")
        return (self._tests_passed, "report")

    def merge_branch(self, repo, branch, **k):
        self.calls.append(("merge", branch))
        return "MERGESHA"

    def revert_commit(self, repo, sha):
        self.calls.append(("revert", sha))
        return "REVERTSHA"

    def current_commit(self, repo):
        return "HEAD"


def _grade(*values):
    it = iter(values)
    return lambda repo: next(it)


CHAMP = {"working": 0.8, "held_out": 0.7}
CLEAN_DIFF = ("diff --git a/src/clive/feature.py b/src/clive/feature.py\n"
              "--- a/src/clive/feature.py\n+++ b/src/clive/feature.py\n")


def _run(ad, grade_fn, diff=CLEAN_DIFF):
    return code_round.run_code_round(adapter=ad, repo="/r", branch="cand", diff_text=diff,
                                     champion_scores=CHAMP, grade_fn=grade_fn, label="cand")


def test_merges_when_all_gates_pass():
    ad = FakeAdapter(tests_passed=True)
    res = _run(ad, _grade({"working": 0.85, "held_out": 0.7},   # candidate (gate)
                          {"working": 0.85, "held_out": 0.7}))  # re-baseline (no regression)
    assert res["action"] == "merged" and res["merge_sha"] == "MERGESHA"
    assert ("merge", "cand") in ad.calls


def test_discards_on_frozen_violation_before_grading():
    ad = FakeAdapter(frozen=["src/clive/selfmod/"])
    bad = ("diff --git a/src/clive/selfmod/gate.py b/src/clive/selfmod/gate.py\n"
           "--- a/src/clive/selfmod/gate.py\n+++ b/src/clive/selfmod/gate.py\n")
    res = _run(ad, _grade({"working": 1.0}), diff=bad)
    assert res["action"] == "discarded" and res["stage"] == "frozen"
    assert "src/clive/selfmod/gate.py" in res["violations"]
    assert "run_tests" not in ad.calls           # frozen check short-circuits BEFORE grading


def test_discards_on_red_tests_without_scenario_eval():
    ad = FakeAdapter(tests_passed=False)
    res = _run(ad, _grade())   # grade_fn must NOT be called when tests are red
    assert res["action"] == "discarded" and "tests_passed" in res["failed"]
    assert not any(isinstance(c, tuple) and c[0] == "merge" for c in ad.calls)


def test_discards_on_scenario_regression():
    ad = FakeAdapter(tests_passed=True)
    res = _run(ad, _grade({"working": 0.5, "held_out": 0.7}))    # worse than champion
    assert res["action"] == "discarded" and "no_working_regression" in res["failed"]


def test_auto_reverts_on_post_merge_regression():
    ad = FakeAdapter(tests_passed=True)
    res = _run(ad, _grade({"working": 0.85, "held_out": 0.7},    # candidate looks good → merge
                          {"working": 0.6, "held_out": 0.7}))    # re-baseline reveals a regression
    assert res["action"] == "auto_reverted"
    assert ("revert", "MERGESHA") in ad.calls and res["revert_sha"] == "REVERTSHA"


def test_halted_kill_switch_aborts(monkeypatch):
    monkeypatch.setattr(killswitch, "is_halted", lambda: True)
    ad = FakeAdapter()
    res = _run(ad, _grade({"working": 1.0}))
    assert res["action"] == "halted"
    assert ad.calls == []   # nothing touched while halted
