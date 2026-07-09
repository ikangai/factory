# Self-Organizing Factory Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this
> plan task-by-task. Design contract: docs/plans/2026-07-09-self-organizing-factory-design.md
> (READ IT FIRST — the authority line and chart schema there are binding).

**Goal:** Per-mission org charts devised by a frontier organizer, routing every task to the
best-fitting model tier/profile with per-class pipeline-stage toggles, learned from a
recorded fit table.

**Architecture:** A store-persisted, code-validated chart document; a pure resolver
(`orchestrator/org.py`) consulted at dispatch/gate sites with fall-through to today's
`resolve_setting`; outcome rows recorded at close-out feed the organizer's next plan.
No chart ⇒ byte-identical current behavior.

**Tech Stack:** stdlib + sqlite (existing Blackboard), isolated `claude -p` for the
organizer (existing `claude_p` plumbing), pytest.

**Binding rules (every task):**
- TDD: failing test first (run it red), then implement, then green, then commit.
- Never touch: brakes/budgets (`autonomy.*` except reading), `SETTINGS_SPEC` contents,
  frozen-path machinery, vendor/, deploy/user-factory/ (except nothing), existing tests.
- All store writes on the MAIN thread (workers run in threads — mirror the existing
  task_evidence/profile pattern in develop.py).
- Comments explain WHY/invariants, matching the repo's voice.
- Full suite (`python3 -m pytest tests -q`) green before each commit; commit messages end
  with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

## Phase 1 — substrate (no LLM)

### Task 1.1: Store — tables, migration, ops

**Files:** Modify `store/schema.sql`, `common/store.py`. Test: `tests/test_org_store.py` (new).

1. Tests (red first): `add_org_chart`/`get_active_org_chart` round-trip (chart_json dict in
   → dict out; latest active wins); `supersede_org_charts(mission_id)` flips actives to
   `superseded`; `set_task_org_class` + task row carries `org_class` (and `''` default on
   a legacy task — exercise `_migrate` by asserting the column exists on a fresh DB);
   `add_routing_outcome` + `fit_rows()` aggregate (class × tier → attempts, done, blocked,
   top stage, avg tokens) over ≥3 seeded rows; rejected chart stored with status
   `rejected` never returned by `get_active_org_chart`.
2. Implement: the two `CREATE TABLE IF NOT EXISTS` blocks from the design doc §1/§4 in
   schema.sql (same comment style as neighbors); `_migrate` gains
   `ALTER TABLE tasks ADD COLUMN org_class TEXT NOT NULL DEFAULT ''` (mirror the existing
   guarded pattern at common/store.py:82-100); store methods:
   `add_org_chart(mission_id, chart, *, rationale='', evidence='', status='active',
   created_by='organizer') -> int` (bumps version = 1 + max(version) for that mission;
   json-serializes chart), `get_active_org_chart(mission_id=None) -> dict|None` (row with
   chart parsed; mission_id None = latest active row overall),
   `supersede_org_charts(mission_id)`, `set_task_org_class(id, org_class)`,
   `add_routing_outcome(task_id, *, shift_id, org_class, profile, tier, outcome, stage='',
   tokens=0)`, `fit_rows() -> list[dict]` (SQL GROUP BY org_class, tier).
3. Green, commit `feat(org): store substrate — org_charts, routing_outcomes, tasks.org_class`.

### Task 1.2: Resolver — validate / classify / task_params / fit table

**Files:** Create `orchestrator/org.py`. Test: `tests/test_org_resolver.py` (new).

1. Tests (red): `ORG_BOOL_KEYS` == exactly the bool-typed leaves of `SETTINGS_SPEC`
   (derive, don't hand-list — assert `"scope_check" in`, `"max_parallel" not in`);
   `validate_chart` accepts the design doc's example and REJECTS (each a distinct test,
   asserting the reason string): unknown stage key, capacity int as a stage
   (`max_parallel`), tier not in palette, `default_class` undefined, bench overflow vs a
   passed `max_profiles`, empty/non-list `match.any`, non-slug class name;
   `classify` first-match over title+detail case-insensitive substrings else default;
   `task_params(store, task)` returns chart overrides for a task with `org_class` set,
   `{}`-overrides (falls through) when no active chart, and classifies-on-the-fly when
   `org_class` is `''`; `render_fit_table(rows)` compact text with a "no evidence yet"
   line when empty.
2. Implement `org.py`: `ORG_BOOL_KEYS = {leaf for key, kind in SETTINGS_SPEC.items() if
   kind is bool for leaf in [key.split('.',1)[1]]}`; `TIER_PALETTE = ('', 'frontier',
   'standard', 'fast')`; `ROLE_TIER_KEYS = ('worker','scope_judge','decomposer',
   'reviewer','investigator')`; `validate_chart(chart, *, max_profiles) ->
   (ok, reasons)`; `classify(chart, task) -> str`; `OrgParams` dataclass
   `{stages: dict, tiers: dict, profile: str, org_class: str}`;
   `task_params(store, task, chart=None) -> OrgParams` (empty OrgParams when chartless);
   `stage_on(params, store, key, default)` helper: chart override if present else
   `resolve_setting(store, f"super_worker.{key}", default)`; `fit_rows` passthrough +
   `render_fit_table`.
3. Green, commit `feat(org): resolver — chart validation, classification, per-task params`.

### Task 1.3: Pipeline consult + outcome recording

**Files:** Modify `orchestrator/develop.py` (dispatch + close-out),
`reporting/scope_check.py` (tier param), Test: `tests/test_org_dispatch.py` (new).

Read develop.py's execute() fully first (the profile/memory-card main-thread block ~:200
and the close-out ~:385 are the two hook points).

1. Tests (red), driving execute()'s seams the way tests/test_develop_glue.py does (fake
   claude, hand-seeded store): with an ACTIVE chart, (a) an unclassified open task gets
   `org_class` persisted at dispatch and its class profile applied to `tasks.profile`
   when the task has none; (b) a class with `stages: {scope_check: false}` skips the
   scope judge for that task while another class still runs it (assert via the injected
   scope_check fake's call log); (c) close-out writes ONE `routing_outcomes` row per
   dispatched task — outcome `done` (stage '') and `blocked` (stage from the gate) both
   covered, tier = the PROFILE's tier alias, tokens = the attempt's ledgered spend;
   (d) with NO chart, zero routing_outcomes semantics change is asserted by the
   EXISTING suite staying green (no new assert needed — note it in the test docstring).
2. Implement: in execute()'s main-thread pre-dispatch block, load `chart =
   org.get_active_chart(store)` once; per task: `params = org.task_params(...)`; persist
   org_class (and profile if task's is '' and class names one); thread the per-task STAGE
   booleans through the existing knob plumbing (scope_check/require_test/acceptance_exec/
   retry_on_discard/reviewer/auto_decompose are resolved once globally today — make them
   per-task lookups defaulting to the global resolution; keep the global names for
   capacity); scope-judge tier: pass `model=` through from params (scope_check.py gains
   an optional `model` parameter defaulting to today's config read). Close-out: next to
   the task_evidence write and the done path, `store.add_routing_outcome(...)` (tier
   alias from the profile dict resolved pre-dispatch; tokens from the same spend value
   the ledger row gets).
3. Green (FULL suite — the dispatch surface is load-bearing), commit
   `feat(org): dispatch consults the org chart; close-out records routing outcomes`.

### Task 1.4: CLI show/fit

**Files:** Modify `orchestrator/orchestrator.py` (argparse + dispatch). Test: extend
`tests/test_org_resolver.py`.

1. Tests (red): `cmd_org(store, action='show')` prints "no active org chart" when none,
   else the chart's classes/bench/rationale; `action='fit'` prints the rendered table.
2. Implement `cmd_org` in org.py (imported by orchestrator dispatch), subparser
   `org` with positional `action` in {show,fit} (plan/replan arrive in Phase 2 — argparse
   accepts them now with a "Phase 2" error? NO — YAGNI, add choices=['show','fit'] and
   extend in 2.2).
3. Green, commit `feat(org): factory org show|fit CLI`.

**Phase-1 gate:** full suite green; integrator review of the diff; then Phase 2.

## Phase 2 — the organizer

### Task 2.1: Organizer role + plan/replan

**Files:** Create `roles/organizer/prompt.md`, extend `orchestrator/org.py`. Test:
`tests/test_organizer.py` (new).

Prompt contract (mirror roles/conductor/prompt.md's structure/tone): seams {MISSION}
{BACKLOG} {BENCH} {FIT} {MEMORY} {BOUNDS}; instructions: partition the backlog into 2-5
classes, per class pick stages (only the listed booleans)/tiers/profile, design the bench
(≤ the stated cap; retire stale), CITE fit rows per tier choice or state "no evidence —
judgment"; output ONLY the chart JSON (no fences).

1. Tests (red), faking `claude_p` (the repo's established fake pattern): happy path —
   valid JSON in → chart stored active, bench upserted (new profile created, retired one
   deactivated), every open task classified + profile-assigned, previous chart
   superseded, spend ledgered with notes='organizer'; invalid JSON → NO chart row
   change, a factory learning recorded (scope='organizer'), returns falsy; validation
   failure → chart stored with status `rejected` + learning + no apply; STOP engaged →
   no claude_p call at all (killswitch checked FIRST, mirroring the investigator).
2. Implement `plan_org(store, *, force=False) -> dict|None`: gather seams (mission via
   the store's active mission accessor — find it; backlog = open tasks id/title/detail
   capped ~60 rows; bench = list_profiles(active); fit = render_fit_table; memory =
   factory_memory card for role 'organizer'; bounds = ORG_BOOL_KEYS + TIER_PALETTE +
   max_profiles stated verbatim); frontier call via the same `claude_p` used by
   conductor-tier roles (model = resolve_model('') — the account default);
   parse (strip fences defensively), validate, apply atomically (single connection,
   ordered: supersede → chart insert → bench → classify+assign), ledger the spend.
3. Green, commit `feat(org): the frontier organizer — plan/apply/supersede with fail-closed validation`.

### Task 2.2: Triggers + CLI plan/replan

**Files:** Modify `orchestrator/org.py` (cmd_org actions), `orchestrator/develop.py` or
`orchestrator/shift.py` (the chartless-mission shift-start hook — put it where the rail
resolves knobs pre-dispatch, main thread, AFTER the STOP check). Test: extend
`tests/test_organizer.py`.

1. Tests (red): shift-start hook plans ONCE when an active mission has no chart (second
   shift reuses — claude_p fake called exactly once across two runs); no mission → no
   call; STOP → no call; `cmd_org('plan')` refuses when a chart exists (points at
   replan), `cmd_org('replan')` supersedes and plans fresh.
2. Implement; green; commit `feat(org): mission-change/chartless-shift triggers + org plan|replan`.

**Phase-2 gate:** full suite; integrator review; adversarial review pass over the whole
branch; then Phase 3.

## Phase 3 — polish

### Task 3.1: Visibility + docs

**Files:** Modify `reporting/fleet_viz.py` (org section in the data payload — follow an
existing section's pattern; keep it JSON + one compact HTML block), `roles/conductor/prompt.md`
(one line: an org chart may assign org_class/profile — respect, don't override),
Create `docs/runbooks/self-organizing-org.md` (chart schema, authority line, CLI, fit
table reading, failure posture). Test: extend `tests/test_fleet_viz.py`-style with a new
`tests/test_org_viz.py` if fleet_viz tests are per-section files (check first).

1. Tests red → implement → green; `bin/factory viz --selfcheck` must PASS.
2. Commit `feat(org): board visibility + conductor contract + runbook`.

**Final gate:** full suite, selfcheck, whole-branch adversarial review, merge decision.
