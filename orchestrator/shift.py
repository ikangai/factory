"""The bounded-shift harness (design: docs/plans/2026-06-25-conductor-loop-design.md,
step 2).

Deterministic rail — NO LLM here. Each call to `run_shift` is one bounded shift:
reap any crashed shift → resolve the active mission → start ONE shift row → run the
conductor under ceilings the harness enforces FROM OUTSIDE → record the outcome + resume
note so the next shift resumes. The conductor is INJECTED (live = the claude conductor,
step 3); here it is a plain callable, so the harness is fully testable without an agent.
"""
from __future__ import annotations

from typing import Callable, Optional

from ..common import config, killswitch


def run_shift(store, *, token_budget: int, conductor: Callable, executor: Optional[Callable] = None,
              refill: Optional[Callable] = None, refill_threshold: int = 2,
              mission: Optional[str] = None, wall_clock_s: int = 1800) -> dict:
    """Run one bounded conductor shift. The conductor PLANS (orients, claims the tasks to
    work); then the `executor` (deterministic, no LLM-driven Bash) runs each claimed task
    through the gated pipeline and closes it — keeping the long-running, backgroundable
    dispatch OUT of the headless conductor's hands. Returns {action, shift_id, reaped,
    shipped}. Always leaves the store clean: a crashed shift is reaped first, and the shift
    row is always closed."""
    reaped = store.reap_orphaned_shifts()          # crash recovery FIRST — before anything new
    store.reap_orphaned_approvals()                # + push approvals stranded 'executing' by a
                                                   #   crash between claim and resolve (Fix 4d)

    if killswitch.is_halted():                     # the brake: don't even start
        return {"action": "halted", "shift_id": None, "reaped": len(reaped), "shipped": 0}

    if mission and not store.active_mission():
        store.set_mission(mission)
    m = store.active_mission()
    if not m:                                       # nothing to steer toward
        return {"action": "no_mission", "shift_id": None, "reaped": len(reaped), "shipped": 0}

    sh = store.start_shift(token_budget=token_budget, mission_id=m["id"])

    # Top up the backlog from research when it's THIN — the generative loop runs on the
    # RAIL, deterministically, not at the conductor's discretion (which left research dry).
    # Bounded by the idle short-circuit upstream: once converged, cmd_run never starts a
    # shift, so this won't spin a researcher forever. refill_threshold ≤ 0 disables it.
    if refill is not None and len(store.list_tasks(status="open")) < refill_threshold:
        try:
            refill(store)
        except Exception:  # noqa: BLE001 — a researcher failure mustn't sink the shift
            pass

    try:
        outcome = conductor(store, shift_id=sh, mission=m,
                            token_budget=token_budget, wall_clock_s=wall_clock_s) or {}
    except TimeoutError:                            # ceiling: wall-clock — killed from outside
        store.requeue_shift_tasks(sh)               # return claimed work to the backlog
        # Spend already ledgered this shift (the refill/conductor rows) must reach the loop
        # brake even on an abnormal end — else the token ceiling under-counts on crash/timeout.
        spent = int(store.shift_spend(sh)["tokens"])
        store.end_shift(sh, status="timed_out", resume_note="conductor exceeded wall-clock",
                        tokens_used=spent)
        return {"action": "timed_out", "shift_id": sh, "reaped": len(reaped), "shipped": 0,
                "tokens_used": spent}
    except Exception as e:                           # noqa: BLE001 — contain a conductor blow-up
        store.requeue_shift_tasks(sh)
        spent = int(store.shift_spend(sh)["tokens"])
        store.end_shift(sh, status="error", resume_note=f"conductor error: {e}",
                        tokens_used=spent)
        return {"action": "error", "shift_id": sh, "reaped": len(reaped), "shipped": 0,
                "tokens_used": spent}

    # THE PER-SHIFT TOKEN BRAKE (Task 0.2): budget_exhausted was schema-legal but nothing
    # enforced it — a decorative brake. Check the LEDGERED spend after the conductor plans,
    # BEFORE the executor dispatches (the workers are the expensive part). token_budget == 0
    # means unlimited (the loop_token_budget convention). The knob defaults ON and lives in
    # config.yaml ONLY — a brake must not be board-toggleable, so it is NOT in SETTINGS_SPEC.
    enforce = bool((config.load_config().get("autonomy") or {}).get("enforce_shift_budget", True))
    spent = int(store.shift_spend(sh)["tokens"])
    budget_hit = enforce and token_budget > 0 and spent >= token_budget

    # EXECUTE the tasks the conductor claimed — deterministically, here, not via the
    # conductor's Bash (which would background + orphan the long dispatch in a headless -p).
    shipped = 0
    if executor is not None and not budget_hit and not killswitch.is_halted():
        try:
            shipped = executor(store, shift_id=sh) or 0
        except Exception:  # noqa: BLE001 — a dispatch failure mustn't sink the shift record
            shipped = 0

    # A STOP that tripped DURING the shift overrides everything, including the budget brake.
    status = ("halted" if killswitch.is_halted()
              else "budget_exhausted" if budget_hit
              else outcome.get("status", "completed"))
    # The budget note is APPENDED to the conductor's own resume note, never replacing it —
    # the next shift's {RESUME} seam needs both the plan context AND the brake reason.
    resume_note = outcome.get("resume_note", "")
    if budget_hit:
        note = (f"budget exhausted: spent {spent} of {token_budget} tokens before dispatch — "
                f"executor skipped, claimed tasks requeued")
        resume_note = f"{resume_note}\n{note}" if resume_note else note
    # Requeue anything STILL in-flight (the executor closes what it ran; this rescues a task
    # the conductor claimed but the executor didn't reach / a crash left dangling).
    store.requeue_shift_tasks(sh)
    # tokens_used = the honest full shift spend (conductor + workers + aux roles) from the
    # ledger, not the conductor's self-report alone (Task 0.6). max() keeps the old behavior
    # when nothing is ledgered (hermetic tests). NOTE: the loop's cumulative token_budget
    # ceiling now counts worker spend too, so cmd_run_loop's brake trips sooner — by design.
    ledgered = store.shift_spend(sh)["tokens"]
    tokens_total = max(int(outcome.get("tokens_used", 0)), int(ledgered))
    store.end_shift(sh, status=status, report=outcome.get("report", ""),
                    resume_note=resume_note,
                    tokens_used=tokens_total)
    return {"action": status, "shift_id": sh, "reaped": len(reaped), "shipped": shipped,
            "tokens_used": tokens_total}   # for the loop's cumulative ceiling
