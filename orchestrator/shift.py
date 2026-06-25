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


def run_shift(store, *, token_budget: int, conductor: Callable, mission: Optional[str] = None,
              wall_clock_s: int = 1800) -> dict:
    """Run one bounded conductor shift. Returns {action, shift_id, reaped}, where action
    ∈ {halted, no_mission, completed, timed_out, error, ...}. Always leaves the store in a
    clean state: a crashed shift is reaped first, and whatever happens to the conductor,
    the shift row is closed with a status + resume note (never left dangling)."""
    reaped = store.reap_orphaned_shifts()          # crash recovery FIRST — before anything new

    if killswitch.is_halted():                     # the brake: don't even start
        return {"action": "halted", "shift_id": None, "reaped": len(reaped)}

    if mission and not store.active_mission():
        store.set_mission(mission)
    m = store.active_mission()
    if not m:                                       # nothing to steer toward
        return {"action": "no_mission", "shift_id": None, "reaped": len(reaped)}

    sh = store.start_shift(token_budget=token_budget, mission_id=m["id"])
    try:
        outcome = conductor(store, shift_id=sh, mission=m,
                            token_budget=token_budget, wall_clock_s=wall_clock_s) or {}
    except TimeoutError:                            # ceiling: wall-clock — killed from outside
        store.requeue_shift_tasks(sh)               # return claimed work to the backlog
        store.end_shift(sh, status="timed_out", resume_note="conductor exceeded wall-clock")
        return {"action": "timed_out", "shift_id": sh, "reaped": len(reaped)}
    except Exception as e:                           # noqa: BLE001 — contain a conductor blow-up
        store.requeue_shift_tasks(sh)
        store.end_shift(sh, status="error", resume_note=f"conductor error: {e}")
        return {"action": "error", "shift_id": sh, "reaped": len(reaped)}

    # A STOP that tripped DURING the shift overrides the conductor's own status.
    status = "halted" if killswitch.is_halted() else outcome.get("status", "completed")
    # ALWAYS requeue work still in-flight at shift end — in this loop a task should be
    # done/blocked/open by the time the conductor stops; anything left claimed/in_progress
    # means it wasn't closed (a backgrounded dispatch, a bug, a kill), so a 'completed'
    # shift would otherwise STRAND it (reap only rescues 'running' shifts).
    store.requeue_shift_tasks(sh)
    store.end_shift(sh, status=status, report=outcome.get("report", ""),
                    resume_note=outcome.get("resume_note", ""),
                    tokens_used=outcome.get("tokens_used", 0))
    return {"action": status, "shift_id": sh, "reaped": len(reaped)}
