"""Autonomy harness (mission axis C): run the optimisation loop UNATTENDED.

`cmd_autonomous` is a thin SEQUENCER over the existing, gain-limited roles. Given
a MISSION STATEMENT it drives, each round, in order:

  1. (research) derive a query from the mission, stage grounded technique briefs
     (direction for the Proposer). Reuses `cmd_research`/`research_cli_agents`.
  2. (baseline) evaluate the reigning champion across the working set to surface
     current ground-truth failures. Reuses `cmd_baseline`.
  3. (propose+round) attempt a proposal. `cmd_propose` self-gates on the gain
     governor (>= N new champion failures). If it proposes, `cmd_round` evaluates
     + judges + reports it, landing it at `awaiting_gate` or `rejected`.
  4. (report) summarise the round; crucially list any candidates now sitting at
     `awaiting_gate` — these await the HUMAN.

It is "the factory does the rest on its own", but it NEVER promotes. Promotion is
the single human lever at the board; there is no promote call anywhere in this
module (and the loop's only stage transitions are the ones the existing roles
make: evaluating/scored/awaiting_gate/rejected). Every existing guardrail —
the gain governor, the per-round BudgetGuard ceiling + circuit breakers,
held-out leakage retirement, Goodhart/divergence detection, role isolation —
is untouched and still enforced by the functions this module calls.

STOP when ANY holds:
  * rounds reached `max_rounds` (hard ceiling);
  * cumulative spend (the budget_ledger) >= `token_budget` (if set, hard ceiling);
  * `no_improvement_rounds` consecutive rounds with no candidate clearing the gate
    (reuses the config value);
  * the gain governor hasn't armed AND research produced nothing new (no work).
"""
from __future__ import annotations

import sys
from typing import Optional

from ..common import config
from ..common.store import Blackboard
from . import triggers


def _spend(store: Blackboard) -> int:
    """Cumulative tokens spent so far, summed from the budget_ledger (authoritative)."""
    return int(store.budget_totals().get("tokens", 0) or 0)


def _awaiting_gate_ids(store: Blackboard) -> list[str]:
    return [c["id"] for c in store.list_candidates("awaiting_gate")]


def _mission_query(mission: str) -> str:
    """Derive a researcher query from the mission. The researcher already defaults
    to a CLI-agent query; we bias it toward the mission's wording so briefs point
    in the mission's direction (still distilled from real fetched papers)."""
    q = " ".join((mission or "").split())
    return q or None  # None lets research_cli_agents fall back to DEFAULT_QUERY


def cmd_autonomous(store: Blackboard, mission: str, *, max_rounds: int,
                   token_budget: Optional[int] = None, do_research: bool = True,
                   research_every: int = 3, dry_run: bool = False) -> dict:
    """Run the optimisation loop unattended toward `mission`.

    Returns a summary dict: rounds run, tokens spent, and the candidate ids left
    at the human gate. NEVER promotes; honours `max_rounds`/`token_budget` as hard
    ceilings and the existing gain governor + no-improvement stop. `dry_run` prints
    the per-round PLAN without invoking any role/LLM/subprocess.
    """
    # Imported here (not at module top) so monkeypatching the orchestrator's
    # cmd_* in tests is seen, and to keep the dependency direction one-way.
    from . import orchestrator as orch

    cfg = config.load_config()
    no_improvement_limit = int(cfg.get("triggers", {})
                               .get("no_improvement_rounds", 3))
    new_failures_threshold = int(cfg.get("triggers", {})
                                 .get("new_failures_to_propose", 3))

    print("=" * 64)
    print(f"AUTONOMOUS LOOP — mission: {mission!r}")
    print(f"  max_rounds={max_rounds}  token_budget="
          f"{token_budget if token_budget is not None else '∞'}  "
          f"research={'on' if do_research else 'off'}  dry_run={dry_run}")
    print(f"  guardrails: gain governor (>= {new_failures_threshold} new champion "
          f"failures to propose), per-round token ceiling + circuit breakers, "
          f"held-out leakage retirement, divergence/Goodhart checks.")
    print("  NOTE: this loop NEVER promotes — candidates only ever reach "
          "'awaiting_gate' for the human at the board.")
    print("=" * 64)

    rounds_run = 0
    consecutive_no_gate = 0
    stop_reason = "max_rounds reached"
    cleared_this_loop: list[str] = []

    for rnd in range(1, max_rounds + 1):
        # --- hard ceilings checked BEFORE doing any work this round -----------
        if token_budget is not None and not dry_run:
            spent = _spend(store)
            if spent >= token_budget:
                stop_reason = (f"token_budget reached ({spent} >= {token_budget} "
                               "tokens) before round start")
                break

        do_research_this_round = bool(do_research) and (
            rnd == 1 or (research_every > 0 and (rnd - 1) % research_every == 0))

        print(f"\n----- ROUND {rnd}/{max_rounds} "
              f"(spent {_spend(store) if not dry_run else 0} tok) -----")

        # --- dry run: print the PLAN only, invoke nothing ---------------------
        if dry_run:
            print("  PLAN (dry-run — no role/LLM/subprocess invoked):")
            if do_research_this_round:
                print(f"    1. cmd_research(query={_mission_query(mission)!r})  "
                      "-> stage grounded technique briefs")
            else:
                print("    1. (skip research this round)")
            print("    2. cmd_baseline()                 -> surface champion failures")
            print(f"    3. cmd_propose()                  -> self-gates on >= "
                  f"{new_failures_threshold} new champion failures; if it proposes a "
                  "candidate, cmd_round(cid) evaluates + judges + reports it")
            print("    4. report round; list any candidates now at 'awaiting_gate' "
                  "(human gate). NEVER promote.")
            rounds_run = rnd
            # Mirror the real stop ceilings so the plan reflects when it would halt.
            if rnd >= max_rounds:
                stop_reason = "max_rounds reached"
            continue

        rounds_run = rnd
        research_staged = 0
        proposed_cid: Optional[str] = None
        cleared_gate = False

        # --- 1. research (direction) -----------------------------------------
        if do_research_this_round:
            print("  [1] research: deriving direction from the mission …")
            before = _research_count(store)
            try:
                orch.cmd_research(store, query=_mission_query(mission))
            except Exception as e:  # noqa: BLE001 — research is best-effort, never fatal
                print(f"  [1] research failed (continuing): {e}", file=sys.stderr)
            research_staged = max(0, _research_count(store) - before)
        else:
            print("  [1] research: skipped this round")

        # --- token ceiling re-check after a (potentially) expensive step ------
        if token_budget is not None and _spend(store) >= token_budget:
            stop_reason = (f"token_budget reached ({_spend(store)} >= "
                           f"{token_budget} tokens) mid-round")
            break

        # --- 2. baseline the champion (surface ground-truth failures) ---------
        print("  [2] baseline: evaluating champion to surface failures …")
        try:
            orch.cmd_baseline(store)
        except Exception as e:  # noqa: BLE001
            print(f"  [2] baseline failed (continuing): {e}", file=sys.stderr)

        if token_budget is not None and _spend(store) >= token_budget:
            stop_reason = (f"token_budget reached ({_spend(store)} >= "
                           f"{token_budget} tokens) mid-round")
            break

        # --- 3. propose (gain-governed) + round it ----------------------------
        fire, n_fail, threshold = triggers.should_propose(store, cfg)
        print(f"  [3] propose: gain governor — {n_fail}/{threshold} new champion "
              f"failures since last proposal ({'ARMED' if fire else 'holding'}).")
        proposed_cid = orch.cmd_propose(store)
        if proposed_cid:
            print(f"  [3] evaluating proposed candidate {proposed_cid} …")
            orch.cmd_round(store, proposed_cid)
            cand = store.get_candidate(proposed_cid)
            stage = cand["stage"] if cand else "?"
            cleared_gate = (stage == "awaiting_gate")
            if cleared_gate and proposed_cid not in cleared_this_loop:
                cleared_this_loop.append(proposed_cid)

        # --- 4. round report --------------------------------------------------
        gate_ids = _awaiting_gate_ids(store)
        print(f"  [4] round {rnd} report:")
        print(f"        research briefs staged this round : {research_staged}")
        print(f"        candidate proposed                : "
              f"{proposed_cid or '(none — governor held or no valid candidate)'}")
        print(f"        cleared the gate this round       : "
              f"{'yes' if cleared_gate else 'no'}")
        print(f"        AWAITING HUMAN at the gate now    : "
              f"{gate_ids if gate_ids else '(none)'}")

        # --- stop bookkeeping -------------------------------------------------
        if cleared_gate:
            consecutive_no_gate = 0
        else:
            consecutive_no_gate += 1

        # no-work: governor didn't arm AND research produced nothing new.
        if not fire and (not do_research_this_round or research_staged == 0):
            stop_reason = ("no work: gain governor not armed and research produced "
                           "nothing new this round")
            break

        # no-improvement: K consecutive rounds with no candidate clearing the gate.
        if consecutive_no_gate >= no_improvement_limit:
            stop_reason = (f"no improvement: {consecutive_no_gate} consecutive rounds "
                           f"with no candidate clearing the gate "
                           f"(>= no_improvement_rounds={no_improvement_limit})")
            break

        # token budget exhausted at end of round.
        if token_budget is not None and _spend(store) >= token_budget:
            stop_reason = (f"token_budget reached ({_spend(store)} >= "
                           f"{token_budget} tokens)")
            break

    # ----------------------------------------------------------------------
    # final summary
    # ----------------------------------------------------------------------
    spent_total = 0 if dry_run else _spend(store)
    gate_ids = [] if dry_run else _awaiting_gate_ids(store)
    summary = {
        "rounds_run": rounds_run,
        "tokens_spent": spent_total,
        "stop_reason": stop_reason,
        "awaiting_gate": gate_ids,
        "dry_run": dry_run,
    }
    print("\n" + "=" * 64)
    print("AUTONOMOUS LOOP — FINAL SUMMARY")
    print(f"  rounds run     : {rounds_run}/{max_rounds}")
    print(f"  tokens spent   : {spent_total}"
          + (f" / {token_budget} budget" if token_budget is not None else ""))
    print(f"  stop reason    : {stop_reason}")
    if dry_run:
        print("  (dry-run: no roles/LLMs/subprocesses were invoked; no tokens spent)")
    else:
        if gate_ids:
            print(f"  AWAITING HUMAN PROMOTION at the gate: {gate_ids}")
            print("  Open the board and click Promote (the one human lever):")
            print("    factory/bin/factory board   ->  http://127.0.0.1:8787")
        else:
            print("  candidates awaiting human promotion: (none)")
    print("  Nothing was promoted automatically. Promotion stays a human action.")
    print("=" * 64)
    return summary


def _research_count(store: Blackboard) -> int:
    """Count staged research briefs on disk (so we can tell if a research step
    produced anything NEW this round, for the no-work stop). Best-effort: any
    error -> 0 so the loop never crashes on bookkeeping."""
    import glob
    import os

    from ..common import paths
    try:
        return len(glob.glob(os.path.join(paths.RESEARCH_STAGING_DIR, "*.yaml")))
    except Exception:  # noqa: BLE001
        return 0
