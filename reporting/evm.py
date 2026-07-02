"""Agent-adapted Earned Value Management (EVM) over the milestone plan + the budget ledger.

The semantics, stated verbatim so they are inspectable:
  * Value unit = planned TOKENS. A milestone's budget_tokens is its Planned Value; per-task
    est_tokens (Task 2.2) refines partial credit where present.
  * PV  = Σ budget_tokens over NON-DROPPED milestones (the baseline).
  * EV  = Σ budget_tokens of DELIVERED milestones + partial credit for ACTIVE ones. Partial
          credit is est-weighted (budget × Σest(done)/Σest(all)) when the linked tasks carry
          estimates, else plain done/total. PLANNED milestones earn 0.
  * AC  = actual tokens (and USD) attributed to each milestone via tasks.milestone_id → the
          ledger's developer:<task_id> rows. Spend NOT attributed to a non-dropped milestone
          (conductor, research, scope/decompose, dev work on unlinked OR dropped-milestone
          tasks) is reported as OVERHEAD, never smeared across milestones. Conservation holds:
          ac_tokens + overhead_tokens = the whole ledger.
  * CPI = EV / AC (cost efficiency; None when nothing is attributed yet).
    percent_complete = EV / PV.
  * SPI needs a time-phased baseline we do not keep — v1 reports CPI, percent_complete and a
    cumulative spend-per-shift series instead. A time-phased PV is a deliberate v2 (see the
    roadmap's out-of-scope list); it is not faked here.
  * Estimate quality is first-class: per-task est_tokens vs ledgered actuals — the conductor's
    feedback loop for revising the plan (Task 2.4).

Pure reads; the rail writes the ledger rows (Phase 0). This module only shapes them.
"""
from __future__ import annotations


def _partial_fraction(rows: list[dict]) -> float:
    """Fraction of an ACTIVE milestone earned: est-weighted when its linked tasks carry
    estimates (Σest of done ÷ Σest of all), else plain done/total. 0 with no linked tasks."""
    total = len(rows)
    if not total:
        return 0.0
    est_all = sum(int(r["est_tokens"] or 0) for r in rows)
    if est_all > 0:
        est_done = sum(int(r["est_tokens"] or 0) for r in rows if r["status"] == "done")
        return est_done / est_all
    done = sum(1 for r in rows if r["status"] == "done")
    return done / total


def evm(store) -> dict:
    """Compute the agent-adapted EVM snapshot (see the module docstring for the mapping)."""
    milestones = [m for m in store.list_milestones() if m["status"] != "dropped"]
    pv = ev = ac_tokens = 0
    ac_cost = 0.0
    rows_out: list[dict] = []
    estimates: list[dict] = []

    for m in milestones:
        budget = int(m.get("budget_tokens") or 0)
        rows = store.milestone_task_rows(m["id"])
        m_ac_tokens = sum(int(r["actual_tokens"] or 0) for r in rows)
        m_ac_cost = sum(float(r["actual_cost"] or 0.0) for r in rows)
        done = sum(1 for r in rows if r["status"] == "done")
        status = m["status"]
        if status == "delivered":
            m_ev = budget
        elif status == "active":
            m_ev = int(round(budget * _partial_fraction(rows)))
        else:                                      # planned — no earned value yet
            m_ev = 0

        pv += budget
        ev += m_ev
        ac_tokens += m_ac_tokens
        ac_cost += m_ac_cost
        rows_out.append({
            "id": m["id"], "title": m["title"], "status": status,
            "pv": budget, "ev": m_ev,
            "ac_tokens": m_ac_tokens, "ac_cost": round(m_ac_cost, 4),
            "progress": {"done": done, "total": len(rows)},
            "est_tokens": sum(int(r["est_tokens"] or 0) for r in rows),
        })
        for r in rows:
            est, act = int(r["est_tokens"] or 0), int(r["actual_tokens"] or 0)
            if est and act:                        # a data point only when BOTH exist
                estimates.append({"task": r["id"], "title": r["title"],
                                  "est": est, "actual": act})

    totals = store.budget_totals()
    overhead_tokens = int(totals["tokens"]) - ac_tokens
    overhead_cost = round(float(totals["cost"]) - ac_cost, 4)
    cpi = (ev / ac_tokens) if ac_tokens else None
    percent_complete = (ev / pv) if pv else None

    # Cumulative spend-per-shift (oldest → newest). shift_spend sums a shift's WHOLE ledger, so
    # this is the total burn curve, not milestone-attributed AC — labelled as such in the UI.
    shift_ids: list[int] = []
    ac_cumulative: list[int] = []
    cum = 0
    for s in reversed(store.list_shifts()):
        cum += int(store.shift_spend(s["id"])["tokens"])
        shift_ids.append(s["id"])
        ac_cumulative.append(cum)

    return {
        "pv": pv, "ev": ev,
        "ac_tokens": ac_tokens, "ac_cost": round(ac_cost, 4),
        "overhead_tokens": overhead_tokens, "overhead_cost": overhead_cost,
        "cpi": cpi, "percent_complete": percent_complete,
        "milestones": rows_out,
        "estimates": estimates,
        "series": {"shift_ids": shift_ids, "ac_cumulative": ac_cumulative},
    }
