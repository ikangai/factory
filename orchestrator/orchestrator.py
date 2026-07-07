"""Orchestrator (spec §9): triggers, sequencing, concurrency + budget control.

The orchestrator sequences the stateless roles and the deterministic runner over
the blackboard. It NEVER promotes — promotion is a human action at the board. The
loops are deliberately gain-limited: the optimisation loop fires only on new
failure data, and evaluation is concurrency- and budget-capped.

CLI:  python3 -m factory.orchestrator.orchestrator <command> [args]
  init                 apply schema; register champion + scenarios
  baseline             evaluate the champion (working + held-out) for comparison
  propose              fire the optimisation trigger -> one candidate (claude -p)
  evaluate <cid>       run a candidate across working set x panel (concurrency-capped)
  round <cid>          evaluate (+held-out sample) -> reporter -> gate to awaiting_gate/rejected
  mine [--limit N]     scenario-miner -> staging (operator vetting)
  status               print a store summary
  report [--mission]   write a human-readable executive summary -> updates/
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
from typing import Optional

import yaml

from ..common import config, paths, scoring, specs
from ..common.budget import BudgetGuard
from ..common.store import Blackboard
from ..runner.runner import run_one
from . import triggers
from .concurrency import run_capped

CHAMPION_ID = "champion"


# ---------------------------------------------------------------------------
# init / registration
# ---------------------------------------------------------------------------
def _load_scenarios_from_disk() -> list[dict]:
    out = []
    for part, d in (("working", paths.WORKING_DIR), ("held-out", paths.HELD_OUT_DIR)):
        for path in sorted(glob.glob(os.path.join(d, "*.yaml"))):
            with open(path, "r", encoding="utf-8") as fh:
                sc = yaml.safe_load(fh) or {}
            sc["_path"] = path
            sc["partition"] = part
            out.append(sc)
    return out


def cmd_init(store: Blackboard) -> None:
    store.init_db()
    # Champion exists both as the reigning spec and as a pseudo-candidate so its
    # baseline runs satisfy the runs->candidates foreign key and feed scoring.
    if not store.get_candidate(CHAMPION_ID):
        store.add_candidate(CHAMPION_ID, "champion", paths.CHAMPION_YAML,
                            change_summary="(champion baseline)", stage="promoted")
    # Only seed the champion on a fresh store. Re-running init must NOT stamp a
    # fresh promoted_at on the baseline row, or get_champion() (ORDER BY
    # promoted_at DESC) would silently revert a human-promoted champion.
    if not store.get_champion():
        store.set_champion(CHAMPION_ID, paths.CHAMPION_YAML, scores={})
    n = 0
    for sc in _load_scenarios_from_disk():
        store.upsert_scenario(sc["id"], cls=sc.get("class", "single"),
                              partition=sc["partition"], source=sc.get("source", "seed"),
                              spec_path=sc["_path"], goal=sc.get("goal", ""),
                              snapshot=sc.get("snapshot", ""), check_path=sc.get("check", ""))
        n += 1
    print(f"init: schema applied, champion registered, {n} scenarios registered")


def _scenario_dict(store_row: dict) -> dict:
    """Reload the authoritative YAML for a scenario row (carries members/token/etc)."""
    with open(store_row["spec_path"], "r", encoding="utf-8") as fh:
        sc = yaml.safe_load(fh) or {}
    sc["id"] = store_row["id"]
    sc["partition"] = store_row["partition"]
    return sc


# ---------------------------------------------------------------------------
# evaluation loop
# ---------------------------------------------------------------------------
def _evaluate(store: Blackboard, candidate_id: str, spec_path: str, *,
              held_out_sample: int = 0, run_judge: bool = True,
              models: Optional[list[dict]] = None,
              scenario_ids: Optional[list[str]] = None,
              work_partition: str = "working",
              update_candidate: bool = True) -> dict:
    cfg = config.load_config()
    cap = int(cfg.get("concurrency", {}).get("cap", 2))
    guard = BudgetGuard()
    models = models if models is not None else config.panel_models()

    def _filter(rows):
        return [s for s in rows if (scenario_ids is None or s["id"] in scenario_ids)]

    working = _filter(store.list_scenarios(partition="working"))
    held = _filter(store.list_scenarios(partition="held-out"))[:held_out_sample]
    plan: list[tuple[dict, dict, str]] = []
    for s in working:
        for m in models:
            plan.append((s, m, work_partition))   # label working runs (e.g. 'holdout-model')
    for s in held:
        for m in models:
            plan.append((s, m, "held-out"))

    if candidate_id != CHAMPION_ID and update_candidate:
        store.set_stage(candidate_id, "evaluating")

    consecutive_errors = {"n": 0}
    progress = {"done": 0, "total": len(plan)}
    print(f"  {progress['total']} runs: {len(working)} working + {len(held)} held-out "
          f"× {len(models)} model(s) [{', '.join(m.get('name','?') for m in models)}]",
          file=sys.stderr)

    def make_task(scenario_row, model_entry, partition):
        scen = _scenario_dict(scenario_row)
        return lambda: {**run_one(candidate_id, spec_path, scen, model_entry,
                                  partition=partition),
                        "partition": partition,
                        "scenario": scenario_row["id"],
                        "model": model_entry.get("name", "?")}

    def on_done(res: dict) -> bool:
        progress["done"] += 1
        mark = "✓" if res.get("outcome") == "pass" else "✗"
        flags = res.get("safety_flags") or []
        sfx = f" ⚠{len(flags)}" if flags else ""
        print(f"  [{progress['done']}/{progress['total']}] {mark} "
              f"{res.get('scenario','?')} @ {res.get('model','?')} → "
              f"{res.get('outcome','?')} ({res.get('tokens',0)} tok){sfx}", file=sys.stderr)
        guard.add(int(res.get("tokens", 0)))
        if res.get("outcome") == "error":
            consecutive_errors["n"] += 1
        else:
            consecutive_errors["n"] = 0
        if guard.exceeded():
            print(f"[circuit-breaker] round token ceiling {guard.round_max_tokens} "
                  f"reached; halting evaluation", file=sys.stderr)
            return False
        if consecutive_errors["n"] >= 4:
            print("[circuit-breaker] 4 consecutive run errors; halting evaluation",
                  file=sys.stderr)
            return False
        return True

    tasks = [make_task(s, m, p) for (s, m, p) in plan]
    results = run_capped(tasks, cap, on_done=on_done)

    if run_judge:
        from ..roles.common import judge
        for r in results:
            rid = r.get("run_id")
            if rid:
                try:
                    judge(store, rid)
                except Exception as e:
                    print(f"[judge] {rid}: {e}", file=sys.stderr)

    scores = scoring.candidate_scores(store, candidate_id)
    if candidate_id != CHAMPION_ID and update_candidate:
        store.set_candidate_scores(candidate_id, scores)
        store.set_stage(candidate_id, "scored")
    return {"results": results, "scores": scores,
            "halted": guard.exceeded() or consecutive_errors["n"] >= 4}


def cmd_baseline(store: Blackboard, sample: Optional[int] = None,
                 scenario_ids: Optional[list[str]] = None,
                 models: Optional[list[dict]] = None) -> None:
    cfg = config.load_config()
    sample = cfg.get("held_out", {}).get("sample_size", 1) if sample is None else sample
    # Run the STORE's current champion spec, not the hardcoded seed — so a board
    # promotion (which repoints the champion at the promoted candidate's spec)
    # actually takes effect for the loop. The champion id stays CHAMPION_ID.
    champ = store.get_champion()
    champ_path = champ["spec_path"] if champ else paths.CHAMPION_YAML
    print(f"baseline: evaluating champion [{os.path.basename(champ_path)}] across "
          f"working set + {sample} held-out …")
    out = _evaluate(store, CHAMPION_ID, champ_path,
                    held_out_sample=sample, run_judge=False,
                    scenario_ids=scenario_ids, models=models)
    store.set_champion(CHAMPION_ID, champ_path, scores=out["scores"])
    print("baseline scores:", json.dumps(out["scores"], indent=2, default=str))


def _resolve_models(names: Optional[list[str]]) -> Optional[list[dict]]:
    """Map model NAMES (from --model) to their panel/held-out config entries."""
    if not names:
        return None
    pool = {m["name"]: m for m in (config.panel_models() + config.held_out_models())}
    out = [pool[n] for n in names if n in pool]
    return out or None


def cmd_evaluate(store: Blackboard, candidate_id: str, run_judge: bool = True,
                 scenario_ids: Optional[list[str]] = None,
                 models: Optional[list[dict]] = None) -> None:
    cand = store.get_candidate(candidate_id)
    if not cand:
        print(f"no such candidate: {candidate_id}", file=sys.stderr)
        return
    out = _evaluate(store, candidate_id, cand["spec_path"], run_judge=run_judge,
                    scenario_ids=scenario_ids, models=models)
    print(f"evaluate {candidate_id}:", json.dumps(out["scores"], indent=2, default=str))


# ---------------------------------------------------------------------------
# optimisation trigger
# ---------------------------------------------------------------------------
def cmd_propose(store: Blackboard) -> Optional[str]:
    cfg = config.load_config()
    fire, n, threshold = triggers.should_propose(store, cfg)
    if not fire:
        print(f"propose: trigger not met ({n}/{threshold} new failures since last "
              f"proposal). The gain governor holds.")
        return None
    print(f"propose: {n} >= {threshold} new failures — firing optimisation loop")
    from ..roles.common import propose
    cid = propose(store)
    print("proposed candidate:", cid or "(none — proposer produced no valid candidate)")
    return cid


# ---------------------------------------------------------------------------
# round = evaluate (+held-out) -> reporter -> gate (human queue)
# ---------------------------------------------------------------------------
def cmd_round(store: Blackboard, candidate_id: str, run_judge: bool = True,
              scenario_ids: Optional[list[str]] = None,
              models: Optional[list[dict]] = None) -> dict:
    cfg = config.load_config()
    sample = int(cfg.get("held_out", {}).get("sample_size", 1))
    leak_threshold = int(cfg.get("held_out", {}).get("leakage_threshold", 5))
    cand = store.get_candidate(candidate_id)
    if not cand:
        print(f"no such candidate: {candidate_id}", file=sys.stderr)
        return {}

    out = _evaluate(store, candidate_id, cand["spec_path"],
                    held_out_sample=sample, run_judge=run_judge,
                    scenario_ids=scenario_ids, models=models)
    promo = scoring.evaluate_promotion(store, candidate_id, CHAMPION_ID, cfg)

    # The held-out scenarios just influenced a promotion decision -> leakage++.
    _held = [s for s in store.list_scenarios(partition="held-out")
             if scenario_ids is None or s["id"] in scenario_ids]
    for s in _held[:sample]:
        store.increment_leakage(s["id"])
        row = store.get_scenario(s["id"])
        if row and row["leakage_count"] >= leak_threshold:
            store.retire_scenario(s["id"])
            print(f"[held-out] {s['id']} retired (leakage {row['leakage_count']} "
                  f">= {leak_threshold}); replace from vetted mined scenarios",
                  file=sys.stderr)

    from ..roles.common import report
    digest_path = report(store, candidate_id)

    if promo["eligible"]:
        store.set_stage(candidate_id, "awaiting_gate")
        print(f"round {candidate_id}: CLEARED the rule -> queued for the HUMAN gate. "
              f"Nothing promotes automatically (Phase 0).")
    else:
        store.set_stage(candidate_id, "rejected")
        reasons = [k for k in ("beats_working", "held_out_ok", "panel_ok", "safety_ok")
                   if not promo[k]]
        print(f"round {candidate_id}: did NOT clear ({', '.join(reasons)}) -> rejected")
    print("digest:", digest_path)
    return {"promotion": promo, "digest": digest_path}


def cmd_holdout_check(store: Blackboard, candidate_id: str) -> None:
    """Arbitration-cadence overfit probe (§5, §9): run the candidate across the
    working set under the HELD-OUT model(s) — never used during optimisation — and
    report the panel-vs-held-out-model gap. Runs are recorded with
    partition='holdout-model' so they never contaminate the panel scoreboard."""
    cand = store.get_candidate(candidate_id)
    if not cand:
        print(f"no such candidate: {candidate_id}", file=sys.stderr)
        return
    models = config.held_out_models()
    if not models:
        print("no held-out model configured in panel.yaml (held_out:)", file=sys.stderr)
        return
    # Record these runs as the held-out-MODEL partition from the start, and do NOT
    # touch the candidate's authoritative working scores or its stage (a queued
    # candidate must stay queued).
    _evaluate(store, candidate_id, cand["spec_path"], held_out_sample=0,
              run_judge=False, models=models, work_partition="holdout-model",
              update_candidate=False)
    sig = scoring.holdout_model_signal(store, candidate_id)
    print(f"holdout-check {candidate_id}:", json.dumps(sig, indent=2, default=str))
    if sig and sig.get("overfit_gap", 0) >= 0.34:
        print("[DIVERGENCE] panel >> held-out model — likely overfit to the panel",
              file=sys.stderr)


def cmd_mine(store: Blackboard, limit: int = 10) -> None:
    from ..roles.common import mine_scenarios
    paths_written = mine_scenarios(store, limit)
    print(f"mined {len(paths_written)} candidate scenarios into staging "
          f"(operator vetting required):")
    for p in paths_written:
        print(" ", p)


def cmd_intake(store: Blackboard, limit: int = 10, max_new: int = 3,
               auto_promote: bool = True) -> dict:
    """Unattended corpus INTAKE (#2): mine candidate scenarios from recent
    production sessions, synthesize + #64-VALIDATE a deterministic oracle for each,
    and AUTO-PROMOTE to the WORKING set ONLY those whose oracle the validator
    PROVES correct.

    This is what keeps the loop self-sustaining: new working scenarios → new
    champion failures → the gain governor re-arms → real optimisation, instead of
    a static corpus stalling the governor.

    SAFETY (operator's decision): a scenario auto-promotes ONLY when
    `check_validation` starts with 'validated:' (the validator exercised the oracle
    against its own recomputed-correct + a perturbation and both held). Oracles that
    are merely structural/unverifiable ('unverified: …' — shell-based, no recomputed
    expected) or rejected stay STAGED for human review. HELD-OUT is NEVER auto-grown
    — overfit hygiene stays a human action at `promote-scenario --partition held-out`.

    `max_new` bounds synth/validate calls per intake (token governor); any surplus
    mined scenarios stay staged for a later round / the human. Returns a stats dict.
    """
    from ..roles.common import mine_scenarios, synth_check  # local: test-monkeypatchable

    written = mine_scenarios(store, limit)
    staged_ids = [os.path.splitext(os.path.basename(p))[0] for p in written]
    to_process, leftover = staged_ids[:max_new], staged_ids[max_new:]

    promoted: list[str] = []
    validated: list[str] = []
    unverified: list[str] = []
    rejected: list[str] = []
    errored: list[str] = []
    for sid in to_process:
        chk = synth_check(store, sid)
        if not chk:                       # oracle rejected/unparseable → human reviews
            rejected.append(sid)
            continue
        staged_path = os.path.join(paths.STAGING_DIR, f"{sid}.yaml")
        try:
            with open(staged_path, "r", encoding="utf-8") as fh:
                sc = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError) as e:
            # Re-read of the just-written staged file failed (transient FS fault):
            # surface it instead of silently dropping the sid from the tally. The
            # file (if any) stays staged — nothing wrong reaches the working set.
            print(f"[intake] {sid}: staged YAML unreadable after synth ({e}); "
                  f"left staged for next round / human.", file=sys.stderr)
            errored.append(sid)
            continue
        if str(sc.get("check_validation", "")).startswith("validated:"):
            validated.append(sid)
            if auto_promote:
                cmd_promote_scenario(store, sid, "working")
                if not os.path.exists(staged_path):   # promotion removes the staging file
                    promoted.append(sid)
        else:                             # adopted but only structurally verified → human reviews
            unverified.append(sid)

    print(f"intake: mined {len(staged_ids)}, validated {len(validated)}, "
          f"auto-promoted {len(promoted)} → working, {len(unverified)} staged "
          f"(unverified), {len(rejected)} rejected, {len(errored)} errored. "
          f"Held-out is never auto-grown.")
    if leftover:
        print(f"intake: capped at max_new={max_new} this round; {len(leftover)} "
              f"mined scenario(s) left staged for a later round / the human.")
    return {"mined": staged_ids, "validated": validated, "promoted": promoted,
            "unverified": unverified, "rejected": rejected, "errored": errored}


def cmd_research(store: Blackboard, query: Optional[str] = None,
                 max_papers: int = 8, max_repos: int = 6) -> None:
    """Researcher role: read recent arXiv papers AND GitHub repos (plus any material
    the human dropped into MISSION.md) on the mission-driven focus, and stage
    GROUNDED, cited technique briefs for operator vetting. The briefs FEED the
    Proposer; nothing is auto-applied (same human-gated discipline as mining). The
    focus defaults to MISSION.md's `## Research focus` when --query is unset."""
    from ..roles.common import research_cli_agents
    written = research_cli_agents(store, query=query, max_papers=max_papers,
                                  max_repos=max_repos,
                                  mission_file=paths.factory("MISSION.md"))
    if not written:
        print("research: no grounded briefs staged (no papers, no applicable "
              "technique, or retrieval failed — see any message above).")
        return
    print(f"research: staged {len(written)} grounded technique brief(s) for "
          f"operator vetting (these feed the Proposer, never auto-applied):")
    for p in written:
        print(" ", p)


def cmd_reset(keep_logs: bool = False) -> None:
    """Wipe the blackboard, generated candidate specs, and run logs, then re-init
    to a clean slate. Run OUTSIDE an open store connection (it deletes the db)."""
    removed = 0
    for suffix in ("", "-wal", "-shm"):
        p = paths.DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p); removed += 1
    for f in glob.glob(os.path.join(paths.CANDIDATES_DIR, "*.yaml")):
        os.remove(f); removed += 1
    if not keep_logs and os.path.isdir(paths.RUNS_DIR):
        shutil.rmtree(paths.RUNS_DIR, ignore_errors=True)
        os.makedirs(paths.RUNS_DIR, exist_ok=True)
    print(f"reset: cleared {removed} store/spec files"
          + ("" if keep_logs else " + run logs") + " — re-initialising…")
    with Blackboard() as store:
        cmd_init(store)


DEMO_CONVENTION = (
    "\n\nOUTPUT CONVENTION: After you finish writing any report or result file for "
    "a task, also create an empty sibling completion-receipt file named "
    "<that filename>.done (for example, `touch status.txt.done`), so a task's "
    "completion is verifiable on disk.")


def _seed_demo_candidate(store: Blackboard) -> str:
    """A deterministic one-change candidate: champion + a completion-receipt
    convention in the system_prompt. Fixes the `gate-demo` scenario the current
    champion fails."""
    champ = specs.load_spec(paths.CHAMPION_YAML)
    cand = {"meta": {"version": champ["meta"].get("version", 1) + 1, "parent": "champion"},
            "open": dict(champ["open"]), "frozen": champ["frozen"]}
    cand["open"]["system_prompt"] = champ["open"].get("system_prompt", "") + DEMO_CONVENTION
    cand["meta"]["hash"] = specs.compute_hash(cand["open"], cand["frozen"])
    res = specs.validate_candidate(cand, champ, max_changed_open_keys=config.load_config()
                                   .get("spec", {}).get("max_changed_open_keys", 1))
    assert res.ok, f"demo candidate failed validation: {res.errors}"
    cid = "cand-demo"
    spec_path = os.path.join(paths.CANDIDATES_DIR, f"{cid}.yaml")
    os.makedirs(paths.CANDIDATES_DIR, exist_ok=True)
    specs.dump_spec(cand, spec_path)
    if not store.get_candidate(cid):
        store.add_candidate(cid, "champion", spec_path,
                            change_summary="system_prompt: teach clive to leave a "
                            "<file>.done completion receipt", diff=res.diff, stage="proposed")
    return cid


def cmd_demo(store: Blackboard) -> None:
    """Promotion-gate demo: the champion FAILS `gate-demo` (writes the report but
    leaves no completion receipt), a one-change candidate that teaches a <file>.done
    receipt convention PASSES, so it clears the rule and lands in the queue for a
    human Promote click."""
    only = ["gate-demo"]
    model = config.smoke_model()
    print("demo: champion fails 'gate-demo' (writes status.txt but no .done receipt); "
          "a one-change candidate that teaches a receipt convention fixes it.\n")
    print("[1/3] champion baseline on gate-demo …")
    cmd_baseline(store, sample=0, scenario_ids=only, models=[model])
    cid = _seed_demo_candidate(store)
    print(f"\n[2/3] candidate {cid}: + a <file>.done completion-receipt convention "
          f"(one system_prompt change)")
    print(f"\n[3/3] evaluation round for {cid} …")
    cmd_round(store, cid, run_judge=False, scenario_ids=only, models=[model])

    cand = store.get_candidate(cid)
    stage = cand["stage"] if cand else "?"
    print("\n" + "=" * 64)
    if stage == "awaiting_gate":
        print(f"✅ {cid} CLEARED the rule and is AWAITING YOUR PROMOTION at the gate.")
        print("   It beat the champion on the working set, no regressions, no safety flag.")
        print("   Open the board and click Promote (the one human lever):")
        print("     factory/bin/factory board   →  http://127.0.0.1:8787")
    else:
        print(f"{cid} ended at stage '{stage}' (expected awaiting_gate).")
    print("Nothing was promoted automatically. Promotion is your action at the board.")
    print("=" * 64)


def _validate_check(abs_path: str) -> tuple[bool, str]:
    try:
        with open(abs_path, "r", encoding="utf-8") as fh:
            src = fh.read()
        compile(src, abs_path, "exec")
    except SyntaxError as e:
        return False, f"syntax error: {e}"
    except OSError as e:
        return False, str(e)
    if "def acceptance" not in src:
        return False, "no acceptance(ctx) function"
    return True, ""


def _staged_check_ready(sc: dict) -> tuple[bool, str]:
    chk = sc.get("check", "") or ""
    if not (chk.endswith(".py")):
        return False, "needs synth-check"
    abs_path = os.path.join(paths.FACTORY_ROOT, chk)
    if not os.path.exists(abs_path):
        return False, "check file missing"
    ok, msg = _validate_check(abs_path)
    return ok, ("ready" if ok else msg)


def cmd_staging() -> None:
    """List mined candidate scenarios awaiting operator vetting."""
    files = sorted(glob.glob(os.path.join(paths.STAGING_DIR, "*.yaml")))
    if not files:
        print("staging is empty — run `factory mine` to propose candidate scenarios.")
        return
    print(f"staged candidate scenarios ({len(files)}) — vet -> synth-check -> promote:")
    for f in files:
        with open(f, "r", encoding="utf-8") as fh:
            sc = yaml.safe_load(fh) or {}
        ready, status = _staged_check_ready(sc)
        seeds = len(sc.get("seed_files") or {})
        print(f"  {sc.get('id','?'):30} [{sc.get('class','single')}] seeds={seeds}  "
              f"check: {'✓ ' + sc['check'] if ready else '✗ ' + status}")
        print(f"      goal: {(sc.get('goal','') or '')[:96]}")


def cmd_show_scenario(staged_id: str) -> None:
    p = os.path.join(paths.STAGING_DIR, f"{staged_id}.yaml")
    if not os.path.exists(p):
        print(f"no staged scenario {staged_id!r}", file=sys.stderr)
        return
    with open(p, "r", encoding="utf-8") as fh:
        print(fh.read())


def cmd_synth_check(store: Blackboard, staged_id: str) -> None:
    """Synthesize a runnable acceptance check for a staged scenario (operator reviews)."""
    from ..roles.common import synth_check
    path = synth_check(store, staged_id)
    if not path:
        print(f"synth-check failed for {staged_id!r} (no staged scenario, or the model "
              f"returned no acceptance()).", file=sys.stderr)
        return
    ok, msg = _validate_check(path)
    rel = os.path.relpath(path, paths.FACTORY_ROOT)
    print(f"synthesized check: {rel}  [{'compiles OK' if ok else 'NEEDS FIX: ' + msg}]")
    print("REVIEW it before promoting — the check is the product, never trusted unread. Then:")
    print(f"  factory/bin/factory promote-scenario {staged_id} --partition working")


def cmd_promote_scenario(store: Blackboard, staged_id: str, partition: str) -> None:
    """Operator action: move a vetted staged scenario into the corpus + register it."""
    if partition not in ("working", "held-out"):
        print("partition must be 'working' or 'held-out'", file=sys.stderr)
        return
    p = os.path.join(paths.STAGING_DIR, f"{staged_id}.yaml")
    if not os.path.exists(p):
        print(f"no staged scenario {staged_id!r}", file=sys.stderr)
        return
    with open(p, "r", encoding="utf-8") as fh:
        sc = yaml.safe_load(fh) or {}
    ready, status = _staged_check_ready(sc)
    if not ready:
        print(f"refusing to promote {staged_id!r}: {status}. Run "
              f"`factory synth-check {staged_id}`, review the check, then promote.",
              file=sys.stderr)
        return
    sc["partition"] = partition
    sc["source"] = "mined"
    sc.setdefault("leakage_count", 0)
    dest_dir = paths.WORKING_DIR if partition == "working" else paths.HELD_OUT_DIR
    dest = os.path.join(dest_dir, f"{staged_id}.yaml")
    with open(dest, "w", encoding="utf-8") as fh:
        yaml.safe_dump(sc, fh, sort_keys=False, allow_unicode=True)
    os.remove(p)
    store.upsert_scenario(staged_id, cls=sc.get("class", "single"), partition=partition,
                          source="mined", spec_path=dest, goal=sc.get("goal", ""),
                          snapshot=sc.get("snapshot", ""), check_path=sc.get("check", ""))
    print(f"promoted {staged_id!r} -> {partition} corpus "
          f"({os.path.relpath(dest, paths.FACTORY_ROOT)}) + registered in the store.")


def cmd_report(store: Blackboard, mission: Optional[str] = None) -> str:
    """Executive-summary presentation of the run for the human's daily update.

    Generates a short plain-language summary (Discoveries / Decisions / Proposed
    next steps) from the store + staged files, prints it, and saves it under
    updates/YYYY-MM-DD-HHMM.md. Read-only: never promotes. The real clock lives
    here in the command layer (the library generator takes `since` explicitly)."""
    from datetime import datetime
    from ..reporting.summary import generate_executive_summary

    now = datetime.now()
    summary = generate_executive_summary(store, since=now.strftime("%Y-%m-%d"),
                                          mission=mission)
    updates_dir = os.path.join(paths.FACTORY_ROOT, "updates")
    os.makedirs(updates_dir, exist_ok=True)
    out_path = os.path.join(updates_dir, now.strftime("%Y-%m-%d-%H%M") + ".md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(summary if summary.endswith("\n") else summary + "\n")
    print(summary)
    print(f"\n[report] saved to {os.path.relpath(out_path, paths.FACTORY_ROOT)}")
    return out_path


def cmd_diary(store: Blackboard, mission: Optional[str] = None) -> str:
    """Write a first-person dev-diary entry (diary skill voice) narrating the latest
    autonomous work to `.dev-diary/<date>-<slug>.md`. Read-only over the store."""
    from datetime import datetime
    from ..reporting.diary import generate_diary_entry
    from .autonomy import _unique_path

    stamp = datetime.now().strftime("%Y-%m-%d")
    slug, entry = generate_diary_entry(store, since=stamp, mission=mission)
    ddir = os.path.join(paths.FACTORY_ROOT, ".dev-diary")
    os.makedirs(ddir, exist_ok=True)
    path = _unique_path(os.path.join(ddir, f"{stamp}-{slug}.md"))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(entry if entry.endswith("\n") else entry + "\n")
    print(entry)
    print(f"\n[diary] saved to {os.path.relpath(path, paths.FACTORY_ROOT)}")
    return path


def cmd_blog(store: Blackboard, mission: Optional[str] = None) -> str:
    """Write an accessible, Ars-Technica-style blog post about the ongoing autonomous
    work to `blog/<date>-<slug>.md`. Read-only over the store."""
    from datetime import datetime
    from ..reporting.blog import generate_blog_post
    from .autonomy import _unique_path

    stamp = datetime.now().strftime("%Y-%m-%d")
    slug, post = generate_blog_post(store, since=stamp, mission=mission)
    bdir = os.path.join(paths.FACTORY_ROOT, "blog")
    os.makedirs(bdir, exist_ok=True)
    path = _unique_path(os.path.join(bdir, f"{stamp}-{slug}.md"))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(post if post.endswith("\n") else post + "\n")
    print(post)
    print(f"\n[blog] saved to {os.path.relpath(path, paths.FACTORY_ROOT)}")
    return path


def cmd_develop_once(store: Blackboard, task: str, *, prod: bool = False,
                     keep: bool = False) -> dict:
    """ONE develop→grade→auto-merge turn against a THROWAWAY clone of the target — the
    dev-account smoke test of the autonomous code loop. A developer super-worker makes a
    bounded code change toward `task`, the round grades it (frozen-check + the target's
    own tests + scenario grade) and auto-merges into the throwaway clone (NEVER the real
    target). Dev mode (default): same-user SOFT boundary. `--prod`: the Guest-House user.

    The scenario grade here is a MECHANICS-smoke placeholder (do-no-harm) — the target's
    own test suite is the live gate. Wiring the full scenario eval over a code-built
    candidate is the next integration."""
    import shutil
    import tempfile
    from .develop import develop_and_merge

    adapter = config.get_adapter()
    sw = config.load_config().get("super_worker", {}) or {}
    as_user = (sw.get("user") or None) if prod else None
    claude_bin = (sw.get("claude_bin") or "claude") if prod else "claude"   # agent's claude only in prod
    print(f"[develop-once] task: {task!r}")
    print(f"[develop-once] mode: {'PROD (Guest House user=' + str(as_user) + ')' if prod else 'DEV (same-user soft boundary)'}")

    work = tempfile.mkdtemp(prefix="cf-champ-", dir="/tmp")
    main = os.path.join(work, "champion")
    champion_scores = {"working": 0.0, "held_out": 0.0}   # smoke baseline; tests are the live gate

    def grade_fn(repo_dir):   # mechanics smoke: do-no-harm; the real signal is run_tests in the round
        return {"working": 0.0, "held_out": 0.0, "held_out_measured": True,
                "divergence_alarm": False, "safety_flag": False}

    try:
        adapter.clone(main)   # throwaway clone of the target = the test champion
        print(f"[develop-once] champion clone: {main}")
        res = develop_and_merge(adapter=adapter, main_repo=main, task=task,
                                champion_scores=champion_scores, grade_fn=grade_fn,
                                as_user=as_user, claude_bin=claude_bin)
        print(f"[develop-once] result: {json.dumps(res, indent=2, default=str)}")
        if keep and res.get("action") == "merged":
            print(f"\n[develop-once] --keep: inspect the candidate the worker produced:")
            print(f"    cd {main} && git show {res.get('merge_sha','HEAD')}   # the merge")
            print(f"    cd {main} && git log --oneline -5")
        return res
    finally:
        if keep:
            print(f"[develop-once] --keep: champion clone left at {main}")
        else:
            shutil.rmtree(work, ignore_errors=True)   # throwaway clone never touches the real target


# Task 1.1: the reopen provenance marker. Prepended (one line per reopen — they STACK) to the
# narrowed brief, so the reopen count needs no schema change: count these lines in detail.
_REOPEN_PREFIX = "previously blocked: "
_MAX_REOPENS = 2                       # a 3rd reopen is refused → escalate to @human


def cmd_task(store: Blackboard, action: str, *, rest: Optional[str] = None,
             source: str = "human", result: str = "", status: Optional[str] = None,
             detail: str = "") -> None:
    """The backlog CLI the conductor drives: `task list [--status open]`,
    `task add "<title>" [--detail "<spec/brief>"]`, `task claim <id>`,
    `task done <id> [--result <sha>]`, `task block <id> [--result why]`,
    `task reopen <id> --detail "<narrowed brief>"` (blocked → open with provenance; Task 1.1).
    claim/done STAMP the running shift, so the loop can tell what a shift shipped (the basis
    for mission-progress). `--detail` carries the bounded brief/spec to the developer."""
    if action == "list":
        for t in store.list_tasks(status=status):
            print(f"{t['id']}\t{t['status']}\t[{t['source']}] {t['title']}")
    elif action == "add":
        import uuid
        tid = f"task-{uuid.uuid4().hex[:8]}"
        store.add_task(tid, rest or "(untitled)", source=source, detail=detail)
        print(f"[task] added {tid}: {rest}")
    elif action == "claim":
        store.set_task_status(rest, "in_progress", shift_id=store.current_shift_id())
        print(f"[task] claimed {rest}")
    elif action == "done":
        store.set_task_status(rest, "done", result=result, shift_id=store.current_shift_id())
        print(f"[task] done {rest}")
    elif action == "block":
        store.set_task_status(rest, "blocked", result=result)
        print(f"[task] blocked {rest}")
    elif action == "reopen":
        # The blocked → narrowed-brief → redispatch loop, without hand-editing tasks.detail.
        # Exact-id discipline (mirrors cmd_plan's _need_task): a partial/unknown id refuses
        # loudly — the silent-0-row-success bug class documented on `task claim`.
        t = store.get_task(rest)
        if t is None:
            print(f"[task] no task matches id '{rest}' exactly — pass the full task-<hash> (0 rows)")
            return
        if t["status"] != "blocked":
            print(f"[task] {rest} is not blocked (status={t['status']}) — "
                  "reopen narrows BLOCKED tasks only (0 rows)")
            return
        if not detail:
            print("[task] reopen requires --detail with the NARROWED brief — redispatching "
                  "the same brief re-runs the same failure (0 rows)")
            return
        # Provenance lines stack newest-first; counting them IS the reopen counter (no schema).
        prior = [ln for ln in (t.get("detail") or "").splitlines()
                 if ln.startswith(_REOPEN_PREFIX)]
        if len(prior) >= _MAX_REOPENS:
            print(f"[task] {rest} was already reopened {_MAX_REOPENS}x and blocked again — "
                  "refusing a 3rd reopen; escalate to @human via agora (0 rows)")
            return
        reason = " ".join((t.get("result") or "").split()) or "(no reason recorded)"
        store.set_task_detail(rest, "\n".join([_REOPEN_PREFIX + reason] + prior + [detail]))
        # The durable spec (target_surface/acceptance) described the OLD brief — left in
        # place, scope_check would fold the stale spec into the redispatched worker brief,
        # contradicting the narrowed detail. Clear it; the next scope check re-derives it.
        store.set_task_spec(rest, None)
        store.set_task_status(rest, "open", result="")   # result is NOT NULL — clear via ''
        print(f"[task] reopened {rest} (reopen {len(prior) + 1} of {_MAX_REOPENS}) — "
              "detail narrowed, status open (1 row)")


def cmd_timesheet(store: Blackboard, *, shift: Optional[int] = None, limit: int = 200) -> None:
    """Agent timesheet: who worked when, how long, at what spend, to what verdict — an aligned
    table of shift-attributed engagements + the all-time per-role rollup (incl. legacy)."""
    from ..reporting import timesheets
    # Filter by shift IN THE QUERY, not after LIMIT — a Python post-filter over the newest
    # `limit` rows silently returns empty for an older shift once the ledger outgrows the window.
    rows = timesheets.timesheet(store, limit=limit, shift_id=shift)
    print(f"{'shift':>5}  {'agent':<22}  {'task':<26}  {'min':>5}  {'tokens':>8}  {'$':>7}  verdict")
    for r in rows:
        print(f"{r['shift'] or '':>5}  {r['agent'][:22]:<22}  {(r['task_title'] or '')[:26]:<26}  "
              f"{(r['seconds'] or 0) / 60:>5.1f}  {int(r['tokens'] or 0):>8,}  "
              f"{float(r['cost'] or 0):>7.3f}  {r['verdict'] or ''}")
    print("\nall-time per-role (incl. legacy):")
    for a in timesheets.by_agent(store):
        print(f"  {a['role']:<16} {a['engagements']:>4} eng  {int(a['tokens']):>10,} tok  "
              f"${float(a['cost']):>8.2f}  {(a['seconds'] or 0) / 60:>7.1f} min")
    # Per-shift WALL-CLOCK (started → ended) — the time counterpart of the per-shift token spend.
    print("\nper-shift clock (wall-time started → ended):")
    for c in timesheets.shift_clock(store, limit=limit):
        if shift is not None and c["shift"] != shift:
            continue
        secs = c["seconds"]
        clk = f"{int(secs) // 60}m {int(secs) % 60}s" if secs is not None else "running"
        print(f"  S{c['shift']:<5} {c['status']:<16} {(c['started_at'] or '')[:19]:<20} {clk:>10}")


def cmd_worker(store: Blackboard, action: str, *, rest: Optional[list] = None,
               description: str = "", overlay: str = "", model: str = "") -> None:
    """The conductor's on-demand workforce lever (worker capability profiles):
      worker list                       # profiles + active flag + per-profile spend/outcomes
      worker add <name> --description D --overlay O [--model frontier|standard|fast]
      worker retire <name>
    A profile is DATA (persona overlay + model tier) only — never toolset/sandbox/frozen/gates.
    Guardrails (slug, tier whitelist, overlay bound, active cap, generalist-unretireable) live in
    reporting.worker_admin so the board's POST /api/worker enforces the exact same policy."""
    from ..reporting import worker_admin, timesheets
    rest = rest or []

    if action == "list":
        roll = {r["profile"]: r for r in timesheets.by_profile(store)}
        profs = store.list_profiles(active_only=False)
        if not profs:
            print("(no profiles yet — the bench seeds at the next `factory run`, or add one with "
                  "`factory worker add …`)")
            return
        print(f"{'name':<16} {'tier':<9} {'state':<7} {'eng':>4} {'merged':>7} {'tokens':>10}  description")
        for p in profs:
            o = roll.get(p["name"], {})
            tier = p.get("model") or "frontier"
            print(f"{p['name']:<16} {tier:<9} {'active' if p['active'] else 'retired':<7} "
                  f"{int(o.get('engagements', 0)):>4} {int(o.get('merged', 0)):>7} "
                  f"{int(o.get('tokens', 0)):>10,}  {(p.get('description') or '')[:48]}")
        return

    name = rest[0] if rest else ""
    if action == "add":
        err = worker_admin.validate_add(name, model, overlay) or worker_admin.cap_error(store, name)
        if err:
            print(f"[worker] {err}")
            raise SystemExit(2)
        store.add_profile(name, description=description, overlay=overlay, model=model,
                          created_by="conductor")
        print(f"[worker] added profile {name} (tier {model or 'frontier'})")
    elif action == "retire":
        err = worker_admin.retire_error(store, name)
        if err:
            print(f"[worker] {err}")
            raise SystemExit(2)
        store.retire_profile(name)
        print(f"[worker] retired profile {name}")


def cmd_evm(store: Blackboard) -> None:
    """Agent-adapted EVM: the PV/EV/AC/CPI/%-complete totals, the per-milestone breakdown, and
    the estimate-vs-actual list (the conductor's plan-revision feedback signal). Overhead
    (conductor/research spend) is reported separately, never smeared across milestones."""
    from ..reporting import evm as evmmod
    e = evmmod.evm(store)
    cpi = f"{e['cpi']:.2f}" if e["cpi"] is not None else "—"
    pct = f"{e['percent_complete'] * 100:.0f}%" if e["percent_complete"] is not None else "—"
    print(f"EVM  PV {e['pv']:,}  EV {e['ev']:,}  AC {e['ac_tokens']:,} tok  CPI {cpi}  "
          f"complete {pct}  overhead {e['overhead_tokens']:,} tok (${e['overhead_cost']:.2f})")
    print(f"{'id':>4}  {'status':<10}  {'title':<30}  {'PV':>10}  {'EV':>10}  {'AC':>10}  prog")
    for m in e["milestones"]:
        pr = m["progress"]
        print(f"{m['id']:>4}  {m['status']:<10}  {m['title'][:30]:<30}  {m['pv']:>10,}  "
              f"{m['ev']:>10,}  {m['ac_tokens']:>10,}  {pr['done']}/{pr['total']}")
    if e["estimates"]:
        print("\nestimate vs actual:")
        for r in e["estimates"]:
            ratio = (r["actual"] / r["est"]) if r["est"] else 0
            print(f"  {r['task'][:22]:<22}  est {r['est']:>10,}  actual {r['actual']:>10,}  ({ratio:.1f}x)")


_MILESTONE_STATUS = ("planned", "active", "delivered", "dropped")


def _parse_milestone_id(token: str) -> Optional[int]:
    """Accept the milestone id in either form the tools DISPLAY: bare `3` or `M3`/`m3`
    (plan list + the conductor's {PLAN} render both print 'M<id>', so the write commands must
    take that back). Returns the int, or None if it isn't a milestone id."""
    s = (token or "").strip()
    if s[:1] in ("M", "m"):
        s = s[1:]
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _parse_tokens(token: str) -> Optional[int]:
    """Parse a token count, accepting the human/LLM shorthand `60k` / `1.5m` as well as a bare
    integer. Returns None if it can't be parsed (so the caller reports a clean error, not a crash)."""
    s = (token or "").strip().lower().replace(",", "").replace("_", "")
    mult = 1
    if s.endswith("k"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    try:
        return int(round(float(s) * mult))
    except (TypeError, ValueError):
        return None


def cmd_plan(store: Blackboard, action: str, *, rest: Optional[list] = None,
             deliverable: str = "", acceptance: str = "", budget_tokens: int = 0,
             order: int = 0, status: Optional[str] = None, profile: str = "") -> None:
    """The conductor's plan lever (the persisted milestone plan):
      plan add "<title>" [--deliverable D] [--acceptance A] [--budget-tokens N] [--order N]
      plan list [--status planned|active|delivered|dropped]
      plan status <milestone-id> <planned|active|delivered|dropped>
      plan link <task-id> <milestone-id>
      plan estimate <task-id> <est-tokens> [--profile NAME]
    link/estimate match the task id EXACTLY and print how many rows changed — the silent-no-op
    bug class known from `task claim` (a partial id must not print a false success)."""
    rest = rest or []

    def _need_task(task_id: str) -> bool:                      # FULL-ID discipline for link/estimate
        if store.get_task(task_id) is None:
            print(f"[plan] no task matches id '{task_id}' exactly — pass the full task-<hash> (0 rows)")
            return False
        return True

    if action == "add":
        title = rest[0] if rest else "(untitled milestone)"
        m = store.active_mission()
        mid = store.add_milestone(title, mission_id=(m["id"] if m else None),
                                  deliverable=deliverable, acceptance=acceptance,
                                  budget_tokens=budget_tokens, planned_order=order)
        print(f"[plan] added milestone {mid}: {title}")
    elif action == "list":
        ms = store.list_milestones(status=status)
        if not ms:
            print("[plan] no plan yet — draft 2-4 milestones with `factory plan add …`")
        for m in ms:
            p = store.milestone_progress(m["id"])
            print(f"M{m['id']}\t{m['status']}\t{p['done']}/{p['total']} tasks\t"
                  f"{m['budget_tokens']:,} tok\t{m['title']}")
    elif action == "status":
        if len(rest) < 2 or rest[1] not in _MILESTONE_STATUS:
            print("[plan] usage: plan status <milestone-id> <" + "|".join(_MILESTONE_STATUS) + ">")
            return
        mid = _parse_milestone_id(rest[0])
        if mid is None or store.get_milestone(mid) is None:  # don't print a false success on a
            print(f"[plan] no milestone matches '{rest[0]}' — see `plan list` (0 rows)")  # missing id
            return
        # Task 3.3: independent milestone-delivery grader (gated OFF). ONLY the 'delivered'
        # transition is guarded: refuse while any linked task is still unresolved (open/claimed/
        # in_progress/blocked — 'done' and 'dropped' are BOTH resolved, so a dropped task can never
        # make delivery unreachable), and refuse an UNVERIFIABLE delivery (total==0 is not trivially
        # complete). The exact-id refusal names the blocking task ids; the '(unverified)' side is a
        # render-time label only (roles/conductor.py:_plan_bullets — never stored).
        if rest[1] == "delivered" and config.resolve_setting(
                store, "super_worker.milestone_verify", False)[0]:
            if store.milestone_progress(mid)["total"] == 0:
                print(f"[plan] milestone M{mid} has NO linked tasks — delivery is UNVERIFIABLE; "
                      f"link its tasks with `plan link` first (0 rows)")
                return
            open_ids = store.milestone_open_task_ids(mid)
            if open_ids:
                print(f"[plan] milestone M{mid} not delivered — {len(open_ids)} linked task(s) "
                      f"still open: {', '.join(open_ids)} (resolve or drop them first) (0 rows)")
                return
        store.set_milestone_status(mid, rest[1])
        print(f"[plan] milestone M{mid} → {rest[1]} (1 row)")
    elif action == "link":
        if len(rest) < 2:
            print("[plan] usage: plan link <task-id> <milestone-id>"); return
        if not _need_task(rest[0]):
            return
        mid = _parse_milestone_id(rest[1])
        if mid is None or store.get_milestone(mid) is None:  # a dangling link is silent corruption
            print(f"[plan] no milestone matches '{rest[1]}' — see `plan list` (0 rows)")
            return
        store.set_task_milestone(rest[0], mid)
        print(f"[plan] linked {rest[0]} → milestone M{mid} (1 row)")
    elif action == "estimate":
        if len(rest) < 2:
            print("[plan] usage: plan estimate <task-id> <est-tokens> [--profile NAME]"); return
        if not _need_task(rest[0]):
            return
        est = _parse_tokens(rest[1])
        if est is None:
            print(f"[plan] can't parse '{rest[1]}' as a token count (try 60000 or 60k) (0 rows)")
            return
        store.set_task_estimate(rest[0], est)
        if profile:
            store.set_task_profile(rest[0], profile)
        print(f"[plan] estimated {rest[0]} at {est:,} tokens"
              + (f", profile={profile}" if profile else "") + " (1 row)")


def _read_mission_md() -> Optional[str]:
    """The `## Mission` statement from MISSION.md, normalized to one line (or None). A seam
    the run-start mission sync + its tests hook (FACTORY_ROOT is a hardcoded path)."""
    from ..research.focus import read_mission
    return read_mission(paths.factory("MISSION.md"))


def _seed_staffing(store: Blackboard) -> list:
    """Ensure the target-derived worker bench exists (Task 5.2) — additive, idempotent. Best
    effort: a config/filesystem hiccup must never break a run (the generalist fallback covers
    dispatch regardless), and it's a tiny helper so the cmd_run tests can stub it like the
    mission sync. Returns the profile names newly seeded this run."""
    try:
        from ..reporting import staffing
        return staffing.ensure_seeded(store, config.clive_entry()[0], config.target_repo_slug())
    except Exception as e:  # noqa: BLE001 — staffing is telemetry setup, not a run gate
        print(f"[run] staffing skipped: {e}")
        return []


def _write_mission_md(statement: str) -> None:
    """Reflect an explicit --mission steer into MISSION.md so it survives the next run-start
    sync (otherwise the unchanged file would overwrite it). A tests-hookable seam."""
    from ..research.focus import write_mission
    write_mission(paths.factory("MISSION.md"), statement)


def cmd_run(store: Blackboard, *, mission: Optional[str] = None, token_budget: Optional[int] = None,
            wall_clock_s: Optional[int] = None, prod: bool = False, plateau_k: int = 3,
            real: bool = False, conductor=None, executor=None, refill=None) -> dict:
    """The conductor loop entry point (design step 6): run ONE bounded shift, then assess
    the mission and surface the status. State persists in the store, so each `run` resumes
    where the last left off — schedule it (launchd) for the unattended daily cadence."""
    from .shift import run_shift
    from .mission import assess

    cfg = config.load_config()
    auton = cfg.get("autonomy", {}) or {}
    token_budget = token_budget or int(auton.get("daily_token_budget", 500000))
    wall_clock_s = wall_clock_s or int(auton.get("shift_wall_clock_s", 1800))
    sw = cfg.get("super_worker", {}) or {}

    # MISSION.md steers the live loop (Task 1.1): the human's file wins at every run start.
    # An explicit --mission always beats the file; an unchanged file never re-steers (compare
    # normalized whitespace on both sides, so a whitespace edit alone can't spawn a mission row).
    if mission is None:
        file_mission = _read_mission_md()
        active = store.active_mission()
        active_norm = " ".join(active["statement"].split()) if active else ""
        if file_mission and file_mission != active_norm:
            store.set_mission(file_mission)
            print(f"[run] mission re-steered from MISSION.md: {file_mission[:80]}…")
    else:                                     # an explicit --mission is durable: write it to the file
        _write_mission_md(mission)            # so the next (file-driven) run doesn't overwrite it

    # The bench follows the target (Task 5.2): seed the stack specialists this target needs,
    # additive + idempotent, before the shift dispatches by profile.
    seeded = _seed_staffing(store)
    if seeded:
        print(f"[run] staffing: seeded {', '.join(seeded)} for this target")

    # IDLE short-circuit: if the loop has been steady for K shifts with an empty backlog and
    # the operator isn't re-steering, DON'T spawn a conductor — surface and wait. This is
    # what makes 'recommend_stop' real (otherwise a scheduled loop spawns forever).
    if mission is None and _should_idle(store, plateau_k):
        print(f"[run] idle: steady state for {plateau_k}+ shifts, backlog empty — nothing to "
              f"do toward the mission. Awaiting a revision (factory run --mission \"…\").")
        return {"action": "idle", "shift_id": None}

    as_user = (sw.get("user") or None) if prod else None
    claude_bin = (sw.get("claude_bin") or "claude") if prod else "claude"   # agent's claude only in prod
    if conductor is None:                              # live: the claude conductor (PLANS only)
        from ..roles.conductor import run_conductor
        if not prod:
            print("[run] ⚠ DEV mode: the conductor + workers run as YOU (same-user) with Bash + "
                  "your MCP loaded — supervised only; do not schedule unattended. Use --prod for "
                  "the Guest-House boundary.")
        conductor = lambda st, **kw: run_conductor(st, as_user=as_user, claude_bin=claude_bin, **kw)
    if real:
        print("[run] REAL mode: gated merges land on branch factory/auto in the REAL target "
              "(git-reversible; your working branch is untouched).")
    # Whitelisted runtime knobs resolve store override → config.yaml → default (Task 6.1), so the
    # board can retune the next shift. _k() reads the store override each run start.
    _k = lambda key, default: config.resolve_setting(store, f"super_worker.{key}", default)[0]
    if executor is None:                               # the deterministic rail EXECUTES claimed tasks
        from .develop import execute_claimed_tasks
        max_tasks = _k("max_tasks_per_shift", 3)        # unattended: cap per-shift fan-out
        max_parallel = _k("max_parallel", 3)            # …run that many super-workers at once
        require_test = _k("require_test", False)         # GSD spec-bound acceptance gate (threaded)
        reviewer = _k("reviewer", False)                 # Phase 8: config-gated pre-merge review gate
        acceptance_exec = _k("acceptance_exec", False)   # Task 3.1: run the spec's named acceptance test
        investigate = _k("investigate_blocked", False)   # Task 4.1: post-shift investigator (P6 2-3)
        scope_on, decompose_on = _k("scope_check", False), _k("auto_decompose", False)
        sj = dc = None                                  # GSD spec-driven checks (config-gated; see super_worker.*)
        if scope_on or decompose_on:
            from ..reporting import scope_check
            if scope_on:                                # #1: pass/split/reject BEFORE dispatch
                sj = lambda task: scope_check.scope_judge(task, as_user=as_user, claude_bin=claude_bin)
            if decompose_on:                            # #4: split a no_candidate AFTER the worker fails
                dc = lambda task: scope_check.decompose_judge(task, as_user=as_user, claude_bin=claude_bin)
        executor = lambda st, *, shift_id: execute_claimed_tasks(
            st, shift_id, as_user=as_user, claude_bin=claude_bin, real=real,
            max_tasks=max_tasks, max_parallel=max_parallel, scope_judge=sj, decomposer=dc,
            require_test=require_test, reviewer=reviewer, acceptance_exec=acceptance_exec,
            investigate_blocked=investigate)
    if refill is None:                                 # …and REFILLS the backlog from research when thin
        from ..roles import research_feed
        refill = lambda st: research_feed.propose_directions(st, as_user=as_user, claude_bin=claude_bin)
    refill_threshold = _k("refill_threshold", 2)

    res = run_shift(store, token_budget=token_budget, conductor=conductor, executor=executor,
                    refill=refill, refill_threshold=refill_threshold,
                    mission=mission, wall_clock_s=wall_clock_s)
    print(f"[run] shift {res.get('shift_id')}: {res['action']} "
          f"(reaped {res.get('reaped', 0)} crashed; shipped {res.get('shipped', 0)})")

    if res.get("shift_id"):                            # a shift actually ran → assess the mission
        shipped_tasks = [t for t in store.list_tasks(status="done")
                         if t.get("shift_id") == res["shift_id"]]
        m = assess(store, shift_id=res["shift_id"], shipped_count=res.get("shipped", len(shipped_tasks)),
                   plateau_k=plateau_k)
        if shipped_tasks:                              # auto-emit the digest — don't trust the LLM to
            store.add_digest(shift_id=res["shift_id"],
                             shipped=[t["id"] for t in shipped_tasks],
                             summary="shipped: " + "; ".join(t["title"] for t in shipped_tasks))
        print(f"[run] mission status: {m['status']} — {m['rationale']}")
        # AFTER assess — a filed lag task must inform the NEXT shift's backlog, not corrupt
        # this shift's status/plateau (mirrors _graduate_after_shift). Passive, every mode.
        _warn_graduation_lag(store)
        if real and res.get("shipped", 0):
            root = config.get_adapter().entry()[0]
            print(f"[run] real-clive: {res['shipped']} merge(s) on branch factory/auto — review: "
                  f"git -C {root} log --oneline factory/auto   (revert/cherry-pick as you like)")
            grad = _graduate_after_shift(store, real=real, shipped=res.get("shipped", 0))
            if grad.get("action") == "synced":
                touched = sum(1 for r in grad.get("synced", [])
                              if r.get("action") in ("comment", "close"))
                print(f"[run] graduated → pushed {grad.get('n_commits', 0)} commit(s) to origin; "
                      f"{touched} issue(s) synced")
            elif grad.get("action") in ("skip", "error"):
                print(f"[run] graduate: {grad['action']} "
                      f"({grad.get('reason') or grad.get('error', '')})")
        if m["recommend_stop"]:
            print(f"[run] ⏸  STEADY STATE for {plateau_k} shifts — nothing left toward the "
                  f"mission. Awaiting a mission revision (re-steer: factory run --mission \"…\").")
    return res


def cmd_learn(store: Blackboard, action: str, *, role: Optional[str] = None, content: str = "",
              scope: str = "general", agent: str = "", limit: int = 20,
              learning_id: Optional[str] = None, apply: bool = False):
    """`factory learn add [--role R] "…"` (or --content "…") / `factory learn list
    [--role R]` / `factory learn retire <id>` / `factory learn verify` — the factory's
    memory CLI. The CLI positional is action-routed by main(): add's TEXT arrives here
    as `content`, retire's id as `learning_id` (Fix 1.3b).
    Agents (the conductor + super-workers via Bash) record durable learnings here; the
    orchestrator injects them back into each role's prompt via
    reporting.factory_memory.memory_card. Adds are dedup'd. `add` defaults to the `factory`
    role; `list` with no role shows EVERY role's learnings. `retire` is the operator's
    correction handle (archived=1, hidden from every prompt — exact integer id, unknown ids
    refuse loudly); `verify` is the zero-token staleness check (flags dead file cites,
    advisory only). (design: docs/plans/2026-06-27-factory-memory-design.md; Task 1.3)"""
    from ..reporting import factory_memory
    if action == "retire":
        # Exact-id discipline (mirrors cmd_plan's _need_task): the id is the integer
        # PRIMARY KEY — anything that isn't one, or doesn't exist, refuses explicitly.
        # A silent 0-row "success" is the documented `task claim` bug class.
        try:
            lid = int(learning_id)                        # type: ignore[arg-type]
        except (TypeError, ValueError):
            print(f"[learn] retire needs an integer learning id, got {learning_id!r} — "
                  "see `factory learn list` (0 rows)")
            return None
        row = store.get_learning(lid)
        if row is None:
            print(f"[learn] no learning matches id {lid} exactly — "
                  "see `factory learn list` (0 rows)")
            return None
        store.archive_learning(lid)
        print(f"[learn] retired #{lid} [{row['role']}]: {row['content']} (1 row)")
        return lid
    if action == "verify":
        report = factory_memory.verify_learnings(store)
        stale = [e for e in report if e["stale"]]
        for e in stale:
            print(f"[learn] #{e['id']} [{e['role']}] may be stale — dead cite(s): "
                  + ", ".join(e["stale_cites"]))
        print(f"[learn] verified {len(report)} cite-carrying learning(s): "
              f"{len(stale)} stale, {len(report) - len(stale)} ok "
              "(advisory — nothing deleted or archived)")
        return report
    if action == "add":
        role = role or "factory"                          # add defaults to the cross-cutting role
        rec = factory_memory.record_learning(store, role, content, agent=agent, scope=scope,
                                             shift_id=store.current_shift_id())
        if rec is None:
            print(f"[learn] not recorded (empty): {content!r}")
            return None
        lid, created = rec
        if created:
            print(f"[learn] recorded #{lid} for {role}: {content}")
        else:                                             # dedup-hit → recurrence counted (Task 0.5)
            hits = (store.get_learning(lid) or {}).get("hits", 1)
            print(f"[learn] reinforced #{lid} (x{hits}) for {role}: {content}")
        return lid
    if action == "list":
        rows = store.learnings_for_role(role, limit=limit) if role else store.all_learnings(limit)
        if not rows:
            print(f"[learn] no learnings for {role or 'any role'} yet.")
        for r in rows:
            mark = (" [archived]" if r.get("archived")
                    else " [stale?]" if r.get("stale") else "")
            # Consult-telemetry (Task 1.4): the merged-share of tasks whose worker card
            # surfaced this row — SUPPRESSED below the minimum denominator (noise floor).
            eff = factory_memory.effectiveness(r)
            eff_s = f", eff {eff[0]:.0%} of {eff[1]}" if eff else ""
            print(f"  [{r['role']}] #{r['id']} (uses {r['uses']}, hits {r.get('hits', 1)}"
                  f"{eff_s}){mark}: {r['content']}")
        return rows
    if action == "distill":
        # `factory learn distill --role R [--apply]` (Task 4.2, P6 stage 4): consolidate a
        # role's overlapping lessons into <=5 pinned rules. Dry-run by DEFAULT (proposals
        # only); --apply is a HUMAN act that inserts scope='distilled', pinned=1 and archives
        # the sources. SPENDS one standard-tier claude_p; STOP vetoes at entry, spend ledgers
        # notes='distill'. Fail-open: a bad reply proposes nothing.
        if not role:
            print("[learn] distill needs --role R (conductor | developer | researcher | factory)")
            return None
        rep = factory_memory.distill_learnings(store, role, apply=apply)
        proposed = rep.get("proposed") or []
        if not proposed:
            print(f"[learn] distill {role}: nothing to consolidate ({rep.get('reason') or 'no rules'})")
            return rep
        verb = "APPLIED" if rep.get("applied") else "propose"
        for p in proposed:
            src = ", ".join(f"#{s}" for s in p["sources"]) or "(no sources cited)"
            print(f"[learn] {verb}: {p['rule']}  <- {src}")
        if rep.get("applied"):
            print(f"[learn] distilled {len(rep.get('distilled_ids') or [])} pinned rule(s) for "
                  f"{role}; cited sources archived")
        else:
            print(f"[learn] dry-run — re-run `factory learn distill --role {role} --apply` to "
                  "insert the rules (pinned, scope='distilled') and archive the sources")
        return rep
    print('[learn] usage: factory learn add [--role R] "…" | '
          'factory learn list [--role R] | factory learn retire <id> | factory learn verify | '
          'factory learn distill --role R [--apply]')
    return None


def cmd_eval_gates(store: Blackboard, *, gate: str = "scope") -> dict:
    """`factory eval-gates [--gate scope]` — replay the hand-authored golden briefs
    (scenarios/gates/<gate>.jsonl) through the LIVE scope judge and print the per-case
    + aggregate scorecard (roadmap Task 2.1, P12: the improvement layer's own
    regression check — run it after every prompt/judge edit). SPENDS TOKENS (one
    judge call per fixture, hard-capped) so it is an explicit OPERATOR act, never
    wired into any loop: STOP vetoes at entry, every case's spend ledgers under
    role='gate_eval' — attributed to the RUNNING shift when one exists (folds into
    the shift/loop token brakes), with a NULL shift_id on standalone runs."""
    from ..reporting import gate_eval
    return gate_eval.run_gate_eval(store, gate=gate, shift_id=store.current_shift_id())


def cmd_graduate(store: Blackboard, *, dry_run: bool = False) -> Optional[dict]:
    """`factory graduate [--dry-run]` — the operator's manual handle on the same flow the
    autopilot runs after a shift ships: ff base→factory/auto, push base to origin, and
    sync the target's GitHub issues for the pushed commits (keyword-gated close).
    --dry-run previews the range + issue actions, mutating nothing."""
    from ..reporting import issue_sync
    repo = config.target_repo_slug()
    if not repo:
        print("[graduate] no target repo resolved (set target.repo in config.yaml) — skipping.")
        return None
    root = config.get_adapter().entry()[0]
    base = config.target_config().get("base_branch") or "chore/extract-factory"
    res = issue_sync.graduate_and_push(root=root, base=base, repo=repo, store=store,
                                       test_fn=_graduation_test_fn(), dry_run=dry_run)
    action = res.get("action")
    if action in ("synced", "dry_run"):
        synced = res.get("synced", [])
        for r in synced:
            if r.get("action") in ("comment", "close"):
                print(f"[graduate]   #{r['issue']}: {r['action']} ({len(r.get('commits', []))} commit(s))")
        verb = "would push" if dry_run else "pushed"
        touched = sum(1 for r in synced if r.get("action") in ("comment", "close"))
        print(f"[graduate] {verb} {res.get('n_commits', 0)} commit(s) over {res.get('range', '')}; "
              f"{touched} issue(s) {'to sync' if dry_run else 'synced'}.")
    else:
        print(f"[graduate] {action}: {res.get('reason') or res.get('error', '')}")
    return res


def _factory_auto_head(root: str) -> Optional[str]:
    """The champion's current HEAD sha (the last accumulated merge on factory/auto)."""
    import subprocess
    r = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def cmd_rebaseline(store: Blackboard, *, dry_run: bool = False, full_scores_fn=None,
                   adapter=None, champ_root: Optional[str] = None, head_sha_fn=None) -> dict:
    """`factory rebaseline [--dry-run]` — the PERIODIC full re-baseline (real-merge-grade Piece 5).
    Runs the FULL scenario suite (working + held-out, which the inline gate defers) against the
    current champion source, compares to the stored baseline, and — gated by
    grade.rebaseline_autorevert (default OFF) — auto-reverts the champion's factory/auto HEAD on a
    regression. Reports either way. Off the merge path (a scheduled job). --dry-run measures +
    reports but stores nothing and never reverts. All I/O seams injectable for hermetic tests."""
    import json
    from ..common import code_gate, paths
    from . import grade as grademod
    from .develop import factory_worktree
    scenarios = store.list_scenarios()
    if not scenarios:
        print("[rebaseline] no active scenarios — nothing to measure.")
        return {"action": "skip", "reason": "no-scenarios"}
    adapter = adapter or config.get_adapter()
    champ_root = champ_root or factory_worktree(adapter)
    model_entry = config.panel_models()[0]
    fs = full_scores_fn or grademod.full_scores
    current = fs(store, clive_root=champ_root, spec_path=paths.CHAMPION_YAML,
                 model_entry=model_entry, scenarios=scenarios)
    prior_raw = store.get_setting("grade.baseline")
    prior = json.loads(prior_raw) if prior_raw else None
    reg = (code_gate.regression_after_merge(prior, current) if prior
           else {"regressed": False, "why": ["first baseline"]})
    autorevert = bool((config.load_config().get("grade") or {}).get("rebaseline_autorevert", False))
    reverted = None
    if reg["regressed"] and autorevert and not dry_run:
        sha = (head_sha_fn or _factory_auto_head)(champ_root)
        if sha:
            reverted = adapter.revert_commit(champ_root, sha)
    if not dry_run:
        store.set_setting("grade.baseline", json.dumps(current))
    status = ("REGRESSED: " + ", ".join(reg["why"])) if reg["regressed"] else "no regression"
    print(f"[rebaseline] working={current.get('working')} held_out={current.get('held_out')} "
          f"(n={current.get('n_working', 0)}w+{current.get('n_held_out', 0)}h) — {status}"
          + (f"; reverted {reverted}" if reverted else "")
          + (" [dry-run]" if dry_run else ""))
    return {"action": "rebaselined", "scores": current, "regression": reg,
            "reverted": reverted, "dry_run": dry_run}


def _graduation_test_fn():
    """The prod-push quality gate's re-test hook (Theme 4), or None when disabled. A config-only
    brake (autonomy.graduation_retest, default ON, like enforce_shift_budget — the board can't
    flip it off): re-run the TARGET's suite on the integrated factory/auto tip before pushing to
    the real repo, since the per-task merges each tested only their own change, not the tip.
    Returns a LAZY closure so config.get_adapter() is resolved only if the gate actually runs
    (keeps callers that inject their own graduate_fn hermetic)."""
    auton = config.load_config().get("autonomy", {}) or {}
    if not auton.get("graduation_retest", True):
        return None
    return lambda root: config.get_adapter().run_tests(root)


def _graduate_after_shift(store: Blackboard, *, real: bool, shipped: int,
                          graduate_fn=None, repo: Optional[str] = None,
                          root: Optional[str] = None, base: Optional[str] = None,
                          stop_check=None) -> dict:
    """After a shift ships in REAL mode, graduate (ff base→factory/auto) + push to
    origin + sync the target's GitHub issues (design:
    docs/plans/2026-06-27-factory-auto-issue-sync-design.md). Fail-CLOSED and NEVER
    raises — any graduate/sync error is logged and swallowed so it cannot kill the
    autonomous loop. Skips entirely unless real AND something shipped."""
    if not (real and shipped):
        return {"action": "skip", "reason": "not-real-or-nothing-shipped"}
    try:
        from ..reporting import issue_sync
        from ..common import killswitch
        graduate_fn = graduate_fn or issue_sync.graduate_and_push
        repo = repo if repo is not None else config.target_repo_slug()
        if not repo:
            return {"action": "skip", "reason": "no-repo"}
        root = root or config.get_adapter().entry()[0]
        base = base or (config.target_config().get("base_branch") or "chore/extract-factory")
        return graduate_fn(root=root, base=base, repo=repo, store=store,
                           stop_check=stop_check or killswitch.is_halted,
                           test_fn=_graduation_test_fn())
    except Exception as e:  # noqa: BLE001 — a graduate/sync error must never crash the loop
        err = str(e)
        print(f"[run] graduate+sync skipped (non-fatal error): {err}")
        _maybe_file_graduation_failure(store, err)
        return {"action": "error", "error": err}


_GRAD_LAG_ALARM = 12   # commits ≈ 2 shifts of merges; beyond this the clean-merge surface is at risk


def _warn_graduation_lag(store: Blackboard, *, threshold: int = _GRAD_LAG_ALARM,
                         lag_fn=None, file_fn=None) -> Optional[dict]:
    """Shift-end PASSIVE alarm on BOTH graduation edges (blindspot fixes 2026-07-07):
    (1) base edge — origin/<base>..factory/auto, the push pipeline stalled (lag hit 105
    commits with zero signal because the only reporting lived inside the real+shipped
    graduation path); (2) publication edge — origin/<release>..factory/auto, pushes land
    on the base branch but nothing PROMOTES them to the target's default branch (same-day
    evidence: base edge read 0 while origin/main sat 105 commits behind). Runs in every
    mode — one or two local rev-lists, no fetch/push/LLM. Prints measurable lags (the
    publication edge only when > 0 — a current default branch is not news); above
    `threshold` each edge routes through the graduation-failure seam (deduped conductor
    task, gated by autonomy.failure_tasks like every failure task — each edge under its
    OWN dedup ref so one edge's open task can't swallow the other's escalation). Returns
    the base-edge dict, plus {"publication": …} when the second edge was measured (absent
    when release == base or the base edge was unmeasurable). Never raises — an alarm must
    not be able to kill the loop it guards — but never silently either: a persistent skip
    would recreate the blindspot, so the except prints its cause."""
    try:
        from ..reporting import issue_sync
        lag_fn = lag_fn or issue_sync.graduation_lag
        file_fn = file_fn or _maybe_file_graduation_failure
        root = config.get_adapter().entry()[0]
        tc = config.target_config()
        base = tc.get("base_branch") or "chore/extract-factory"
        lag = lag_fn(root=root, base=base)
        ahead = lag.get("ahead")
        if ahead is None:
            return lag
        print(f"[run] graduation lag: {ahead} commit(s) on factory/auto not yet pushed to origin/{base}")
        if ahead > threshold:
            print(f"[run] ⚠ graduation lag {ahead} > {threshold} — run `factory graduate` "
                  f"(or check why the autopilot isn't graduating)")
            file_fn(store, f"graduation lag: {ahead} ungraduated commit(s) on factory/auto",
                    ref="graduation:lag-base")
        release = tc.get("release_branch") or "main"
        if release == base:                    # one branch plays both roles — one edge suffices
            return lag
        pub = lag_fn(root=root, base=release)
        p = pub.get("ahead")
        if p is not None:
            if p > 0:
                print(f"[run] publication lag: {p} commit(s) on factory/auto not on "
                      f"origin/{release} (the target's default branch)")
            if p > threshold:
                print(f"[run] ⚠ publication lag {p} > {threshold} — base pushes are current but "
                      f"nothing promotes them to origin/{release} — promote/merge on GitHub or "
                      f"set target.release_branch")
                file_fn(store, f"publication lag: {p} commit(s) not on origin/{release} — "
                               f"graduation pushes the base branch but nothing promotes it to "
                               f"the default branch",
                        ref="graduation:lag-publication")
        return {**lag, "publication": pub}
    except Exception as e:  # noqa: BLE001 — the alarm must never crash the loop
        print(f"[run] lag alarm skipped (non-fatal): {e}")
        return None


def _maybe_file_graduation_failure(store: Blackboard, error: str, *,
                                   ref: str = "graduation") -> None:
    """Task 5.1: when the unattended graduation/issue-sync path fails, turn the swallowed
    error into a deduped, conductor-only backlog task + a factory learning instead of
    letting it vanish into a log print. `ref` scopes the dedup marker per failure class
    (default 'graduation' for graduate/sync callers; the lag alarm passes edge-specific
    refs so its two edges escalate independently). Gated OFF by default
    (autonomy.failure_tasks) — a passive store write (no LLM, no spend), so no STOP/mode
    gate is needed: the caller only reaches this path when a shift actually shipped in
    REAL mode, which STOP already blocks upstream. Never raises — it runs inside the
    loop-protecting except handler."""
    try:
        auton = config.load_config().get("autonomy", {}) or {}
        if not auton.get("failure_tasks", False):
            return
        from ..reporting import factory_memory
        factory_memory.record_graduation_failure(store, error=error, ref=ref)
    except Exception as ex:  # noqa: BLE001 — diagnostics must never crash the loop
        print(f"[run] failure-task filing skipped (non-fatal): {ex}")


def _should_idle(store: Blackboard, plateau_k: int) -> bool:
    """True when the last K mission statuses are all steady_state AND the backlog is empty —
    the conductor has already run research K times and found nothing, so don't spawn again."""
    recent = store.mission_status_history(plateau_k)
    return (len(recent) >= plateau_k
            and all(r["status"] == "steady_state" for r in recent)
            and len(store.list_tasks(status="open")) == 0)


def cmd_run_loop(store: Blackboard, *, mission: Optional[str] = None, token_budget=None,
                 wall_clock_s=None, prod: bool = False, real: bool = False, plateau_k: int = 3,
                 max_shifts: int = 50, loop_token_budget=None, loop_deadline_s=None,
                 run_fn=None, now_fn=None, sleep_fn=None) -> int:
    """The autonomous runner. In AUTO mode it works shift after shift on its own (no human
    between shifts) until the mission converges, STOP trips, the dashboard toggles back to
    SHIFT, OR a HARD UNATTENDED-SAFETY CEILING is hit: max_shifts, a cumulative token budget,
    or a wall-clock deadline (default 4h). In SHIFT mode it runs ONE shift, then pauses. The
    mode + STOP are re-read BETWEEN shifts so the dashboard toggle is live. `run_fn`/`now_fn`
    are injectable so the loop is testable without spawning agents or real time."""
    import time
    from ..common import killswitch, mode as modemod
    auton = config.load_config().get("autonomy", {}) or {}
    if loop_token_budget is None:                                               # real default ceiling;
        loop_token_budget = int(auton.get("loop_token_budget", 5_000_000)) or None   # config 0 → unlimited
    if loop_deadline_s is None:
        loop_deadline_s = int(auton.get("loop_deadline_s", 14400))              # default 4h wall-clock
    run_fn = run_fn or cmd_run
    now = now_fn or time.monotonic
    sleep = sleep_fn or time.sleep
    max_consec_err = int(auton.get("max_consecutive_errors", 3))   # circuit-breaker threshold
    error_backoff_s = int(auton.get("error_backoff_s", 30))        # backoff between failed shifts
    consec_err = 0
    start = now()
    spent = 0
    n = 0
    hit_ceiling = False              # a per-process SAFETY ceiling (not benign convergence) was hit
    ceilings = f"max_shifts={max_shifts}"
    if loop_token_budget:
        ceilings += f", tokens≤{loop_token_budget:,}"
    if loop_deadline_s:
        ceilings += f", deadline={loop_deadline_s}s"
    print(f"[loop] autonomy mode: {modemod.read_mode().upper()} "
          f"(toggle on the dashboard, or `factory mode auto|shift`) | ceilings: {ceilings}")
    while n < max_shifts:
        if killswitch.is_halted():
            print("[loop] STOP engaged — halting.")
            break
        if loop_deadline_s and (now() - start) >= loop_deadline_s:
            print(f"[loop] wall-clock deadline ({loop_deadline_s}s) reached — stopping after {n} shift(s).")
            hit_ceiling = True
            break
        res = run_fn(store, mission=mission, token_budget=token_budget, wall_clock_s=wall_clock_s,
                     prod=prod, real=real, plateau_k=plateau_k)
        mission = None                                  # mission only steers the first shift
        n += 1
        spent += int(res.get("tokens_used", 0) or 0)
        if res.get("action") in ("idle", "no_mission"):
            print(f"[loop] {res.get('action')} — nothing left to do; stopped after {n} shift(s).")
            break
        if res.get("action") in ("error", "timed_out"):
            consec_err += 1                                  # error/timed_out ledger ~0 tokens, so
            if consec_err >= max_consec_err:                 # the token ceiling never catches them
                print(f"[loop] circuit breaker: {consec_err} consecutive failed shifts "
                      f"({res.get('action')}) — halting after {n} shift(s).")
                hit_ceiling = True
                break
            sleep(error_backoff_s)                           # back off before retrying a failing loop
        else:
            consec_err = 0                                   # a healthy shift resets the failure streak
        if loop_token_budget and spent >= loop_token_budget:
            print(f"[loop] token budget exhausted ({spent:,}/{loop_token_budget:,}) — "
                  f"stopping after {n} shift(s).")
            hit_ceiling = True
            break
        if modemod.read_mode() != modemod.AUTO:
            print(f"[loop] SHIFT mode — paused after shift {res.get('shift_id')}. "
                  f"Toggle AUTO on the dashboard (or run again) for the next shift.")
            break
        print(f"[loop] AUTO — continuing to the next shift (#{n + 1})…")
    else:
        print(f"[loop] reached max_shifts={max_shifts} — stopping (safety cap).")
        hit_ceiling = True
    if hit_ceiling:                      # a deliberate ceiling-stop must NOT look like a crash to
        modemod.set_mode(modemod.SHIFT)  # restart_if_auto — flip AUTO→SHIFT so its not_auto veto
        print("[loop] safety ceiling reached — autonomy paused (mode→SHIFT). "   # stops the respawn
              "Clear the cause / raise the ceiling, then `factory mode auto` to resume.")
    from . import autopilot
    autopilot.clear_pid_if_mine()       # this runner's pid file must not outlive it (phantom guard)
    return n


def cmd_mode(new_mode: Optional[str] = None) -> str:
    """Read or set the autonomy mode. `factory mode` prints it; `factory mode auto|shift` sets it."""
    from ..common import mode as modemod
    if new_mode:
        m = modemod.set_mode(new_mode)
        print(f"[mode] set to {m.upper()}")
        return m
    m = modemod.read_mode()
    print(f"[mode] {m.upper()}")
    return m


def cmd_autopilot(action: str = "status") -> dict:
    """`factory autopilot status|stop` — see or HARD-stop the dashboard's AUTO runner. stop
    kills the runner's whole process group (the runner + its conductors/developers, which are
    in its session) found via the PID file OR a process scan — so it works even when the PID
    file was lost and the board shows 'idle' for a still-alive orphan."""
    import signal
    from . import autopilot
    pid = autopilot.runner_alive() or autopilot._scan_for_runner()
    if action == "stop":
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)   # the runner + every worker it spawned
            except (OSError, ProcessLookupError):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        try:
            os.remove(autopilot.pid_path())
        except OSError:
            pass
        print(f"[autopilot] stopped runner {pid if pid else '(none found)'} + its workers")
        return {"stopped": True, "pid": pid}
    print(f"[autopilot] {'running — pid ' + str(pid) if pid else 'idle (no runner)'}")
    return {"running": pid is not None, "pid": pid}


def _dup_title(title: str, existing_text: str) -> bool:
    """True when `title` matches an already-open issue in `existing_text` (the bulleted
    '- #N: title [labels]' list from fetch_issues) — normalized case/space, either string
    containing the other. The dedup that stops unattended bug-filing from spamming re-files."""
    import re
    def norm(s):
        return " ".join((s or "").lower().split())
    nt = norm(title)
    if not nt:
        return False
    for line in existing_text.splitlines():
        m = re.match(r"-\s*#\d+:\s*(.*?)(?:\s+\[[^\]]*\])?\s*$", line)
        if not m:
            continue
        ne = norm(m.group(1))
        if ne and (ne == nt or ne in nt or nt in ne):
            return True
    return False


def cmd_issue(action: str, *, title: Optional[str] = None, body: str = "",
              repo: Optional[str] = None, label: str = "auto-filed") -> Optional[str]:
    """`factory issue create --title … [--body …]` — file a target-repo issue WITH DEDUP so
    the fleet (conductor + developers, unattended) can't re-file the same bug every shift.
    Skips silently when no repo resolves, the action isn't 'create', or an open issue already
    covers the title. Tags auto-filed issues with a label for visibility."""
    import subprocess
    repo = repo or config.target_repo_slug()
    if action != "create" or not title:
        print("[issue] usage: factory issue create --title \"…\" [--body \"…\"]")
        return None
    if not repo:
        print("[issue] no target repo resolved (set target.repo in config.yaml) — skipping.")
        return None
    from ..roles.research_feed import fetch_issues
    if _dup_title(title, fetch_issues(repo)):
        print(f"[issue] an open issue already covers this — not filing: {title!r}")
        return None
    base = ["gh", "issue", "create", "-R", repo, "--title", title, "--body", body]
    try:
        out = subprocess.run(base + ["--label", label], capture_output=True, text=True, timeout=30)
        if out.returncode != 0:                       # label may not exist on the repo → retry plain
            out = subprocess.run(base, capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        print(f"[issue] gh failed: {e}")
        return None
    url = (out.stdout or "").strip()
    print(f"[issue] filed: {url or title}")
    return url or None


def cmd_viz(store: Blackboard, *, open_browser: bool = True, serve: bool = False,
            port: int = 8788, selfcheck: bool = False):
    """The fleet visualization of the (super) worker instances + activities. `--serve`:
    a LIVE 'mission control' — an animated conductor-loop that shows the active phase, the
    live workers, and the mission's progress, auto-updating while a run is in flight.
    `--selfcheck`: the deterministic dashboard gate (roadmap Task 0.6) — node --check the
    inline JS + raw-{PLACEHOLDER}/section scans, no browser, no server, zero tokens.
    Default: write + open a one-shot HTML snapshot (logs/fleet.html)."""
    if selfcheck:
        from ..checks import visual_check
        report = visual_check.check_dashboard()
        print(visual_check.format_report(report))
        return report
    if serve:
        from ..dashboard import fleet_server
        return fleet_server.serve(port=port, open_browser=open_browser)
    from ..reporting import fleet_viz
    from ..common.store import now_iso
    path = fleet_viz.generate_fleet_html(store, generated_at=now_iso())
    print(f"[viz] fleet visualization → {path}")
    if open_browser:
        import subprocess
        try:
            subprocess.run(["open", path], check=False, capture_output=True)
        except Exception:  # noqa: BLE001
            print(f"[viz] open it in a browser: file://{path}")
    return path


def cmd_research_feed(store: Blackboard, *, prod: bool = False) -> list:
    """The conductor-loop research feed (distinct from the spec-side `research`): a web
    researcher proposes bounded directions toward the active mission — outcome-informed by
    the shipped digests, de-duped against the backlog — landing as research tasks. The
    conductor invokes this when the backlog runs low (the generative loop)."""
    from ..roles import research_feed
    sw = config.load_config().get("super_worker", {}) or {}
    as_user = (sw.get("user") or None) if prod else None
    claude_bin = (sw.get("claude_bin") or "claude") if prod else "claude"   # agent's claude only in prod
    added = research_feed.propose_directions(store, as_user=as_user, claude_bin=claude_bin)
    print(f"[research-feed] proposed {len(added)} new direction(s):")
    for a in added:
        print(f"  + {a['id']}: {a['title']}")
    return added


def cmd_research_convert(store: Blackboard) -> list:
    """Human-triggered: promote vetted staged research briefs (research/staging/*.yaml,
    status=='staged' + grounded) into the backlog as source='research' tasks, then flip
    each converted yaml's status to 'converted'. No auto call site — the operator runs this
    after vetting the staged briefs (auto-refill wiring is a named follow-up)."""
    from ..roles import research_feed
    added = research_feed.convert_briefs(store)
    print(f"[research-convert] converted {len(added)} staged brief(s) to backlog task(s):")
    for a in added:
        print(f"  + {a['id']}: {a['title']}")
    return added


# --- the 09:00 daily update -------------------------------------------------
# "Larger" daily run (operator choice): several rounds + a generous-but-bounded
# token ceiling so the unattended run makes real headway without runaway spend.
# Overridable via config.autonomy.{daily_max_rounds,daily_token_budget}.
DAILY_MAX_ROUNDS = 8
DAILY_TOKEN_BUDGET = 500_000
DEFAULT_MISSION = ("Improve the target harness so it completes real tasks more "
                   "reliably and efficiently. Change only the open config "
                   "(system prompt, toolset, observation/recovery policy, skills); "
                   "never the frozen safety block.")


def cmd_daily(store: Blackboard) -> dict:
    """The 09:00 daily update. Runs a bounded autonomous session toward MISSION.md's
    mission (LARGER budget — several rounds), which ends by presenting + saving the
    executive summary (Discoveries / Decisions / Proposed next steps) for the human.

    The human STEERS by editing files: MISSION.md's `## Mission`/`## Research focus`
    and `## Material from the human`, plus config.autonomy.* for the budget. NEVER
    promotes — cmd_autonomous guarantees the only stage transitions are the roles'
    own (… → awaiting_gate for the human)."""
    from . import autonomy
    from ..research.focus import read_mission

    acfg = config.load_config().get("autonomy", {})
    max_rounds = int(acfg.get("daily_max_rounds", DAILY_MAX_ROUNDS))
    tb = acfg.get("daily_token_budget", DAILY_TOKEN_BUDGET)
    token_budget = int(tb) if tb is not None else None

    mission = read_mission(paths.factory("MISSION.md")) or DEFAULT_MISSION
    print(f"[daily] mission: {mission!r}")
    print(f"[daily] budget: max_rounds={max_rounds} token_budget="
          f"{token_budget if token_budget is not None else '∞'}")
    # The daily run also writes a dev-diary entry (always) and an accessible blog
    # post (the once-a-day cadence) about the day's autonomous work.
    return autonomy.cmd_autonomous(store, mission, max_rounds=max_rounds,
                                   token_budget=token_budget, do_research=True,
                                   do_diary=True, do_blog=True)


def cmd_schedule_install(*, loop: bool = False) -> None:
    """Install + load the launchd agent at 09:00 daily. Default: `factory daily` (the
    spec-side update). --loop: `factory run` (the autonomous CONDUCTOR loop). Per-user,
    no sudo; reversible via `factory schedule-uninstall [--loop]`."""
    import subprocess
    from . import scheduling

    command = ("run", "--loop") if loop else ("daily",)   # --loop = the autonomous runner (auto/shift)
    label = scheduling.RUN_LABEL if loop else scheduling.PLIST_LABEL
    python_bin = sys.executable or "python3"
    xml = scheduling.launchd_plist(paths.FACTORY_ROOT, python_bin, command=command, label=label)
    path = scheduling.plist_path(label)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(paths.LOGS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(xml)
    # unload first so a re-install reloads cleanly (ignore "not loaded").
    subprocess.run(["launchctl", "unload", path], capture_output=True)
    res = subprocess.run(["launchctl", "load", path], capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[schedule] launchctl load failed: {res.stderr.strip()}", file=sys.stderr)
        print(f"[schedule] plist written to {path} — load manually: "
              f"launchctl load {path}", file=sys.stderr)
        return
    print(f"[schedule] installed {label}: `factory {command[0]}` runs at 09:00 every day.")
    print(f"[schedule] plist : {path}")
    print(f"[schedule] python: {python_bin}")
    print(f"[schedule] logs  : "
          f"{os.path.join(paths.LOGS_DIR, command[0] + '-launchd.{out,err}.log')}")
    print(f"[schedule] uninstall: factory schedule-uninstall{' --loop' if loop else ''}")


def cmd_schedule_uninstall(*, loop: bool = False) -> None:
    """Unload + remove the launchd agent (the conductor loop with --loop, else daily)."""
    import subprocess
    from . import scheduling

    label = scheduling.RUN_LABEL if loop else scheduling.PLIST_LABEL
    path = scheduling.plist_path(label)
    subprocess.run(["launchctl", "unload", path], capture_output=True)
    existed = os.path.exists(path)
    if existed:
        os.remove(path)
    print(f"[schedule] {'removed' if existed else 'no plist at'} {path}; {label} unloaded.")


def cmd_status(store: Blackboard) -> None:
    champ = store.get_champion()
    print("=== clive-harness-factory status ===")
    print("champion:", champ["id"] if champ else "(none)",
          "scores:", champ["scores_json"] if champ else "{}")
    print("\ncandidates by stage:")
    for stage in ("proposed", "evaluating", "scored", "awaiting_gate", "promoted", "rejected"):
        rows = store.list_candidates(stage)
        if rows:
            print(f"  {stage}: {[r['id'] for r in rows]}")
    print("\nscenarios:")
    for s in store.list_scenarios():
        print(f"  {s['id']} [{s['partition']}/{s['class']}] leakage={s['leakage_count']}")
    bt = store.budget_totals()
    print(f"\nbudget: {bt['tokens']} tokens, ${bt['cost']:.4f}")
    flags = store.all_safety_flags()
    if flags:
        print(f"\nsafety flags: {len(flags)}")
        for f in flags[:10]:
            print(f"  [{f['severity']}] {f['kind']} ({f['candidate_id']}): {f['detail'][:80]}")
    # divergence for scored/queued candidates
    for c in store.list_candidates():
        if c["stage"] in ("scored", "awaiting_gate"):
            d = scoring.divergence_signal(store, c["id"], champ["id"] if champ else None)
            if d["alarm"]:
                print(f"\n[DIVERGENCE ALARM] {c['id']}: {', '.join(d['reasons'])}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="factory.orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    rs = sub.add_parser("reset"); rs.add_argument("--keep-logs", action="store_true")
    bp = sub.add_parser("baseline"); bp.add_argument("--sample", type=int, default=None)
    bp.add_argument("--scenario", action="append"); bp.add_argument("--model", action="append")
    sub.add_parser("propose")
    ep = sub.add_parser("evaluate"); ep.add_argument("cid"); ep.add_argument("--no-judge", action="store_true")
    ep.add_argument("--scenario", action="append"); ep.add_argument("--model", action="append")
    rp = sub.add_parser("round"); rp.add_argument("cid"); rp.add_argument("--no-judge", action="store_true")
    rp.add_argument("--scenario", action="append"); rp.add_argument("--model", action="append")
    hp = sub.add_parser("holdout-check"); hp.add_argument("cid")
    mp = sub.add_parser("mine"); mp.add_argument("--limit", type=int, default=10)
    rch = sub.add_parser("research")
    rch.add_argument("--query", default=None)
    rch.add_argument("--max-papers", type=int, default=8)
    rch.add_argument("--max-repos", type=int, default=6)
    rch_sub = rch.add_subparsers(dest="research_action")   # optional nested action
    rch_sub.add_parser("convert")   # human-triggered: staged briefs → backlog tasks (Task 5.4)
    sub.add_parser("staging")
    sp = sub.add_parser("show-scenario"); sp.add_argument("id")
    yp = sub.add_parser("synth-check"); yp.add_argument("id")
    pp = sub.add_parser("promote-scenario"); pp.add_argument("id")
    pp.add_argument("--partition", choices=["working", "held-out"], default="working")
    sub.add_parser("status")
    rep = sub.add_parser("report")
    rep.add_argument("--mission", default=None,
                     help="optional mission statement to frame the executive summary")
    dia = sub.add_parser("diary")       # first-person dev-diary entry → .dev-diary/
    dia.add_argument("--mission", default=None)
    blg = sub.add_parser("blog")        # accessible Ars-Technica-style post → blog/
    blg.add_argument("--mission", default=None)
    dev1 = sub.add_parser("develop-once")   # one develop→grade→auto-merge turn (code loop)
    dev1.add_argument("--task", required=True, help="what the developer should change/build")
    dev1.add_argument("--prod", action="store_true",
                      help="run the developer in the Guest-House user (default: dev-mode, same-user)")
    dev1.add_argument("--keep", action="store_true",
                      help="keep the throwaway clone so you can inspect the merged candidate diff")
    rfe = sub.add_parser("research-feed")   # propose backlog directions toward the mission
    rfe.add_argument("--prod", action="store_true",
                     help="run the researcher in the Guest-House user (default: dev-mode, same-user)")
    run = sub.add_parser("run")             # ONE conductor shift (the autonomous loop entry)
    run.add_argument("--mission", default=None, help="set/replace the mission (else use the active one)")
    run.add_argument("--budget", type=int, default=None, help="token budget for the shift")
    run.add_argument("--wall-clock", type=int, default=None, help="hard wall-clock seconds for the shift")
    run.add_argument("--prod", action="store_true", help="run the conductor in the Guest-House user")
    run.add_argument("--real", action="store_true",
                     help="merge into the REAL target's factory/auto branch (default: throwaway clones)")
    run.add_argument("--loop", action="store_true",
                     help="autonomous runner: shift-after-shift in AUTO mode, one-and-pause in SHIFT mode")
    run.add_argument("--max-shifts", type=int, default=50, help="safety cap on shifts in --loop")
    mod = sub.add_parser("mode")            # the auto/shift autonomy toggle
    mod.add_argument("set", nargs="?", choices=["auto", "shift"],
                     help="set the autonomy mode (omit to just read it)")
    apc = sub.add_parser("autopilot")       # see / hard-stop the dashboard AUTO runner
    apc.add_argument("action", nargs="?", choices=["status", "stop"], default="status")
    grd = sub.add_parser("graduate")        # ff base->factory/auto + push + sync the target's issues
    grd.add_argument("--dry-run", action="store_true",
                     help="preview the push range + issue actions without mutating anything")
    rbl = sub.add_parser("rebaseline")      # periodic full re-baseline: full suite vs the champion
    rbl.add_argument("--dry-run", action="store_true",
                     help="measure + report but store nothing and never auto-revert")
    lrn = sub.add_parser("learn")           # the factory's memory: agents record + read learnings
    lrn.add_argument("action", choices=["add", "list", "retire", "verify", "distill"])
    lrn.add_argument("rest", nargs="?", default=None,
                     help="the learning text (add — same as --content) or the integer "
                          "learning id (retire — exact id, see `learn list`)")
    lrn.add_argument("--role", default=None,
                     help="conductor | developer | researcher | factory "
                          "(add defaults to factory; list with no role shows ALL roles; "
                          "distill REQUIRES one)")
    lrn.add_argument("--content", default="", help="the learning to record (for add)")
    lrn.add_argument("--scope", default="general", help="free tag, e.g. no_candidate / graduation")
    lrn.add_argument("--agent", default="", help="optional agent handle/identity")
    lrn.add_argument("--limit", type=int, default=20)
    lrn.add_argument("--apply", action="store_true",
                     help="distill: actually INSERT the consolidated rules (pinned, "
                          "scope='distilled') and archive the sources — default is DRY-RUN")
    evg = sub.add_parser("eval-gates")      # golden-case eval of the LLM gates (Task 2.1, P12)
    evg.add_argument("--gate", choices=["scope"], default="scope",
                     help="which gate's goldens to replay (decompose/reviewer are follow-ups); "
                          "SPENDS tokens — one live judge call per fixture")
    iss = sub.add_parser("issue")           # dedup'd issue-filing for the fleet
    iss.add_argument("action", choices=["create"])
    iss.add_argument("--title", required=True)
    iss.add_argument("--body", default="")
    iss.add_argument("--label", default="auto-filed")
    viz = sub.add_parser("viz")             # HTML visualization of the fleet + activities
    viz.add_argument("--serve", action="store_true", help="live mission-control server (auto-updating)")
    viz.add_argument("--port", type=int, default=8788, help="port for --serve (default 8788)")
    viz.add_argument("--no-open", action="store_true", help="don't open the browser")
    viz.add_argument("--selfcheck", action="store_true",
                     help="deterministic dashboard gate: node --check the inline JS + "
                          "placeholder/section scans; exit 1 on failure (no browser/server)")
    tsk = sub.add_parser("task")            # the backlog CLI the conductor drives
    tsk.add_argument("action", choices=["list", "add", "claim", "done", "block", "reopen"])
    tsk.add_argument("rest", nargs="?", help='title (add) or FULL task id (claim/done/block/reopen)')
    tsk.add_argument("--detail", default="",
                     help="bounded brief/spec for `task add` (target surface + acceptance); "
                          "the NARROWED brief for `task reopen` (required there)")
    tsk.add_argument("--source", default="human")
    tsk.add_argument("--result", default="")
    tsk.add_argument("--status", default=None, help="filter for `task list`")
    pl = sub.add_parser("plan")             # the plan: conductor-maintained milestones
    pl.add_argument("action", choices=["add", "list", "status", "link", "estimate"])
    pl.add_argument("rest", nargs="*", help="title (add) / <id> [value] (status/link/estimate)")
    pl.add_argument("--deliverable", default="", help="the artifact/state that proves a milestone")
    pl.add_argument("--acceptance", default="", help="how delivery is verified")
    pl.add_argument("--budget-tokens", type=int, default=0, dest="budget_tokens", help="planned effort (EVM value)")
    pl.add_argument("--order", type=int, default=0, help="planned_order within the mission")
    pl.add_argument("--status", default=None, help="filter for `plan list`")
    pl.add_argument("--profile", default="", help="worker profile for `plan estimate`")
    tsh = sub.add_parser("timesheet")       # agent timesheets — engagements + per-role rollup
    tsh.add_argument("--shift", type=int, default=None, help="filter to one shift")
    tsh.add_argument("--limit", type=int, default=200)
    sub.add_parser("evm")                   # agent-adapted earned value over the plan + ledger
    wk = sub.add_parser("worker")           # worker capability profiles — the on-demand workforce
    wk.add_argument("action", choices=["list", "add", "retire"])
    wk.add_argument("rest", nargs="*", help="<name> for add/retire")
    wk.add_argument("--description", default="", help="capabilities (for the conductor + board)")
    wk.add_argument("--overlay", default="", help="persona/emphasis block injected at {PROFILE}")
    wk.add_argument("--model", default="", help="tier alias: frontier|standard|fast ('' = frontier)")
    sub.add_parser("daily")             # the 09:00 update: bounded autonomous run + summary
    sci = sub.add_parser("schedule-install")  # install the launchd 09:00 agent
    sci.add_argument("--loop", action="store_true", help="schedule `factory run` (conductor loop), not `daily`")
    scu = sub.add_parser("schedule-uninstall")
    scu.add_argument("--loop", action="store_true", help="uninstall the conductor-loop agent")
    sub.add_parser("demo")
    au = sub.add_parser("autonomous")
    au.add_argument("--mission", required=True,
                    help="mission statement that steers the unattended loop")
    au.add_argument("--max-rounds", type=int, default=5,
                    help="hard ceiling on rounds (default 5)")
    au.add_argument("--token-budget", type=int, default=None,
                    help="hard ceiling on cumulative tokens (budget_ledger); unset = no cap")
    au.add_argument("--no-research", action="store_true",
                    help="skip the researcher step (no grounded briefs staged)")
    au.add_argument("--no-intake", action="store_true",
                    help="skip the intake step (no mining/auto-promotion of scenarios)")
    au.add_argument("--no-diary", action="store_true",
                    help="skip writing the dev-diary entry at the end of the run")
    au.add_argument("--blog", action="store_true",
                    help="also write an accessible blog post about the run")
    au.add_argument("--dry-run", action="store_true",
                    help="print the per-round PLAN without invoking any role/LLM/subprocess")
    a = ap.parse_args(argv)

    # reset deletes the db file, so it must run BEFORE a store connection is opened.
    if a.cmd == "reset":
        cmd_reset(keep_logs=a.keep_logs)
        return 0
    # demo = clean slate + the champion-fails / candidate-fixes walkthrough.
    if a.cmd == "demo":
        cmd_reset(keep_logs=False)
        with Blackboard() as store:
            cmd_demo(store)
        return 0
    # schedule (un)install touch launchd, not the store — handle before connecting.
    if a.cmd == "schedule-install":
        cmd_schedule_install(loop=a.loop)
        return 0
    if a.cmd == "schedule-uninstall":
        cmd_schedule_uninstall(loop=a.loop)
        return 0

    with Blackboard() as store:
        # Auto-apply the schema on open. It's all CREATE ... IF NOT EXISTS, so it's
        # idempotent + additive — a DB created before a table existed (e.g. the conductor
        # tables) gains it here with no data loss, and the code's schema never drifts from
        # the live DB. (The bug this fixes: `factory run` against a pre-conductor DB →
        # "no such table: shifts".)
        store.init_db()
        if a.cmd == "init":
            cmd_init(store)
        elif a.cmd == "baseline":
            cmd_baseline(store, a.sample, scenario_ids=a.scenario,
                         models=_resolve_models(a.model))
        elif a.cmd == "propose":
            cmd_propose(store)
        elif a.cmd == "evaluate":
            cmd_evaluate(store, a.cid, run_judge=not a.no_judge,
                         scenario_ids=a.scenario, models=_resolve_models(a.model))
        elif a.cmd == "round":
            cmd_round(store, a.cid, run_judge=not a.no_judge,
                      scenario_ids=a.scenario, models=_resolve_models(a.model))
        elif a.cmd == "holdout-check":
            cmd_holdout_check(store, a.cid)
        elif a.cmd == "mine":
            cmd_mine(store, a.limit)
        elif a.cmd == "research":
            if getattr(a, "research_action", None) == "convert":
                cmd_research_convert(store)
            else:
                cmd_research(store, query=a.query, max_papers=a.max_papers,
                             max_repos=a.max_repos)
        elif a.cmd == "staging":
            cmd_staging()
        elif a.cmd == "show-scenario":
            cmd_show_scenario(a.id)
        elif a.cmd == "synth-check":
            cmd_synth_check(store, a.id)
        elif a.cmd == "promote-scenario":
            cmd_promote_scenario(store, a.id, a.partition)
        elif a.cmd == "status":
            cmd_status(store)
        elif a.cmd == "report":
            cmd_report(store, mission=a.mission)
        elif a.cmd == "diary":
            cmd_diary(store, mission=a.mission)
        elif a.cmd == "blog":
            cmd_blog(store, mission=a.mission)
        elif a.cmd == "develop-once":
            cmd_develop_once(store, a.task, prod=a.prod, keep=a.keep)
        elif a.cmd == "research-feed":
            cmd_research_feed(store, prod=a.prod)
        elif a.cmd == "viz":
            out = cmd_viz(store, open_browser=not a.no_open, serve=a.serve, port=a.port,
                          selfcheck=a.selfcheck)
            if a.selfcheck and not (isinstance(out, dict) and out.get("ok")):
                return 1                                   # the selfcheck is a GATE
        elif a.cmd == "run":
            if a.loop:
                cmd_run_loop(store, mission=a.mission, token_budget=a.budget,
                             wall_clock_s=a.wall_clock, prod=a.prod, real=a.real,
                             max_shifts=a.max_shifts)
            else:
                cmd_run(store, mission=a.mission, token_budget=a.budget,
                        wall_clock_s=a.wall_clock, prod=a.prod, real=a.real)
        elif a.cmd == "mode":
            cmd_mode(a.set)
        elif a.cmd == "autopilot":
            cmd_autopilot(a.action)
        elif a.cmd == "graduate":
            cmd_graduate(store, dry_run=a.dry_run)
        elif a.cmd == "rebaseline":
            cmd_rebaseline(store, dry_run=a.dry_run)
        elif a.cmd == "learn":
            # The learn positional is action-routed (the task-CLI pattern): the learning
            # TEXT for `add` (--content also works), the integer id for `retire`. Binding
            # add's text to the id slot silently DROPPED the lesson — the documented
            # task-add content-drop bug class (Fix 1.3b).
            cmd_learn(store, a.action, role=a.role,
                      content=a.content or (a.rest if a.action == "add" else "") or "",
                      scope=a.scope, agent=a.agent, limit=a.limit,
                      learning_id=None if a.action == "add" else a.rest,
                      apply=a.apply)
        elif a.cmd == "eval-gates":
            rep = cmd_eval_gates(store, gate=a.gate)
            if not (isinstance(rep, dict) and rep.get("ok_all")):
                return 1                               # a failing golden (or STOP) is a GATE
        elif a.cmd == "issue":
            cmd_issue(a.action, title=a.title, body=a.body, label=a.label)
        elif a.cmd == "task":
            cmd_task(store, a.action, rest=a.rest, source=a.source,
                     result=a.result, status=a.status, detail=a.detail)
        elif a.cmd == "plan":
            cmd_plan(store, a.action, rest=a.rest, deliverable=a.deliverable,
                     acceptance=a.acceptance, budget_tokens=a.budget_tokens, order=a.order,
                     status=a.status, profile=a.profile)
        elif a.cmd == "timesheet":
            cmd_timesheet(store, shift=a.shift, limit=a.limit)
        elif a.cmd == "evm":
            cmd_evm(store)
        elif a.cmd == "worker":
            cmd_worker(store, a.action, rest=a.rest, description=a.description,
                       overlay=a.overlay, model=a.model)
        elif a.cmd == "daily":
            cmd_daily(store)
        elif a.cmd == "autonomous":
            from .autonomy import cmd_autonomous
            cmd_autonomous(store, a.mission, max_rounds=a.max_rounds,
                           token_budget=a.token_budget,
                           do_research=not a.no_research,
                           do_intake=not a.no_intake,
                           do_diary=not a.no_diary, do_blog=a.blog,
                           dry_run=a.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
