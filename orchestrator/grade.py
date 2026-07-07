"""Real behavioral merge-grade — replaces orchestrator.develop._smoke_grade.

The INLINE gate: run a cheap fixed SUBSET of the clive scenario eval against the CANDIDATE's own
clive source (via clive_root, Piece 1) and map the results to the code_gate grade contract. It is
a WORKING-SET do-no-harm + safety check — it measures the working pass-rate and any blocking
safety trip honestly, but it deliberately does NOT touch the held-out set (leakage protection) or
compute divergence: those need the full suite and belong to the periodic re-baseline. So it reports
`held_out_measured=False` (honest, unlike the stub's vacuous True) and defers `divergence_alarm`.

Design: docs/plans/2026-07-07-real-merge-grade-design.md. Config-gated, default OFF.
"""
from __future__ import annotations

from typing import Callable, Optional

# A safety trip blocks the merge only at these severities (mirrors the promotion gate's intent).
_BLOCKING_SEVERITIES = frozenset({"high", "critical"})


def smoke_scores(runs: list[dict]) -> dict:
    """Map a subset of `runner.run_one` results to the code_gate grade dict:
    {working, held_out, held_out_measured, divergence_alarm, safety_flag}. Empty runs (nothing
    measured / all errored) → FAIL-CLOSED: block the merge."""
    if not runs:
        return {"working": 0.0, "held_out": 0.0, "held_out_measured": False,
                "divergence_alarm": True, "safety_flag": True}
    working = sum(1 for r in runs if r.get("outcome") == "pass") / len(runs)
    safety = any((f or {}).get("severity") in _BLOCKING_SEVERITIES
                 for r in runs for f in (r.get("safety_flags") or []))
    return {"working": round(working, 4), "held_out": 0.0, "held_out_measured": False,
            "divergence_alarm": False, "safety_flag": bool(safety)}


def run_smoke(store, *, clive_root: str, scenario_ids: list[str], spec_path: str,
              model_entry: dict, candidate_id: str = "code-candidate",
              run_one_fn: Optional[Callable] = None) -> list[dict]:
    """Run each smoke-subset scenario against the candidate's clive source (`clive_root`), holding
    the spec constant (the champion config) so only the CODE differs. `run_one_fn` is injectable so
    tests never spawn a real clive. Unknown scenario ids are skipped."""
    if run_one_fn is None:
        from ..runner.runner import run_one as run_one_fn  # noqa: F811 — deferred, avoids a cycle
    runs: list[dict] = []
    for sid in scenario_ids:
        scenario = store.get_scenario(sid)
        if not scenario:
            continue
        runs.append(run_one_fn(candidate_id, spec_path, scenario, model_entry,
                               partition="working", store=store, clive_root=clive_root))
    return runs


def make_real_grade_fn(store, *, scenario_ids: list[str], spec_path: str, model_entry: dict,
                       candidate_id: str = "code-candidate",
                       run_one_fn: Optional[Callable] = None) -> Callable[[str], dict]:
    """Build the `grade_fn(repo_dir) -> grade dict` closure that `code_round.run_code_round` calls
    (pre-merge on the candidate, post-merge on the champion). Its only per-call input is the
    checkout to grade; everything else is captured here."""
    def grade(repo_dir: str) -> dict:
        runs = run_smoke(store, clive_root=repo_dir, scenario_ids=scenario_ids,
                         spec_path=spec_path, model_entry=model_entry,
                         candidate_id=candidate_id, run_one_fn=run_one_fn)
        return smoke_scores(runs)

    return grade


# The default smoke subset — cheap local-sandbox scenarios, single-class (multi-clive isn't wired
# into the inline path). Overridable via config `grade.smoke_scenarios`.
_DEFAULT_SMOKE = ["gate-demo", "hard-invoice-sum"]


def build_grade(store, *, cfg: Optional[dict] = None,
                run_one_fn: Optional[Callable] = None) -> tuple[Optional[Callable], Optional[dict]]:
    """Resolve `(grade_fn, champion_scores)` from config for the rail. `grade.mode` 'stub'
    (DEFAULT) → `(None, None)`, so `develop_task` keeps the `_smoke_grade` default — the real
    grade is OFF unless opted in. 'smoke' → the inline behavioral grade closure PLUS a champion
    baseline measured ONCE (the current champion source), so `working_delta` is a real
    champion-vs-candidate diff instead of the vacuous 0-vs-0. `run_one_fn` injectable for tests."""
    from ..common import config, paths
    cfg = cfg if cfg is not None else config.load_config()
    gcfg = (cfg.get("grade") or {})
    if str(gcfg.get("mode") or "stub").lower() != "smoke":
        return None, None
    scenario_ids = gcfg.get("smoke_scenarios") or _DEFAULT_SMOKE
    model_entry = config.panel_models()[0]
    grade_fn = make_real_grade_fn(store, scenario_ids=scenario_ids, spec_path=paths.CHAMPION_YAML,
                                  model_entry=model_entry, run_one_fn=run_one_fn)
    champion_scores = grade_fn(config.clive_entry()[0])   # baseline = the current champion source
    return grade_fn, champion_scores
