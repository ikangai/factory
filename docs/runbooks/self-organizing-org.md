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
  no per-task LLM classification).
- `classes[].stages` — keys must be one of `ORG_BOOL_KEYS` (every *boolean* leaf of
  `SETTINGS_SPEC`, derived not hand-listed — see the authority line below).
- `classes[].tiers` — keys must be one of `worker`, `scope_judge`, `decomposer`, `reviewer`,
  `investigator`; values must be one of `''`, `'frontier'`, `'standard'`, `'fast'` (`''` and
  `'frontier'` are synonyms for the account's default, most capable model).
- `classes[].profile` — must name an existing active `worker_profiles` row OR a bench entry
  in THIS chart (self-contained; `''` is always valid — the generalist fallback).
- `default_class` — must name one of `classes[]`.
- `bench` — new/replaced worker profiles, capped at `max_profiles` (a capacity int, never
  org-controllable — see below).
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

`ORG_BOOL_KEYS` (every boolean leaf of `SETTINGS_SPEC`) validates as a legal `stages` key
under the authority line, but two of them are structurally **narrow-only**: `scope_check`
and `auto_decompose`. Dispatch (`orchestrator/develop.py`) only constructs the scope-judge/
decomposer **callable at all** when the shift-level config knob already resolved on, so a
class can **narrow** either one (turn it OFF where the shift-level knob is on) but can never
**conjure** it on when the shift-level knob is off — the callable simply doesn't exist to
run. The organizer's own `{BOUNDS}` prompt seam states this distinction explicitly (see
`orchestrator/org.py:_NARROW_ONLY_STAGES`/`_bounds_text`).

Every OTHER `ORG_BOOL_KEYS` member is presented to the organizer as working "both ways" (a
class may force it on or off, independent of the shift-level default) — but as of this
writing only four of them have a real per-task dispatch consult site wired: `require_test`,
`reviewer`, `acceptance_exec`, `retry_on_discard` (impl plan Task 1.3's own scope; threaded
via `_class_override`/`org.stage_on` in `orchestrator/develop.py`). `milestone_verify`
(a milestone-level, not task-level, check) and `investigate_blocked` (the post-shift
investigator still runs at a fixed global gate/tier) validate as legal class overrides but
have **no effect today** if a chart sets them — nothing dispatch-side consults a per-class
override for either one yet. `dispatch_waves` is an int, not a bool, and is out of
`ORG_BOOL_KEYS` entirely (capacity knobs stay global). Widening either gap is a named,
scoped follow-up, not a silent promise this runbook should overstate.

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
| valid | applied atomically: supersede the mission's prior active chart → insert the new chart row → upsert bench (`add_profile`/`retire_profile`) → classify + assign every OPEN task |

A rejected chart is never returned by `get_active_org_chart` (dispatch's consult point), so
it can never half-apply — but it stays visible via `store.latest_org_chart` / the board's
`org.latest` key / `factory learn list --role factory`, so a bad organizer proposal is loud,
not silent.

## Replan semantics

`factory org replan` (or a forced `plan_org(force=True)`) **re-stamps every open task's
`org_class` AND `profile`** per the new chart — this is deliberate and NOT additive:
manual `--profile` assignments and any profile a conductor set earlier this mission are
overwritten. The organizer owns routing at plan time; a conductor that wants a specific
profile on a specific task after a replan has to reassign it again (or the org chart's own
class match rules will keep re-classifying it the same way next replan). `factory org plan`
(no force) refuses outright when an active chart already exists, precisely so an accidental
re-stamp never happens without the operator/organizer explicitly asking for one.

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

## Board visibility

`reporting/fleet_viz.py:org_state(store)` feeds the `"org"` key of `fleet_json`'s payload
(`--serve`'s `/api/fleet`) and a compact `<section>` in the one-shot static snapshot
(`factory viz` → `logs/fleet.html`): the active chart's version/default_class/per-class
routing, the fit table, the `organizer_on` knob state, and the LATEST chart's status (so a
rejected proposal stays visible even though it never applied). A dedicated Org tab in the
live dashboard is deliberately out of scope here (design §5) — follow-up polish.
