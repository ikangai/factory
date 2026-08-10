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

import ast
import os
import shutil
import tempfile
from typing import Callable, Optional

from ..common import code_gate, target_exec, frozen_source, killswitch
from ..common.textutil import clean_line

# F6 (round-2 integration fix, Component E): a hard ceiling on individual pytest
# invocations the red-proof stage will run per candidate — each is cheap (one node, not a
# whole file) but still a real subprocess with its own timeout; an unbounded count from a
# candidate touching many test files must not turn one merge decision into an
# open-ended pytest marathon.
MAX_RED_PROOF_NODES = 20

# -- crash-consistency intent-row wrapping (design: docs/plans/2026-08-08-crash-
# consistency-design.md, Component B). run_code_round executes on a WORKER THREAD (the
# ThreadPoolExecutor in orchestrator/develop.py's execute_claimed_tasks) — the store's
# single sqlite3.Connection is main-thread-only, so these helpers open their OWN
# short-lived connection per call (WAL mode already tolerates concurrent connections)
# rather than sharing one across threads. `db_path=None` (every caller/test that doesn't
# thread it) is a pure no-op — zero behavior change for anyone who hasn't opted in.
# BINDING CONSTRAINT: a store hiccup here must never break or block a merge — every
# helper is fail-soft (log + continue, never raise).


def _op_begin(db_path, kind, idem_key, **kw):
    if not db_path or not idem_key:
        return None
    try:
        from ..common.store import Blackboard
        with Blackboard(db_path) as s:
            return s.begin_operation(kind, idem_key, **kw)
    except Exception as e:  # noqa: BLE001 — fail-soft: never block the merge on a store hiccup
        print(f"[reconcile] begin_operation({kind}) failed (non-fatal): {e}", flush=True)
        return None


def _op_complete(db_path, op_id, receipt):
    if not db_path or not op_id:
        return
    try:
        from ..common.store import Blackboard
        with Blackboard(db_path) as s:
            s.complete_operation(op_id, receipt)
    except Exception as e:  # noqa: BLE001 — fail-soft
        print(f"[reconcile] complete_operation failed (non-fatal): {e}", flush=True)


def _op_set_status(db_path, op_id, status, detail=""):
    if not db_path or not op_id:
        return
    try:
        from ..common.store import Blackboard
        with Blackboard(db_path) as s:
            s.set_operation_status(op_id, status, detail)
    except Exception as e:  # noqa: BLE001 — fail-soft
        print(f"[reconcile] set_operation_status failed (non-fatal): {e}", flush=True)


def _collect_test_bodies(file_path: str) -> dict[str, str]:
    """AST-parse `file_path` (no subprocess — a static read) into
    {pytest_node_suffix: source_text} for every function pytest's OWN default discovery
    would collect: module-level `test_*` functions, and `test_*` methods inside `Test*`
    classes (node suffix `TestClass::test_method`, matching pytest's own node-id shape
    after the `path.py::` prefix). Async defs count too (pytest-asyncio et al). A missing
    file / syntax error / anything unreadable returns {} — fail-open: the caller treats
    an empty/unresolvable result as 'nothing here to red-proof', never a crash and never
    a false discard."""
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
    except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
        return {}
    bodies: dict[str, str] = {}

    def _is_test_def(node) -> bool:
        return (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name.startswith("test_"))

    for node in ast.iter_child_nodes(tree):
        if _is_test_def(node):
            bodies[node.name] = ast.get_source_segment(src, node) or ""
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for inner in ast.iter_child_nodes(node):
                if _is_test_def(inner):
                    bodies[f"{node.name}::{inner.name}"] = ast.get_source_segment(src, inner) or ""
    return bodies


def _changed_test_nodes(base_wt: str, cand_repo: str, rel_path: str) -> tuple[list[str], str]:
    """Node-level red-proof targeting (F6): the OLD file-level check ran an entire
    changed test FILE against the base — for a file that already existed (the NORMAL way
    to ship a discriminating test: add a case to an existing tests/test_x.py, or fix an
    existing one in place), pytest happily ran every OTHER, unrelated, already-passing
    test in that file and reported a trivial file-level 'passed', discarding the
    candidate for a reason that had nothing to do with what actually changed. This
    compares AST-parsed test bodies between the candidate's version of `rel_path` and the
    base's: a node id ABSENT from the base (new name, or the whole file is new) or present
    with DIFFERENT source text is a genuine change worth red-proofing; a node id present
    with IDENTICAL text is untouched and is never run — nothing new to prove, and running
    it would risk the exact false-discard this fix exists to close.

    Returns (`['<rel_path>::<node>', ...]`, `skip_reason`) — `skip_reason` is '' on
    success; non-empty means the file was skipped ENTIRELY (deleted from the candidate,
    or unresolvable via AST), and the caller must NOT discard on this file, only note it."""
    cand_path = os.path.join(cand_repo, rel_path)
    if not os.path.isfile(cand_path):
        # Deleted from the candidate (a legitimate maintenance action, not something that
        # needs to "discriminate") — never red-proofed. Also covers a rename-away.
        return [], "deleted from the candidate — never red-proofed"
    cand_bodies = _collect_test_bodies(cand_path)
    if not cand_bodies:
        return [], "no pytest-discoverable test function found via AST parse — skipped"
    base_path = os.path.join(base_wt, rel_path)
    base_bodies = _collect_test_bodies(base_path) if os.path.isfile(base_path) else {}
    changed = [f"{rel_path}::{name}" for name, body in cand_bodies.items()
              if base_bodies.get(name) != body]
    return changed, ""


def run_code_round(*, adapter, main_repo: str, cand_repo: str, branch: str,
                   champion_scores: dict, grade_fn: Callable[[str], dict],
                   changed_paths=None, diff_text: str = None,
                   label: str = "candidate", task_ref: str = "",
                   regression_tol: float = 0.0,
                   require_test: bool = False, acceptance_ref: str = None,
                   require_held_out: bool = True, red_proof: bool = False,
                   base_repo: Optional[str] = None, base_sha: Optional[str] = None,
                   task_id: str = "", db_path: Optional[str] = None) -> dict:
    """Grade + auto-merge / discard one code candidate. Returns a result dict whose
    `action` is one of: halted | discarded | merged | auto_reverted | revert_failed.

    The candidate is GRADED in `cand_repo` (an isolated checkout of `branch` — its OWN
    code, the review's candidate-checkout fix), and only on success is `branch` merged
    into `main_repo` (the champion), which is then re-baselined. The caller sets up the
    checkout (adapter.fetch_candidate + add_worktree) and records the result to the
    store + diary. Prefer passing `changed_paths` (from adapter.changed_paths(),
    NUL-delimited and unquoted); `diff_text` is the fallback.

    `require_held_out` defaults to `True` (fail-closed, adversarial-review fix round,
    2026-08-05 — restores this function's OWN default to match `code_gate.
    auto_merge_eligible`'s fail-closed posture; a caller wanting the `grade.mode: smoke`
    per-merge scope-out must now say so EXPLICITLY at its own call site, which
    orchestrator/develop.py's real caller does: `require_held_out=False`).

    `red_proof` (super_worker.red_proof, docs/plans/2026-08-06-publication-broker-design.md
    Component E): a shipped test must DISCRIMINATE — fail on the pristine base, not just
    pass on the candidate. When true (AND `require_test` is also true — there's nothing to
    red-proof if no test is even required) each changed TEST file runs against a fresh
    detached worktree at `base_sha` (inside `base_repo`, e.g. the developer's own clone,
    recorded BEFORE it made any change); a file that already passes there doesn't prove
    anything and discards the candidate (stage 'no_test'). `base_repo`/`base_sha` are the
    caller's seam (develop.py threads the pristine clone + its pre-dispatch HEAD) — when
    either is missing, red-proofing is silently skipped (nothing to check against), never
    a crash and never a false discard.

    `task_id`/`db_path` (crash consistency, Component B): when BOTH are given, the merge
    (and its possible auto-revert self-heal) is wrapped in an `operations` intent row
    keyed `merge:<task_id>:<cand_tip_sha>` — see the module's `_op_*` helpers. Either
    missing (the default) is a byte-identical no-op: no extra git/store call, today's
    exact behavior."""
    if killswitch.is_halted():
        return {"action": "halted"}

    # 1. frozen-safety — structural, BEFORE any expensive grading.
    changed = (changed_paths if changed_paths is not None
               else frozen_source.changed_paths_from_diff(diff_text))
    frozen_ok, violations = frozen_source.validate_code_candidate(
        changed_paths=changed, frozen_patterns=adapter.frozen_paths())
    if not frozen_ok:
        return {"action": "discarded", "stage": "frozen", "violations": violations}

    extra: dict = {}

    # 1.5 spec-bound acceptance (GSD): a code change must SHIP A TEST — the gate measures
    #     fulfillment, not just non-regression. Config-gated; cheap (diff-level), before tests.
    if require_test:
        from ..reporting import acceptance
        ok, why = acceptance.acceptance_ok(changed)
        if not ok:
            return {"action": "discarded", "stage": "no_test", "why": why}

        # 1.6 red-proof (Component E): "ships a test" is gameable — a test that already
        #     passes on the pristine base proves nothing. NODE-level targeting (F6, round-2
        #     integration fix): the original file-level check ran an ENTIRE changed test
        #     file against the base, so adding/fixing ONE case in an existing file (the
        #     normal way to ship a discriminating test) dragged every OTHER, unrelated,
        #     already-passing test in that file along — pytest reported a trivial
        #     file-level 'passed' and the candidate was discarded for a reason that had
        #     nothing to do with what actually changed; deleting an obsolete test file was
        #     discarded outright (there is nothing to "discriminate" about a deletion).
        #     `_changed_test_nodes` AST-diffs each changed test file's node bodies against
        #     the base and red-proofs ONLY the added/changed nodes, one at a time, fail
        #     fast, BEFORE the (expensive) full suite, capped at MAX_RED_PROOF_NODES.
        #     Silently skipped when the caller didn't thread base_repo/base_sha (nothing
        #     to red-proof against) or no changed path is itself a test file.
        if red_proof and base_repo and base_sha:
            test_files = [p for p in changed if acceptance._is_test(p)]
            if test_files:
                base_wt = (tempfile.mkdtemp(prefix="cf-redproof-")
                           if not target_exec.isolation_active() else None)
                try:
                    # Component C, same reasoning as the candidate checkout: a detached
                    # worktree still links back to base_repo's .git, and the grader runs
                    # pytest in here.
                    if target_exec.isolation_active():
                        base_wt = target_exec.new_export(adapter, base_repo, base_sha,
                                                         prefix="cf-redproof-")
                    else:
                        adapter.add_worktree_detached(base_repo, base_wt, base_sha)
                    to_check: list[str] = []
                    skipped: list[str] = []
                    for rel in test_files:
                        nodes, reason = _changed_test_nodes(base_wt, cand_repo, rel)
                        if reason:
                            skipped.append(f"{rel}: {reason}")
                        else:
                            to_check.extend(nodes)
                    capped = len(to_check) > MAX_RED_PROOF_NODES
                    if capped:
                        skipped.append(f"capped at {MAX_RED_PROOF_NODES} of "
                                       f"{len(to_check)} changed test node(s)")
                        to_check = to_check[:MAX_RED_PROOF_NODES]
                    missing = 0
                    for ref in to_check:
                        status, report = adapter.run_named_test(base_wt, ref)
                        if status == "passed":
                            return {"action": "discarded", "stage": "no_test",
                                   "why": f"test passes on the pristine base: {ref}",
                                   "tests_report": report}
                        if status == "refused":
                            # The isolation wrapper refused to run it. NOT a test result —
                            # discard rather than let an infrastructure failure satisfy the
                            # discriminating-test gate (which 'missing' legitimately does).
                            return {"action": "discarded", "stage": "tests",
                                   "why": f"grading isolation refused the red-proof run: {ref}",
                                   "tests_report": report}
                        if status == "missing":
                            missing += 1
                    # Fail-open honesty (F6): 'missing' satisfies red-proof (a file/node
                    # absent from the base IS discriminating), but if EVERY single check
                    # resolved 'missing' the gate may simply be vacuous — a misconfigured
                    # test_command, or a target whose test IDs don't resolve the way this
                    # heuristic expects — never silently. Rides out as telemetry, never a
                    # discard (an honest gate that can't verify still isn't a false one).
                    if to_check and missing == len(to_check):
                        extra["red_proof_all_missing"] = True
                    if skipped:
                        extra["red_proof_skipped"] = skipped
                finally:
                    try:
                        adapter.remove_worktree(base_repo, base_wt)
                    except Exception:  # noqa: BLE001 — cleanup must never crash the round
                        pass
                    shutil.rmtree(base_wt, ignore_errors=True)

    # 2. the target's own tests — the hard correctness gate. Skip the (expensive)
    #    scenario eval if they're red.
    tests_passed, report = adapter.run_tests(cand_repo)
    if not tests_passed:
        return {"action": "discarded", "stage": "tests", "failed": ["tests_passed"],
                "tests_report": report, **extra}

    # 2.5 spec-named acceptance test (Task 3.1, P2): the spec's OWN named acceptance test, run in
    #     the candidate AFTER the suite gate. A RED run discards (stage 'acceptance') — the change
    #     didn't satisfy its declared done-condition. A MISSING test (the worker didn't create the
    #     contracted ref) is a telemetry-first SKIP (correction b): acceptance_skipped rides out so
    #     the rail counts it — we do NOT discard-on-missing yet (that flip waits until the prompt
    #     contract is live and the skip rate is known). Gated: only runs when acceptance_ref is set.
    if acceptance_ref:
        status, acc_report = adapter.run_named_test(cand_repo, acceptance_ref)
        if status == "failed":
            return {"action": "discarded", "stage": "acceptance",
                    "tests_report": acc_report, "acceptance_ref": acceptance_ref, **extra}
        if status == "missing":
            extra["acceptance_skipped"] = acceptance_ref

    # 3. scenario eval → deltas vs the champion → the auto-merge gate.
    cand = grade_fn(cand_repo)
    working_delta = cand["working"] - champion_scores["working"]
    held_out_delta = cand.get("held_out", 0.0) - champion_scores.get("held_out", 0.0)
    # Pass the safety signals FAIL-CLOSED: if grade_fn omits
    # divergence_alarm / safety_flag, the gate blocks rather than silently merges.
    #
    # held-out is REBASELINE scope, not merge scope: sampling it on every merge
    # would select candidates against it and it would stop being held out.
    # `factory rebaseline` measures it. Whatever a grade DOES report is still
    # checked — no_held_out_regression stays live — this only stops the gate
    # demanding a measurement this stage deliberately does not make.
    verdict = code_gate.auto_merge_eligible(
        tests_passed=True, frozen_ok=True, working_delta=working_delta,
        held_out_delta=held_out_delta,
        held_out_measured=cand.get("held_out_measured", False),
        divergence_alarm=cand.get("divergence_alarm", True),
        safety_flag=cand.get("safety_flag", True), regression_tol=regression_tol,
        require_held_out=require_held_out)
    if not verdict["eligible"]:
        return {"action": "discarded", "stage": "gate", "failed": verdict["failed"], **extra}

    # 4. AUTO-MERGE (full-auto: no human gate) — one revertible commit.
    #    Re-check the brake right before the (irreversible-ish) merge: a STOP dropped
    #    while grading must not result in a merge.
    if killswitch.is_halted():
        return {"action": "halted", "stage": "pre_merge", **extra}
    before = {"working": champion_scores["working"],
              "held_out": champion_scores.get("held_out", 0.0), "tests_passed": True}

    # Crash-consistency intent row (Component B), keyed on task + candidate tip — the
    # SAME candidate branch re-graded twice (e.g. a reconciler re-entry after a crash)
    # must never merge a second time. Fail-soft + opt-in: db_path/task_id absent (every
    # caller/test that hasn't threaded them) is a pure no-op.
    op_id = None
    if db_path and task_id:
        try:
            cand_tip_sha = adapter.current_commit(cand_repo)
        except Exception:  # noqa: BLE001 — no tip sha to key on → skip tracking, never crash
            cand_tip_sha = None
        if cand_tip_sha:
            begun = _op_begin(db_path, "merge", f"merge:{task_id}:{cand_tip_sha}",
                              target_ref=branch, tip_sha=cand_tip_sha,
                              payload={"task_id": task_id, "label": label})
            if begun:
                op = begun.get("operation") or {}
                if begun.get("skip"):
                    # Already applied/reconciled — this exact merge already happened;
                    # repeating it would double-merge the same candidate.
                    return {"action": "merged", "merge_sha": op.get("receipt", ""),
                            "scores": None, "idempotent_skip": True, **extra}
                op_id = op.get("id")

    try:
        # Provenance trailer (blindspot fix 2026-07-07): the sha→task chain must survive
        # WITHOUT the blackboard — the public repo's history was unexplainable on DB loss.
        # Sanitized to ONE printable line at this single choke point (63035a2 review):
        # task titles are free/LLM-authored, and an embedded newline in the ref could
        # forge/shadow the trailer — provenance must be tamper-evident.
        ref = clean_line(task_ref, cap=160)
        message = f"factory: {label}" + (f"\n\nFactory-Task: {ref}" if ref else "")
        merge_sha = adapter.merge_branch(main_repo, branch, message=message)
    except Exception as e:  # merge conflict / git failure → clean discard (adapter aborted)
        _op_set_status(db_path, op_id, "failed", f"merge failed: {e}"[:2000])
        return {"action": "discarded", "stage": "merge", "error": str(e), **extra}
    _op_complete(db_path, op_id, merge_sha)

    # 5. re-baseline the NEW champion + self-heal. ANY failure here (a regression OR a
    #    grading crash) auto-reverts — never leave an ungraded merge in the repo.
    try:
        # Component D — the site the FIRST Phase 3 design would have missed entirely: after
        # the merge lands, the candidate's code is re-graded IN main_repo, the factory's own
        # persistent factory/auto worktree. Isolating only the pre-merge checkout would leave
        # the worker's code running there with full authority. With isolation on, re-baseline
        # a throwaway export of the merged tree instead.
        if target_exec.isolation_active():
            rebase_dir = target_exec.new_export(adapter, main_repo, "HEAD",
                                                prefix="cf-rebaseline-")
            try:
                after_scores = grade_fn(rebase_dir)
                after = {"working": after_scores["working"],
                         "held_out": after_scores.get("held_out", 0.0),
                         "tests_passed": adapter.run_tests(rebase_dir)[0]}
            finally:
                target_exec.remove_export(rebase_dir)
        else:
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
            _op_set_status(db_path, op_id, "failed", f"revert failed: {e}"[:2000])
            return {"action": "revert_failed", "stage": "revert",
                    "merge_sha": merge_sha, "error": str(e), "why": reg["why"], **extra}
        # Auto-revert self-heal: the SAME operation row moves applied -> reconciled — we
        # already KNOW the full fate of this merge (landed, then reverted), so there is
        # no ambiguity left for a reconciler to resolve later.
        _op_set_status(db_path, op_id, "reconciled", f"auto-reverted -> {revert_sha}")
        return {"action": "auto_reverted", "merge_sha": merge_sha,
                "revert_sha": revert_sha, "why": reg["why"], **extra}
    return {"action": "merged", "merge_sha": merge_sha, "scores": after_scores, **extra}
