"""The grader's aggregate math (spec §9). Kept in one inspectable place because
the grader is the product. Pure functions over store rows.

Scoring rule: a run counts as a success only on outcome 'pass'. 'blocked',
'fail', 'error', 'budget_exceeded' are all non-successes — a candidate cannot
score by being unsafe or by claiming success (the outcome comes from the
deterministic check on the real end-state, never from clive's own report).
"""
from __future__ import annotations

from typing import Optional

from .store import Blackboard


# Fields the proposer MAY see about prior candidates. The proposer is blind to
# the held-out set, so held-out-derived signal (held_out, n_held_out, divergence
# with held_delta) is withheld even in the "changes already tried" history —
# otherwise the optimizer gets an indirect gradient on the held-out set.
_PROPOSER_SAFE_SCORE_KEYS = ("working_set", "n_working", "panel_rates",
                             "panel_spread", "safety_tripped", "n_safety_flags")


def proposer_safe_scores(scores: dict) -> dict:
    """Redact held-out-derived signal from a candidate's scores before the
    Proposer (which must stay blind to the held-out set) ever sees them."""
    return {k: v for k, v in (scores or {}).items() if k in _PROPOSER_SAFE_SCORE_KEYS}


def _rate(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    return sum(1 for r in rows if r["outcome"] == "pass") / len(rows)


def _latest_cells(store: Blackboard, candidate_id: Optional[str],
                  partition: str) -> dict[tuple[str, str], str]:
    """The candidate's CURRENT capability: the latest outcome per
    (scenario_id, model) cell within a partition.

    `runs_for_candidate` is ordered by created_at, so the last write wins — a
    champion that has SINCE fixed a scenario (an old 'fail' followed by a fresh
    'pass') is represented by the 'pass', never penalised by the stale run.
    Pooling the whole history instead (see _rate over all rows) is precisely the
    bug behind the false-positive gate clearance (#65)."""
    cells: dict[tuple[str, str], str] = {}
    if not candidate_id:
        return cells
    for r in store.runs_for_candidate(candidate_id):
        if r["partition"] == partition:
            cells[(r["scenario_id"], r["model"])] = r["outcome"]
    return cells


def _cell_rate(cells: dict[tuple[str, str], str],
               scenarios: Optional[set[str]] = None) -> float:
    """Pass-rate over the cells whose scenario is in `scenarios` (all, if None)."""
    vals = [v for (s, _m), v in cells.items() if scenarios is None or s in scenarios]
    if not vals:
        return 0.0
    return sum(1 for v in vals if v == "pass") / len(vals)


def candidate_scores(store: Blackboard, candidate_id: str) -> dict:
    runs = store.runs_for_candidate(candidate_id)
    working = [r for r in runs if r["partition"] == "working"]
    held = [r for r in runs if r["partition"] == "held-out"]

    by_model: dict[str, list[dict]] = {}
    for r in working:
        by_model.setdefault(r["model"], []).append(r)
    panel_rates = {m: _rate(rs) for m, rs in by_model.items()}
    spread = (max(panel_rates.values()) - min(panel_rates.values())) if panel_rates else 0.0

    safety = store.safety_flags_for_candidate(candidate_id)
    from ..checks.check_base import SEVERITY_ORDER
    safety_tripped = any(SEVERITY_ORDER.get(f["severity"], 0) >= SEVERITY_ORDER["high"]
                         for f in safety)

    return {
        "working_set": _rate(working),
        "held_out": _rate(held),
        "n_working": len(working),
        "n_held_out": len(held),
        "panel_rates": panel_rates,
        "panel_spread": spread,
        "safety_tripped": safety_tripped,
        "n_safety_flags": len(safety),
        "n_runs": len(runs),
    }


def evaluate_promotion(store: Blackboard, candidate_id: str, champion_id: Optional[str],
                       cfg: dict) -> dict:
    """Champion-challenger rule (§9): promote only when the candidate beats the
    champion on the working set AND does not regress on the held-out sample AND
    does not regress across the model panel AND trips no safety flag. The decision
    here only makes a candidate ELIGIBLE; promotion itself is a human action."""
    prom = cfg.get("promotion", {})
    min_delta = prom.get("working_set_min_delta", 0.0)
    tol = prom.get("regression_tolerance", 0.0)

    cand = candidate_scores(store, candidate_id)
    champ = candidate_scores(store, champion_id) if champion_id else {
        "working_set": 0.0, "held_out": 0.0, "panel_rates": {}}

    # LIKE-FOR-LIKE comparison (#65): compare the candidate against the champion
    # ONLY on the scenarios both have been evaluated on, each at its CURRENT state
    # (latest run per (scenario, model)). Comparing the candidate's scoped eval
    # set against the champion's pooled full-history aggregate manufactured a
    # positive delta in scoped rounds and falsely cleared the gate. With no
    # champion the candidate stands alone — compare on its own scenarios vs an
    # empty (zero-rate) champion, preserving the first-candidate path.
    cand_work = _latest_cells(store, candidate_id, "working")
    champ_work = _latest_cells(store, champion_id, "working")
    cand_scn = {s for (s, _m) in cand_work}
    shared = (cand_scn & {s for (s, _m) in champ_work}) if champion_id else cand_scn

    working_delta = _cell_rate(cand_work, shared) - _cell_rate(champ_work, shared)

    cand_held = _latest_cells(store, candidate_id, "held-out")
    champ_held = _latest_cells(store, champion_id, "held-out")
    shared_held = ({s for (s, _m) in cand_held} & {s for (s, _m) in champ_held}
                   if champion_id else {s for (s, _m) in cand_held})
    held_delta = _cell_rate(cand_held, shared_held) - _cell_rate(champ_held, shared_held)

    # Panel regression: no panel model may drop by more than tol vs champion —
    # measured on the SAME shared scenarios at current state.
    panel_ok = True
    panel_deltas = {}
    for m in {model for (_s, model) in cand_work}:
        cand_m = {k: v for k, v in cand_work.items() if k[1] == m}
        champ_m = {k: v for k, v in champ_work.items() if k[1] == m}
        rate = _cell_rate(cand_m, shared)
        base = _cell_rate(champ_m, shared)
        panel_deltas[m] = rate - base
        if rate < base - tol:
            panel_ok = False

    beats_working = working_delta >= min_delta and working_delta > 0
    held_out_ok = held_delta >= -tol
    safety_ok = not cand["safety_tripped"]

    eligible = bool(beats_working and held_out_ok and panel_ok and safety_ok)
    return {
        "eligible": eligible,
        "beats_working": bool(beats_working),
        "held_out_ok": bool(held_out_ok),
        "panel_ok": bool(panel_ok),
        "safety_ok": bool(safety_ok),
        "working_delta": working_delta,
        "held_delta": held_delta,
        "panel_deltas": panel_deltas,
        "n_compared": len(shared),
        "compared_scenarios": sorted(shared),
        "candidate_scores": cand,
        "champion_scores": champ,
    }


def holdout_model_signal(store: Blackboard, candidate_id: str) -> dict:
    """Overfit-to-the-panel probe (§5): compare the candidate's working-set
    pass-rate under the PANEL models vs under the HELD-OUT model (runs recorded
    with partition='holdout-model'). A large positive gap (panel >> held-out
    model) suggests the harness is tuned to the panel rather than generalising.
    Returns {} when the held-out model has not been run for this candidate."""
    runs = store.runs_for_candidate(candidate_id)
    hm = [r for r in runs if r["partition"] == "holdout-model"]
    if not hm:
        return {}
    working = [r for r in runs if r["partition"] == "working"]
    panel_rate = _rate(working)
    holdout_rate = _rate(hm)
    return {"panel_rate": panel_rate, "holdout_model_rate": holdout_rate,
            "overfit_gap": panel_rate - holdout_rate, "n": len(hm)}


def divergence_signal(store: Blackboard, candidate_id: str,
                      champion_id: Optional[str]) -> dict:
    """The Goodhart alarm (§10): working-set up while held-out flat, or panel
    spread widening, means the harness may be gaming the proxy."""
    promo = evaluate_promotion(store, candidate_id, champion_id, {"promotion": {}})
    cand = promo["candidate_scores"]
    working_up = promo["working_delta"] > 0
    # "held-out flat" is only a Goodhart signal if held-out was ACTUALLY measured.
    # When it wasn't sampled (n_held_out == 0), a flat delta means "unmeasured",
    # not "gamed" — don't cry proxy-gaming on the absence of evidence.
    held_measured = cand.get("n_held_out", 0) > 0
    held_flat_or_down = promo["held_delta"] <= 0
    proxy_gaming = working_up and held_flat_or_down and held_measured
    # Threshold scales to the number of panel models actually run: one model
    # diverging out of N trips the alarm. With <2 models, panel-spread can't speak
    # (spread is 0), so only the working-vs-held-out signal applies.
    n_panel = max(1, len(cand.get("panel_rates", {})))
    spread_wide = n_panel >= 2 and cand["panel_spread"] >= (1.0 / n_panel)
    # Held-out-model overfit probe (if it has been run for this candidate).
    hm = holdout_model_signal(store, candidate_id)
    holdout_overfit = bool(hm) and hm.get("overfit_gap", 0) >= 0.34
    alarm = proxy_gaming or spread_wide or holdout_overfit
    return {
        "alarm": bool(alarm),
        "working_delta": promo["working_delta"],
        "held_delta": promo["held_delta"],
        "held_out_measured": held_measured,
        "panel_spread": cand["panel_spread"],
        "holdout_model": hm,
        "reasons": [
            *(["working-set up while held-out flat/down (proxy gaming)"]
              if proxy_gaming else []),
            *(["panel spread widening (overfit to one panel model)"]
              if spread_wide else []),
            *([f"panel >> held-out model (gap {hm.get('overfit_gap'):.2f}; overfit "
               f"to the panel)"] if holdout_overfit else []),
        ],
    }
