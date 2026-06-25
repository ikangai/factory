"""Code-candidate round (design: docs/plans/2026-06-25-autonomous-code-factory.md).

The full-auto orchestration: grade ONE code candidate (a `branch` a developer
super-worker produced in its clone, fetched into the main repo and checked out into
`cand_repo`) and AUTO-MERGE it into the champion, or discard it, with auto-revert
self-heal. There is NO human gate — the automated checks ARE the authority, and every
action is a revertible git commit.

The flow, short-circuiting cheap/structural gates first:

  kill-switch → frozen-check (structural) → target tests (hard, on the candidate) →
  scenario eval (candidate) → auto-merge gate → re-check brake → merge into champion →
  re-baseline → regression? → auto-revert : keep

All live execution is injected so the decision flow is testable without running the
target: `adapter` does git + tests; `grade_fn(repo_dir) ->
{working, held_out, held_out_measured?, divergence_alarm?, safety_flag?}` is the
scenario eval of the target as it stands in a given checkout (the candidate, then —
after the merge — the champion).
"""
from __future__ import annotations

from typing import Callable

from ..common import code_gate, frozen_source, killswitch


def run_code_round(*, adapter, main_repo: str, cand_repo: str, branch: str,
                   champion_scores: dict, grade_fn: Callable[[str], dict],
                   changed_paths=None, diff_text: str = None,
                   label: str = "candidate", regression_tol: float = 0.0) -> dict:
    """Grade + auto-merge / discard one code candidate. Returns a result dict whose
    `action` is one of: halted | discarded | merged | auto_reverted | revert_failed.

    The candidate is GRADED in `cand_repo` (an isolated checkout of `branch` — its OWN
    code, the review's candidate-checkout fix), and only on success is `branch` merged
    into `main_repo` (the champion), which is then re-baselined. The caller sets up the
    checkout (adapter.fetch_candidate + add_worktree) and records the result to the
    store + diary. Prefer passing `changed_paths` (from adapter.changed_paths(),
    NUL-delimited and unquoted); `diff_text` is the fallback."""
    if killswitch.is_halted():
        return {"action": "halted"}

    # 1. frozen-safety — structural, BEFORE any expensive grading.
    changed = (changed_paths if changed_paths is not None
               else frozen_source.changed_paths_from_diff(diff_text))
    frozen_ok, violations = frozen_source.validate_code_candidate(
        changed_paths=changed, frozen_patterns=adapter.frozen_paths())
    if not frozen_ok:
        return {"action": "discarded", "stage": "frozen", "violations": violations}

    # 2. the target's own tests — the hard correctness gate. Skip the (expensive)
    #    scenario eval if they're red.
    tests_passed, report = adapter.run_tests(cand_repo)
    if not tests_passed:
        return {"action": "discarded", "stage": "tests", "failed": ["tests_passed"],
                "tests_report": report}

    # 3. scenario eval → deltas vs the champion → the auto-merge gate.
    cand = grade_fn(cand_repo)
    working_delta = cand["working"] - champion_scores["working"]
    held_out_delta = cand.get("held_out", 0.0) - champion_scores.get("held_out", 0.0)
    # Pass the safety signals FAIL-CLOSED: if grade_fn omits held_out_measured /
    # divergence_alarm / safety_flag, the gate blocks rather than silently merges.
    verdict = code_gate.auto_merge_eligible(
        tests_passed=True, frozen_ok=True, working_delta=working_delta,
        held_out_delta=held_out_delta,
        held_out_measured=cand.get("held_out_measured", False),
        divergence_alarm=cand.get("divergence_alarm", True),
        safety_flag=cand.get("safety_flag", True), regression_tol=regression_tol)
    if not verdict["eligible"]:
        return {"action": "discarded", "stage": "gate", "failed": verdict["failed"]}

    # 4. AUTO-MERGE (full-auto: no human gate) — one revertible commit.
    #    Re-check the brake right before the (irreversible-ish) merge: a STOP dropped
    #    while grading must not result in a merge.
    if killswitch.is_halted():
        return {"action": "halted", "stage": "pre_merge"}
    before = {"working": champion_scores["working"],
              "held_out": champion_scores.get("held_out", 0.0), "tests_passed": True}
    try:
        merge_sha = adapter.merge_branch(main_repo, branch, message=f"factory: {label}")
    except Exception as e:  # merge conflict / git failure → clean discard (adapter aborted)
        return {"action": "discarded", "stage": "merge", "error": str(e)}

    # 5. re-baseline the NEW champion + self-heal. ANY failure here (a regression OR a
    #    grading crash) auto-reverts — never leave an ungraded merge in the repo.
    try:
        after_scores = grade_fn(main_repo)
        after = {"working": after_scores["working"],
                 "held_out": after_scores.get("held_out", 0.0),
                 "tests_passed": adapter.run_tests(main_repo)[0]}
        reg = code_gate.regression_after_merge(before, after, tol=regression_tol)
    except Exception as e:  # noqa: BLE001 — a broken re-baseline is treated as a regression
        after_scores, reg = None, {"regressed": True, "why": [f"re-baseline failed: {e}"]}

    if reg["regressed"]:
        try:
            revert_sha = adapter.revert_commit(main_repo, merge_sha)
        except Exception as e:  # revert itself failed — can't self-heal; surface loudly
            return {"action": "revert_failed", "stage": "revert",
                    "merge_sha": merge_sha, "error": str(e), "why": reg["why"]}
        return {"action": "auto_reverted", "merge_sha": merge_sha,
                "revert_sha": revert_sha, "why": reg["why"]}
    return {"action": "merged", "merge_sha": merge_sha, "scores": after_scores}
