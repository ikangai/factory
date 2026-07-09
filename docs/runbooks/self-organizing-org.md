# Self-organizing org chart runbook

Companion to `orchestrator/org.py` (the resolver + the organizer) and `roles/organizer/prompt.md`
— design: docs/plans/2026-07-09-self-organizing-factory-design.md, impl plan:
docs/plans/2026-07-09-self-organizing-factory-impl.md. A frontier-tier `claude -p` call
(the "organizer") can devise a per-mission **org chart**: it partitions the open backlog
into a handful of task classes, and per class picks which pipeline stages run, which model
tier each pipeline role uses, and which worker profile dispatches the task. Everything the
chart proposes is validated **in code** before it ever applies — the organizer's own claim
of compliance is never trusted.

Off by default. A chartless mission runs byte-identical to before this feature existed.

## Chart schema

A chart is one JSON document, stored in `org_charts.chart_json` (`common/store.py`
`add_org_chart`/`get_active_org_chart`), validated by `orchestrator/org.py:validate_chart`:

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

- `classes[].match.any` — a non-empty list of case-insensitive substrings matched against a
  task's title+detail (keyword rules only in v1 — the organizer WRITES the rules; there is
  no per-task LLM classification). A blank/whitespace-only keyword is rejected — it would
  match EVERY task (an accidental catch-all).
- At most `MAX_CLASSES` (8) classes per chart — a handful of reusable buckets, not one class
  per task (the prompt's own guidance stays tighter: 2-5). Class names must be unique —
  `classify()` matches the FIRST same-named class but apply keys a dict on name and applies
  the LAST, so a duplicate silently means two different things depending which code path
  reads it; validation rejects it outright instead.
- `classes[].stages` — keys must be one of `ORG_WIRED_KEYS` (`scope_check`, `auto_decompose`,
  `require_test`, `reviewer`, `acceptance_exec`, `retry_on_discard` — the SIX with a live
  per-task dispatch consult site; see "The narrow-only asymmetry" below). The wider
  `ORG_BOOL_KEYS` (every *boolean* leaf of `SETTINGS_SPEC`, derived not hand-listed) is a
  strict superset — `ORG_WIRED_KEYS ⊆ ORG_BOOL_KEYS` is itself asserted by a drift-guard
  test. `milestone_verify`/`investigate_blocked` are legal SETTINGS_SPEC booleans but are
  **rejected outright** if named here (see below).
- `classes[].tiers` — keys must be one of `worker`, `scope_judge`, `decomposer`, `reviewer`,
  `investigator`; values must be one of `''`, `'frontier'`, `'standard'`, `'fast'` (`''` and
  `'frontier'` are synonyms for the account's default, most capable model). All five roles
  are WIRED to a live per-task consult site except `investigator` (accepted, no effect
  today — see "Tier wiring" below).
- `classes[].profile` — must name an existing active `worker_profiles` row OR a bench entry
  in THIS chart (self-contained; `''` is always valid — the generalist fallback).
- `default_class` — must name one of `classes[]`.
- `bench` — new/replaced worker profiles. Each entry is validated by reusing
  `reporting/worker_admin.validate_add` (the SAME guardrail `factory worker add`/
  `POST /api/worker` enforce — slug, tier, and overlay length ≤ `MAX_OVERLAY_CHARS`), plus a
  non-empty `description` (organizer-schema-only). The cap is `max_profiles` — checked
  against the **resulting active, non-generalist profile count** (today's active profiles,
  plus this chart's bench adds, minus this chart's retires — mirrors
  `worker_admin.cap_error`'s exact semantics), not a raw `len(bench)` count.
- `retire` — profile names to deactivate; `generalist` can never be retired (the
  unretireable fail-open floor).
- Class names and bench entry names must be slugs (`^[a-z0-9][a-z0-9-]{1,31}$`).

Any violation rejects the **whole chart** — nothing in it applies, not even the parts that
were fine (`orchestrator/org.py:validate_chart`).

## The authority line (verbatim, design doc §"The authority line")

> The org chart's "full structural authority" inherits the repo's EXISTING constitution:
>
> - **Org-controllable, per task class**: every `SETTINGS_SPEC` *boolean* (which pipeline
>   stages run for that class of work); the model tier of the worker and of each pipeline
>   role (scope judge, decomposer, reviewer, investigator); the worker profile (bench
>   create/retire via `worker_profiles`, capped by `max_profiles`).
> - **Never org-controllable**: STOP/mode, every budget/brake (`enforce_shift_budget`,
>   `loop_*`, `push_approval`, `graduation_retest`), frozen paths, sandbox/toolset
>   boundaries, the human promotion gate, and the capacity INTs in SETTINGS_SPEC
>   (max_parallel, max_tasks_per_shift, refill_threshold, max_profiles) — global load
>   management stays operator-owned.
>
> A chart that tries to reach past the line fails VALIDATION and is rejected wholesale
> (fail-closed to the current global behavior), with the rejection recorded.

## The narrow-only asymmetry

`ORG_WIRED_KEYS` (the six `stages` keys validate accepts — see the schema section above) has
a real per-task dispatch consult site for every member, but two of them are structurally
**narrow-only**: `scope_check` and `auto_decompose`. Dispatch (`orchestrator/develop.py`)
only constructs the scope-judge/decomposer **callable at all** when the shift-level config
knob already resolved on, so a class can **narrow** either one (turn it OFF where the
shift-level knob is on) but can never **conjure** it on when the shift-level knob is off —
the callable simply doesn't exist to run. The organizer's own `{BOUNDS}` prompt seam states
this distinction explicitly (see `orchestrator/org.py:_NARROW_ONLY_STAGES`/`_bounds_text`).

The other four `ORG_WIRED_KEYS` members work "both ways" (a class may force it on or off,
independent of the shift-level default): `require_test`, `reviewer`, `acceptance_exec`,
`retry_on_discard` — threaded via `org.stage_on` in `orchestrator/develop.py` (a PURE
override-or-passthrough function; the caller resolves the global default once, `stage_on`
never re-queries the store — see "Simplification" below).

`milestone_verify` (a milestone-level, not task-level, check) and `investigate_blocked` (the
post-shift investigator still runs at a fixed global gate/tier) are legal `SETTINGS_SPEC`
booleans (`ORG_BOOL_KEYS` members) but are **NOT** in `ORG_WIRED_KEYS` — naming either as a
`stages` key is a hard VALIDATION REJECTION (not a silent no-op, which is what an earlier
draft of this feature did: a chart could set them and nothing dispatch-side would ever
consult the override). They stay GLOBAL-ONLY (config/board-controlled) until a future
per-task consult site wires them. `dispatch_waves` is an int, not a bool, and is out of
`ORG_BOOL_KEYS` entirely (capacity knobs stay global).

## Tier wiring

All five `ROLE_TIER_KEYS` (`worker`, `scope_judge`, `decomposer`, `reviewer`,
`investigator`) validate as legal tier roles, but `investigator` has **no live per-task
consult site today**: the post-shift investigator (`reporting/factory_memory.
investigate_blocked`) runs once per shift, at a single FIXED tier, over up to 3 blocked
tasks — it is not a per-task dispatch the way the other four roles are, so a chart setting
`tiers.investigator` is accepted (never rejected — it's within the authority line) but has
NO effect until a future wiring. The other four are fully wired:

| role | consult site | override reaches |
|---|---|---|
| `worker` | `orchestrator/develop.py` profile-resolution loop | the profile's tier alias is OVERRIDDEN by `tiers.worker` for model resolution only (the overlay/persona still comes from the profile); the EFFECTIVE tier is what `routing_outcomes.tier` records |
| `scope_judge` | the injected scope-judge wrapper in `execute_claimed_tasks` | `scope_check.scope_judge`'s `model=` kwarg (an explicit override; `None` preserves the config-derived `scope_check_tier` read) |
| `decomposer` | the injected decomposer wrapper in `execute_claimed_tasks`'s close-out | `scope_check.decompose_judge`'s `model=` kwarg (mirrors scope_judge's) |
| `reviewer` | `orchestrator/develop.py`'s `_review_candidate` | `reviewer_model`, threaded through `develop_task`/`develop_and_merge`; `None` preserves the config-derived `reviewer_tier` read |

Every override uses `None` (never `''`) as the "no override" sentinel, because `''` is
itself a legal tier alias (frontier) — `key in params.tiers` (presence), not truthiness, is
the actual check at each site.

## Cadence + triggers

- **Config knob**: `super_worker.organizer` (config.yaml), OFF by default. Deliberately
  **NOT** in `SETTINGS_SPEC` — `ORG_BOOL_KEYS` derives from the spec's own boolean leaves, so
  listing `organizer` there would hand a chart control of its own trigger. It is read the
  same way every other LLM-spending stage-gate is (`sw.get("organizer", False)` in
  `orchestrator/shift.py`), never through `resolve_setting`/the board's store override.
- **Automatic**: with the knob on, `run_shift` (`orchestrator/shift.py`) calls
  `org.maybe_plan_org` at shift start, main thread, right after the STOP check — it plans
  ONCE per mission (a mission with no chart yet) and is a fast no-op every shift after
  that. A mission change is covered for free (a new `mission_id` has no chart either).
- **Explicit, regardless of the knob**: `factory org plan` (refuses if an active chart
  already exists — points at replan) and `factory org replan` (force — supersedes and plans
  fresh). The CLI always works even with the knob off; the knob only gates the *automatic*
  shift-start trigger.
- **STOP vetoes the call** — `killswitch.is_halted()` is checked FIRST in both
  `plan_org` and `maybe_plan_org`, before even attempting the frontier `claude -p` call.

## The fit table

One row per **dispatched** task at close-out (done AND blocked), written on the main thread
next to the existing `task_evidence` write: `store.add_routing_outcome(task_id, shift_id,
org_class, profile, tier, outcome, stage, tokens)`. `fit_rows()` aggregates by
`(org_class, tier)` into: `attempts`, `done`, `blocked`, `top_stage` (the most common
blocked-stage), `avg_tokens`. Read it via:

```bash
factory org fit
```

which prints `render_fit_table`'s compact rendering, or via the board's `org.fit` JSON key
(reporting/fleet_viz.py `org_state`). An empty table renders "no evidence yet — the
organizer proceeds on judgment, citing so explicitly" — the organizer's prompt contract
REQUIRES it to cite a fit row or state that line for every tier assignment, mirroring the
operator's own right-sizing discipline (evidence over judgment).

## Failure posture

| failure | what happens |
|---|---|
| STOP engaged | no `claude -p` call at all |
| transport/parse failure (unparseable reply) | **no chart change** — a `factory learning` row (`scope='organizer'`) is recorded; the pipeline falls through to today's global behavior |
| validation failure (schema/authority-line violation) | the chart is stored with `status='rejected'` (audit trail — see `factory org` board section / `latest_org_chart`) **plus** a learning row; it is never applied |
| valid | applied atomically: INSERT the new chart row (active) → supersede every OTHER active chart for the mission → upsert bench (`add_profile`/`retire_profile`) → classify + re-stamp every OPEN task |

A rejected chart is never returned by `get_active_org_chart` (dispatch's consult point), so
it can never half-apply — but it stays visible via `store.latest_org_chart` / the board's
`org.latest` key / `factory learn list --role factory`, so a bad organizer proposal is loud,
not silent.

The apply order is deliberately **insert-then-supersede**, not supersede-then-insert: a
crash between the two steps must never leave a mission with ZERO active charts.
`get_active_org_chart` always picks the newest active row (`ORDER BY version DESC, id
DESC`), so even mid-crash — with the OLD chart still technically `active` alongside the
brand-new one — the newest always wins; `supersede_org_charts(mission_id, except_id=<new
row>)` then cleans up the (by now harmless) ambiguity. The shift-start hook itself
(`orchestrator/shift.py`'s `org_planner` call) is no longer silent on failure either: a
blow-up prints a `[org]` line AND records a `factory` learning (`scope='organizer'`) before
letting the shift continue — mirroring the investigator's own advisory-failure posture.

## Replan semantics

`factory org replan` (or a forced `plan_org(force=True)`) re-classifies + re-stamps every
open task's `org_class` per the new chart's rules — but a task's `profile` is **NOT**
unconditionally overwritten. Precedence is consistent everywhere in this feature, both at
plan-apply time (here) and at dispatch time (the sticky-profile rule below): **operator pin
> chart**. Before superseding, plan_org captures the OLD chart's own class→profile set; a
task's profile is only re-stamped when it's currently blank (`''`) OR names a profile the
OLD chart itself stamped (i.e. not an operator's own doing) — a hand-picked profile (`plan
estimate <task-id> <tokens> --profile <name>`, or a conductor's own assignment) survives a
replan untouched, and a non-empty profile is NEVER blanked. `factory org plan` (no force)
refuses outright when an active chart already exists, precisely so an accidental re-stamp
never happens without the operator/organizer explicitly asking for one (`factory org
replan`).

Task classification itself (`org_class`) is handled differently from profile: `task_params`
(the dispatch-side consult point) **always classifies fresh** against the CURRENT chart — a
task's stamped `org_class` is a written RECORD of a past `classify()` call, never an input
read back. Both the stamp and a fresh call are `classify()` outputs over the same chart, so
recomputing is strictly fresher (or identical); trusting a stale stamp just because its
class NAME still happens to exist in the current chart would silently outlive its own
accuracy the moment a replan redefines what that name matches, or a task's title/detail
itself changes (`task reopen`). Dispatch re-stamps `org_class` on every claimed task
unconditionally (an idempotent write either way), so the record never drifts stale between
replans.

## Chart scoping — no cross-mission inheritance

`get_active_org_chart(mission_id=None)` selects ONLY **standing** charts (`mission_id IS
NULL` rows) — never "the latest active chart of any mission". And
`org.py`'s `_active_row` (the shared resolver for both dispatch's `get_active_chart` and
`cmd_org show`) falls back to a standing chart ONLY when there is **no active mission at
all** — an active mission WITHOUT its own chart is **CHARTLESS**, full stop, never
inheriting a sibling mission's chart or a standing one just because it happens to lack its
own. This is the design's own explicit YAGNI ("cross-mission standing orgs") made
impossible in code, not just policy: a mission-B-inherits-mission-A-chart scenario is a
regression test (`tests/test_org_resolver.py`), not a supported feature. A genuinely
mission-less run (no active mission at all — a bare dev/test invocation) still gets the
standing-chart fallback, so a hand-authored standing chart keeps working.

## Simplification: stage_on is pure

`org.stage_on(params, key, current)` is a PURE override-or-passthrough: the chart's
override for `key` wins (coerced to bool); else `current` — the CALLER's already-resolved
global value — passes through unchanged, including `None` (so `require_test`'s own further
config.yaml fallback inside `develop_and_merge` still applies). It does no store I/O at
all — every call site resolves its own global default ONCE (e.g. `cmd_run`'s
`config.resolve_setting` call at shift start) and hands it to `stage_on` as `current`;
`stage_on` never re-derives it from the store, which the pre-fix version did on every
single task × key lookup (same result, pure waste). `develop.py`'s former `_class_override`
helper (used for `require_test`/`reviewer`/`acceptance_exec`) is gone — `stage_on` now
covers all six wired stages through one function.

The per-class rendering (name/profile/stages/tiers as compact text) is likewise shared: both
`cmd_org show`'s plain-text output and the board's `_org_section_html` call
`orchestrator.org.class_summary(c)` — the same one function — so the two presentations can
never silently diverge.

## Uninstall / disable

Set `super_worker.organizer: false` (or omit it — that's the default) in config.yaml. This
only disables the **automatic shift-start trigger**; it does not touch dispatch's consult
point. Any chart that's still `status='active'` in `org_charts` stays live and keeps
routing tasks — the knob gates *proposing new charts*, not *consulting an existing one*.
To fully stand down org-based routing, also clear the active chart (there is no CLI
delete; a chart is only ever superseded by a fresh `factory org replan`, or you can leave
it — `org_charts` rows are pure history, harmless to leave inert). A chartless mission
(no active chart ever, or after supersession with nothing new applied) resolves every
stage/tier exactly as `resolve_setting`/`resolve_model` always have.

## CLI reference

```bash
factory org show     # the active chart's classes/bench/rationale, or "no active org chart"
factory org fit       # the rendered fit table
factory org plan      # plan once — refuses if a chart already exists (points at replan)
factory org replan    # supersede the active chart and plan fresh
```

`factory task list` appends a trailing `` [class/profile] `` marker to any task with a
classification and/or profile set (chart-stamped OR operator-pinned) — the conductor's own
way to OBSERVE what's already been routed before claiming/estimating a task itself. Omitted
entirely when neither is set, so a chartless/unassigned backlog's output is unchanged.

## Board visibility

`reporting/fleet_viz.py:org_state(store)` feeds the `"org"` key of `fleet_json`'s payload
(`--serve`'s `/api/fleet`) and a compact `<section>` in the one-shot static snapshot
(`factory viz` → `logs/fleet.html`): the active chart's version/default_class/per-class
routing, the fit table, the `organizer_on` knob state, and the LATEST chart's status (so a
rejected proposal stays visible even though it never applied). The LIVE dashboard SPA
(`dashboard/static/fleet.html`, served by `fleet_server`) renders the same `data.org`
payload as an "Org chart" card on the **Resources** tab (alongside the worker bench it
routes to) — per-class stages/tiers, the fit-evidence row count, and an explicit "no org
chart" state. A dedicated Org TAB (its own top-level screen) is still deliberately out of
scope (design §5) — follow-up polish; this is the compact card version of that.
