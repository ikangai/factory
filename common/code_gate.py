"""The full-auto gate for a CODE candidate (design: docs/plans/2026-06-25-...).

With no human promotion gate, this deterministic decision IS the authority. A code
candidate auto-merges iff ALL automated gates hold:

  * the target's own tests pass        (hard correctness)
  * no frozen-safety path was touched  (the factory can't weaken safety)
  * no working-set regression          (do no harm to the scenario metric)
  * no held-out regression             (generalisation holds)
  * no Goodhart/divergence alarm
  * no safety-battery flag

The gate is DO-NO-HARM + tests-green, not strict-improve: a feature change may not move
the scenario metric yet still be a valid, tested improvement. After a merge,
`regression_after_merge` is the self-healing check — if the new champion regressed, the
loop auto-reverts the commit (git is reversible, so a slipped mistake heals itself).
"""
from __future__ import annotations


def auto_merge_eligible(*, tests_passed: bool, frozen_ok: bool, working_delta: float,
                        held_out_delta: float = 0.0, divergence_alarm: bool = False,
                        safety_flag: bool = False, regression_tol: float = 0.0) -> dict:
    """Return {eligible, failed, checks}. Eligible iff every gate holds."""
    checks = {
        "tests_passed": bool(tests_passed),
        "frozen_ok": bool(frozen_ok),
        "no_working_regression": working_delta >= -regression_tol,
        "no_held_out_regression": held_out_delta >= -regression_tol,
        "no_divergence_alarm": not divergence_alarm,
        "no_safety_flag": not safety_flag,
    }
    failed = [name for name, ok in checks.items() if not ok]
    return {"eligible": not failed, "failed": failed, "checks": checks}


def regression_after_merge(before: dict, after: dict, *, tol: float = 0.0) -> dict:
    """Did the champion regress after a merge? Compares before/after
    {working, held_out, tests_passed}. True → the loop should auto-revert."""
    why: list[str] = []
    if before.get("tests_passed", True) and not after.get("tests_passed", True):
        why.append("tests went red")
    if after.get("working", 0.0) < before.get("working", 0.0) - tol:
        why.append("working-set regression")
    if after.get("held_out", 0.0) < before.get("held_out", 0.0) - tol:
        why.append("held-out regression")
    return {"regressed": bool(why), "why": why}
