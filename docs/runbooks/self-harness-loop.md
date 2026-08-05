# Self-harness loop runbook

Companion to `common/harness_surface.py` (the surface manifest), `reporting/weakness.py`
(the weakness miner), `orchestrator/harness.py` (the proposer/validator/applier), and
`roles/harness_engineer/prompt.md` — design: `docs/plans/2026-08-05-self-harness-loop-
design.md`. A frontier-tier `claude -p` call (the "harness engineer") reads the factory's
own mined failure evidence and proposes a small batch of BOUNDED edits to the factory's
OWN harness — knob settings, role-prompt patches, corrections to its own learnings
playbook — each citing the evidence rows that motivated it. Every proposal is validated
**in code** before it is even stored as `'proposed'`, and **nothing is ever applied
automatically**: an operator reviews and applies (or rejects) each one by hand.

Off by default. With the trigger knob off, the factory runs byte-identical to before this
feature existed — `factory harness mine|plan|show|apply|reject` still work as explicit
human acts regardless of the knob.

## The three-stage loop

1. **Weakness mining** (`reporting/weakness.py`, read-only, zero LLM): clusters the
   factory's own failure telemetry — `task_evidence` (action×stage), `routing_outcomes`/
   `fit_rows` (class×tier blocked-rate misroutes), `tasks.result` scope-check verdicts,
   `learnings` proven counterproductive by their own outcome counters, `gate_eval_results`
   ok→fail flips, and `shifts` terminal-state attrition — into stable-slug clusters, each
   carrying a citable `evidence_ids` vocabulary. `mine_weaknesses(store, window=200)` ->
   `[cluster]`; `render_weakness_table(clusters)` renders the {WEAKNESS} prompt seam.
2. **Bounded proposal** (`orchestrator/harness.py:plan_harness`): ONE isolated, FRONTIER-
   tier `claude -p` call (`roles/harness_engineer/prompt.md`) reads the weakness table +
   the editable-surface manifest + current settings, and proposes up to 5 changes, each
   `{"weakness", "kind", "target", "change", "rationale", "evidence", "expected_effect",
   "risk"}`.
3. **Validation** (`orchestrator/harness.py:validate_proposals`, never the harness
   engineer's own claim of compliance): wholesale — a batch with ANY violation is
   rejected in full (nothing in it applies, not even the parts that were fine).

## The editable surface (`common/harness_surface.py`)

Pure module, no I/O — the CODE-level authority a proposal is checked against:

- `FROZEN_SURFACES` — glob/dir patterns over the factory tree (reusing
  `frozen_source._is_frozen`'s matching semantics): `common/code_gate.py`,
  `common/frozen_source.py`, `common/killswitch.py`, `common/harness_surface.py` (self),
  `store/schema.sql`, `common/store.py`, `vendor/`, `reporting/approvals.py`,
  `reporting/human_queue.py`, `dashboard/`, budget/ledger code, `tests/`. A `prompt`
  target touching any of these is rejected.
- `FROZEN_KNOB_PREFIXES` / `_is_frozen_knob` — every `autonomy.*` brake and `grade.*`
  key, and any `*.organizer`/`*.harness_engineer` trigger, is frozen **independent of
  SETTINGS_SPEC's own contents** (belt and suspenders) — even a future SETTINGS_SPEC
  edit that lists one of these can never make it proposable.
- `SANE_BOUNDS` — per-knob numeric ranges for every `SETTINGS_SPEC` **int** key
  (`max_parallel` 1–8, `max_tasks_per_shift` 1–20, `refill_threshold` 0–20, `max_profiles`
  1–40, `dispatch_waves` 1–4). Boolean knobs need no bounds — their domain is exactly
  `{true, false}`.
- `check_target(kind, target) -> (ok, reason)` — the format/surface check for one
  proposal target. For `learning_corrective` this validates only the `learning:<id>`
  SHAPE; existence + not-pinned-by-operator needs a store, so
  `orchestrator.harness.validate_proposals` checks that separately.

## The authority line (verbatim, design doc §"The authority line")

> The harness engineer PROPOSES; it never applies. Its proposals may touch only the
> declared editable surface: `SETTINGS_SPEC` knobs, role prompt files, and learnings
> rows. Brakes, budgets, gates, verifiers, the killswitch, the bus, the store schema,
> and this manifest itself are FROZEN — a proposal naming a frozen surface is rejected
> wholesale. Every proposal cites the evidence rows that motivated it. Application is an
> operator action. The trigger knob is config-only and outside `SETTINGS_SPEC`, so the
> loop can never widen or re-arm itself.

Rendered verbatim by `orchestrator/harness.py:_bounds_text()` into the role prompt's
`{BOUNDS}` seam, and asserted literally in `tests/test_harness.py`. The concrete surface
DETAIL (which keys, which bounds, which paths) is a separate `{SURFACE}` seam
(`_surface_text()`) — the harness engineer's prompt splits "what the line says" from
"what it means in practice" into two seams, unlike the organizer's single combined
`{BOUNDS}`.

## `validate_proposals` — every rule, wholesale

A batch fails if ANY proposal violates ANY of:

- `kind` is one of `setting` | `prompt` | `learning_corrective`.
- `weakness` names a cluster id present in the CURRENT weakness report.
- `evidence` is a non-empty list of strings, every one of which is drawn from that same
  report's `evidence_ids` vocabulary (an invented or off-table id rejects the batch).
- `target` passes `harness_surface.check_target(kind, target)`.
- `setting`: `change.value` casts via `common.config._cast_setting` to the knob's type
  AND sits inside `harness_surface.SANE_BOUNDS` (int knobs only).
- `prompt`: `change.patch` is a non-empty string.
- `learning_corrective`: `change.op` is `archive` or `pin`; `target`'s `learning:<id>`
  must EXIST and must NOT be pinned by the operator already.
- At most `MAX_PROPOSALS` (5) proposals per batch.
- No duplicate `(kind, target)` pair within one batch — the later duplicate is the
  violation.

An **empty** batch (`[]`) is a legitimate "the evidence doesn't clearly support a bounded
change this round" answer and validates as `ok`.

## Apply-path asymmetry (deliberate, design §D)

`factory harness apply <id>` (`orchestrator/harness.py:apply_proposal`) — always an
OPERATOR act, re-checking `harness_surface.check_target` at apply time too:

| kind | apply behavior |
|---|---|
| `setting` | `store.set_setting(target, value)` — the existing runtime-override seam (visible in `resolve_setting`'s `'override'` source, reversible the same way). Auto-applied; marks `'applied'`. |
| `learning_corrective` | archives or pins the cited learning (`change.op`), then — if `change.corrective` is non-empty — records a NEW learning on the same role with provenance naming the proposal id (`scope='harness-corrective'`). Auto-applied; marks `'applied'`. This is the ACE-playbook repair path the 2026-07-07 self-poisoning incident needed. |
| `prompt` | **NEVER writes a file.** Prints the patch + target and marks `'approved'` (not `'applied'`) — a human/agent lands it through normal git review. v1 keeps file writes out of the loop entirely (explicit YAGNI, design §"Out of scope"). |

`factory harness reject <id>` marks a live proposal `'rejected'` with an optional note.
Both apply/reject stamp `decided_by='operator-cli'` and `decided_at`; `applied_at` is
stamped ONLY when a proposal reaches `'applied'` (a later transition, e.g. `superseded`,
can never blank it — `COALESCE` in `set_harness_proposal_status`).

## Cadence + triggers

- **Config knob**: `super_worker.harness_engineer` (config.yaml), OFF by default.
  Deliberately **NOT** in `SETTINGS_SPEC` — exactly the `organizer` precedent, so a
  proposal can never widen or re-arm its own trigger.
- **Automatic**: with the knob on, `orchestrator/orchestrator.py`'s `cmd_run` calls
  `orchestrator.harness.maybe_plan_harness` at **shift END**, AFTER outcomes are
  recorded (`routing_outcomes`/`task_evidence` are written by the executor INSIDE
  `run_shift`, so by the time `cmd_run` regains control this shift's own evidence is
  already in the store). This differs from the organizer's shift-**START** hook
  (`orchestrator/shift.py`) — the harness engineer reasons about evidence a shift just
  produced, not evidence from before it ran.
- **Evidence-freshness gate**: `maybe_plan_harness` is a FREE no-op (no frontier call)
  unless at least `MIN_NEW_EVIDENCE` (10) new `task_evidence` rows have landed since the
  watermark of the last proposal batch (`store.latest_harness_proposal()`'s
  `created_at`; no prior batch = counted from the beginning). This is the gain governor
  that keeps the loop from spending frontier tokens on stale, already-mined evidence.
- **STOP vetoes the call** — `killswitch.is_halted()` is checked FIRST in both
  `plan_harness` and `maybe_plan_harness`, before even attempting the frontier call.
- **Explicit, regardless of the knob**: `factory harness mine|plan|show|apply|reject`
  (explicit human acts) work whether or not the automatic trigger is armed.

DEVIATION (noted per the design's own instruction to prefer the organizer precedent on
ambiguity): the design text lists "STOP check, config gate, and an evidence gate" as
`maybe_plan_harness`'s three internal gates. `org.maybe_plan_org`'s own precedent does
NOT read config itself — the `super_worker.organizer` knob gates whether `maybe_plan_org`
is even WIRED as the shift hook, at the `cmd_run`/`run_shift` call site, not inside the
function. This implementation mirrors that precedent: `super_worker.harness_engineer`
gates whether `maybe_plan_harness` is wired at all (`cmd_run`), not a second internal
config read inside the function — functionally identical (the trigger never fires when
the knob is off either way) and keeps the un-self-widenable property simple to reason
about in one place.

## Failure posture

| failure | what happens |
|---|---|
| STOP engaged | no `claude -p` call at all |
| evidence-freshness gate not met | no `claude -p` call at all (free no-op, `maybe_plan_harness` only — `plan_harness`/`factory harness plan` always calls through) |
| transport/parse failure (unparseable reply) | **nothing persisted** — a `factory learning` row (`scope='harness_engineer'`) is recorded |
| validation failure (any proposal in the batch violates any rule) | EVERY proposal in the reply is persisted with `status='rejected'` (audit trail) **plus** a learning; nothing is applied |
| valid | every proposal is persisted `status='proposed'` — awaiting an operator's `apply`/`reject` |
| the shift-end hook itself blows up | non-fatal to the shift (mirrors the org-planner hook): a `[harness]` line is printed AND a factory learning is recorded (`scope='harness_engineer'`) — never silent |

A rejected batch is never returned by `plan_harness`/`maybe_plan_harness` (`None`), but
every individual proposal in it stays visible via `store.harness_proposals()` /
`factory harness show` / the board's `harness.newest` — a bad batch is loud, not silent.

## Board visibility

`reporting/fleet_viz.py:harness_state(store)` feeds the `"harness"` key of `fleet_json`'s
payload (`--serve`'s `/api/fleet`, a read-path addition to an existing endpoint — no
dashboard write-action whitelist change needed) and a compact `<section>`
(`_harness_section_html`, modeled directly on `_org_section_html`) in the one-shot static
snapshot (`factory viz` → `logs/fleet.html`): proposed/approved counts, the
`harness_engineer_on` knob state, and the newest 5 proposals with their live status.
`derive_queue` (the Work Queue) surfaces a `"harness proposal(s) awaiting decision"` item
whenever any proposal is `'proposed'`, naming `"resources"` as the owning tab (mirroring
where the org chart's own card lives). A dedicated interactive card in the LIVE dashboard
SPA (`dashboard/static/fleet.html`'s Resources tab) is deliberately left for a follow-up
— this phase's dashboard work is the read-only `fleet_json`/static-viz surface only (the
design marks tab/SPA additions as optional "if a small section fits an existing tab").

## CLI reference

```bash
factory harness mine         # print the mined weakness table (no LLM, no store write)
factory harness plan         # the frontier harness engineer proposes a batch (<=5)
factory harness show         # every proposal, newest first
factory harness apply <id>   # apply ONE proposal (asymmetric per kind — see above)
factory harness reject <id>  # reject ONE proposal
```

## Uninstall / disable

Set `super_worker.harness_engineer: false` (or omit it — that's the default) in
config.yaml. This only disables the **automatic shift-end trigger**; `factory harness
plan` still works as an explicit human act. Live proposals already sitting `'proposed'`
stay visible via `factory harness show` / the board until an operator applies or rejects
them — the knob gates *proposing new batches*, not *deciding on existing ones*.
