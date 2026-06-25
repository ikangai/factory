"""Code-candidate round (design: docs/plans/2026-06-25-autonomous-code-factory.md).

The full-auto orchestration: grade ONE code candidate (a `branch` a developer
super-worker produced in its clone, already fetched into `repo`) and AUTO-MERGE it, or
discard it, with auto-revert self-heal. There is NO human gate — the automated checks
ARE the authority, and every action is a revertible git commit.

The flow, short-circuiting cheap/structural gates first:

  kill-switch → frozen-check (structural) → target tests (hard) → scenario eval →
  auto-merge gate → merge → re-baseline → regression? → auto-revert : keep

All live execution is injected so the decision flow is testable without running the
target: `adapter` does git + tests (TargetAdapter.{frozen_paths,run_tests,merge_branch,
revert_commit}); `grade_fn(repo) -> {working, held_out, divergence_alarm?, safety_flag?}`
is the scenario eval of the target as it stands in `repo`.
"""
from __future__ import annotations

from typing import Callable

from ..common import code_gate, frozen_source, killswitch


def run_code_round(*, adapter, repo: str, branch: str, diff_text: str,
                   champion_scores: dict, grade_fn: Callable[[str], dict],
                   label: str = "candidate", regression_tol: float = 0.0) -> dict:
    """Grade + auto-merge / discard one code candidate. Returns a result dict whose
    `action` is one of: halted | discarded | merged | auto_reverted. The caller records
    it to the store + diary; this function makes only the git/test/merge calls."""
    if killswitch.is_halted():
        return {"action": "halted"}

    # 1. frozen-safety — structural, BEFORE any expensive grading.
    changed = frozen_source.changed_paths_from_diff(diff_text)
    frozen_ok, violations = frozen_source.validate_code_candidate(
        changed_paths=changed, frozen_patterns=adapter.frozen_paths())
    if not frozen_ok:
        return {"action": "discarded", "stage": "frozen", "violations": violations}

    # 2. the target's own tests — the hard correctness gate. Skip the (expensive)
    #    scenario eval if they're red.
    tests_passed, report = adapter.run_tests(repo)
    if not tests_passed:
        return {"action": "discarded", "stage": "tests", "failed": ["tests_passed"],
                "tests_report": report}

    # 3. scenario eval → deltas vs the champion → the auto-merge gate.
    cand = grade_fn(repo)
    working_delta = cand["working"] - champion_scores["working"]
    held_out_delta = cand.get("held_out", 0.0) - champion_scores.get("held_out", 0.0)
    verdict = code_gate.auto_merge_eligible(
        tests_passed=True, frozen_ok=True, working_delta=working_delta,
        held_out_delta=held_out_delta, divergence_alarm=cand.get("divergence_alarm", False),
        safety_flag=cand.get("safety_flag", False), regression_tol=regression_tol)
    if not verdict["eligible"]:
        return {"action": "discarded", "stage": "gate", "failed": verdict["failed"]}

    # 4. AUTO-MERGE (full-auto: no human gate) — one revertible commit.
    before = {"working": champion_scores["working"],
              "held_out": champion_scores.get("held_out", 0.0), "tests_passed": True}
    merge_sha = adapter.merge_branch(repo, branch, message=f"factory: {label}")

    # 5. re-baseline the NEW champion + self-heal: if it regressed, auto-revert.
    after_scores = grade_fn(repo)
    after = {"working": after_scores["working"],
             "held_out": after_scores.get("held_out", 0.0),
             "tests_passed": adapter.run_tests(repo)[0]}
    reg = code_gate.regression_after_merge(before, after, tol=regression_tol)
    if reg["regressed"]:
        revert_sha = adapter.revert_commit(repo, merge_sha)
        return {"action": "auto_reverted", "merge_sha": merge_sha,
                "revert_sha": revert_sha, "why": reg["why"]}
    return {"action": "merged", "merge_sha": merge_sha, "scores": after_scores}
