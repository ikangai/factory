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
              org_planner: Optional[Callable] = None, harness_planner: Optional[Callable] = None,
              mission: Optional[str] = None, wall_clock_s: int = 1800) -> dict:
    """Run one bounded conductor shift. The conductor PLANS (orients, claims the tasks to
    work); then the `executor` (deterministic, no LLM-driven Bash) runs each claimed task
    through the gated pipeline and closes it — keeping the long-running, backgroundable
    dispatch OUT of the headless conductor's hands. Returns {action, shift_id, reaped,
    shipped}. Always leaves the store clean: a crashed shift is reaped first, and the shift
    row is always closed.

    `org_planner` (self-organizing factory, Task 2.2): the shift-start trigger
    (`orchestrator.org.maybe_plan_org`) — injected exactly like `executor`/`refill` so
    every EXISTING run_shift test (none of which pass it) stays byte-identical. `None`
    (the default) is a pure no-op.

    `harness_planner` (self-harness loop, adversarial-review fix round 2026-08-05 —
    moved here FROM a post-run_shift hook in orchestrator.cmd_run): the shift-END trigger
    (`orchestrator.harness.maybe_plan_harness`), injected exactly like `org_planner`.
    Invoked near the BOTTOM of this function, after outcomes are recorded and BEFORE the
    tokens_used ledger rollup — see the call site's own comment for why a post-run_shift
    hook was wrong on three counts (spend visibility to the loop brake, firing after a
    tripped brake, and no clean injection seam for tests). `None` (the default) is a pure
    no-op, so every EXISTING run_shift test stays byte-identical."""
    reaped = store.reap_orphaned_shifts()          # crash recovery FIRST — before anything new
    # Publication broker (Component D): a broker-armed approval legitimately SITS
    # 'executing' for up to autonomy.envelope_ttl_hours while the operator's broker is
    # offline/asleep — the default 1h orphan floor would mislabel that in-flight envelope
    # "crashed". Widen the floor past the envelope's own expiry (+1h grace) ONLY when the
    # broker is armed; OFF (the default) calls reap_orphaned_approvals exactly as before —
    # byte-identical to the pre-broker behavior.
    auton = config.load_config().get("autonomy", {}) or {}
    if auton.get("publication_broker", False):
        ttl_h = float(auton.get("envelope_ttl_hours", 24) or 24)
        store.reap_orphaned_approvals(max_age_hours=ttl_h + 1.0)
        try:                                        # receipt ingestion: never sinks the shift
            from ..reporting import approvals
            ingested = approvals.ingest_broker_receipts(store)
            if ingested:
                print(f"[broker] ingested {len(ingested)} receipt(s) at shift start", flush=True)
        except Exception as e:  # noqa: BLE001 — a spool/store hiccup must not block the shift
            print(f"[broker] receipt ingestion error (non-fatal): {e}", flush=True)
    else:
        store.reap_orphaned_approvals()             # + push approvals stranded 'executing' by a
                                                     #   crash between claim and resolve (Fix 4d)

    if killswitch.is_halted():                     # the brake: don't even start
        return {"action": "halted", "shift_id": None, "reaped": len(reaped), "shipped": 0}

    if mission and not store.active_mission():
        store.set_mission(mission)
    m = store.active_mission()
    if not m:                                       # nothing to steer toward
        return {"action": "no_mission", "shift_id": None, "reaped": len(reaped), "shipped": 0}

    sh = store.start_shift(token_budget=token_budget, mission_id=m["id"])

    # Claim leases (Component F, docs/plans/2026-08-06-publication-broker-design.md): a
    # task orphaned OUTSIDE a shift (no shift_id) — or by a shift that crashed and never
    # restarted — stays 'claimed'/'in_progress' forever; reap_orphaned_shifts only rescues
    # a shift's own in-flight tasks while that shift row is still 'running'. keep_shift_id
    # = THIS shift (just started above) so its own fresh claims are never mistaken for a
    # stale lease. super_worker.claim_lease_minutes is a board-editable capacity knob
    # (SETTINGS_SPEC, NOT frozen — see harness_surface.py's own comment on why).
    lease_minutes, _ = config.resolve_setting(store, "super_worker.claim_lease_minutes", 240)
    reclaimed_leases = store.reap_expired_task_leases(int(lease_minutes), keep_shift_id=sh)
    if reclaimed_leases:
        print(f"[leases] reclaimed {len(reclaimed_leases)} expired claim(s): "
              f"{', '.join(reclaimed_leases)}", flush=True)

    # Self-organizing factory (design: docs/plans/2026-07-09-self-organizing-factory-
    # design.md §3; impl Task 2.2): the shift-start / mission-change trigger. Placed HERE —
    # main thread, right after the STOP check + shift start, before anything below reads a
    # knob — so a chart planned THIS call already applies to this same shift's dispatch
    # (maybe_plan_org itself re-checks STOP and no-ops once a chart already exists for the
    # mission, so the frontier call happens at most once per mission). Fail-open: an
    # organizer blow-up is real judgment work, not a brake — it must never sink the shift
    # it was trying to improve (mirrors the `refill` fail-open just below).
    if org_planner is not None:
        try:
            org_planner(store, shift_id=sh)
        except Exception as e:  # noqa: BLE001 — an organizer failure mustn't sink the shift…
            # …but Fix 3c (self-organizing-factory adversarial review) says it must never
            # be SILENT either: a printed line + a durable factory learning, mirroring
            # every other advisory-role blow-up in this file (refill just below) and in
            # develop.py's own investigator guard.
            print(f"[org] planner error (non-fatal — the shift continues): {e}", flush=True)
            try:
                from ..reporting import factory_memory
                factory_memory.record_learning(
                    store, "factory", f"the org planner blew up mid-shift: {e}"[:500],
                    scope="organizer", shift_id=sh)
            except Exception:  # noqa: BLE001 — the learning write itself must never sink the shift
                pass

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
    if reclaimed_leases:                                # close-out visibility (Component F)
        note = f"reclaimed {len(reclaimed_leases)} expired claim(s)"
        resume_note = f"{resume_note}\n{note}" if resume_note else note

    # Self-harness loop (design: docs/plans/2026-08-05-self-harness-loop-design.md,
    # Component E; adversarial-review fix round, 2026-08-05): the shift-END trigger,
    # placed HERE — AFTER outcomes are recorded (the executor above already wrote this
    # shift's routing_outcomes/task_evidence) and BEFORE the tokens_used ledger rollup
    # below, so the harness engineer's own ledgered spend (store.add_budget inside
    # maybe_plan_harness/plan_harness) is INCLUDED in `ledgered`/`tokens_total` — a
    # post-run_shift hook (the ORIGINAL placement, in cmd_run) landed its spend too LATE
    # for the unattended loop's cumulative token brake (cmd_run_loop's loop_token_budget)
    # to ever see it, and left shifts.tokens_used disagreeing with shift_spend(shift_id).
    # Skipped entirely when the shift ended budget_exhausted/timed_out/halted — a tripped
    # brake must not spend MORE frontier tokens trying to improve the very loop that
    # tripped it ('timed_out'/'error' shifts never reach this point at all: both are
    # early returns above; 'timed_out'/'halted' are named explicitly anyway for a reader
    # who doesn't want to trace the early-return paths). Fail-open + loud, mirroring
    # org_planner's own failure posture.
    if harness_planner is not None and status not in ("budget_exhausted", "timed_out", "halted"):
        try:
            harness_result = harness_planner(store, shift_id=sh)
            if harness_result:
                print(f"[harness] proposed {len(harness_result)} proposal(s) this shift — "
                      f"review with `factory harness show`")
        except Exception as e:  # noqa: BLE001 — the harness engineer's own failure mustn't
            print(f"[harness] planner error (non-fatal — the shift continues): {e}",  # sink the shift
                 flush=True)
            try:
                from ..reporting import factory_memory
                factory_memory.record_learning(
                    store, "factory", f"the harness engineer blew up mid-shift: {e}"[:500],
                    scope="harness_engineer", shift_id=sh)
            except Exception:  # noqa: BLE001 — the learning write itself must never sink the shift
                pass

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
