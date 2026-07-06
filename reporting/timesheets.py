"""Agent timesheets — who worked when, for how long, at what spend, to what verdict.

Pure reads over budget_ledger (+ tasks for titles). The rail writes the rows (Phase 0);
this module only shapes them for the CLI, the board and EVM. SQL lives in the store's
ledger_rows()/ledger_by_role() (the CRUD layer) — no duplicated SQL here.

Scope asymmetry (intended): timesheet() shows conductor-loop engagements only (shift_id IS
NOT NULL) while by_agent() rolls up the WHOLE ledger incl. legacy old-loop rows — label the
rollup 'all-time (incl. legacy)' wherever it renders so the two views don't look contradictory.
"""
from __future__ import annotations

from datetime import datetime

_ISO = "%Y-%m-%dT%H:%M:%S.%fZ"   # common.store.now_iso() format


def duration_seconds(started_at, ended_at):
    """The canonical CLOCKTIME metric — wall-clock seconds between two store.now_iso() timestamps.
    Returns None when either is missing/unparseable (a still-running or crashed shift has no
    ended_at); clamped at 0 so a clock skew never yields a negative. This is the time counterpart
    of per-shift token spend, derived from the started_at/ended_at the shifts table already records.
    Metric only — no brake (a shift is never killed on wall-clock)."""
    try:
        a = datetime.strptime(started_at or "", _ISO)
        b = datetime.strptime(ended_at or "", _ISO)
    except (ValueError, TypeError):
        return None
    return max(0.0, (b - a).total_seconds())


def shift_clock(store, limit: int = 50) -> list[dict]:
    """Per-shift wall-clock duration (started_at → ended_at), newest first (mirrors list_shifts) —
    the time counterpart of per-shift token spend. Each row: {shift, status, started_at, ended_at,
    seconds, running}. seconds is None for a shift with no ended_at (still running or crashed),
    flagged running=True. Pure read over the shifts table; metric only, no brake."""
    out = []
    for s in store.list_shifts(limit=limit):
        secs = duration_seconds(s.get("started_at"), s.get("ended_at"))
        out.append({"shift": s["id"], "status": s["status"],
                    "started_at": s.get("started_at"), "ended_at": s.get("ended_at"),
                    "seconds": secs, "running": secs is None})
    return out


def timesheet(store, limit: int = 200, shift_id: int | None = None) -> list[dict]:
    """Shift-attributed engagements, newest first. developer:<task> rows carry the task title.
    `shift_id` filters to one shift in the query (so it survives the LIMIT, not after it)."""
    out = []
    for r in store.ledger_rows(limit, shift_id=shift_id):
        role, _, ref = r["role_or_run"].partition(":")
        task = store.get_task(ref) if role == "developer" and ref else None
        out.append({"shift": r["shift_id"], "agent": r["role_or_run"], "role": role,
                    "task_title": (task or {}).get("title", ""), "at": r["at"],
                    "seconds": r["seconds"], "tokens": r["tokens"], "cost": r["cost"],
                    "profile": r["profile"], "verdict": r["notes"]})
    return out


def by_agent(store) -> list[dict]:
    """All-time per-role rollup (incl. legacy old-loop rows), highest-spend first."""
    return store.ledger_by_role()


def _median(xs: list[float]):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def by_profile(store) -> list[dict]:
    """Per-worker-profile outcome rollup — the workforce-evolution signal (Task 5.7). For each
    profile that has earned developer spend: {profile, engagements, merged, blocked, tokens, cost,
    est_accuracy}. est_accuracy = median(actual/est) over the profile's tasks carrying both an
    estimate and ledgered actuals (None when there's no est-vs-actual data point yet). This is
    what the conductor's {WORKERS} block and the Resources tab render, making profile
    generation/retirement INFORMED rather than decorative."""
    rows = store.profile_task_actuals()
    # A task reassigned across profiles has PARTIAL per-profile actuals but the FULL task est on
    # each row — comparing them would understate accuracy. Only count est-accuracy for tasks
    # worked by exactly ONE profile, where actual/est is unambiguous.
    per_task = {}
    for r in rows:
        per_task[r["task_id"]] = per_task.get(r["task_id"], 0) + 1
    ratios: dict[str, list[float]] = {}
    for r in rows:
        est, actual = int(r["est"] or 0), int(r["actual"] or 0)
        if est > 0 and actual > 0 and per_task[r["task_id"]] == 1:
            ratios.setdefault(r["profile"], []).append(actual / est)
    out = []
    for s in store.profile_stats():
        row = dict(s)
        row["est_accuracy"] = _median(ratios.get(row["profile"], []))
        out.append(row)
    return out
