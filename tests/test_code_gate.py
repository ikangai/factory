"""The full-auto gate for a CODE candidate (design: docs/plans/2026-06-25-...).

With no human promotion gate, THIS decision is the authority: a code candidate
auto-merges iff ALL automated gates hold. And after a merge, regression_after_merge
is the self-healing check that triggers an auto-revert if the new champion regressed.
Pure + deterministic.
"""
from factory.common import code_gate as cg


def test_eligible_when_all_gates_hold():
    v = cg.auto_merge_eligible(tests_passed=True, frozen_ok=True, working_delta=0.05)
    assert v["eligible"] and v["failed"] == []


def test_do_no_harm_holds_is_eligible():
    # a feature change that doesn't move the scenario metric but doesn't regress and
    # passes tests is eligible (the gate is do-no-harm + tests-green, not strict-improve)
    v = cg.auto_merge_eligible(tests_passed=True, frozen_ok=True, working_delta=0.0)
    assert v["eligible"]


def test_red_tests_block_merge():
    v = cg.auto_merge_eligible(tests_passed=False, frozen_ok=True, working_delta=0.1)
    assert not v["eligible"] and "tests_passed" in v["failed"]


def test_frozen_touch_blocks_merge():
    v = cg.auto_merge_eligible(tests_passed=True, frozen_ok=False, working_delta=0.1)
    assert not v["eligible"] and "frozen_ok" in v["failed"]


def test_regressions_and_alarms_block_merge():
    v = cg.auto_merge_eligible(tests_passed=True, frozen_ok=True, working_delta=-0.2)
    assert "no_working_regression" in v["failed"]
    v2 = cg.auto_merge_eligible(tests_passed=True, frozen_ok=True, working_delta=0.1,
                                held_out_delta=-0.1)
    assert "no_held_out_regression" in v2["failed"]
    v3 = cg.auto_merge_eligible(tests_passed=True, frozen_ok=True, working_delta=0.1,
                                divergence_alarm=True)
    assert "no_divergence_alarm" in v3["failed"]
    v4 = cg.auto_merge_eligible(tests_passed=True, frozen_ok=True, working_delta=0.1,
                                safety_flag=True)
    assert "no_safety_flag" in v4["failed"]


def test_regression_after_merge_detects_drops_and_red_tests():
    before = {"working": 0.8, "held_out": 0.7, "tests_passed": True}
    assert not cg.regression_after_merge(before, {"working": 0.85, "held_out": 0.7,
                                                  "tests_passed": True})["regressed"]
    r = cg.regression_after_merge(before, {"working": 0.6, "held_out": 0.7,
                                           "tests_passed": True})
    assert r["regressed"] and any("working" in w for w in r["why"])
    r2 = cg.regression_after_merge(before, {"working": 0.8, "held_out": 0.7,
                                            "tests_passed": False})
    assert r2["regressed"] and any("test" in w for w in r2["why"])
