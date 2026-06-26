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

import os
import shutil
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
                 champion_scores: Optional[dict] = None) -> dict:
    """Run ONE task through the gated pipeline and return the round result. The conductor
    NEVER runs this itself (a headless `claude -p` backgrounds + orphans a long sub-command).
    `real=False` (default): merge into a THROWAWAY clone (mechanics only, discarded).
    `real=True`: merge into the persistent `factory/auto` worktree of the REAL target — the
    work persists, git-reversible, on a branch that never disturbs the operator's checkout."""
    adapter = config.get_adapter()
    cs = champion_scores or {"working": 0.0, "held_out": 0.0}
    gf = grade_fn or _smoke_grade
    if real:
        main = factory_worktree(adapter)            # persistent — do NOT delete
        return develop_and_merge(adapter=adapter, main_repo=main, task=task_text,
                                 champion_scores=cs, grade_fn=gf,
                                 as_user=as_user, claude_bin=claude_bin)
    work = tempfile.mkdtemp(prefix="cf-champ-", dir="/tmp")
    main = os.path.join(work, "champion")
    try:
        adapter.clone(main)
        return develop_and_merge(adapter=adapter, main_repo=main, task=task_text,
                                 champion_scores=cs, grade_fn=gf,
                                 as_user=as_user, claude_bin=claude_bin)
    finally:
        shutil.rmtree(work, ignore_errors=True)   # throwaway — never touches the real target


def execute_claimed_tasks(store, shift_id: int, *, as_user: Optional[str] = None,
                          claude_bin: str = "claude", real: bool = False,
                          develop_fn: Optional[Callable] = None,
                          max_tasks: Optional[int] = None) -> int:
    """Deterministically run the tasks the conductor claimed this shift through the gated
    pipeline and CLOSE each: merged → done(sha), anything else → blocked(reason, for the
    conductor to reopen/refine next shift). Returns the count shipped. `real` merges into
    the real target's factory/auto branch; default is throwaway clones. `develop_fn` is
    injectable for tests so no live worker spawns. `max_tasks` caps the per-shift fan-out
    (unattended safety) — the rest stay in_progress for run_shift to requeue. The STOP
    kill-switch is re-checked between tasks so a long execute phase can be interrupted."""
    run = develop_fn or develop_task
    claimed = store.tasks_in_flight(shift_id)        # the in_progress tasks claimed this shift
    if max_tasks is not None and len(claimed) > max_tasks:
        print(f"[execute] {len(claimed)} tasks claimed — capping at {max_tasks} this shift; "
              f"the rest stay in_progress and are requeued for the next shift.", flush=True)
        claimed = claimed[:max_tasks]
    shipped = 0
    for i, task in enumerate(claimed, 1):
        if killswitch.is_halted():                   # STOP can trip DURING a long execute phase
            print(f"[execute] STOP engaged — halting after {i - 1}/{len(claimed)}.", flush=True)
            break
        print(f"[execute] task {i}/{len(claimed)} {task['id']}: {task['title']} "
              f"— running the gated pipeline (clone + developer TDD + the target's test "
              f"suite; a few minutes, no live output)…", flush=True)
        text = task["title"] + ((": " + task["detail"]) if task.get("detail") else "")
        try:
            res = run(text, as_user=as_user, claude_bin=claude_bin, real=real)
        except Exception as e:                        # noqa: BLE001 — contain a dispatch blow-up
            res = {"action": "error", "error": str(e)}
        if res.get("action") == "merged":
            store.set_task_status(task["id"], "done", result=res.get("merge_sha", ""),
                                  shift_id=shift_id)
            shipped += 1
            print(f"[execute]   → merged {res.get('merge_sha', '')[:12]} — SHIPPED", flush=True)
        else:                                         # no_candidate/discarded/auto_reverted/error
            # CAPTURE WHY — a bare 'error' is undiagnosable. Thread the exception message
            # (or the discard stage) into the result so the operator + the conductor (next
            # shift) can see what failed.
            reason = res.get("action", "no result")
            if res.get("error"):
                reason = f"error: {str(res['error'])[:180]}"
            elif res.get("stage"):
                reason = f"{reason} ({res['stage']})"
            store.set_task_status(task["id"], "blocked", result=reason, shift_id=shift_id)
            print(f"[execute]   → {reason} — blocked", flush=True)
    return shipped


def develop_and_merge(*, adapter, main_repo: str, task: str, champion_scores: dict,
                      grade_fn: Callable[[str], dict], as_user: Optional[str] = None,
                      claude_bin: str = "claude", label: Optional[str] = None) -> dict:
    """Run one develop→grade→auto-merge turn. Returns the round result dict (or
    {action: "no_candidate"} if the worker produced no change, "halted" if the brake is
    on). Never leaves clones behind."""
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
                import subprocess
                subprocess.run(["sudo", "chown", "-R", as_user, dev_clone],
                               check=True, capture_output=True, text=True)
            except Exception as e:  # noqa: BLE001
                return {"action": "discarded", "stage": "chown", "error": str(e)}

        develop_candidate(dev_clone, task=task, branch=branch,
                          test_cmd=" ".join(adapter.test_command()),
                          frozen=adapter.frozen_paths(), as_user=as_user, claude_bin=claude_bin)

        changed = adapter.changed_paths(dev_clone, base, branch)
        if not changed:                                # the worker made no committed change
            return {"action": "no_candidate", "branch": branch}

        adapter.fetch_candidate(main_repo, dev_clone, branch)
        cand_wt = os.path.join(work, "wt")
        adapter.add_worktree(main_repo, cand_wt, branch)
        try:
            return code_round.run_code_round(
                adapter=adapter, main_repo=main_repo, cand_repo=cand_wt, branch=branch,
                champion_scores=champion_scores, grade_fn=grade_fn,
                changed_paths=changed, label=branch)
        finally:
            try:
                adapter.remove_worktree(main_repo, cand_wt)
            except Exception:  # noqa: BLE001
                pass
    finally:
        shutil.rmtree(work, ignore_errors=True)
