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

from ..common import killswitch


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
        store.end_shift(sh, status="timed_out", resume_note="conductor exceeded wall-clock")
        return {"action": "timed_out", "shift_id": sh, "reaped": len(reaped), "shipped": 0}
    except Exception as e:                           # noqa: BLE001 — contain a conductor blow-up
        store.requeue_shift_tasks(sh)
        store.end_shift(sh, status="error", resume_note=f"conductor error: {e}")
        return {"action": "error", "shift_id": sh, "reaped": len(reaped), "shipped": 0}

    # EXECUTE the tasks the conductor claimed — deterministically, here, not via the
    # conductor's Bash (which would background + orphan the long dispatch in a headless -p).
    shipped = 0
    if executor is not None and not killswitch.is_halted():
        try:
            shipped = executor(store, shift_id=sh) or 0
        except Exception:  # noqa: BLE001 — a dispatch failure mustn't sink the shift record
            shipped = 0

    # A STOP that tripped DURING the shift overrides the conductor's own status.
    status = "halted" if killswitch.is_halted() else outcome.get("status", "completed")
    # Requeue anything STILL in-flight (the executor closes what it ran; this rescues a task
    # the conductor claimed but the executor didn't reach / a crash left dangling).
    store.requeue_shift_tasks(sh)
    store.end_shift(sh, status=status, report=outcome.get("report", ""),
                    resume_note=outcome.get("resume_note", ""),
                    tokens_used=outcome.get("tokens_used", 0))
    return {"action": status, "shift_id": sh, "reaped": len(reaped), "shipped": shipped,
            "tokens_used": outcome.get("tokens_used", 0)}   # for the loop's cumulative ceiling
