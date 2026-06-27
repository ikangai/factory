"""One turn of the autonomous CODE loop (design: docs/plans/2026-06-25-...).

`develop_and_merge` ties the fleet flow together: clone the target → a DEVELOPER
super-worker makes a bounded code change on a branch (gets the target's tests green) →
fetch the branch back → grade it in an isolated worktree and AUTO-MERGE into the
champion (run_code_round), with auto-revert self-heal → clean up.

`main_repo` is the champion checkout the merge lands in — for a DEV-account test this is
a THROWAWAY clone of the target (never the real repo), so the whole thing is reversible
by discarding it. `as_user` runs the developer in the Guest House (HARD boundary);
omitted = same-user dev mode (SOFT boundary, supervised, scoped to the disposable clone).
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import uuid
from typing import Callable, Optional

from ..common import config, killswitch
from . import code_round


def _smoke_grade(repo_dir: str) -> dict:
    """Mechanics-smoke grade — do-no-harm; the target's own test suite (run inside the
    round) is the live gate. The full scenario eval over a code-built candidate is the
    next integration."""
    return {"working": 0.0, "held_out": 0.0, "held_out_measured": True,
            "divergence_alarm": False, "safety_flag": False}


def factory_worktree(adapter, *, branch: str = "factory/auto") -> str:
    """Ensure a persistent WORKTREE of the REAL target at `branch` exists, and return its
    path (`<target>.factory-auto`). The branch is created off the target's current HEAD if
    missing; the worktree is a separate checkout, so the operator's own working tree/branch
    is never touched. Merges accumulate on `branch` in the real repo — reviewable
    (`git -C <target> log <branch>`) and fully reversible (revert the commit / delete the
    branch). NOT throwaway: the work persists here."""
    import subprocess
    root = os.path.abspath(adapter.entry()[0])
    wt = root.rstrip("/") + ".factory-auto"
    if os.path.exists(os.path.join(wt, ".git")):   # already a worktree → reuse it
        return wt
    have = subprocess.run(["git", "-C", root, "rev-parse", "--verify", "--quiet", branch],
                          capture_output=True, text=True)
    if have.returncode != 0:                        # create the branch off current HEAD
        subprocess.run(["git", "-C", root, "branch", branch], check=True,
                       capture_output=True, text=True)
    subprocess.run(["git", "-C", root, "worktree", "add", "--quiet", wt, branch],
                   check=True, capture_output=True, text=True)
    return wt


def develop_task(task_text: str, *, as_user: Optional[str] = None, claude_bin: str = "claude",
                 real: bool = False, grade_fn: Optional[Callable] = None,
                 champion_scores: Optional[dict] = None, merge_lock=None,
                 memory: str = "") -> dict:
    """Run ONE task through the gated pipeline and return the round result. The conductor
    NEVER runs this itself (a headless `claude -p` backgrounds + orphans a long sub-command).
    `real=False` (default): merge into a THROWAWAY clone (mechanics only, discarded).
    `real=True`: merge into the persistent `factory/auto` worktree of the REAL target — the
    work persists, git-reversible, on a branch that never disturbs the operator's checkout.
    `merge_lock` serializes the shared factory/auto worktree across PARALLEL workers (real)."""
    adapter = config.get_adapter()
    cs = champion_scores or {"working": 0.0, "held_out": 0.0}
    gf = grade_fn or _smoke_grade
    if real:
        with (merge_lock or contextlib.nullcontext()):   # find/create the ONE worktree race-safe
            main = factory_worktree(adapter)             # persistent — do NOT delete
        return develop_and_merge(adapter=adapter, main_repo=main, task=task_text,
                                 champion_scores=cs, grade_fn=gf, as_user=as_user,
                                 claude_bin=claude_bin, merge_lock=merge_lock, memory=memory)
    work = tempfile.mkdtemp(prefix="cf-champ-", dir="/tmp")    # throwaway: isolated → no lock needed
    main = os.path.join(work, "champion")
    try:
        adapter.clone(main)
        return develop_and_merge(adapter=adapter, main_repo=main, task=task_text,
                                 champion_scores=cs, grade_fn=gf,
                                 as_user=as_user, claude_bin=claude_bin, memory=memory)
    finally:
        shutil.rmtree(work, ignore_errors=True)   # throwaway — never touches the real target


def execute_claimed_tasks(store, shift_id: int, *, as_user: Optional[str] = None,
                          claude_bin: str = "claude", real: bool = False,
                          develop_fn: Optional[Callable] = None,
                          max_tasks: Optional[int] = None,
                          max_parallel: Optional[int] = None,
                          scope_judge: Optional[Callable] = None) -> int:
    """Run the tasks the conductor claimed this shift through the gated pipeline and CLOSE
    each: merged → done(sha), anything else → blocked(reason). Returns the count shipped.

    The super-workers run IN PARALLEL (up to `max_parallel`) — the conductor claims distinct-
    file tasks, each develops in its OWN clone, so the slow clone+developer+tests overlap. In
    REAL mode the merge into the ONE shared factory/auto worktree is serialized under a lock
    (so only that fast section is mutually exclusive). Task-status writes happen on the MAIN
    thread (the store's SQLite connection is single-threaded). `max_tasks` caps the fan-out;
    STOP is honored — already-engaged → nothing dispatches; tripped mid-flight → queued
    workers skip (they stay in_progress for run_shift to requeue)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    run = develop_fn or develop_task
    claimed = store.tasks_in_flight(shift_id)        # the in_progress tasks claimed this shift
    if max_tasks is not None and len(claimed) > max_tasks:
        print(f"[execute] {len(claimed)} tasks claimed — capping at {max_tasks} this shift; "
              f"the rest stay in_progress and are requeued for the next shift.", flush=True)
        claimed = claimed[:max_tasks]
    if not claimed:
        return 0
    if killswitch.is_halted():
        print("[execute] STOP engaged — not dispatching.", flush=True)
        return 0

    if scope_judge is not None:                            # GSD spec-driven pre-dispatch scope check:
        from ..reporting import scope_check                # reject/split over-broad briefs BEFORE a
        before = len(claimed)                              # worker is spent (kills no_candidate upstream)
        claimed = scope_check.prefilter(store, claimed, shift_id=shift_id, judge=scope_judge)
        if len(claimed) != before:
            print(f"[execute] scope check: {before - len(claimed)} task(s) rejected/split, "
                  f"{len(claimed)} dispatching.", flush=True)
        if not claimed:
            return 0

    from ..reporting import factory_memory                  # factory memory: lessons → each worker
    dev_card = factory_memory.memory_card(store, "developer")

    merge_lock = threading.Lock() if real else None  # serialize the shared factory/auto merge
    workers = max(1, min(max_parallel or len(claimed), len(claimed)))
    print(f"[execute] dispatching {len(claimed)} task(s) — up to {workers} super-worker(s) "
          f"in parallel (clone + developer TDD + the target's tests; a few minutes)…", flush=True)

    def work(task):
        if killswitch.is_halted():                   # STOP tripped before this one started
            return task, {"action": "halted"}
        text = task["title"] + ((": " + task["detail"]) if task.get("detail") else "")
        if task.get("spec"):                          # scope check passed a sharpened contract
            from ..reporting import scope_check
            text += scope_check.spec_brief(task["spec"])
        print(f"[execute] ▶ {task['id']}: {task['title']}", flush=True)
        try:
            return task, run(text, as_user=as_user, claude_bin=claude_bin, real=real,
                             merge_lock=merge_lock, memory=dev_card)
        except Exception as e:                        # noqa: BLE001 — contain a dispatch blow-up
            return task, {"action": "error", "error": str(e)}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = [f.result() for f in [ex.submit(work, t) for t in claimed]]

    shipped = 0                                       # close out on the MAIN thread (single-writer)
    for task, res in results:
        action = res.get("action")
        if action != "halted":                        # a STOP-braked run is incomplete — don't
            for lesson in res.get("learnings") or []: # attribute its emitted learnings as durable
                factory_memory.record_learning(store, "developer", lesson, shift_id=shift_id)
        if action == "merged":
            store.set_task_status(task["id"], "done", result=res.get("merge_sha", ""),
                                  shift_id=shift_id)
            shipped += 1
            print(f"[execute]   {task['id']} → merged {res.get('merge_sha', '')[:12]} — SHIPPED", flush=True)
        elif action == "halted":                      # STOP — leave in_progress for requeue
            print(f"[execute]   {task['id']} → skipped (STOP)", flush=True)
        else:                                         # no_candidate/discarded/auto_reverted/error
            reason = action or "no result"            # CAPTURE WHY — a bare 'error' is undiagnosable
            if res.get("error"):
                reason = f"error: {str(res['error'])[:180]}"
            elif res.get("stage"):
                reason = f"{reason} ({res['stage']})"
            store.set_task_status(task["id"], "blocked", result=reason, shift_id=shift_id)
            print(f"[execute]   {task['id']} → {reason} — blocked", flush=True)
            fl = factory_memory.lesson_for_block(action, res.get("stage", ""))  # stage-aware lesson
            if fl:
                factory_memory.record_learning(store, "factory", fl, scope="blocked",
                                               shift_id=shift_id)
    return shipped


def develop_and_merge(*, adapter, main_repo: str, task: str, champion_scores: dict,
                      grade_fn: Callable[[str], dict], as_user: Optional[str] = None,
                      claude_bin: str = "claude", label: Optional[str] = None,
                      merge_lock=None, memory: str = "") -> dict:
    """Run one develop→grade→auto-merge turn. Returns the round result dict (or
    {action: "no_candidate"} if the worker produced no change, "halted" if the brake is
    on). Never leaves clones behind. `merge_lock`, when given, serializes the
    SHARED-worktree section (fetch + worktree + grade + merge) so parallel workers in REAL
    mode don't race on the one factory/auto worktree — the slow clone+develop runs unlocked."""
    from ..roles.common import develop_candidate

    if killswitch.is_halted():
        return {"action": "halted"}

    branch = label or f"factory/cand-{uuid.uuid4().hex[:8]}"
    work = tempfile.mkdtemp(prefix="cf-dev-", dir="/tmp")
    dev_clone = os.path.join(work, "clone")
    try:
        adapter.clone(dev_clone)                       # the developer's own clone of the target
        base = adapter.default_branch(dev_clone)
        if as_user:                                    # Guest House: the worker user must own the clone
            try:
                subprocess.run(["sudo", "chown", "-R", as_user, dev_clone],
                               check=True, capture_output=True, text=True)
            except Exception as e:  # noqa: BLE001
                return {"action": "discarded", "stage": "chown", "error": str(e)}

        dev = develop_candidate(dev_clone, task=task, branch=branch,
                          test_cmd=" ".join(adapter.test_command()),
                          frozen=adapter.frozen_paths(), as_user=as_user,
                          claude_bin=claude_bin, memory=memory)
        # The developer can't write the factory DB from its clone (Guest-House user in prod),
        # so it emits learnings in its reply; carry them out for the MAIN thread to record.
        from ..reporting import factory_memory
        learnings = factory_memory.parse_learnings(dev.get("reply", ""))

        if not adapter.branch_exists(dev_clone, branch):   # worker crashed / committed nothing →
            return {"action": "no_candidate", "branch": branch, "learnings": learnings}  # NO branch
        try:
            changed = adapter.changed_paths(dev_clone, base, branch)
        except subprocess.CalledProcessError:          # 2nd exit-128 site: branch exists but the diff
            return {"action": "no_candidate", "branch": branch, "learnings": learnings}  # unresolvable
        if not changed:                                # the worker made no committed change
            return {"action": "no_candidate", "branch": branch, "learnings": learnings}

        # The shared-worktree section mutates main_repo; in REAL mode main_repo is the ONE
        # factory/auto worktree shared by parallel workers, so serialize it under merge_lock.
        # The slow part (clone + the developer worker) already ran in parallel above.
        with (merge_lock or contextlib.nullcontext()):
            adapter.fetch_candidate(main_repo, dev_clone, branch)
            cand_wt = os.path.join(work, "wt")
            adapter.add_worktree(main_repo, cand_wt, branch)
            try:
                require_test = bool((config.load_config().get("super_worker", {}) or {})
                                    .get("require_test", False))   # GSD spec-bound acceptance gate
                res = code_round.run_code_round(
                    adapter=adapter, main_repo=main_repo, cand_repo=cand_wt, branch=branch,
                    champion_scores=champion_scores, grade_fn=grade_fn,
                    changed_paths=changed, label=branch, require_test=require_test)
                res["learnings"] = learnings
                return res
            finally:
                try:
                    adapter.remove_worktree(main_repo, cand_wt)
                except Exception:  # noqa: BLE001
                    pass
    finally:
        shutil.rmtree(work, ignore_errors=True)
