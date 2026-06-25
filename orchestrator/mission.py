"""Mission-check + steady-state (design: docs/plans/2026-06-25-conductor-loop-design.md,
step 5).

Deterministic — NO LLM. `assess` classifies the mission's progress from store signals
and records it on the timeline, and recommends stopping only after K consecutive steady
shifts. It NEVER asserts 'reached': declaring the mission accomplished is subjective and
stays a human call (the design's "converge to steady-state and surface, don't confidently
declare done"). The mission — not an empty queue — is the terminator: `assess` only
*recommends* a stop (research dry + backlog empty + nothing shipped for K shifts), which
the harness/conductor surfaces as "nothing left, awaiting mission revision."
"""
from __future__ import annotations


def assess(store, *, shift_id: int, shipped_count: int = 0, plateau_k: int = 3) -> dict:
    """Classify + record this shift's mission status. Returns
    {status, recommend_stop, rationale, metrics}. status ∈ {advancing, blocked, steady_state}."""
    open_backlog = len(store.list_tasks(status="open"))
    blocked = len(store.list_tasks(status="blocked"))

    if shipped_count > 0 or open_backlog > 0:
        status = "advancing"
        rationale = f"{shipped_count} shipped this shift, {open_backlog} open in the backlog"
    elif blocked > 0:
        status = "blocked"
        rationale = f"{blocked} task(s) blocked, nothing open to pick up"
    else:
        status = "steady_state"
        rationale = "backlog empty, nothing blocked, nothing shipped"

    metrics = {"backlog": open_backlog, "blocked": blocked, "shipped": shipped_count}
    store.record_mission_status(shift_id=shift_id, status=status, rationale=rationale,
                                metrics=metrics)

    # Recommend a stop only when the last K statuses (this one included) are ALL steady —
    # a single quiet shift isn't enough (the tail is where late work hides).
    recent = store.mission_status_history(plateau_k)
    recommend_stop = (status == "steady_state" and len(recent) >= plateau_k
                      and all(r["status"] == "steady_state" for r in recent))

    return {"status": status, "recommend_stop": recommend_stop,
            "rationale": rationale, "metrics": metrics}
