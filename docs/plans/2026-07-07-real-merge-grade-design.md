# Real merge-grade (replacing `_smoke_grade`) — design

**Date:** 2026-07-07
**Decision:** tiered inline smoke gate + periodic full re-baseline; config-gated, **default OFF**.

## Problem

`orchestrator/develop.py:_smoke_grade` returns a hardcoded `{working: 0.0, held_out: 0.0,
held_out_measured: True, divergence_alarm: False, safety_flag: False}`. It asserts "no
regression, no alarm" **without measuring anything**, so the merge gate
(`common/code_gate.py:auto_merge_eligible`) collapses to `tests_passed AND frozen_ok`.
"Shipped" therefore carries no behavioral signal.

## The gate contract (unchanged)

`grade_fn(repo_dir) -> dict` is called by `orchestrator/code_round.py`:
- **pre-merge** on `cand_repo` (`:84`) → deltas vs `champion_scores` feed the gate (`:89`);
- **post-merge** on `main_repo` (`:113`) → `regression_after_merge` auto-reverts a regressed champion.

Required keys: `working` (float pass-rate, **hard-required**), `held_out` (float),
`held_out_measured` (bool), `divergence_alarm` (bool), `safety_flag` (bool). The two danger
signals **default to True/BLOCK** — a grade that forgets them silently blocks every merge, so
the real grade MUST compute them. Philosophy is **do-no-harm** (`code_gate.py:13`): blocks on a
*regression* (`working_delta >= -tol`), not on failure-to-improve; the post-merge auto-revert is
the self-healing net.

## Reusable assets (already in the tree, just unwired — the old "loop A")

- `runner/runner.py:run_one(candidate_id, spec_path, scenario, model_entry, partition, store)`
  — provisions a disposable env, runs clive, scores the **real end-state** via the scenario's
  deterministic acceptance check + safety battery, records a `runs` row.
- `common/scoring.py` — `candidate_scores` (working_set pass-rate from `runs`),
  `divergence_signal` (the Goodhart alarm), `holdout_model_signal`, safety aggregation.
- `checks/safety.py:run_safety_battery`, `checks/check_base.py` — the safety flag.
- Live scenarios: **18 single-working + 1 multi-clive-working + 1 held-out** (`scenarios` table);
  panel = 1 model (`panel.yaml`). A `run_one` is a **real clive LLM+shell run** (minutes, ≤16k tok).

## The one integration gap

`clive_invoke.build:82` runs `config.clive_entry()` — the **globally-configured** clive, not the
candidate's checkout. Grading a *code* candidate requires running **`cand_repo`'s** clive source.
`config.clive_entry()` is `lru_cache`d with no override, and grades run concurrently across rail
workers, so the fix is an **explicit `clive_root` override threaded** through the single call site.

## Architecture (build order)

1. **Reparameterize the clive source (concurrency-safe).** Add optional `clive_root` (and derived
   `clive_py`) to `clive_invoke.build`/`run`, `adapters/base.run` + `clive.run`, and
   `runner.run_one`. When `None`, behaviour is exactly today's (`config.clive_entry()`). When set,
   run `<clive_root>/clive.py`. Pure addition — no existing caller changes.

2. **The inline smoke evaluator + grade closure.** `orchestrator/grade.py`:
   - `run_smoke(store, *, clive_root, scenario_ids, spec_path, model_entry) -> list[run]` — calls
     `run_one` per subset scenario with `clive_root=cand_repo`, partition `working`.
   - `smoke_scores(runs) -> dict` — `working` = pass-rate; `divergence_alarm` from
     `scoring.divergence_signal` (or a conservative rule on this tiny sample); `safety_flag` from any
     safety trip; `held_out_measured=False` (the inline gate samples working only — honest, unlike
     the stub's vacuous True).
   - `make_real_grade_fn(store, *, scenario_ids, spec_path, model_entry) -> Callable[[str], dict]`
     — the closure `run_code_round` calls; its per-call arg is the `repo_dir` to grade.

3. **Champion baseline.** `champion_scores` today is hardcoded `{working:0,held_out:0}`. Measure the
   champion's smoke score once per shift (run the same subset against the champion checkout) and
   pass it as `champion_scores`, so `working_delta` is a real champion-vs-candidate diff.

4. **Wire into the rail, config-gated (default OFF).** `config.yaml` `grade:` block:
   `grade.mode: stub|smoke` (default `stub`), `grade.smoke_scenarios: [gate-demo, hard-invoice-sum]`,
   `grade.regression_tol`, `grade.model` (panel entry). `develop_task`/`execute_claimed_tasks`
   build `grade_fn` + `champion_scores` from config; `stub` keeps `_smoke_grade` verbatim.

5. **Periodic full re-baseline.** `factory rebaseline` (CLI + optional launchd template): run the
   **full** scenario suite against the current champion (`factory/auto` tip), compare to the stored
   champion `runs`, and **auto-revert** the champion HEAD on a regression (reuse
   `code_gate.regression_after_merge` + `adapter.revert_commit`). Catches what the inline subset
   misses. Async/scheduled — never blocks a merge.

## Cost

Inline smoke (2–3 sandbox scenarios × 1 model, ×2 per merge) ≈ 1–3 min + ~50k tok per merge —
paid only when `grade.mode: smoke`. Full re-baseline ≈ 10–30 min + ~300k tok — scheduled, off the
merge path. Stub (default) stays free.

## Testing (hermetic — no real clive in the suite)

- Reparameterization: assert `build()` argv points at `<clive_root>/clive.py` when overridden,
  and at `config.clive_entry()` when not.
- `smoke_scores`: table-drive run dicts → assert the gate keys (pass-rate, divergence, safety,
  `held_out_measured=False`).
- Closure + wiring: inject a fake `run_smoke`; assert the gate dict shape and that
  `grade.mode: stub` yields `_smoke_grade` unchanged. No `run_one`/LLM in tests.
- Re-baseline: inject grade + adapter; assert auto-revert fires on a seeded regression.

## Risks

- **Reparameterization is load-bearing** — verified first, in isolation, before anything builds on it.
- **Flaky scenario could block a real merge** — mitigated by default-OFF, `regression_tol`, and the
  post-merge auto-revert net (a slipped regression self-heals; a false block just discards one
  candidate, which the rail retries).
- **Full end-to-end can't be cheaply exercised** (needs a real clive LLM run) — covered by hermetic
  TDD; a live smoke run is a manual, paid check the operator can run via `grade.mode: smoke`.
