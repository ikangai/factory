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

This runbook describes the system AFTER an adversarial-review fix round (2026-08-05,
same day as the initial build) hardened it considerably — every section below reflects
the FIXED behavior, not the originally-shipped one. Each fix is called out inline where
it changed something a prior reader would otherwise expect.

## The three-stage loop

1. **Weakness mining** (`reporting/weakness.py`, read-only, zero LLM): clusters the
   factory's own failure telemetry — `task_evidence` (action×stage), `routing_outcomes`/
   `fit_rows` (class×tier blocked-rate misroutes), `tasks.result` scope-check verdicts,
   `learnings` proven counterproductive by their own outcome counters (excluding archived
   AND pinned rows — see "Bad-lore convergence" below), `gate_eval_results` ok→fail
   flips, and `shifts` terminal-state attrition — into stable-slug clusters, each
   carrying a citable `evidence_ids` vocabulary. `mine_weaknesses(store, window=200)` ->
   `[cluster]`; `render_weakness_table(clusters)` renders the {WEAKNESS} prompt seam.
2. **Bounded proposal** (`orchestrator/harness.py:plan_harness`): ONE isolated, FRONTIER-
   tier `claude -p` call (`roles/harness_engineer/prompt.md`) reads the weakness table +
   the editable-surface manifest + current settings, and proposes up to 5 changes, each
   `{"weakness", "kind", "target", "change", "rationale", "evidence", "expected_effect",
   "risk"}` — `rationale`/`expected_effect`/`risk` are all REQUIRED non-empty strings.
3. **Validation** (`orchestrator/harness.py:validate_proposals`, never the harness
   engineer's own claim of compliance): wholesale — a batch with ANY violation is
   rejected in full (nothing in it applies, not even the parts that were fine).

## The editable surface (`common/harness_surface.py`)

Pure module, no I/O — the CODE-level authority a proposal is checked against:

- `FROZEN_SURFACES` — glob/dir patterns over the factory tree (reusing
  `frozen_source._is_frozen`'s matching semantics): `common/code_gate.py`,
  `common/frozen_source.py`, `common/killswitch.py`, `common/harness_surface.py` (self),
  `common/store.py`, `common/budget.py` (the ledger module), `common/bus.py` (the bus —
  named explicitly in the authority line), `store/schema.sql`, `vendor/`,
  `reporting/approvals.py`, `reporting/human_queue.py`, `dashboard/`, `tests/`. A
  `prompt` target touching any of these is rejected.
- `FROZEN_KNOB_PREFIXES` / `FROZEN_KNOB_KEYS` / `_is_frozen_knob` — every `autonomy.*`
  brake, `grade.*` key, every GATE/VERIFIER `SETTINGS_SPEC` key (`FROZEN_KNOB_KEYS`:
  `super_worker.require_test`, `super_worker.acceptance_exec`,
  `super_worker.scope_check`, `super_worker.reviewer`, `super_worker.milestone_verify`),
  and any `*.organizer`/`*.harness_engineer` trigger, is frozen **independent of
  SETTINGS_SPEC's own contents** (belt and suspenders) — even a future SETTINGS_SPEC
  edit that lists one of these can never make it proposable.
  **Fix (adversarial-review round, 2026-08-05):** `FROZEN_KNOB_KEYS` originally shipped
  EMPTY — the authority line says "gates, verifiers... are FROZEN" but nothing enforced
  it, so a proposal setting `super_worker.require_test=false` validated clean and, once
  applied, would have let the rail merge code with NO test from the very next shift. The
  five keys above are every SETTINGS_SPEC boolean that flips a merge/dispatch-quality
  CHECK on or off; the non-gate booleans (`auto_decompose`, `retry_on_discard`,
  `investigate_blocked`) and every capacity int stay editable.
- `SANE_BOUNDS` — per-knob numeric ranges for every EDITABLE `SETTINGS_SPEC` **int** key
  (`max_parallel` 1–8, `max_tasks_per_shift` 1–20, `refill_threshold` 0–20,
  `max_profiles` 1–40, `dispatch_waves` 1–4). Boolean knobs need no bounds — their
  domain is exactly `{true, false}`. An int key with NO `SANE_BOUNDS` entry is REJECTED
  by `validate_proposals` (not silently accepted) — see "validate_proposals" below.
- `FROZEN_ROLE_PROMPTS` — role prompts that are frozen even though they match the
  `roles/<x>/prompt.md` shape: the harness engineer's OWN prompt
  (`roles/harness_engineer/prompt.md` — the design's meta-harness exclusion; the loop
  must never tune the very prompt that states its own bounds) and every verifier/gate
  role's prompt (`roles/reviewer/prompt.md`, `roles/scope_check/prompt.md`,
  `roles/decompose/prompt.md`, `roles/judge/prompt.md`).
  **Fix (adversarial-review round):** this list didn't exist originally, and the
  `FROZEN_SURFACES`-only check for `prompt` targets was structurally DEAD — no
  `FROZEN_SURFACES` glob pattern could ever match a `roles/*/prompt.md` shape, so
  nothing actually blocked a `prompt` proposal from naming its own prompt file.
- `check_target(kind, target) -> (ok, reason)` — the format/surface check for one
  proposal target. For `learning_corrective` this validates only the `learning:<id>`
  SHAPE; existence + not-pinned-by-operator needs a store, so
  `orchestrator.harness.validate_proposals` checks that separately. For `prompt`, the
  shape check is now `posixpath.normpath(target) == target` (no traversal) AND a
  single-segment regex `^roles/[a-z0-9_-]+/prompt\.md$` (no nesting) BEFORE the
  `FROZEN_ROLE_PROMPTS`/`FROZEN_SURFACES` checks. **Fix (adversarial-review round):** the
  original naive `startswith("roles/") and endswith("/prompt.md")` check let a
  traversal target like `"roles/../common/code_gate.py/prompt.md"` pass the shape gate
  (it DOES start with `roles/` and end with `/prompt.md`) while `_is_frozen`'s glob match
  ran against the RAW, untraversed string and never fired — the target validated clean.
  `normpath` collapses the `..` before either check runs; a target whose normalized form
  differs from its raw form is rejected outright.

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
`{BOUNDS}`. **Fix (adversarial-review round):** `_surface_text()`'s `learning_corrective`
bullet used to reference "the `{MEMORY}` section above" as an f-string `{{MEMORY}}`
literal — which rendered as the literal text `"{MEMORY}"` INSIDE the finished SURFACE
text. `build_harness_prompt` fills `{SURFACE}` before `{MEMORY}`, so that stray literal
got a SECOND replace pass and the real memory-card text was spliced into the middle of
the SURFACE section's own sentence (and rendered TWICE when a card existed). Fixed by
never emitting a brace-wrapped seam name in prose; `build_harness_prompt` now also
ASSERTS the loaded template contains all six required seam markers before filling —
raising `ValueError` loudly if a future prompt edit ever drops one, instead of silently
shipping an incomplete prompt (a `.replace()` on an absent seam is a silent no-op, not an
error).

## `validate_proposals` — every rule, wholesale

A batch fails if ANY proposal violates ANY of:

- `kind` is one of `setting` | `prompt` | `learning_corrective`.
- `rationale`, `expected_effect`, and `risk` are all non-empty strings.
- `weakness` names a cluster id present in the CURRENT weakness report.
- `evidence` is a non-empty list of strings, every one of which is drawn from the
  **NAMED cluster's own** `evidence_ids` — not a union across every cluster in the
  report. **Fix (adversarial-review round, BLOCKER):** the original check pooled every
  cluster's `evidence_ids` into one global set, so a proposal could cite rows from an
  UNRELATED cluster and still pass — any cluster's evidence justified any edit. A
  `learning_corrective`'s own `target` must additionally be among the cited evidence —
  citing SOME row from the right cluster isn't enough; it must cite the row it corrects.
- `target` passes `harness_surface.check_target(kind, target)`.
- `setting`: `change.value` casts via `common.config._cast_setting` to the knob's type
  AND sits inside `harness_surface.SANE_BOUNDS`; an int key with NO `SANE_BOUNDS` entry
  rejects (the documented-but-previously-unenforced rule — see "The editable surface").
- `prompt`: `change.patch` is a non-empty string.
- `learning_corrective`: `change.op` is `archive` or `pin`; `target`'s `learning:<id>`
  must EXIST and must NOT be pinned by the operator already; `op='pin'` additionally
  rejects when the target learning `is_counterproductive` — a proven-bad lesson may only
  be `archive`d, never pinned (pinning it would re-inject proven-false lore into every
  worker's card — the EXACT self-poisoning failure this loop exists to fix).
- At most `MAX_PROPOSALS` (5) proposals per batch.
- No duplicate `(kind, target)` pair within one batch — the later duplicate is the
  violation.

An **empty** batch (`[]`) is a legitimate "the evidence doesn't clearly support a bounded
change this round" answer and validates as `ok` — but see "Watermark marker rows" below
for how an empty/unparseable OUTCOME is still recorded.

## Apply-path asymmetry (deliberate, design §D)

`factory harness apply <id>` (`orchestrator/harness.py:apply_proposal`) — always an
OPERATOR act, re-checking `harness_surface.check_target` at apply time too, and PRINTING
what it is about to do BEFORE doing it:

| kind | apply behavior |
|---|---|
| `setting` | Re-casts + re-bounds-checks `change.value` at apply time (never trusts the propose-time result survives — see "Raw-vs-cast apply" below), then `store.set_setting(target, str(casted))` with the CANONICAL cast value. Auto-applied; marks `'applied'`. |
| `learning_corrective` | Re-checks `pinned` (an operator pin made AFTER the proposal was filed must win, never be silently overridden) and, for `op='pin'`, re-checks `is_counterproductive` (a learning proven bad BETWEEN propose and apply must never be pinned). Archives/pins the cited learning, then — if `change.corrective` is non-empty — records a NEW learning on the same role with provenance naming the proposal id (`scope='harness-corrective'`). Auto-applied; marks `'applied'`. This is the ACE-playbook repair path the 2026-07-07 self-poisoning incident needed. |
| `prompt` | **NEVER writes a file.** Prints the patch + target and marks `'approved'` (not `'applied'`) — a human/agent lands it through normal git review. v1 keeps file writes out of the loop entirely (explicit YAGNI, design §"Out of scope"). |

`factory harness reject <id>` marks a live proposal `'rejected'` with an optional note.
Both apply/reject stamp `decided_by='operator-cli'` and `decided_at`; `applied_at` is
stamped ONLY when a proposal reaches `'applied'` (a later transition, e.g. `superseded`,
can never blank it — `COALESCE` in `set_harness_proposal_status`).

### Raw-vs-cast apply (fixed — adversarial-review round, BLOCKER)

The original `apply_proposal` wrote `str(change.get("value"))` — the RAW, uncast JSON
value — straight into `store.set_setting`. A validated `{"value": 2.0}` for an INT knob
stored the literal string `'2.0'`; `config._cast_setting`/`resolve_setting` then raised
`ValueError` on `int('2.0')` at EVERY subsequent read (`cmd_run`, `_settings_text`, the
dashboard Resources tab) — bricking the rail AND this module's own next
`factory harness plan`. `apply_proposal` now re-runs the FULL cast+bounds validation at
apply time (`_cast_and_check_setting`, the same helper `validate_proposals` uses) and
stores `str(casted)` — the canonical form only, never the raw value. If the re-check
fails (e.g. `SANE_BOUNDS` narrowed between propose and apply), the proposal is marked
`'rejected'` with the reason, and nothing is written.

## Watermark marker rows (fixed — adversarial-review round, BLOCKER)

An honest, VALIDATED empty batch (`[]`) or an UNPARSEABLE reply still spent a real
frontier call — but the ORIGINAL `plan_harness` persisted NOTHING for either outcome. The
evidence-freshness gate's watermark (`store.latest_harness_proposal()`'s `created_at`)
never advanced, so `maybe_plan_harness` re-fired a frontier call every SINGLE shift
forever once its evidence threshold was crossed once (confirmed by probe: 5 shifts of
unchanged evidence = 5 frontier calls = ~250k tokens for nothing).

Fixed with a MARKER ROW convention: `orchestrator/harness.py:_persist_marker` inserts
ONE `harness_proposals` row, `kind='none'`, `status` in `('empty', 'error')`, on either
outcome. A marker row is NEVER a real proposal (`kind='none'` is not a `VALID_KINDS`
member) — it exists solely so its `created_at` advances the watermark. Store-level
consequences:

- `store.harness_proposals(...)` — **EXCLUDES** marker rows by default (`NOT (kind =
  'none' AND status IN ('empty', 'error'))`); pass `include_markers=True` for an
  audit/debug view that wants them. `factory harness show` and the board both use the
  default (excluded) view — an operator is never shown a "proposal" with nothing in it.
- `store.harness_proposal_counts()` — the same exclusion, `{status: count}` via
  `COUNT(*)` (see "Board visibility" below for why this exists).
- `store.latest_harness_proposal()` — the ONE reader that **INCLUDES** marker rows; it
  IS the watermark, so it must see them.

`store/schema.sql`'s `harness_proposals.status` `CHECK` constraint was widened to
include `'empty'`/`'error'` alongside the five real-proposal statuses; `kind` stays
un-constrained (as it always was — a rejected/malformed batch must still persist even
when the reply named a bogus kind).

## Cadence + triggers

- **Config knob**: `super_worker.harness_engineer` (config.yaml), OFF by default.
  Deliberately **NOT** in `SETTINGS_SPEC` — exactly the `organizer` precedent, so a
  proposal can never widen or re-arm its own trigger.
- **Automatic**: with the knob on, `orchestrator.orchestrator.cmd_run` wires
  `orchestrator.harness.maybe_plan_harness` as `harness_planner`, passed into
  `orchestrator.shift.run_shift` — which invokes it INSIDE the shift, at the shift's END,
  AFTER outcomes are recorded (`routing_outcomes`/`task_evidence` are written by the
  executor earlier in the SAME `run_shift` call) and BEFORE the `tokens_used` ledger
  rollup. This differs from the organizer's shift-**START** hook (also inside
  `run_shift`, via `org_planner`) — the harness engineer reasons about evidence a shift
  just produced, not evidence from before it ran. The wiring itself is nested inside
  `cmd_run`'s `if executor is None:` block, exactly mirroring `org_planner` — a caller
  supplying their OWN executor must opt into `harness_planner` explicitly.
  **Fix (adversarial-review round, BLOCKER — hook restructure):** the ORIGINAL
  implementation was a hook living AFTER the `run_shift(...)` call returned, inside
  `cmd_run` itself. Three confirmed problems, all fixed by moving the trigger INTO
  `run_shift` (mirroring `org_planner`'s own seam):
  1. **Spend visibility** — a post-`run_shift` hook's `store.add_budget(...)` call
     landed AFTER `run_shift` had already computed `tokens_used` and closed the shift
     row, so the unattended loop's cumulative token brake (`cmd_run_loop`'s
     `loop_token_budget`) never saw it, and `shifts.tokens_used` disagreed with
     `shift_spend(shift_id)`. Now the ledger write happens BEFORE the rollup, so both
     agree.
  2. **Firing after a tripped brake** — the old hook fired regardless of how the shift
     ended, including `budget_exhausted`/`timed_out`/`halted` — spending MORE frontier
     tokens trying to improve the very loop that just tripped a brake. `run_shift` now
     skips `harness_planner` entirely when `status` is `budget_exhausted`, `timed_out`,
     or `halted` (the latter two are additionally unreachable via early-return paths
     already, but named explicitly for a reader who doesn't trace those).
  3. **No clean injection seam** — the old hook could only be tested by monkeypatching
     `orchestrator.harness.maybe_plan_harness` and threading assertions through
     `cmd_run`'s own broad `try/except`, which — per item below — could SWALLOW a guard
     regression silently. `run_shift(..., harness_planner=...)` is now directly
     injectable and testable, exactly like `org_planner`.
- **Evidence-freshness gate**: `maybe_plan_harness` is a FREE no-op (no frontier call)
  unless at least `MIN_NEW_EVIDENCE` (10) new `task_evidence` rows have landed since the
  watermark of the last proposal batch (`store.latest_harness_proposal()`'s
  `created_at`, INCLUDING marker rows — see "Watermark marker rows" above; no prior
  batch = counted from the beginning). This is the gain governor that keeps the loop
  from spending frontier tokens on stale, already-mined evidence.
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

## Bad-lore convergence (fixed — adversarial-review round, BLOCKER)

Three confirmed problems in the mining↔proposal↔apply loop for the `bad-lore` cluster
kind, all fixed together:

1. **Archived learnings re-mined forever** — `_bad_lore_clusters` scanned
   `store.all_learnings(...)` with no `archived` filter, so an already-retired row kept
   appearing in every weakness report (its counters are frozen, so the "corrective"
   proposed would be the identical, already-actioned one every batch). Fixed:
   `_bad_lore_clusters` now excludes `archived` rows.
2. **Pinned-but-counterproductive learnings mined a corrective that could never apply**
   — a pinned row's only legal `learning_corrective` (`op='archive'`, since `op='pin'`
   on an already-counterproductive row is itself rejected — see below) was ALSO rejected
   by `validate_proposals`'s pre-existing "target is pinned" check, wasting a proposal
   slot on something guaranteed to fail. Fixed: `_bad_lore_clusters` now also excludes
   `pinned` rows.
3. **`op='pin'` on a proven-bad learning re-injected it into every worker's card** — a
   pinned row survives `is_counterproductive`'s own suppression in
   `factory_memory.memory_card_with_ids` and leads the card FOREVER; pinning a row
   ALREADY proven counterproductive is the exact self-poisoning failure this whole loop
   exists to fix (the 2026-07-07 incident). Fixed at TWO layers: `validate_proposals`
   rejects `op='pin'` when the target `is_counterproductive`, and `apply_proposal`
   re-checks the SAME condition at apply time (a learning can become counterproductive
   between propose and apply).

## Evidence binding (fixed — adversarial-review round, BLOCKER — see `validate_proposals` above)

Covered above under "validate_proposals — every rule, wholesale"; called out again here
because it's the mechanism the operator-gate visibility work (below) depends on for
trustworthy citations.

## Operator-gate visibility (fixed — adversarial-review round, BLOCKER)

No operator surface used to render the PROPOSED VALUE or OP — `require_test=false` was
indistinguishable from a benign retune in both the CLI list and the board. Fixed:

- `factory harness show` (the list view) now prints an inline change summary per row
  (`orchestrator.harness._change_summary`): `setting` → `value=X`; `learning_corrective`
  → `op=X corrective='...'`; `prompt` → `summary='...'`.
- `factory harness show <id>` (NEW — the positional id now works for `show`, not only
  `apply`/`reject`) prints the FULL detail of one proposal: kind, target, weakness, the
  raw `change` JSON, every cited evidence id, and the full `rationale` text — which now
  includes `expected_effect`/`risk` folded in (see below), decided-by/at/result once
  decided.
- `apply_proposal` PRINTS what it is about to do — the concrete value/op/corrective —
  BEFORE performing it, for every kind (previously only the `prompt` branch printed
  anything).
- `reporting.fleet_viz.harness_state`'s `newest` entries, and the board's HTML section,
  now carry the same `change_summary` string.

**`expected_effect`/`risk` were silently DROPPED before** — `_persist` only stored
`rationale`. Fixed: `_compose_rationale` folds `rationale` + `"Expected effect: ..."` +
`"Risk: ..."` into ONE persisted text block (bounded to 2000 chars), so
`factory harness show <id>` is where an operator reads them back before deciding.

## Failure posture

| failure | what happens |
|---|---|
| STOP engaged | no `claude -p` call at all |
| evidence-freshness gate not met | no `claude -p` call at all (free no-op, `maybe_plan_harness` only — `plan_harness`/`factory harness plan` always calls through) |
| transport/parse failure (unparseable reply) | **no real proposal row** — a `factory learning` (`scope='harness_engineer'`) AND a watermark marker row (`status='error'`) are recorded |
| validation failure (any proposal in the batch violates any rule) | EVERY proposal in the reply is persisted with `status='rejected'` (audit trail) **plus** a learning; nothing is applied |
| valid, non-empty batch | every proposal is persisted `status='proposed'` — awaiting an operator's `apply`/`reject` |
| valid, EMPTY batch (`[]`) | **no real proposal row** — a watermark marker row (`status='empty'`) is recorded, so the evidence-freshness gate doesn't immediately re-fire |
| the shift-end hook itself blows up | non-fatal to the shift (mirrors the org-planner hook): a `[harness]` line is printed AND a factory learning is recorded (`scope='harness_engineer'`) — never silent |
| the shift ended `budget_exhausted`/`timed_out`/`halted` | the harness planner is SKIPPED entirely — a tripped brake must not spend more frontier tokens trying to improve the very loop that tripped it |

A rejected batch is never returned by `plan_harness`/`maybe_plan_harness` (`None`), but
every individual proposal in it stays visible via `store.harness_proposals()` /
`factory harness show` / the board's `harness.newest` — a bad batch is loud, not silent.

## Board visibility

`reporting/fleet_viz.py:harness_state(store)` feeds the `"harness"` key of `fleet_json`'s
payload (`--serve`'s `/api/fleet`, a read-path addition to an existing endpoint — no
dashboard write-action whitelist change needed) and a compact `<section>`
(`_harness_section_html`, modeled directly on `_org_section_html`) in the one-shot static
snapshot (`factory viz` → `logs/fleet.html`): EXACT proposed/approved counts (via
`store.harness_proposal_counts()`'s `COUNT(*)` — not a `limit=50` window, which could
undercount once there were more than 50 non-marker rows), the `harness_engineer_on` knob
state, and the newest 5 proposals with their live status AND a `change_summary` (see
"Operator-gate visibility" above). `derive_queue` (the Work Queue) surfaces a
`"harness proposal(s) awaiting decision"` item whenever any proposal is `'proposed'`,
naming `"resources"` as the owning tab (mirroring where the org chart's own card lives).
A dedicated interactive card in the LIVE dashboard SPA (`dashboard/static/fleet.html`'s
Resources tab) is deliberately left for a follow-up — this phase's dashboard work is the
read-only `fleet_json`/static-viz surface only (the design marks tab/SPA additions as
optional "if a small section fits an existing tab").

## CLI reference

```bash
factory harness mine         # print the mined weakness table (no LLM, no store write)
factory harness plan         # the frontier harness engineer proposes a batch (<=5)
factory harness show         # every proposal (any status), newest first, with an inline change summary
factory harness show <id>    # the FULL detail of ONE proposal (change JSON, rationale/expected-effect/risk, evidence ids)
factory harness apply <id>   # apply ONE proposal (asymmetric per kind — see above)
factory harness reject <id>  # reject ONE proposal
```

## A related, same-day fix riding this branch: `require_held_out`'s fail-closed default

`orchestrator/code_round.py:run_code_round`'s OWN `require_held_out` parameter shipped
with a `False` default (commit `056f96f`, landed on this branch from a separate process
mid-flight) — the ONE real production caller (`orchestrator/develop.py`) relied on that
default silently rather than opting into the per-merge held-out scope-out at its own call
site. Restored to `True` (matching `common/code_gate.py:auto_merge_eligible`'s own
fail-closed default); `orchestrator/develop.py`'s call now passes
`require_held_out=False` EXPLICITLY, so the opt-out is a visible call-site decision, not
a silently-inherited one. This is unrelated to the self-harness loop itself but landed as
part of the same adversarial-review fix round — see `tests/test_gate_require_held_out.py`.

## Uninstall / disable

Set `super_worker.harness_engineer: false` (or omit it — that's the default) in
config.yaml. This only disables the **automatic shift-end trigger**; `factory harness
plan` still works as an explicit human act. Live proposals already sitting `'proposed'`
stay visible via `factory harness show` / the board until an operator applies or rejects
them — the knob gates *proposing new batches*, not *deciding on existing ones*.
