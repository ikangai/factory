# Self-organizing factory — org charts + fit-based model routing (2026-07-09)

## Goal (operator, 2026-07-09)

> Make the factory self-organizing. Depending on the work, an internal org chart should be
> devised (done by the most capable model). This org is used for the actual work with
> different models for different tasks. The focus is to use the best fitting model for the
> given task. This is an update for the current pipeline structure.

Operator decisions (AskUserQuestion, 2026-07-09): **full structural authority** for the
organizer (bounded as below), **per-mission** cadence, **evidence loop in v1**.

## What exists (the substrate this builds on)

- `worker_profiles` (store): persona overlay + model TIER per profile; `tasks.profile`
  routes each task; dispatch resolves profile → `config.resolve_model(tier)`
  (orchestrator/develop.py ~:209) — fails open DOWNWARD, never up.
- `SETTINGS_SPEC` (common/config.py): the whitelist of BOARD-toggleable pipeline knobs
  (scope_check, require_test, auto_decompose, reviewer, acceptance_exec, retry_on_discard,
  milestone_verify, investigate_blocked, dispatch_waves + capacity ints), resolved
  store-override → config → default by `resolve_setting`. Brakes/budgets are deliberately
  config-only and NOT in the spec.
- Evidence: `task_evidence` (per blocked task: action/stage/report), `budget_ledger`
  (spend attributed per profile + shift), timesheets/EVM.
- Roles run as isolated `claude -p` with tier knobs (`scope_check_tier`, `reviewer_tier`).

What's missing is exactly the goal: nothing DESIGNS the org per unit of work, role tiers
are static, and nothing learns which tier fits which kind of task.

## The authority line (the design's spine)

The org chart's "full structural authority" inherits the repo's EXISTING constitution:

- **Org-controllable, per task class**: every `SETTINGS_SPEC` *boolean* (which pipeline
  stages run for that class of work); the model tier of the worker and of each pipeline
  role (scope judge, decomposer, reviewer, investigator); the worker profile (bench
  create/retire via `worker_profiles`, capped by `max_profiles`).
- **Never org-controllable**: STOP/mode, every budget/brake (`enforce_shift_budget`,
  `loop_*`, `push_approval`, `graduation_retest`), frozen paths, sandbox/toolset
  boundaries, the human promotion gate, and the capacity INTs in SETTINGS_SPEC
  (max_parallel, max_tasks_per_shift, refill_threshold, max_profiles) — global load
  management stays operator-owned.

A chart that tries to reach past the line fails VALIDATION and is rejected wholesale
(fail-closed to the current global behavior), with the rejection recorded.

## Components

### 1. The org chart artifact (store)

New table `org_charts`:

```sql
CREATE TABLE IF NOT EXISTS org_charts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id  INTEGER,                    -- the mission this org serves (NULL = standing)
    version     INTEGER NOT NULL DEFAULT 1, -- bumped per replan of the same mission
    status      TEXT NOT NULL DEFAULT 'active',  -- active | superseded | rejected
    chart_json  TEXT NOT NULL,              -- the validated chart document
    rationale   TEXT NOT NULL DEFAULT '',   -- organizer's cited reasoning (incl. fit refs)
    evidence    TEXT NOT NULL DEFAULT '',   -- the fit-table snapshot the organizer saw
    created_by  TEXT NOT NULL DEFAULT 'organizer',
    created_at  TEXT NOT NULL
);
```

`chart_json` schema (validated in code, not by the LLM):

```json
{
  "classes": [
    {"name": "mechanical-fix",
     "match": {"any": ["typo", "rename", "docstring", "comment"]},
     "stages": {"scope_check": false, "reviewer": false},
     "tiers":  {"worker": "fast", "scope_judge": "fast", "reviewer": "",
                "decomposer": "standard", "investigator": "standard"},
     "profile": "python-dev"},
    {"name": "risky-core", "match": {"any": ["llm.py", "planner", "concurrency"]},
     "stages": {"reviewer": true, "retry_on_discard": true},
     "tiers":  {"worker": "standard", "reviewer": ""},
     "profile": "core-surgeon"}
  ],
  "default_class": "standard-dev",
  "bench": [
    {"name": "python-dev", "model": "fast", "overlay": "...", "description": "..."},
    {"name": "core-surgeon", "model": "standard", "overlay": "...", "description": "..."}
  ],
  "retire": ["stale-profile"]
}
```

Validation rules (rejected wholesale on any violation): every `stages` key ∈ the
SETTINGS_SPEC boolean set; every tier ∈ {'', 'frontier', 'standard', 'fast'} (and resolved
via `resolve_model`, which fails downward); `default_class` names a defined class; bench
size after apply ≤ `max_profiles`; class names slug-like; `match.any` non-empty lists of
strings.

New columns (additive `_migrate` pattern): `tasks.org_class TEXT NOT NULL DEFAULT ''`.

### 2. The resolver (the pipeline update)

`orchestrator/org.py` — pure store+config reads, no LLM:

- `active_chart(store) -> dict | None` — the active chart (latest active row).
- `classify(chart, task) -> str` — match rules over title+detail (case-insensitive
  substring for v1); first matching class, else `default_class`. Used when a task has no
  `org_class` yet (new mid-mission tasks; assigned + persisted at claim/dispatch time).
- `task_params(store, task) -> OrgParams` — the ONE consult point:
  `{stage overrides: dict, tiers: dict, profile: str}` from the task's class; every
  stage/tier NOT set by the chart falls through to today's `resolve_setting`/config
  values. **No active chart → empty overrides → byte-identical current behavior.**

Consult sites (each takes an explicit param instead of reading config directly, so the
override threads through without new global state):

- dispatch (develop.py execute): per-task profile+worker tier (chart wins over
  `tasks.profile` when both set? NO — the chart ASSIGNS `tasks.profile` at apply/claim
  time; dispatch keeps reading `tasks.profile`, so the existing path is unchanged and
  auditable), per-task stage gates: scope_check, auto_decompose, require_test,
  acceptance_exec, retry_on_discard, reviewer, dispatch_waves(class-less, stays global),
  investigate_blocked (post-shift, per-task class of the blocked task).
- scope_check / decomposer / reviewer / investigator calls: tier passed in from
  `task_params` (default: today's config knobs).

### 3. The organizer role (frontier)

`roles/organizer/prompt.md` + `orchestrator/org.py:cmd_org_plan` — an isolated one-shot
`claude -p` at the FRONTIER tier (org design is judgment; deliberately the most capable
model, per the goal). Input seams: {MISSION}, {BACKLOG} (open tasks: id/title/detail),
{BENCH} (active profiles), {FIT} (the fit table, §4), {MEMORY} (factory learnings card),
{BOUNDS} (the authority line + models palette + max_profiles, stated explicitly).
Output: the chart JSON only.

Apply (code, after validation): supersede the previous active chart for the mission;
upsert bench profiles (`add_profile`/deactivate for `retire`); set `tasks.org_class` +
`tasks.profile` for every OPEN task per classification; store the chart row with
rationale + evidence snapshot. Everything auditable on the board and via CLI.

Triggers: `factory org plan` (explicit), automatically when the active mission changes or
a shift starts with a mission that has NO chart (one frontier call, then cached), and
`factory org replan` (force). A shift with no chart and no mission runs exactly as today.

Failure posture: transport/parse/validation failure → NO chart change, loud log +
factory learning row; the pipeline falls through to global behavior (fail-open downward,
never a half-applied org).

### 4. The evidence loop (fit table)

New table `routing_outcomes` — one row per dispatched task at close-out (done AND
blocked; written on the main thread next to the existing `task_evidence` write):

```sql
CREATE TABLE IF NOT EXISTS routing_outcomes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    shift_id   INTEGER,
    org_class  TEXT NOT NULL DEFAULT '',
    profile    TEXT NOT NULL DEFAULT '',
    tier       TEXT NOT NULL DEFAULT '',   -- tier ALIAS used (not the raw model id)
    outcome    TEXT NOT NULL,              -- done | blocked
    stage      TEXT NOT NULL DEFAULT '',   -- gate stage when blocked ('' when done)
    tokens     INTEGER NOT NULL DEFAULT 0, -- ledgered spend for the attempt
    created_at TEXT NOT NULL
);
```

`fit_table(store)` aggregates class × tier → attempts, done-rate, top block-stages, avg
tokens; rendered (compact text) into the organizer's {FIT} seam and a CLI/board view.
The organizer's contract REQUIRES citing fit rows (or "no evidence yet — judgment") per
class assignment, mirroring the operator's own right-sizing discipline: evidence >
judgment, probe-then-record, never assert fit without a record.

### 5. Surfaces

- CLI: `factory org plan|replan|show|fit` (show = active chart + rationale; fit = the
  table).
- Board: v1 exposes the chart + fit table via the fleet-viz data JSON (a compact section);
  a dedicated tab is follow-up polish.

## Phasing

1. **Substrate** (no LLM): tables + migrations + store ops; `org.py` resolver
   (classify/task_params/fit_table); dispatch + gate consult sites threaded; outcome
   recording at close-out; CLI show/fit. Hand-authored charts fully exercise routing in
   tests.
2. **Organizer**: prompt + plan/replan CLI + validation/apply/supersede + triggers
   (mission change, chartless shift start) + failure posture + learnings row.
3. **Polish**: fleet-viz section; conductor prompt line (the chart exists, respect
   `org_class`); runbook.

Each phase: TDD, suite green, adversarial review before merge.

## Out of scope (YAGNI, explicit)

- Per-task LLM classification (keyword rules only in v1; the organizer writes them).
- Org-controlled capacity/parallelism (operator-owned).
- Cross-mission standing orgs, org marketplaces, dynamic mid-shift re-org.
- Changing role PROMPTS per class (persona overlay already covers the worker; role
  prompts stay fixed).
