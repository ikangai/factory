"""The full-auto gate for a CODE candidate (design: docs/plans/2026-06-25-...).

With no human promotion gate, THIS decision is the authority: a code candidate
auto-merges iff ALL automated gates hold — and the gate is FAIL-CLOSED (review
2026-06-25): held-out must be measured (no vacuous pass) and the divergence/safety
signals default to unsafe so an omitted computation blocks rather than admits.
regression_after_merge is the self-healing check behind an auto-revert. Pure.
"""
from factory.common import code_gate as cg

# the fail-closed signals a real, fully-graded candidate must supply to be eligible
OK = dict(held_out_measured=True, divergence_alarm=False, safety_flag=False)


def test_eligible_when_all_gates_hold():
    v = cg.auto_merge_eligible(tests_passed=True, frozen_ok=True, working_delta=0.05, **OK)
    assert v["eligible"] and v["failed"] == []


def test_do_no_harm_holds_is_eligible():
    v = cg.auto_merge_eligible(tests_passed=True, frozen_ok=True, working_delta=0.0, **OK)
    assert v["eligible"]


def test_red_tests_block_merge():
    v = cg.auto_merge_eligible(tests_passed=False, frozen_ok=True, working_delta=0.1, **OK)
    assert not v["eligible"] and "tests_passed" in v["failed"]


def test_frozen_touch_blocks_merge():
    v = cg.auto_merge_eligible(tests_passed=True, frozen_ok=False, working_delta=0.1, **OK)
    assert not v["eligible"] and "frozen_ok" in v["failed"]


def test_working_regression_blocks():
    v = cg.auto_merge_eligible(tests_passed=True, frozen_ok=True, working_delta=-0.2, **OK)
    assert "no_working_regression" in v["failed"]


def test_held_out_regression_blocks():
    v = cg.auto_merge_eligible(tests_passed=True, frozen_ok=True, working_delta=0.1,
                               held_out_delta=-0.1, held_out_measured=True,
                               divergence_alarm=False, safety_flag=False)
    assert "no_held_out_regression" in v["failed"]


def test_unmeasured_held_out_fails_closed():
    # no held-out sample → BLOCKED (not a vacuous pass, the old promotion-gate bug)
    v = cg.auto_merge_eligible(tests_passed=True, frozen_ok=True, working_delta=0.1,
                               divergence_alarm=False, safety_flag=False)  # held_out_measured default False
    assert not v["eligible"] and "held_out_measured" in v["failed"]


def test_omitted_safety_signals_fail_closed():
    # omit divergence_alarm/safety_flag → they default UNSAFE → BLOCKED
    v = cg.auto_merge_eligible(tests_passed=True, frozen_ok=True, working_delta=0.1,
                               held_out_measured=True)
    assert not v["eligible"]
    assert "no_divergence_alarm" in v["failed"] and "no_safety_flag" in v["failed"]


def test_alarm_and_safety_flag_each_block():
    v = cg.auto_merge_eligible(tests_passed=True, frozen_ok=True, working_delta=0.1,
                               held_out_measured=True, divergence_alarm=True, safety_flag=False)
    assert "no_divergence_alarm" in v["failed"]
    v2 = cg.auto_merge_eligible(tests_passed=True, frozen_ok=True, working_delta=0.1,
                                held_out_measured=True, divergence_alarm=False, safety_flag=True)
    assert "no_safety_flag" in v2["failed"]


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
