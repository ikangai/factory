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
                   do_intake: bool = True, intake_limit: int = 10,
                   intake_max_new: int = 3,
                   research_every: int = 3, dry_run: bool = False,
                   base_max_papers: int = 8, base_max_repos: int = 6,
                   max_research_breadth: int = 4,
                   idle_research_rounds: Optional[int] = None) -> dict:
    """Run the optimisation loop unattended toward `mission`.

    Returns a summary dict: rounds run, tokens spent, and the candidate ids left
    at the human gate. NEVER promotes; honours `max_rounds`/`token_budget` as hard
    ceilings and the existing gain governor + no-improvement stop. `dry_run` prints
    the per-round PLAN without invoking any role/LLM/subprocess.

    KEEP BUSY: when the gain governor isn't armed (the champion is robust, so no
    candidate can be proposed) the loop does NOT idle-stop — it BROADENS the
    research sweep each idle round (escalating breadth up to `max_research_breadth`x
    over the base) to keep surfacing fresh discoveries for the daily update. It
    only gives up when even the broadened sweep finds nothing new for
    `idle_research_rounds` consecutive rounds (defaults to no_improvement_rounds).
    """
    # Imported here (not at module top) so monkeypatching the orchestrator's
    # cmd_* in tests is seen, and to keep the dependency direction one-way.
    from . import orchestrator as orch

    cfg = config.load_config()
    no_improvement_limit = int(cfg.get("triggers", {})
                               .get("no_improvement_rounds", 3))
    new_failures_threshold = int(cfg.get("triggers", {})
                                 .get("new_failures_to_propose", 3))
    idle_limit = int(idle_research_rounds if idle_research_rounds is not None
                     else no_improvement_limit)

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
    idle_streak = 0       # consecutive rounds the governor held (drives broadening)
    dry_research = 0      # consecutive idle rounds the broadened sweep found nothing
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

        # KEEP BUSY: once we go idle (governor holding), research EVERY round to
        # keep finding work — not just on the scheduled cadence. Breadth escalates
        # with the idle streak (capped) so each idle round casts a wider net.
        broadening = idle_streak > 0
        cadence_round = (rnd == 1 or broadening
                         or (research_every > 0 and (rnd - 1) % research_every == 0))
        do_research_this_round = bool(do_research) and cadence_round
        do_intake_this_round = bool(do_intake) and cadence_round
        breadth = min(1 + idle_streak, max_research_breadth)

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
            if do_intake_this_round:
                print("    1b. cmd_intake()                 -> mine + #64-validate + "
                      "auto-promote new working scenarios (held-out stays human-gated)")
            else:
                print("    1b. (skip intake this round)")
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
        intake_promoted = 0
        proposed_cid: Optional[str] = None
        cleared_gate = False

        # --- 1. research (direction) -----------------------------------------
        if do_research_this_round:
            label = "broadened " if broadening else ""
            print(f"  [1] research ({label}breadth x{breadth}): deriving direction "
                  "from the mission …")
            before = _research_count(store)
            try:
                orch.cmd_research(store, query=_mission_query(mission),
                                  max_papers=base_max_papers * breadth,
                                  max_repos=base_max_repos * breadth)
            except Exception as e:  # noqa: BLE001 — research is best-effort, never fatal
                print(f"  [1] research failed (continuing): {e}", file=sys.stderr)
            research_staged = max(0, _research_count(store) - before)
        else:
            print("  [1] research: skipped this round")

        # --- 1b. intake (grow the working corpus) ----------------------------
        # The self-sustaining arrow: mine + #64-validate + auto-promote VALIDATED
        # scenarios into the working set. New working scenarios → new champion
        # failures → the gain governor re-arms (so propose() runs and the briefs
        # above actually get used). Held-out is never auto-grown.
        if do_intake_this_round:
            print("  [1b] intake: mining + #64-validating new scenarios "
                  "(auto-promote validated → working) …")
            try:
                res = orch.cmd_intake(store, limit=intake_limit,
                                      max_new=intake_max_new)
                intake_promoted = len(res.get("promoted", []) if res else [])
            except Exception as e:  # noqa: BLE001 — intake is best-effort, never fatal
                print(f"  [1b] intake failed (continuing): {e}", file=sys.stderr)
        else:
            print("  [1b] intake: skipped this round")

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
        print(f"        scenarios auto-promoted (intake)  : {intake_promoted}")
        print(f"        candidate proposed                : "
              f"{proposed_cid or '(none — governor held or no valid candidate)'}")
        print(f"        cleared the gate this round       : "
              f"{'yes' if cleared_gate else 'no'}")
        print(f"        AWAITING HUMAN at the gate now    : "
              f"{gate_ids if gate_ids else '(none)'}")

        # --- stop bookkeeping -------------------------------------------------
        if fire:
            # ACTIVE optimisation: the governor armed, so we attempted a proposal.
            # Reset the keep-busy counters; track gate stagnation as before.
            idle_streak = 0
            dry_research = 0
            if cleared_gate:
                consecutive_no_gate = 0
            else:
                consecutive_no_gate += 1
            # no-improvement: K consecutive armed rounds with nothing clearing.
            if consecutive_no_gate >= no_improvement_limit:
                stop_reason = (f"no improvement: {consecutive_no_gate} consecutive "
                               f"rounds with no candidate clearing the gate "
                               f"(>= no_improvement_rounds={no_improvement_limit})")
                break
        else:
            # KEEP BUSY: governor not armed (champion robust). Don't idle-stop —
            # we broadened the research sweep this round. Keep going while EITHER
            # intake (a new working scenario) OR research (a new brief) surfaced
            # NEW work; give up only after `idle_limit` rounds that found neither.
            consecutive_no_gate = 0
            idle_streak += 1
            if research_staged > 0 or intake_promoted > 0:
                dry_research = 0
            else:
                dry_research += 1
            if dry_research >= idle_limit:
                stop_reason = (f"research exhausted: neither the broadened sweep nor "
                               f"intake produced anything new for {dry_research} "
                               f"consecutive idle rounds (>= {idle_limit})")
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

    # ----------------------------------------------------------------------
    # human-readable executive summary (Discoveries / Decisions / Next steps).
    # Only for a REAL run — a dry-run invoked no roles and spent nothing, so there
    # is nothing to present (and we must not spend an LLM call on it). Best-effort:
    # the generator never crashes, but guard the print/save defensively too.
    # ----------------------------------------------------------------------
    if not dry_run:
        try:
            from datetime import datetime
            import os
            from ..common import paths
            from ..reporting.summary import generate_executive_summary

            now = datetime.now()
            exec_summary = generate_executive_summary(
                store, since=now.strftime("%Y-%m-%d"), mission=mission)
            print("\n" + "=" * 64)
            print("EXECUTIVE SUMMARY (for the human's daily update)")
            print("=" * 64)
            print(exec_summary)
            updates_dir = os.path.join(paths.FACTORY_ROOT, "updates")
            os.makedirs(updates_dir, exist_ok=True)
            out_path = os.path.join(updates_dir, now.strftime("%Y-%m-%d-%H%M") + ".md")
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(exec_summary if exec_summary.endswith("\n")
                         else exec_summary + "\n")
            print(f"\n[report] saved to "
                  f"{os.path.relpath(out_path, paths.FACTORY_ROOT)}")
            summary["report_path"] = out_path
        except Exception as e:  # noqa: BLE001 — presentation is never fatal to a run
            print(f"[report] executive summary skipped: {e}", file=sys.stderr)

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
