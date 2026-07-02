"""Agent timesheets — who worked when, for how long, at what spend, to what verdict.

Pure reads over budget_ledger (+ tasks for titles). The rail writes the rows (Phase 0);
this module only shapes them for the CLI, the board and EVM. SQL lives in the store's
ledger_rows()/ledger_by_role() (the CRUD layer) — no duplicated SQL here.

Scope asymmetry (intended): timesheet() shows conductor-loop engagements only (shift_id IS
NOT NULL) while by_agent() rolls up the WHOLE ledger incl. legacy old-loop rows — label the
rollup 'all-time (incl. legacy)' wherever it renders so the two views don't look contradictory.
"""
from __future__ import annotations


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
