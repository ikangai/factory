# Self-Improving Factory Roadmap

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Every task is TDD: write the failing test first, then the minimal implementation, run the suite, commit. One task = one commit.

**Goal:** Make every factory run leave the next run smarter — close the fail→investigate→verify→distill→consult loop, give the super-worker rail independent verification with objective done-conditions, and make every brake real — so the factory compounds instead of merely accumulating.

**Architecture:** Extend the seams that already exist (blind reviewer, learnings table + `{MEMORY}` card, `resume_note` contract, `spec_json` column, deterministic merge gate) — rebuild nothing. Deterministic checks before LLM checks; capture signal before refining it; every new LLM behavior config-gated OFF, ledgered, and behind STOP/deadline/token brakes; every LLM gate gets its own golden-case eval.

**Tech stack:** Python 3 (stdlib + sqlite3), pytest hermetic fixtures (monkeypatched `claude_p`/`claude_super`), existing additive-migration pattern in `common/store.py:76-98`.

**Provenance:** Synthesized 2026-07-03 from an 11-agent investigation (5 subsystem maps → 3 design lenses → 3 adversarial critiques, every file:line claim code-verified). Source article: "Build self-improving agent systems in 14 steps" (patterns P1–P12 below). All 20 proposals survived critique as keep/modify; the modifications are folded in here as binding.

---

## Part 1 — Analysis: article patterns vs. factory reality

The article's core thesis maps cleanly onto the factory's north star: *self-improvement is a property of the system, not the model*. The investigation confirmed the factory already implements more of the article than expected — and found exactly where it stalls.

| # | Pattern | Factory status | Evidence |
|---|---------|---------------|----------|
| P1 | Independent verifier ≠ self-critique | **BUILT, OFF** | Blind reviewer `_review_candidate` sees only task+diff, can block merge (`orchestrator/develop.py:248-273`); gated off (`config.yaml:152`) |
| P2 | Objective stop condition, independent grader | **Partial** | Merge gate = target's own tests, deterministic (`code_round.py:63-66`); but `spec_json.acceptance` is prose never executed; milestone "delivered" is self-declared (`orchestrator.py:869-878`) |
| P3 | Fan-out / adversarial verify / loop-until-dry | **Partial** | Parallel workers exist (`develop.py:181-182`); no adversarial-per-finding, no same-shift loop-until-dry |
| P4 | Worktree isolation | **DONE** | Pristine clones per worker, locked merges, auto-revert self-heal — nothing to do |
| P5 | Trigger layer (scheduled/event) | **Partial** | launchd daily + autopilot + gain governor; unattended graduation failures vanish into a log print (`orchestrator.py:1128-1130`) |
| P6 | 5-stage memory (fail→investigate→verify→distill→consult) | **Stages 1+4+5 partial; 2+3 ABSENT** | Canned lessons auto-recorded and consulted via `{MEMORY}` card; failure *evidence* discarded at close-out; nothing ever verifies/retires a learning; recurrence destroyed at dedup (`factory_memory.py:64-66`) |
| P7 | State file, write-before-leave / read-at-start | **DONE at shift granularity** | `resume_note` written on every end path incl. crash-reap, read at `{RESUME}` — but one free-text field, and the prompt's promise that blocked outcomes appear in the backlog is FALSE (`roles/conductor.py:86-90`) |
| P8 | Prompts that compound (known-failure-modes) | **Partial** | Lessons reach prompts via the runtime `{MEMORY}` seam (better than editing prompt.md: DB-versioned, retirable); but high-value rows age out of the newest-8 card and nothing pins/distills |
| P9 | Vision self-verification | **ABSENT** | Zero screenshot/headless code; the one recorded visual failure class (inline-JS syntax error freezes the board) is guarded by an operator habit, and is fully catchable *deterministically* |
| P10 | Route by complexity | **DONE (static)** | Tier routing fails open downward, frontier reserved for judgment (`common/config.py:70-87`); `claude_p` has NO model param, so reviewer/investigator-class calls can't be down-tiered yet |
| P11 | Classifier-block ≠ error ≠ no-candidate | **GAP on the worker path** | Any no-branch run collapses to `no_candidate` (`develop.py:320-327`) — a refusal or dead transport masquerades as "brief too big" and triggers auto-decompose spend + a false lesson |
| P12 | Eval the improvement layer itself | **ABSENT** | `tests/test_scope_check.py` monkeypatches every judge; an edit to `roles/scope_check/prompt.md` is regression-tested by nothing |

**Confirmed defects found during investigation** (fix regardless of the article):

1. **Scope/decompose judges are mis-grounded**: prompt says "you are looking at the target repo" but both run with `workdir=paths.FACTORY_ROOT` (`reporting/scope_check.py:218,241`) — verdicts about clive files are judged against the factory codebase.
2. **The per-shift token budget is decorative**: `budget_exhausted` is legal in the schema and status vocabulary but nothing enforces it; `set_shift_tokens` has zero non-test callers (`store.py:426`). Operator memory names this exact hazard.
3. **The conductor prompt lies**: step 4 promises blocked outcomes "at the top of the backlog"; the seam injects `status='open'` only.
4. **The scope judge has never rejected or split in production** (zero `scope-%` results in the live blackboard.db despite `auto_decompose` firing) — it may be miscalibrated toward pass; untestable until P12 goldens exist.
5. **Failure evidence is unrecoverable**: `tests_report` and the worker's reply are computed, then reduced to a ≤200-char reason string. The factory literally cannot re-read why a task failed.

---

## Part 2 — Binding design principles (from the critique round)

- **Extend, never rebuild.** Every proposal that re-plumbed an existing seam was cut in critique (e.g. spec-threading to the reviewer — the spec is already folded into `{TASK}` at `develop.py:167-169`).
- **Deterministic before LLM.** node --check beats headless-Chrome-plus-vision for the recorded failure class; `milestone_progress` beats an LLM grader; regex staleness checks cost zero tokens.
- **Capture before refine.** Evidence and recurrence data lost today is lost forever — the capture tasks land first so their compounding clock starts immediately.
- **Gates OFF by default, behind brakes, ledgered.** All new LLM spend goes through `add_budget(shift_id=…)` so it folds into `shift_spend` → the loop token brake. STOP vetoes everything, including read-only eval spend. New *brakes* default ON.
- **No telemetry in the settings namespace.** `SETTINGS_SPEC` is the board's operator-dial whitelist; counters go to learnings/ledger notes where existing readers surface them.
- **Store realities are law.** Main-thread-only writes; exact-id discipline (bare-hash `task claim` silently no-ops); `tasks.result` NOT NULL; `tasks.source` CHECK has no `'factory'`; `milestones.status` CHECK has no detail column; `schema.sql` AND `_migrate` both get every new column.
- **Fail-open only for advisory gates — and never stacked.** Two stacked fail-opens (reviewer + confirm) silently neutralize the gate; a dead confirm channel must flip to "reject stands".

---

## Part 3 — Phased implementation plan

Dependency spine: **Phase 0 (truth & brakes) → Phase 1 (feedback to the planner) → Phase 2 (verify the verifiers) → Phase 3 (objective done-conditions) → Phase 4 (LLM memory refinement) → Phase 5 (extended autonomy)**. Within a phase, tasks are independent unless noted. Every task: failing test → minimal implementation → full `pytest` green → commit.

### Phase 0 — Truth & brakes (deterministic, zero LLM, no new autonomy)

#### Task 0.1: Split the empty-handed-worker collapse (P11)

**Files:** Modify `orchestrator/develop.py:320-327`, `reporting/factory_memory.py:142-173`; test `tests/test_develop_glue.py`, `tests/test_factory_memory.py`

In `develop_and_merge`'s no-branch path, classify `dev['reply']` before collapsing to `no_candidate`:
- sentinel containing `timed out` → `{action:'error', stage:'timeout'}` — **still decompose-eligible** (a 30-min timeout is the strongest "task too big" evidence in the system)
- sentinel containing `rc=` → `{action:'error', stage:'worker_failed'}` — **still decompose-eligible** (includes max-turns exhaustion)
- FileNotFoundError-shaped `[claude -p unavailable:` (no `timed out`) → `{action:'error', stage:'transport'}` — decompose **suppressed**
- short reply (<600 chars) with a refusal marker near the start (module-level list: `"I can't help"`, `"I cannot assist"`, `"unable to comply"`, …) AND no branch → `{action:'error', stage:'refusal'}`, first ~300 chars persisted in the blocked reason — decompose suppressed
- else → genuine `no_candidate` (today's path)

Extend the `develop.py:227` decompose trigger to include `stage in {timeout, worker_failed}`. Every new error result must carry the `learnings`/spend keys the current `no_candidate` return carries (`develop.py:321`). Add `transport`/`refusal` entries to `factory_memory.lesson_for_block` so the false "brief bundled too much" lesson stops being written.

**Tests first:** one per branch — feed a fake `dev` dict with each reply shape, assert action/stage, assert decompose fires only for timeout/worker_failed/no_candidate, assert the refusal reason lands in the close-out result. **Follow-up (separate task):** repeated-transport counter surfacing as a red Work Queue item.

#### Task 0.2: Enforce the per-shift token budget (kill the decorative brake)

**Files:** Modify `orchestrator/shift.py:69-77`, `config.yaml`; test `tests/test_shift.py` (or wherever run_shift's hermetic fixtures live)

After the conductor returns, BEFORE the executor dispatches: `spent = store.shift_spend(sh)['tokens']`; if `token_budget > 0 and spent >= token_budget` → skip the executor, let the existing requeue at `:82` run, and end the shift `status='budget_exhausted'` with the budget note **APPENDED to the conductor's own resume_note** (never replacing it — the next shift's `{RESUME}` seam needs both). `token_budget == 0` means unlimited (matches `loop_token_budget` convention); hermetic tests ledger nothing so `spent=0` never trips.

Knob `autonomy.enforce_shift_budget` defaults **TRUE** (this is a brake, not autonomy) and stays **config.yaml-only — deliberately NOT in SETTINGS_SPEC** (a brake should not be board-toggleable). **Follow-up:** mid-dispatch re-check between tasks in `execute_claimed_tasks`; wire the dead `set_shift_tokens` seam (`store.py:426`) from the close-out loop so mid-shift spend shows on the board.

#### Task 0.3: Ground the scope/decompose judges in the target repo

**Files:** Modify `reporting/scope_check.py:218,241`; test `tests/test_scope_check.py`

Resolve the target root via the adapter/config (as `develop.py` does) and pass it as `workdir` to both `scope_judge` and `decompose_judge`. Judges are Read/Grep/Glob-only, so the operator's checkout is safe. Test: assert the workdir passed to the monkeypatched `claude_super` is the target root, not `paths.FACTORY_ROOT`. Smallest slice in the whole plan; unblocks honest baselines for Task 2.1.

#### Task 0.4: Persist per-task failure evidence (`task_evidence`) — P6 stage 1

**Files:** Modify `store/schema.sql`, `common/store.py` (additive migration + CRUD), `orchestrator/develop.py`; test `tests/test_factory_memory.py`

New table `task_evidence(task_id, shift_id, action, stage, tests_report, reply_head, created_at)` — `CREATE TABLE IF NOT EXISTS` in schema.sql (init_db is idempotent). Carry `res['reply_head'] = (dev.get('reply') or '')[:2000]` out of `develop_and_merge` next to the existing learnings extraction (`develop.py:318`); `tests_report` already rides out of `run_code_round` (`code_round.py:65-66`) and is currently dropped. One insert in the blocked branch of the close-out loop, main thread only — and **BEFORE the auto-decompose `continue` at `develop.py:238`**, or decomposed no_candidates lose their evidence. No gate: passive write, zero LLM.

**Follow-ups:** `factory task evidence <id>` read verb; dashboard blocked-card link; Task 4.1 consumes it.

#### Task 0.5: Count recurrence on dedup-hit (`hits` column) — stop destroying the frequency signal

**Files:** Modify `common/store.py` (+ `store/schema.sql` — critique: the CREATE TABLE must gain the column too, not just `_migrate`), `reporting/factory_memory.py`, `orchestrator/orchestrator.py` (cmd_learn print); test `tests/test_factory_memory.py`

`_is_dup` returns the matched row instead of a bool; on dup, bump `hits` and have `record_learning` return `(id, created: bool)` — a bare id can't drive the "reinforced #id (xN)" print (critique fix). Call-site audit is cheap: only `cmd_learn` consumes the return; develop.py's and scope_check's call sites are fire-and-forget. `factory learn list` shows hits; memory_card appends `(recurring xN)` for `hits >= 3`.

#### Task 0.6: Deterministic dashboard self-check (codify the node --check habit)

**Files:** Create `checks/visual_check.py`; modify `orchestrator/orchestrator.py` (CLI arm `factory viz --selfcheck` or `factory check dashboard`); test `tests/test_fleet_viz.py`

Extract inline `<script>` blocks from `dashboard/static/fleet.html` → `node --check` via subprocess (pytest.skip + reported skip when node absent); scan for raw `{PLACEHOLDER}` braces and required named sections. This converts the operator-memory lesson (JS syntax error silently freezes the board while the server stays green) into a permanent executable gate at zero tokens. The critique explicitly inverted the original vision-LLM proposal into this; headless-Chrome + vision grading is **parked** (Part 4). **Follow-up (gated OFF):** rail hook — run the checker when a candidate's changed paths match renderable globs, `stage='visual'`.

### Phase 1 — Close the feedback loops to the planner (cheap, deterministic)

#### Task 1.1: `{BLOCKED}` seam in the conductor prompt + `task reopen` verb

**Files:** Modify `roles/conductor.py:80-105`, `roles/conductor/prompt.md`, `orchestrator/orchestrator.py` (cmd_task + argparse choices `:1521`), `common/store.py` (new `set_task_detail`); test `tests/test_conductor.py`

Slice 1: `{BLOCKED}` seam rendering the last ~8 blocked tasks **newest-first** (`ORDER BY updated_at DESC` — `list_tasks` orders by created_at) as `- <id>: <title> — <result[:160]>`; fix the prompt's false "top of the backlog" promise and drop the shell-out advice. Slice 2: `task reopen <id> --detail "<narrowed brief>"` → status `open`, detail REPLACED, provenance prefix `previously blocked: <old result>` prepended. **MUST use exact-id discipline** — mirror `_need_task` (`orchestrator.py:848-852`): unknown/partial id → explicit refusal, never a silent 0-row success (the documented silent-no-op bug class). Slice 3: reopen-count guard — **count provenance-prefix occurrences in detail** (no schema change); refuse a 3rd reopen with "escalate to @human".

This closes the factory's #1 failure loop: blocked → narrowed brief → redispatch, without the operator editing `tasks.detail` by hand.

#### Task 1.2: Sectioned resume note (P7 structure at the write site)

**Files:** Modify `roles/conductor/prompt.md` (final-JSON contract `:115-122`), `roles/conductor.py:142-145`; test `tests/test_conductor.py`

Three optional keys — `verified` (facts checked this shift), `open` (unresolved, citing task/learning ids), `next` — folded into one labeled block (`VERIFIED: … / OPEN FAILURES: … / NEXT: …`) in the existing `shifts.resume_note` column. Bare string passes through unchanged (fail-open floor = status quo); every abnormal end path (timeout/error/crash-reap `store.py:621-625`) untouched. **Follow-up:** auto-promote `verified` lines into `scope='verified'` learnings — the stage-3 bridge from shift state to durable memory.

#### Task 1.3: Learnings hygiene — `learn retire` + deterministic staleness verify

**Files:** Modify `orchestrator/orchestrator.py` (cmd_learn `:1048-1073`), `common/store.py` (+ schema.sql: `archived` column — **this migration lands here; Task 4.2 reuses it, hard dependency**), `reporting/factory_memory.py`; test `tests/test_factory_memory.py`

(a) `factory learn retire <id>` → `archived=1`; `learnings_for_role` gains `WHERE archived=0`. This is the correction handle that must exist BEFORE any LLM authors lessons (Phase 4). (b) `factory learn verify`: regex-extract file cites, resolve **per role** — developer/conductor cites against the target checkout, factory-role cites against the factory repo (critique: live row 109 cites `reporting/scope_check.py`) — with a **unique-basename rglob fallback** (live cites are mostly bare basenames like `session.py:278`; a path-prefix resolve would no-op). Missing path / line beyond EOF → `stale=1` flag + `(may be stale — cited file moved)` card suffix. Advisory only, never deletes. **Follow-up:** report-only verify inside the 09:00 launchd daily.

#### Task 1.4: Consult-telemetry + per-task relevant memory card

**Files:** Modify `reporting/factory_memory.py`, `common/store.py` (merged_after/blocked_after columns), `orchestrator/develop.py:139,144-156`, `orchestrator/orchestrator.py` (display); test `tests/test_factory_memory.py`

`memory_card_with_ids(store, role, topic=None)` returning `(text, ids)`; `memory_card` stays as a thin wrapper. **The per-task card is CORE, not a follow-up** (critique: shift-wide attribution is a confounded near-noise signal): score a 50-row window by normalized-keyword overlap with `topic=title+detail` (reuse `_key`/`_norm`), top-4 relevant + newest-4, computed in the **main-thread profiles loop** (`develop.py:144-156`), replacing the single shift-wide `dev_card` at `:139`. At close-out, one batched UPDATE bumps `merged_after`/`blocked_after` on the surfaced ids. `learn list` shows the effectiveness ratio **suppressed below a minimum denominator (~10 attributions)**. No embeddings, no LLM, no gate.

#### Task 1.5: Feed EVM CPI/overhead into the conductor's `{PLAN}` seam

**Files:** Modify `roles/conductor.py:34-53`, `roles/conductor/prompt.md`; test `tests/test_conductor.py`

One header line from `reporting.evm.evm(store)` — CPI, percent_complete, overhead share — plus one prompt sentence: shrink scope/estimates when CPI degrades. Today the factory's only cost-efficiency signal is consumed by NOTHING automated (grep-verified); this routes it to the only decision-maker at zero new infrastructure.

### Phase 2 — Verify the verifiers (P12 eval loops + reviewer sharpening)

#### Task 2.1: Golden-case eval loop for the LLM gates (`factory eval-gates`)

**Files:** Create `reporting/gate_eval.py`, `scenarios/gates/scope.jsonl`; modify `orchestrator/orchestrator.py` (CLI); test `tests/test_gate_eval.py` (hermetic, judge monkeypatched)

**Depends on Task 0.3** (measure the grounded judge, not the broken one). Fixtures are **hand-authored** — the critique proved the "harvest from history" plan impossible (zero scope-reject/split rows exist in the live DB): take real briefs from the 26 spec_json-carrying tasks as expected-pass cases, then WRITE adversarial reject/split cases (over-bundled multi-surface briefs, frozen-surface briefs). **First hypothesis the goldens must probe: the live judge has never once rejected or split in production — it may be miscalibrated toward pass.** Expected verdicts are SETS (`{"expected":["reject","split"]}`) to absorb LLM nondeterminism. Runner replays through the LIVE `scope_judge` via `normalize_verdict`, prints per-case + aggregate scorecard, ledgers spend under `role='gate_eval'`, `killswitch.is_halted()` checked at entry (STOP vetoes even read-only spend), fixture count capped. A case flipping pass→fail records a factory learning.

**Follow-ups:** decompose/reviewer fixtures (diffs + expected approve/reject); weekly launchd agent via the parameterized `scheduling.launchd_plist` — install stays an explicit operator act.

#### Task 2.2: Reviewer plumbing — truncation marker, `claude_p` model param, tier knob, trial-enable

**Files:** Modify `orchestrator/develop.py:255-260`, `roles/common.py:59` (`_isolated_claude_argv`), `common/config.py` (SETTINGS_SPEC), `config.yaml`; test `tests/test_gsd_review_fixes.py`

The critique KILLED the original spec-threading slice — the spec is already folded into the task text the reviewer receives (`develop.py:167-169`); replace the `'(in the task above)'` literal with a comment explaining the fold so nobody re-proposes it. What ships: (a) explicit `[diff truncated at 20,000 of N chars]` marker when the `:256` truncation fires — the reviewer must know it graded a partial artifact; (b) optional `model` param on `claude_p` (append `--model`; pattern exists in `_super_worker_argv`) — **shared plumbing for Tasks 2.3 and 4.1**; (c) `super_worker.reviewer_tier` knob, `''` default = frontier (preserves the config.yaml:148-150 rationale), resolved via `resolve_model` (fails open downward, never up). (d) Operational, no code: flip `super_worker.reviewer=true` from the board for a trial window; watch `notes='review'` ledger rows and `stage='review'` discard rate.

#### Task 2.3: Reviewer + scope-check calibration (gate outcome scoring)

**Files:** Modify `orchestrator/develop.py`, `reporting/factory_memory.py:157-164`; test `tests/test_gsd_review_fixes.py`, `tests/test_develop_glue.py`

Slice 1: carry the verdict out — `res['review']={'approved':bool,'reason':str}`; add a `'review'` entry to `_DISCARD_BY_STAGE` (a reject currently gets the generic "discarded" lesson); record a reviewer-MISS learning when an APPROVED candidate ends `auto_reverted`. **Counters live as `scope='reviewer_calibration'` learnings / ledger notes — NOT settings keys** (SETTINGS_SPEC is the operator-dial whitelist; bare counters there are unread state). Slice 2: scope-miss scoring — when `action==no_candidate` AND the task carries a judge-attached spec (proof the scope check passed it), record a scope_check-scoped learning; mirrors the existing spec-creep feedback on the failure side. Slice 3 (separate, **depends on reviewer traffic from 2.2d**): adversarial confirm-on-reject — one blind cheap-tier `claude_p` over diff + alleged defect, reject stands only if confirmed; **count confirm transport failures and flip to "reject stands" after N consecutive failures in a shift** (never let a dead confirmer silently neutralize the reviewer).

#### Task 2.4: Scope-judge tier knob (cheap-grader pattern, eval-validated)

**Files:** Modify `reporting/scope_check.py:240-244`, `config.yaml`, `common/config.py`; test `tests/test_scope_check.py`

`super_worker.scope_check_tier` (`''` = today's frontier) threaded into the judge's `claude_super` call. **Lands strictly AFTER Task 2.1**: run `eval-gates` at frontier vs fast on the same goldens, compare, then decide. The judge currently burns frontier tokens per claimed task.

### Phase 3 — Objective done-conditions (P2)

#### Task 3.1: Execute the spec's named acceptance test (`stage='acceptance'`)

**Files:** Modify `reporting/acceptance.py` (`extract_test_ref`), `adapters/base.py` (**`run_named_test(cwd, ref)` — an injected adapter seam, sibling of `run_tests`**; the critique rejected a raw subprocess inside code_round as violating its injected-execution design contract `code_round.py:15-19`), `orchestrator/code_round.py` (optional `acceptance_ref` param), `orchestrator/develop.py` (extract on main thread; **`acceptance_ref` is a new kwarg on the injectable `develop_fn` seam** — budget the signature-change test updates), `roles/developer/prompt.md`, `common/config.py`, `config.yaml`; test `tests/test_scope_check.py`, `tests/test_develop_glue.py`

`extract_test_ref` = conservative regex for `tests/<path>.py[::<name>]` with a safe-charset whitelist; prose → None (fail-open). Gate `super_worker.acceptance_exec` default OFF, board-toggleable. Runs in the isolated `cand_repo` AFTER the suite gate; red run → `{action:'discarded', stage:'acceptance', tests_report:…}`. Live data is favorable: sampled `spec_json.acceptance` strings are already mostly runnable pytest refs.

Three critique corrections are binding: (a) **surface the parsed ref in the developer brief as a hard contract line** ("the gate will execute exactly `<ref>` — create it there"), so a missing file is worker non-compliance, not a judge typo; (b) **skip-on-missing ships as TELEMETRY first** (count `acceptance_skipped` per shift), flip to discard once the prompt contract is live and the skip rate is known — otherwise the skip rule exempts the dominant gaming case; (c) ref authorship: nudge `roles/scope_check/prompt.md` to emit refs, and note the judge can only verify refs against what it can Read (grounded in the target after Task 0.3). Add `'acceptance'` to `_DISCARD_BY_STAGE`.

#### Task 3.2: One informed retry on gate-discard (minimal maker→grader→retry loop)

**Files:** Modify `orchestrator/develop.py:163-179` (`work()`), `common/config.py`, `config.yaml`; test `tests/test_develop_glue.py` (critique: `test_develop_rail.py` does not exist)

Gate `super_worker.retry_on_discard` default OFF. When attempt 1 returns `discarded` with `stage in {tests, no_test}` (+ `acceptance` once 3.1 lands; NEVER `frozen`): retry exactly ONCE with the failure evidence appended. **Compute `retry_budget_ok` on the MAIN THREAD before dispatch** (`store.shift_spend` vs the shift budget — no thread store access, brake-honest from day one, composes with Task 0.2). **Word the retry suffix honestly**: "a previous INDEPENDENT attempt at this task was discarded at stage=`<stage>`; its failure evidence follows — you start from a clean base" (the retry clones the PRISTINE base; per operator memory, never imply the prior code is visible). Sum tokens/cost/seconds across attempts into the single ledger write; `res['attempts']=2`. STOP re-checks at `develop_and_merge` entry and pre-merge already bound it. **Follow-up:** feed reviewer-reject reasons as the retry brief once 2.3 has traffic.

#### Task 3.3: Independent milestone-delivery grader

**Files:** Modify `orchestrator/orchestrator.py` (cmd_plan status arm `:869-878`), `roles/conductor.py:34-53` (`_plan_bullets`), `config.yaml`; test `tests/test_conductor_store.py`

Gate `super_worker.milestone_verify` default OFF. Critique corrections are binding — both original slices were wrong at the edges: (a) refuse `delivered` only while linked tasks remain `open/in_progress/blocked` — **treat `dropped` as resolved** (a legal task status; counting it against done==total makes delivery permanently unreachable) and **treat `total==0` as unverifiable, not trivially complete**; refusal message names the open task ids. (c) **`delivered (unverified)` is derived at render time in `_plan_bullets`** — `milestones.status` is CHECK-constrained with no detail column, so the label is never stored. (b — follow-up, depends on 3.1's `extract_test_ref`): a runnable milestone acceptance ref runs in a fresh throwaway clone of factory/auto; fail → refused + factory learning.

### Phase 4 — LLM-assisted memory refinement (all gated OFF, capped, ledgered)

#### Task 4.1: Post-shift investigator for blocked tasks (P6 stages 2–3)

**Files:** Create `roles/investigator/prompt.md`; modify `reporting/factory_memory.py`, `orchestrator/develop.py`, **`roles/common.py`** (critique: the `claude_p` model kwarg from Task 2.2 is REQUIRED — without it the investigator silently runs at frontier, violating its own P10 promise), `config.yaml`, `common/config.py`; test `tests/test_factory_memory.py`

**Depends on Tasks 0.4 (evidence) and 2.2 (model param); 1.3 (retire) should exist as the correction handle.** Gate `super_worker.investigate_blocked` default OFF, board-toggleable. After close-out: up to 3 blocked-this-shift tasks with a `task_evidence` row, **scoped to `discarded(tests)` and `error` stages only** (critique: skip auto-decomposed no_candidates — the decomposer already gave a second opinion; skip frozen/no_test — the canned lessons already state the cause precisely). One isolated `claude_p` at standard tier over title+detail+spec+evidence → `{cause, lesson, followup_title?, followup_detail?}`; lesson recorded `scope='investigated'` (not 'verified'), spend ledgered `notes='investigate'` with shift_id so it counts into the loop brake; `killswitch.is_halted()` checked first; fail-open to the canned lesson. **Follow-ups:** create the narrowed follow-up task via `add_subtasks`; 3 golden fixtures for the investigator prompt (P12).

#### Task 4.2: `factory learn distill` + pinned card ranking (P6 stage 4 + P8)

**Files:** Create `roles/learn_distill/prompt.md`; modify `common/store.py` (+schema: `pinned` column; `archived` landed in 1.3), `reporting/factory_memory.py`, `orchestrator/orchestrator.py`; test `tests/test_factory_memory.py`

**Depends on 0.5 (hits ordering) and 1.3 (archived plumbing — hard dependency, not a suggestion).** Slice 1 (deterministic): `pinned` rows render FIRST in memory_card and never age out, **capped at ~6 per role** (critique: unbounded pins regrow the card and recreate the problem). Slice 2: `factory learn distill --role R [--apply]` — dry-run default; one isolated `claude_p` (standard tier) reads content+hits+effectiveness and proposes ≤5 general rules citing source ids; `--apply` inserts `scope='distilled', pinned=1` and archives sources. **Distill must include existing pinned/distilled rows as consolidation candidates** (else repeat runs accumulate). **The dedup scan must INCLUDE archived rows** (else archived lessons re-enter as fresh dups) — binding, via a store accessor. `--apply` stays human. **The launchd-daily dry-run follow-up is DROPPED unless its spend is ledgered** (critique: an unledgered LLM call in `factory daily` is spend outside every brake).

Live-DB motivation: 130 rows in 5 days; factory seed rows with uses 85–95 have ALREADY rotated out of the newest-8 card. Realizing P8 through the DB-versioned `{MEMORY}` seam (not prompt-file edits) is deliberate: retirable, no prompt drift.

### Phase 5 — Extended autonomy (most new behavior; last)

#### Task 5.1: Unattended-failure event trigger (graduation/issue-sync errors → backlog + learnings)

**Files:** Modify `orchestrator/orchestrator.py` (`_graduate_after_shift` call sites `:1128-1130`), `reporting/factory_memory.py`, `config.yaml`; test `tests/test_orchestrator.py`

Gate `autonomy.failure_tasks` default OFF. On graduation error: `add_task(source='worker', …)` (the `tasks.source` CHECK has no `'factory'`), **deduped against open AND blocked tasks** (critique: a scope-rejected previous failure task leaves 'open', so open-only dedup re-spams) — or stamp/dedup on a `source_ref='graduation'` marker. **Detail is an explicit conductor-only instruction**: "do NOT claim this for a developer worker — the rail cannot fix factory infrastructure; escalate to @human via agora with the error below, then mark done/blocked." Plus `record_learning(role='factory', scope='graduation')`. **Follow-up:** same treatment for `revert_failed` close-outs and repeated autopilot runner deaths.

#### Task 5.2: Bounded second-wave dispatch (same-shift loop-until-dry, 2 waves max)

**Files:** Modify `orchestrator/develop.py`, `reporting/scope_check.py:164-179` (`add_subtasks` returns ids), `common/config.py`, `config.yaml`, `common/store.py` (`set_task_milestone` one-liner); test `tests/test_develop_glue.py`

**Depends hard on Task 0.2 (enforced shift budget).** Gate `super_worker.dispatch_waves: 1` default (= today). When 2: after close-out, if any no_candidate decomposed this shift AND STOP clear AND headroom under `max_tasks_per_shift` AND shift spend < budget AND **an explicit time guard** — thread the shift's start time into `execute_claimed_tasks` and skip wave 2 unless `elapsed + waves×worker_timeout` fits the loop-deadline share (critique: the claimed "shift wall-clock" does NOT exist over the executor — only per-worker 1800s timeouts, and the loop deadline is checked only between shifts) — claim the new sub-task ids and run ONE more identical pass with `decomposer=None` (hard recursion stop). **Plan-link wave-2 sub-tasks to the parent's `milestone_id`** so EVM/timesheet attribution survives the rail claiming tasks itself. Trial with low `max_tasks` from the board.

#### Task 5.3: Autopilot watchdog (brake-respecting self-heal in AUTO)

**Files:** Modify `orchestrator/autopilot.py`, `dashboard/fleet_server.py` (`fleet_state`); test `tests/test_autopilot.py`

`restart_if_auto()`: if `mode.is_auto()` and not `killswitch.is_halted()` and `runner_alive() is None` → `start_runner()`, debounced via a module-level monotonic timestamp (once per N minutes max — a crash-looping runner must not thrash). Called from the dashboard's 2s poll. STOP and mode=shift both veto — every existing brake still brakes; the human already opted into AUTO. Today `start_runner`'s ONLY call site is the mode-toggle POST, so a crashed runner in AUTO stays down until a human re-toggles.

#### Task 5.4: Staged-brief → backlog converter (make the blue queue item true)

**Files:** Modify `roles/research_feed.py`, `orchestrator/orchestrator.py` (CLI `factory research convert`); test `tests/test_research_feed.py`

The Work Queue promises staged briefs "await the conductor's conversion" — no code path exists (`fleet_viz.py:205-207`). `convert_briefs(store)`: for each `research/staging/*.yaml` with `status=='staged'` and no `provenance_warning`, `add_task(source='research', …)` with backlog-title dedup, then rewrite the yaml `status='converted'`. **Human-triggered CLI only at first** — vetting stays with the operator until trusted; wiring into the pre-shift refill is a named follow-up.

---

## Part 4 — Parked / explicitly rejected

- **Headless-Chrome + vision-LLM verification (P9 full form)** — parked until the factory targets a UI-shaped repo. Clive is CLI-shaped: the rail hook would ~never fire (memory that exists but doesn't compound — the article's own anti-pattern), `file://` renders of the fetch-backed board screenshot an empty shell, and the recorded failure class is fully covered by Task 0.6 at zero tokens.
- **Spec-threading to the reviewer** — rejected in critique: already achieved by the task-text fold at `develop.py:167-169`.
- **Fixture harvest from DB history for eval-gates** — impossible (zero negative verdicts exist); goldens are hand-authored.
- **Automated profile tier changes / retirement** — routing stays conductor-advisory; revisit after calibration data (2.3) accrues.
- **Article claims not adopted:** cloud CMA/Routines (factory is deliberately local, launchd covers scheduling); editing `roles/*/prompt.md` with accumulated lessons (the DB-versioned `{MEMORY}` seam + pinning is strictly better here); Opus-fallback-on-classifier-block (Task 0.1's refusal stage surfaces blocks honestly instead — rerouting is an operator decision).

## Rollout & verification protocol

1. Full `pytest` green before every commit; one task = one commit on a feature branch off `main` (repo stays local — no push).
2. New LLM gates trial from the board (SETTINGS_SPEC) with STOP disengaged only under supervision; watch the ledger (`notes=` rows) and stage-discard rates for 2–3 shifts before leaving anything ON.
3. Order of activation after code lands: reviewer trial (2.2d) → acceptance-exec telemetry (3.1) → investigator trial (4.1) → distill dry-runs (4.2) → second-wave last, only after `budget_exhausted` has been observed working (0.2).
4. Every phase ends by running `factory eval-gates` (once 2.1 exists) — the improvement layer's own regression check.
5. `node --check` / `factory viz --selfcheck` before claiming any dashboard change works (operator memory; now Task 0.6's gate).

## The compounding claim, stated honestly

After Phases 0–2, run N+1 inherits from run N: truthful failure taxonomy (0.1), durable failure evidence (0.4), honest recurrence counts (0.5), blocked reasons as guaranteed prompt input plus a verb to act on them (1.1), effectiveness-attributed relevant memory (1.4), and eval-scored gates (2.1). After Phases 3–5 it also inherits executable acceptance contracts, calibration data on the verifiers themselves, evidence-informed second attempts, case-specific investigated lessons, and a consolidated pinned rulebook. That is the article's compound stack, realized on the factory's own seams.
