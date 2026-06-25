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
    claude_bin = sw.get("claude_bin") or "claude"
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


def cmd_task(store: Blackboard, action: str, *, rest: Optional[str] = None,
             source: str = "human", result: str = "", status: Optional[str] = None) -> None:
    """The backlog CLI the conductor drives: `task list [--status open]`,
    `task add "<title>"`, `task claim <id>`, `task done <id> [--result <sha>]`,
    `task block <id> [--result why]`. claim/done STAMP the running shift, so the loop can
    tell what a shift shipped (the basis for mission-progress)."""
    if action == "list":
        for t in store.list_tasks(status=status):
            print(f"{t['id']}\t{t['status']}\t[{t['source']}] {t['title']}")
    elif action == "add":
        import uuid
        tid = f"task-{uuid.uuid4().hex[:8]}"
        store.add_task(tid, rest or "(untitled)", source=source)
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


def cmd_run(store: Blackboard, *, mission: Optional[str] = None, token_budget: Optional[int] = None,
            wall_clock_s: Optional[int] = None, prod: bool = False, plateau_k: int = 3,
            conductor=None) -> dict:
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

    # IDLE short-circuit: if the loop has been steady for K shifts with an empty backlog and
    # the operator isn't re-steering, DON'T spawn a conductor — surface and wait. This is
    # what makes 'recommend_stop' real (otherwise a scheduled loop spawns forever).
    if mission is None and _should_idle(store, plateau_k):
        print(f"[run] idle: steady state for {plateau_k}+ shifts, backlog empty — nothing to "
              f"do toward the mission. Awaiting a revision (factory run --mission \"…\").")
        return {"action": "idle", "shift_id": None}

    if conductor is None:                              # live: the claude conductor (dev/prod user)
        from ..roles.conductor import run_conductor
        as_user = (sw.get("user") or None) if prod else None
        claude_bin = sw.get("claude_bin") or "claude"
        if not prod:
            print("[run] ⚠ DEV mode: the conductor runs as YOU (same-user) with Bash + your "
                  "MCP loaded — supervised only; do not schedule unattended. Use --prod for the "
                  "Guest-House boundary.")
        conductor = lambda st, **kw: run_conductor(st, as_user=as_user, claude_bin=claude_bin, **kw)

    res = run_shift(store, token_budget=token_budget, conductor=conductor,
                    mission=mission, wall_clock_s=wall_clock_s)
    print(f"[run] shift {res.get('shift_id')}: {res['action']} "
          f"(reaped {res.get('reaped', 0)} crashed shift(s))")

    if res.get("shift_id"):                            # a shift actually ran → assess the mission
        shipped_tasks = [t for t in store.list_tasks(status="done")
                         if t.get("shift_id") == res["shift_id"]]
        m = assess(store, shift_id=res["shift_id"], shipped_count=len(shipped_tasks),
                   plateau_k=plateau_k)
        if shipped_tasks:                              # auto-emit the digest — don't trust the LLM to
            store.add_digest(shift_id=res["shift_id"],
                             shipped=[t["id"] for t in shipped_tasks],
                             summary="shipped: " + "; ".join(t["title"] for t in shipped_tasks))
        print(f"[run] mission status: {m['status']} — {m['rationale']}")
        if m["recommend_stop"]:
            print(f"[run] ⏸  STEADY STATE for {plateau_k} shifts — nothing left toward the "
                  f"mission. Awaiting a mission revision (re-steer: factory run --mission \"…\").")
    return res


def _should_idle(store: Blackboard, plateau_k: int) -> bool:
    """True when the last K mission statuses are all steady_state AND the backlog is empty —
    the conductor has already run research K times and found nothing, so don't spawn again."""
    recent = store.mission_status_history(plateau_k)
    return (len(recent) >= plateau_k
            and all(r["status"] == "steady_state" for r in recent)
            and len(store.list_tasks(status="open")) == 0)


def cmd_research_feed(store: Blackboard, *, prod: bool = False) -> list:
    """The conductor-loop research feed (distinct from the spec-side `research`): a web
    researcher proposes bounded directions toward the active mission — outcome-informed by
    the shipped digests, de-duped against the backlog — landing as research tasks. The
    conductor invokes this when the backlog runs low (the generative loop)."""
    from ..roles import research_feed
    sw = config.load_config().get("super_worker", {}) or {}
    as_user = (sw.get("user") or None) if prod else None
    claude_bin = sw.get("claude_bin") or "claude"
    added = research_feed.propose_directions(store, as_user=as_user, claude_bin=claude_bin)
    print(f"[research-feed] proposed {len(added)} new direction(s):")
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

    command = ("run",) if loop else ("daily",)
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
    tsk = sub.add_parser("task")            # the backlog CLI the conductor drives
    tsk.add_argument("action", choices=["list", "add", "claim", "done", "block"])
    tsk.add_argument("rest", nargs="?", help='title (add) or task id (claim/done/block)')
    tsk.add_argument("--source", default="human")
    tsk.add_argument("--result", default="")
    tsk.add_argument("--status", default=None, help="filter for `task list`")
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
        elif a.cmd == "run":
            cmd_run(store, mission=a.mission, token_budget=a.budget,
                    wall_clock_s=a.wall_clock, prod=a.prod)
        elif a.cmd == "task":
            cmd_task(store, a.action, rest=a.rest, source=a.source,
                     result=a.result, status=a.status)
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
