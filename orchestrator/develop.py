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


def develop_task(task_text: str, *, as_user: Optional[str] = None, claude_bin: str = "claude",
                 grade_fn: Optional[Callable] = None,
                 champion_scores: Optional[dict] = None) -> dict:
    """Run ONE task through the gated pipeline against a THROWAWAY champion clone; return
    the round result. This is the deterministic execution the conductor's claimed work goes
    through — the conductor NEVER runs this itself (a headless `claude -p` can't reliably
    block on a long sub-command; it backgrounds it and orphans it at shift end)."""
    adapter = config.get_adapter()
    work = tempfile.mkdtemp(prefix="cf-champ-", dir="/tmp")
    main = os.path.join(work, "champion")
    try:
        adapter.clone(main)
        return develop_and_merge(adapter=adapter, main_repo=main, task=task_text,
                                 champion_scores=champion_scores or {"working": 0.0, "held_out": 0.0},
                                 grade_fn=grade_fn or _smoke_grade,
                                 as_user=as_user, claude_bin=claude_bin)
    finally:
        shutil.rmtree(work, ignore_errors=True)   # throwaway — never touches the real target


def execute_claimed_tasks(store, shift_id: int, *, as_user: Optional[str] = None,
                          claude_bin: str = "claude", develop_fn: Optional[Callable] = None) -> int:
    """Deterministically run EVERY task the conductor claimed this shift through the gated
    pipeline and CLOSE it: merged → done(sha), anything else → blocked(reason, for the
    conductor to reopen/refine next shift). Returns the count shipped. `develop_fn` is
    injectable for tests so no live worker spawns."""
    run = develop_fn or develop_task
    shipped = 0
    for task in store.tasks_in_flight(shift_id):     # the in_progress tasks claimed this shift
        text = task["title"] + ((": " + task["detail"]) if task.get("detail") else "")
        try:
            res = run(text, as_user=as_user, claude_bin=claude_bin)
        except Exception as e:                        # noqa: BLE001 — contain a dispatch blow-up
            res = {"action": "error", "error": str(e)}
        if res.get("action") == "merged":
            store.set_task_status(task["id"], "done", result=res.get("merge_sha", ""),
                                  shift_id=shift_id)
            shipped += 1
        else:                                         # no_candidate/discarded/auto_reverted/error
            store.set_task_status(task["id"], "blocked", result=res.get("action", "no result"),
                                  shift_id=shift_id)
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
