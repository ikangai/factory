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
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Callable, Optional

from ..common import config, killswitch
from . import code_round

# Task 0.1 (P11): an EMPTY-HANDED worker (no candidate branch) is not always "brief too
# big" — classify the reply first so a dead transport or a refusal stops masquerading as
# no_candidate (which triggers auto-decompose spend + a false factory lesson).
# Fix 0.1b: markers require an explicit refusal VERB (help/assist/comply/decline) —
# bare capability statements ("i'm unable to …") are how an HONEST empty-handed worker
# reports back (prompt.md: committing nothing is valid when no safe change exists), and
# must stay genuine no_candidate so auto-decompose still sees real "too big" evidence.
REFUSAL_MARKERS = (
    "i can't help", "i cannot help", "i can't assist", "i cannot assist",
    "unable to comply", "i can't comply", "i cannot comply",
    "i must decline", "i won't help",
)
_REFUSAL_MAX_LEN = 600      # refusals are short — a long reply is real work, not a block
_REFUSAL_HEAD = 200         # a marker must appear near the START of the reply
_REFUSAL_REASON_LEN = 300   # how much refusal text the blocked reason keeps
# error stages that stay decompose-eligible: a 30-min timeout is the strongest "task too
# big" evidence in the system; worker_failed includes max-turns exhaustion. A transport
# failure never attempted the brief and a refusal is not scope evidence — both suppressed.
_DECOMPOSE_STAGES = ("timeout", "worker_failed")
# stages where the worker NEVER ran, so no model consulted the memory card (Fix 1.4b):
# 'transport' = claude binary unavailable; 'chown' = Guest-House clone ownership failed
# before dispatch. Outcome attribution must skip these — during a transport outage every
# dispatched task would otherwise bump blocked_after on its surfaced learnings each
# shift, poisoning the effectiveness ratio for the newest/most-relevant lessons.
_NO_CONSULT_STAGES = ("transport", "chown")
# Task 3.2: stages where a candidate FAILED a gradeable done-condition — a red suite (tests),
# a missing-test discard (no_test), or a red spec-named acceptance test (acceptance, Task 3.1).
# These carry actionable failure evidence, so ONE informed retry is worth it. NEVER 'frozen'
# (a structural safety veto a retry can only re-violate), never review/gate/merge/transport
# (not a gradeable code outcome the worker can act on).
_RETRY_STAGES = ("tests", "no_test", "acceptance")
# Task 5.2: the per-worker wall (there is NO shift wall-clock over the executor — only these
# per-worker timeouts). The bounded second wave reserves waves×this against the loop deadline
# before it dispatches, because the loop deadline is checked only BETWEEN shifts.
_WORKER_TIMEOUT_S = 1800


def classify_empty_handed(reply: str) -> Optional[dict]:
    """Classify a no-branch worker reply into {action:'error', stage, error} — or None
    for a GENUINE no_candidate (the worker honestly came back empty-handed). Stages:
    'timeout' (worker killed at the wall), 'worker_failed' (non-zero rc), 'transport'
    (claude binary unavailable — never attempted), 'refusal' (short reply refusing the
    brief; its head rides out in `error` so the blocked reason carries the diagnosis)."""
    r = (reply or "").strip()
    if r.startswith("[claude -p"):                 # a transport sentinel, not worker prose
        if "timed out" in r:
            return {"action": "error", "stage": "timeout", "error": r[:180]}
        if "rc=" in r:
            return {"action": "error", "stage": "worker_failed", "error": r[:180]}
        if r.startswith("[claude -p unavailable:"):
            return {"action": "error", "stage": "transport", "error": r[:180]}
    head = r[:_REFUSAL_HEAD].lower().replace("’", "'")
    if r and len(r) < _REFUSAL_MAX_LEN and any(m in head for m in REFUSAL_MARKERS):
        return {"action": "error", "stage": "refusal", "error": r[:_REFUSAL_REASON_LEN]}
    return None


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
                 memory: str = "", profile_overlay: str = "", model: str = "",
                 require_test: Optional[bool] = None, reviewer: bool = False,
                 acceptance_ref: Optional[str] = None, task_ref: str = "") -> dict:
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
                                 claude_bin=claude_bin, merge_lock=merge_lock, memory=memory,
                                 profile_overlay=profile_overlay, model=model,
                                 require_test=require_test, reviewer=reviewer,
                                 acceptance_ref=acceptance_ref, task_ref=task_ref)
    work = tempfile.mkdtemp(prefix="cf-champ-", dir="/tmp")    # throwaway: isolated → no lock needed
    main = os.path.join(work, "champion")
    try:
        adapter.clone(main)
        return develop_and_merge(adapter=adapter, main_repo=main, task=task_text,
                                 champion_scores=cs, grade_fn=gf,
                                 as_user=as_user, claude_bin=claude_bin, memory=memory,
                                 profile_overlay=profile_overlay, model=model,
                                 require_test=require_test, reviewer=reviewer,
                                 acceptance_ref=acceptance_ref, task_ref=task_ref)
    finally:
        shutil.rmtree(work, ignore_errors=True)   # throwaway — never touches the real target


def execute_claimed_tasks(store, shift_id: int, *, as_user: Optional[str] = None,
                          claude_bin: str = "claude", real: bool = False,
                          develop_fn: Optional[Callable] = None,
                          max_tasks: Optional[int] = None,
                          max_parallel: Optional[int] = None,
                          scope_judge: Optional[Callable] = None,
                          decomposer: Optional[Callable] = None,
                          require_test: Optional[bool] = None,
                          reviewer: bool = False,
                          acceptance_exec: bool = False,
                          investigate_blocked: bool = False,
                          shift_started: Optional[float] = None,
                          loop_deadline_s: Optional[float] = None) -> int:
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

    # Resolve each task's worker profile (Phase 5) AND its memory card (Task 1.4) ON THE MAIN
    # THREAD — both are store I/O, and the workers run in threads that must never touch the
    # single-writer connection. The card is PER-TASK: scored by keyword overlap with the task's
    # own title+detail (replacing the old single shift-wide card, whose whole-shift attribution
    # would be a confounded near-noise signal), and its surfaced ids get the task's OUTCOME
    # attributed back at close-out. A profile supplies only a persona overlay + a rail-resolved
    # model tier; capability stays rail-fixed.
    # Task 3.1 (P2): when the acceptance-exec gate is ON, extract each task's OWN named
    # acceptance test (a tests/<path>.py[::name] ref parsed from spec.acceptance) ON THE MAIN
    # THREAD — the workers run in threads that must not touch the store, and the parse is pure.
    # A prose acceptance / no safe ref → None (the gate simply doesn't run for that task).
    from ..reporting import acceptance
    profiles = {}
    cards: dict = {}                                 # task id → (card text, surfaced learning ids)
    accept_refs: dict = {}                           # task id → the named acceptance ref (or None)
    for t in claimed:
        topic = (t.get("title") or "") + " " + (t.get("detail") or "")
        cards[t["id"]] = factory_memory.memory_card_with_ids(store, "developer", topic=topic)
        accept_refs[t["id"]] = (acceptance.extract_test_ref(t.get("spec") or {})
                                if acceptance_exec else None)
        raw = t.get("profile") or ""
        prof = store.get_profile(raw)                    # '' / 'generalist' → synthetic generalist
        if prof is None:                                 # a NAMED profile that no longer exists:
            print(f"[execute] task {t['id']} names unknown profile {raw!r} — failing open to "
                  f"standard tier (never frontier)", flush=True)
            profiles[t["id"]] = {"name": "generalist", "overlay": "",
                                 "model": config.resolve_model("standard")}
        else:
            profiles[t["id"]] = {"name": prof.get("name") or "generalist",
                                 "overlay": prof.get("overlay", ""),
                                 "model": config.resolve_model(prof.get("model", ""))}

    # Real merge-grade (Piece 4): resolve (grade_fn, champion_scores) ON THE MAIN THREAD — in
    # 'smoke' mode this measures the champion baseline once (real clive runs) before dispatch;
    # the DEFAULT ('stub') returns (None, None) cheaply so develop_task keeps _smoke_grade.
    from . import grade as grademod
    grade_fn, champion_scores = grademod.build_grade(store)
    merge_lock = threading.Lock() if real else None  # serialize the shared factory/auto merge
    workers = max(1, min(max_parallel or len(claimed), len(claimed)))
    print(f"[execute] dispatching {len(claimed)} task(s) — up to {workers} super-worker(s) "
          f"in parallel (clone + developer TDD + the target's tests; a few minutes)…", flush=True)

    # Task 3.2 (minimal maker→grader→retry loop): resolve the retry gate AND compute the budget
    # headroom ON THE MAIN THREAD — the workers run in threads that must never touch the single-
    # writer store. retry_budget_ok is brake-honest: a retry is suppressed once the shift's
    # LEDGERED spend has reached its token_budget (composes with Task 0.2's per-shift brake; a
    # token_budget of 0 = unlimited, matching that brake's convention). Computed once, pre-dispatch.
    retry_on = bool(config.resolve_setting(store, "super_worker.retry_on_discard", False)[0])
    retry_budget_ok = False
    if retry_on:
        _budget = int((store.get_shift(shift_id) or {}).get("token_budget") or 0)
        _spent = int(store.shift_spend(shift_id)["tokens"])
        retry_budget_ok = _budget == 0 or _spent < _budget

    def work(task):
        if killswitch.is_halted():                   # STOP tripped before this one started
            return task, {"action": "halted"}
        text = task["title"] + ((": " + task["detail"]) if task.get("detail") else "")
        if task.get("spec") and "SPEC:" not in (task.get("detail") or ""):   # avoid a double-fold:
            from ..reporting import scope_check                              # detail may already
            text += scope_check.spec_brief(task["spec"])                     # carry the spec block
        acc_ref = accept_refs.get(task["id"])
        if acc_ref:                                    # Task 3.1 correction (a): a HARD CONTRACT line —
            text += ("\n\nACCEPTANCE CONTRACT (hard): after your suite passes, the factory will run "
                     f"EXACTLY `{acc_ref}` in your candidate. You MUST create/keep that test at that "
                     f"path and make it pass — a missing or red `{acc_ref}` blocks the merge. This is "
                     "the spec's declared done-condition; a missing file is your non-compliance.")
        prof = profiles[task["id"]]
        print(f"[execute] ▶ {task['id']}: {task['title']}"
              + (f" [{prof['name']}]" if prof["name"] != "generalist" else ""), flush=True)

        def _dispatch(brief):
            try:
                return run(brief, as_user=as_user, claude_bin=claude_bin, real=real,
                           merge_lock=merge_lock, memory=cards[task["id"]][0],
                           profile_overlay=prof["overlay"], model=prof["model"],
                           require_test=require_test, reviewer=reviewer,
                           acceptance_ref=acc_ref, grade_fn=grade_fn,
                           champion_scores=champion_scores,
                           task_ref=f"{task['id']}: {task['title'][:100]}")
            except Exception as e:                    # noqa: BLE001 — contain a dispatch blow-up
                return {"action": "error", "error": str(e)}

        res = _dispatch(text)
        # Task 3.2: ONE informed retry on a gradeable gate-discard. Gate ON + budget headroom
        # (both resolved on the MAIN THREAD above) + STOP still clear + a retry-eligible stage
        # (tests / no_test / acceptance; NEVER frozen). The retry runs a fresh INDEPENDENT
        # attempt off the PRISTINE base (develop_and_merge re-clones) — so the suffix is worded
        # honestly: the prior code is NOT visible, only its failure evidence (operator memory).
        # develop_and_merge's own STOP re-checks (entry + pre-merge) still fire inside _dispatch.
        if (retry_on and retry_budget_ok and not killswitch.is_halted()
                and res.get("action") == "discarded" and res.get("stage") in _RETRY_STAGES):
            evidence = str(res.get("tests_report") or res.get("why") or res.get("error") or "")[:2000]
            suffix = (
                f"\n\nRETRY CONTEXT — a previous INDEPENDENT attempt at this task was discarded at "
                f"stage={res['stage']}; its failure evidence follows — you start from a clean base "
                f"(you cannot see the prior attempt's code, only this evidence). Make the failing "
                f"done-condition pass this time:\n{evidence}")
            res2 = _dispatch(text + suffix)
            for key in ("tokens", "cost", "seconds"):   # sum spend across BOTH attempts into the
                res2[key] = (res.get(key) or 0) + (res2.get(key) or 0)   # SINGLE close-out ledger write
            res2["attempts"] = 2
            return task, res2
        return task, res

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = [f.result() for f in [ex.submit(work, t) for t in claimed]]

    shipped = 0                                       # close out on the MAIN thread (single-writer)
    wave2_ids: list = []                              # Task 5.2: ids decomposed this shift → a bounded 2nd wave
    for task, res in results:
        action = res.get("action")
        has_spend = bool(res.get("tokens") or res.get("cost") or res.get("seconds"))
        # Task 3.1 correction (b): TELEMETRY-FIRST skip count. The acceptance-exec gate wanted to
        # run the spec's named test but the candidate never created it — count the skip (a scope=
        # 'acceptance' factory learning; dedup bumps its `hits` so recurrence = the skip rate) so
        # the operator can decide when to flip to discard-on-missing. NOT a settings key; applies on
        # ANY close-out path (a missing test that then merged still counts). MAIN thread only.
        if res.get("acceptance_skipped"):
            factory_memory.record_learning(
                store, "factory",
                "the acceptance-exec gate found the spec's named acceptance test MISSING in a "
                "candidate — the worker didn't create it at the contracted path; skipped, not yet "
                "discarded (telemetry-first). Author acceptance refs the worker actually creates.",
                scope="acceptance", shift_id=shift_id)
        # Task 1.4 consult-telemetry: attribute the task's OUTCOME to the learnings its
        # worker card surfaced — one batched UPDATE per task, MAIN thread only. A halted
        # run is incomplete (STOP braked it), so it attributes nothing. Fix 1.4b: attribute
        # ONLY when a model actually consulted the card — skip infrastructural failures
        # (_NO_CONSULT_STAGES) and the bare stage-less error from work()'s except handler
        # (a pre-dispatch blow-up). Refusal/timeout/worker_failed DID consume the brief,
        # so they still attribute.
        card_ids = cards.get(task["id"], ("", []))[1]
        consulted = (action != "halted"
                     and res.get("stage") not in _NO_CONSULT_STAGES
                     and not (action == "error" and not res.get("stage")))
        if card_ids and consulted:
            store.bump_learning_outcomes(card_ids, merged=(action == "merged"))
        if action != "halted":                        # a STOP-braked run is incomplete — don't
            for lesson in res.get("learnings") or []: # attribute its emitted learnings as durable
                factory_memory.record_learning(store, "developer", lesson, shift_id=shift_id)
        # Ledger real developer spend on EVERY path that ran the worker — including a mid-round
        # STOP (run_code_round returns 'halted' AFTER the worker + tests ran, carrying spend)
        # and a post-dispatch error. A bare PRE-dispatch halt carries no spend keys, so this
        # stays a no-op there — undercounting exactly when the operator brakes is the bug.
        if action != "halted" or has_spend:
            store.add_budget(f"developer:{task['id']}", int(res.get("tokens") or 0),
                             float(res.get("cost") or 0.0), notes=action or "",
                             shift_id=shift_id, seconds=float(res.get("seconds") or 0.0),
                             profile=profiles[task["id"]]["name"])   # Task 5.4: attribute the profile
        if res.get("review_tokens"):                    # Phase 8: the pre-merge reviewer's own spend
            store.add_budget("reviewer", int(res.get("review_tokens") or 0),
                             float(res.get("review_cost") or 0.0), notes="review", shift_id=shift_id)
        if action == "merged":
            store.set_task_status(task["id"], "done", result=res.get("merge_sha", ""),
                                  shift_id=shift_id)
            shipped += 1
            print(f"[execute]   {task['id']} → merged {res.get('merge_sha', '')[:12]} — SHIPPED", flush=True)
            spec = task.get("spec")                    # GSD #6: spec-fulfillment feedback
            if spec and res.get("changed_paths") is not None:
                from ..reporting import scope_check
                matched, _why = scope_check.spec_fulfillment(spec, res["changed_paths"])
                if not matched:
                    factory_memory.record_learning(
                        store, "factory",
                        "a task delivered changes BEYOND its declared target_surface — size "
                        "target_surface to the real change so the scope check stays accurate",
                        scope="spec_creep", shift_id=shift_id)
        elif action == "halted":                      # STOP — leave in_progress for requeue
            print(f"[execute]   {task['id']} → skipped (STOP)", flush=True)
        else:                                         # no_candidate/discarded/auto_reverted/error
            reason = action or "no result"            # CAPTURE WHY — a bare 'error' is undiagnosable
            if res.get("stage"):
                reason = f"{reason} ({res['stage']})"
            if res.get("error"):
                # a refusal's own words ARE the diagnosis — persist ~300 chars, not 180 (Task 0.1)
                cut = _REFUSAL_REASON_LEN if res.get("stage") == "refusal" else 180
                reason = f"{reason}: {str(res['error'])[:cut]}"
            # Task 0.4 (P6 stage 1): persist the failure EVIDENCE — the full tests_report +
            # the worker's reply head outlive the ≤200-char reason. MAIN thread only, and
            # BEFORE the auto-decompose `continue` below, or decomposed no_candidates lose
            # their evidence forever. Passive write, zero LLM, no gate.
            store.add_task_evidence(task["id"], shift_id=shift_id, action=action or "",
                                    stage=str(res.get("stage") or ""),
                                    tests_report=str(res.get("tests_report") or ""),
                                    reply_head=str(res.get("reply_head") or ""))
            # Task 2.3 slice 2 (scope-check calibration): a no_candidate whose task carries a spec
            # means the scope check PASSED this brief (it attached/kept the spec) — yet the worker
            # came back empty-handed, so the judge under-rejected. Mirror of the merged-side spec-
            # creep feedback, on the failure side. scope='scope_check' learning (a counter/ledger
            # note, NEVER a settings key). Recorded BEFORE the decompose `continue` so a decomposed
            # no_candidate still scores the miss.
            if action == "no_candidate" and task.get("spec"):
                factory_memory.record_learning(
                    store, "factory",
                    "a task the scope check PASSED (spec attached) still returned no_candidate — "
                    "the scope judge under-rejected; split/reject briefs this broad before a "
                    "worker is spent.",
                    scope="scope_check", shift_id=shift_id)
            # Task 2.3 slice 1 (reviewer calibration): the blind reviewer APPROVED a candidate that
            # the correctness gate then AUTO-REVERTED — a reviewer MISS. scope='reviewer_calibration'
            # learning (counter/ledger note, never a settings key), so the reviewer's own calibration
            # is scorable. Only a reviewer-approved verdict scores — fail-open (no verdict) does not.
            review = res.get("review")
            if action == "auto_reverted" and review and review.get("approved"):
                factory_memory.record_learning(
                    store, "factory",
                    "the blind pre-merge reviewer APPROVED a candidate that then AUTO-REVERTED on "
                    "the correctness gate — a reviewer miss; the review isn't catching regressions "
                    "the target's own tests catch.",
                    scope="reviewer_calibration", shift_id=shift_id)
            sub_ids: list = []                        # GSD #4: turn no_candidate into forward progress
            # Task 0.1: error(timeout)/error(worker_failed) stay decompose-eligible — both are
            # "task too big" evidence; transport/refusal never reach the decomposer (pure spend).
            decompose_ok = action == "no_candidate" or (
                action == "error" and res.get("stage") in _DECOMPOSE_STAGES)
            if decompose_ok and decomposer is not None:
                from ..reporting import scope_check
                sub_ids = scope_check.decompose_no_candidate(
                    store, task, shift_id=shift_id, decomposer=decomposer)
            if sub_ids:
                wave2_ids.extend(sub_ids)             # Task 5.2: gather for a bounded second wave
                n_sub = len(sub_ids)
                label = f"{action} ({res['stage']})" if res.get("stage") else action
                store.set_task_status(
                    task["id"], "blocked",
                    result=f"{label} → decomposed into {n_sub} sub-tasks"[:200],
                    shift_id=shift_id)
                print(f"[execute]   {task['id']} → {label}, auto-decomposed into "
                      f"{n_sub} sub-task(s) — blocked", flush=True)
                continue                              # decomposition replaces the canned lesson
            store.set_task_status(task["id"], "blocked", result=reason, shift_id=shift_id)
            print(f"[execute]   {task['id']} → {reason} — blocked", flush=True)
            fl = factory_memory.lesson_for_block(action, res.get("stage", ""))  # stage-aware lesson
            if fl:
                factory_memory.record_learning(store, "factory", fl, scope="blocked",
                                               shift_id=shift_id)

    # Task 4.1 (P6 stages 2-3): AFTER close-out (evidence now persisted), a config-gated
    # post-shift investigator distills a case-specific lesson for up to 3 blocked-this-shift
    # tasks. All spend is STOP-vetoed FIRST, standard-tier, capped and ledgered inside the
    # function. Wrapped fail-open so an investigator blow-up never crashes the shift close-out.
    if investigate_blocked:
        try:
            factory_memory.investigate_blocked(store, shift_id)
        except Exception as e:  # noqa: BLE001 — advisory memory refinement; never fatal to close-out
            print(f"[execute] investigator skipped (non-fatal): {e}", flush=True)

    # Task 5.2 (P3, loop-until-dry): a BOUNDED second wave. Gate super_worker.dispatch_waves
    # (1 = today; 2 = one more pass). Only from wave 1 — decomposer is not None here, and the
    # wave-2 recursion runs with decomposer=None, a HARD recursion stop (there is never a wave 3).
    # Every brake is re-checked on the MAIN THREAD before claiming: STOP clear, dispatch_waves≥2,
    # headroom under max_tasks_per_shift, shift spend < token_budget (composes with Task 0.2), AND
    # an EXPLICIT TIME GUARD — the executor has NO wall-clock (only the per-worker 1800s timeouts)
    # and the loop deadline is checked only BETWEEN shifts, so wave 2 must reserve its own deadline
    # headroom (elapsed + waves×worker_timeout ≤ the loop-deadline share) or it can silently overrun
    # the loop deadline. Missing time inputs → fail CLOSED (a brake never fails toward more autonomy).
    if wave2_ids and decomposer is not None and not killswitch.is_halted():
        waves = int(config.resolve_setting(store, "super_worker.dispatch_waves", 1)[0])
        if waves >= 2:
            remaining = (max_tasks - len(claimed)) if max_tasks is not None else len(wave2_ids)
            budget = int((store.get_shift(shift_id) or {}).get("token_budget") or 0)
            spent = int(store.shift_spend(shift_id)["tokens"])
            budget_ok = budget == 0 or spent < budget
            started = shift_started if shift_started is not None else time.monotonic()
            elapsed = time.monotonic() - started
            time_ok = (loop_deadline_s is not None
                       and elapsed + waves * _WORKER_TIMEOUT_S <= loop_deadline_s)
            if remaining > 0 and budget_ok and time_ok:
                wave2 = wave2_ids[:remaining]         # respect the per-shift fan-out cap across waves
                for tid in wave2:                     # claim the new sub-task ids for THIS shift
                    store.set_task_status(tid, "in_progress", shift_id=shift_id)
                print(f"[execute] second wave: {len(wave2)} decomposed sub-task(s) claimed — one "
                      f"more pass (decomposer OFF, hard recursion stop).", flush=True)
                shipped += execute_claimed_tasks(
                    store, shift_id, as_user=as_user, claude_bin=claude_bin, real=real,
                    develop_fn=develop_fn, max_tasks=remaining, max_parallel=max_parallel,
                    scope_judge=scope_judge, decomposer=None,   # HARD recursion stop — no wave 3
                    require_test=require_test, reviewer=reviewer, acceptance_exec=acceptance_exec,
                    investigate_blocked=investigate_blocked,
                    shift_started=shift_started, loop_deadline_s=loop_deadline_s)
    return shipped


_REVIEW_DIFF_LIMIT = 20000   # the reviewer sees at most this many diff chars (a blind read, bounded)


def _review_candidate(dev_clone: str, base: str, branch: str, task: str) -> tuple:
    """Phase 8 pre-merge review (config-gated): an ISOLATED, blind claude_p reads
    `git diff base..branch` + the task and returns {approve, reason}. Runs at super_worker
    .reviewer_tier ('' = frontier — review is judgment; the config.yaml rationale), resolved via
    resolve_model (fails open DOWNWARD, never up on a typo). FAIL-OPEN — a transport OR parse
    failure returns (None, spend) so a reviewer hiccup never blocks a merge. Returns
    (verdict|None, review_spend)."""
    from ..roles.common import claude_p, _first_json_object, _load_prompt
    try:
        full = subprocess.run(["git", "-C", dev_clone, "diff", f"{base}..{branch}"],
                              capture_output=True, text=True, timeout=30).stdout
    except Exception:  # noqa: BLE001 — no diff (e.g. not a git clone in a test) → review empty
        full = ""
    diff = full[:_REVIEW_DIFF_LIMIT]
    if len(full) > _REVIEW_DIFF_LIMIT:                 # the reviewer must KNOW it graded a PARTIAL
        diff += (f"\n\n[diff truncated at {_REVIEW_DIFF_LIMIT:,} of {len(full):,} chars]")
    # {SPEC} is a fixed NOTE, not a separate thread: the task's spec is ALREADY folded into {TASK}
    # (execute_claimed_tasks appends scope_check.spec_brief to the task text before dispatch). Re-
    # threading it here would only duplicate it — do NOT re-plumb the spec to the reviewer (critique).
    prompt = (_load_prompt("reviewer").replace("{TASK}", task)
              .replace("{SPEC}", "(folded into the task above)").replace("{DIFF}", diff))
    tier = ((config.load_config().get("super_worker") or {}).get("reviewer_tier") or "")
    text, tok, cost = claude_p(prompt, timeout=180, model=config.resolve_model(tier))
    spend = {"review_tokens": int(tok or 0), "review_cost": float(cost or 0.0)}
    raw = _first_json_object(text or "")
    if not raw:
        return None, spend                              # unparseable → fail-open (approve-by-default)
    try:
        obj = json.loads(raw)
        # FAIL-OPEN: only an EXPLICIT boolean false rejects. A missing/null/misspelled approve key
        # (a reviewer output hiccup) must NOT block a legitimate merge.
        approved = obj.get("approve", True) is not False
        return {"approve": approved, "reason": str(obj.get("reason", ""))}, spend
    except Exception:  # noqa: BLE001
        return None, spend


def develop_and_merge(*, adapter, main_repo: str, task: str, champion_scores: dict,
                      grade_fn: Callable[[str], dict], as_user: Optional[str] = None,
                      claude_bin: str = "claude", label: Optional[str] = None,
                      merge_lock=None, memory: str = "",
                      profile_overlay: str = "", model: str = "",
                      require_test: Optional[bool] = None, reviewer: bool = False,
                      acceptance_ref: Optional[str] = None, task_ref: str = "") -> dict:
    """Run one develop→grade→auto-merge turn. Returns the round result dict (or
    {action: "no_candidate"} if the worker produced no change, "halted" if the brake is
    on). Never leaves clones behind. `merge_lock`, when given, serializes the
    SHARED-worktree section (fetch + worktree + grade + merge) so parallel workers in REAL
    mode don't race on the one factory/auto worktree — the slow clone+develop runs unlocked.
    `task_ref` rides into the merge commit as a Factory-Task trailer so provenance survives
    without the blackboard."""
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

        t0 = time.monotonic()
        dev = develop_candidate(dev_clone, task=task, branch=branch,
                          test_cmd=" ".join(adapter.test_command()),
                          frozen=adapter.frozen_paths(), as_user=as_user,
                          claude_bin=claude_bin, memory=memory,
                          profile_overlay=profile_overlay, model=model)
        # Developer spend rides out on EVERY post-dispatch result path (Task 0.2) — the rail
        # ledgers it (Task 0.3). Previously dropped: nothing reached budget_ledger.
        spend = {"tokens": int(dev.get("tokens") or 0), "cost": float(dev.get("cost") or 0.0),
                 "seconds": round(time.monotonic() - t0, 1)}
        # The developer can't write the factory DB from its clone (Guest-House user in prod),
        # so it emits learnings in its reply; carry them out for the MAIN thread to record.
        from ..reporting import factory_memory
        learnings = factory_memory.parse_learnings(dev.get("reply", ""))
        # Task 0.4 (P6 stage 1): the reply head rides out on every post-worker path so the
        # close-out can persist failure EVIDENCE (task_evidence) — not just a reason string.
        evidence = {"learnings": learnings, "reply_head": (dev.get("reply") or "")[:2000]}

        if not adapter.branch_exists(dev_clone, branch):   # worker crashed / committed nothing →
            err = classify_empty_handed(dev.get("reply", ""))   # Task 0.1: timeout/rc/transport/
            if err:                                             # refusal must NOT collapse into
                return {**err, "branch": branch, **evidence, **spend}          # no_candidate
            return {"action": "no_candidate", "branch": branch, **evidence, **spend}  # NO branch
        try:
            changed = adapter.changed_paths(dev_clone, base, branch)
        except subprocess.CalledProcessError:          # 2nd exit-128 site: branch exists but the diff
            return {"action": "no_candidate", "branch": branch, **evidence, **spend}  # unresolvable
        if not changed:                                # the worker made no committed change
            return {"action": "no_candidate", "branch": branch, **evidence, **spend}

        review_spend = {}
        review_verdict = None
        if reviewer:                                   # Phase 8: config-gated pre-merge review gate
            verdict, review_spend = _review_candidate(dev_clone, base, branch, task)
            if verdict is not None:                    # Task 2.3: carry the verdict out so the rail
                review_verdict = {"approved": bool(verdict["approve"]),   # can score the reviewer's
                                  "reason": str(verdict.get("reason", ""))}  # calibration at close-out
            if verdict is not None and not verdict["approve"]:      # an EXPLICIT reject discards it
                return {"action": "discarded", "stage": "review",
                        "error": ("review: " + (verdict["reason"] or "rejected"))[:180],
                        "review": review_verdict,
                        **evidence, **spend, **review_spend}
            # approve, OR fail-open (verdict is None on a transport/parse failure) → proceed to merge

        # The shared-worktree section mutates main_repo; in REAL mode main_repo is the ONE
        # factory/auto worktree shared by parallel workers, so serialize it under merge_lock.
        # The slow part (clone + the developer worker) already ran in parallel above.
        with (merge_lock or contextlib.nullcontext()):
            cand_wt = os.path.join(work, "wt")
            try:
                adapter.fetch_candidate(main_repo, dev_clone, branch)
                adapter.add_worktree(main_repo, cand_wt, branch)
                # GSD spec-bound acceptance gate. Threaded from the run entry (Task 6.1) so a
                # store override can retune it; None = fall back to config.yaml (unchanged default).
                rt = require_test if require_test is not None else bool(
                    (config.load_config().get("super_worker", {}) or {}).get("require_test", False))
                res = code_round.run_code_round(
                    adapter=adapter, main_repo=main_repo, cand_repo=cand_wt, branch=branch,
                    champion_scores=champion_scores, grade_fn=grade_fn,
                    changed_paths=changed, label=branch, task_ref=task_ref, require_test=rt,
                    acceptance_ref=acceptance_ref)   # Task 3.1: run the spec's named test in the candidate
                res.update(evidence)                  # learnings + reply_head (Task 0.4);
                res["changed_paths"] = changed        # for the spec-fulfillment check (GSD #6)
                res.update(spend)                     # developer tokens/cost/seconds (Task 0.2)
                res.update(review_spend)              # the pre-merge reviewer's own spend (Phase 8)
                if review_verdict is not None:        # Task 2.3: the reviewer's verdict rides out
                    res["review"] = review_verdict    # on the round result (merged/auto_reverted/…)
                return res
            except Exception as e:  # noqa: BLE001 — a fetch/worktree/grade blow-up AFTER the
                # worker ran still spent it; carry the spend out so the rail ledgers it (Task 0.2)
                return {"action": "error", "stage": "merge", "error": str(e)[:180],
                        **evidence, **spend, **review_spend}
            finally:
                try:
                    adapter.remove_worktree(main_repo, cand_wt)
                except Exception:  # noqa: BLE001
                    pass
    finally:
        shutil.rmtree(work, ignore_errors=True)
