# Self-harness loop — weakness mining → bounded proposal → gated adoption (2026-08-05)

**Provenance:** operator asked to apply Lilian Weng, *"Harness Engineering for
Self-Improvement"* (lilianweng.github.io/posts/2026-07-04-harness/) to the factory.
The post's central claim: near-term recursive self-improvement runs through the
**harness** — the loops, memory, tools, and evaluators around the model — not the
weights. Its Self-Harness pattern is a three-stage loop: **weakness mining** (cluster
real failures) → **bounded proposal** (targeted harness edits on an explicit editable
surface) → **validation** (evidence-grounded, with human oversight and verification
kept *outside* the loop being optimized). This design lands that loop in the factory.
It is also "loop A" that `docs/plans/2026-07-02-dashboard-wishlist-roadmap.md` §828
explicitly deferred: *"Factory self-modification — separate design doc."* This is that
design doc.

## Goal (operator, 2026-08-05)

Per accumulated evidence, the factory proposes bounded improvements to its own
harness — knob settings, role-prompt seams, and corrections to its own learnings
playbook — each citing the failure rows that motivated it; the operator approves or
rejects; nothing frozen is ever self-editable.

## What exists (the substrate this builds on)

- **Failure telemetry, already recorded, never mined**: `task_evidence`
  (action×stage per failed task), `routing_outcomes` (+ `store.fit_rows()`
  aggregator), `shifts` terminal states, scope-check verdict prefixes in
  `tasks.result`, `gate_eval_results` flips, `learnings` effectiveness counters
  (`merged_after`/`blocked_after`, `is_counterproductive`).
- **The ACE playbook**: `learnings` table + `reporting/factory_memory.py` memory
  card. Has dedup, staleness cites, distill — but no corrective path driven by
  outcome evidence. The 2026-07-07 self-poisoning (false "19 pre-existing failures"
  lore echoed by every worker for weeks) was cleaned up *by hand*.
- **The frontier-role pattern**: `orchestrator/org.py` `plan_org` — killswitch
  first, ledger spend regardless of outcome, wholesale `validate_chart` rejection
  with every reason enumerated, rejected artifacts persisted for audit, config-only
  trigger deliberately absent from `SETTINGS_SPEC`.
- **Bounded-knob authority**: `SETTINGS_SPEC` (`common/config.py:33`) — the single
  whitelist of runtime-writable knobs; `resolve_setting` store-override precedence.
- **Frozen-path enforcement — for the TARGET only**: `common/frozen_source.py` is
  pure and target-agnostic, but the frozen set comes from the target adapter. The
  factory has **no manifest of its own** editable-vs-frozen surfaces.
- **Human gate machinery**: `pending_approvals` / Queue tab / operator CLI.

## The authority line (the design's spine)

Verbatim, enforced in code, echoed in the role prompt:

> The harness engineer PROPOSES; it never applies. Its proposals may touch only the
> declared editable surface: `SETTINGS_SPEC` knobs, role prompt files, and learnings
> rows. Brakes, budgets, gates, verifiers, the killswitch, the bus, the store schema,
> and this manifest itself are FROZEN — a proposal naming a frozen surface is
> rejected wholesale. Every proposal cites the evidence rows that motivated it.
> Application is an operator action. The trigger knob is config-only and outside
> `SETTINGS_SPEC`, so the loop can never widen or re-arm itself.

Post-alignment notes: evidence grounding is Weng's "ground all edits in failure
analysis"; the frozen manifest is the EVOLVE-BLOCK boundary; operator-only apply is
"human oversight external to the loop"; the read-only miner is AHE experience
observability.

## Components

### A. `common/harness_surface.py` — the factory's own surface manifest
Pure module, no I/O. Declares:
- `FROZEN_SURFACES`: glob/dir patterns over the factory tree — `common/code_gate.py`,
  `common/frozen_source.py`, `common/killswitch.py`, `common/harness_surface.py`
  (self), `store/schema.sql`, `common/store.py`, `vendor/`, `reporting/approvals.py`,
  `reporting/human_queue.py`, `dashboard/`, budget/ledger code, tests.
- `FROZEN_KNOB_PREFIXES` / explicit frozen keys: every `autonomy.*` brake
  (push_approval, budgets, deadlines), `grade.*`, any `*.organizer` /
  `*.harness_engineer` trigger — even if a future edit lists one in SETTINGS_SPEC,
  the manifest still blocks it (belt and suspenders).
- `EDITABLE_SURFACES`: `SETTINGS_SPEC` keys minus frozen; `roles/*/prompt.md`;
  `learning:<id>` rows.
- `check_target(kind, target) -> (ok, reason)` — reuses `frozen_source._is_frozen`
  matching semantics for paths.
- `SANE_BOUNDS`: per-knob numeric ranges for `setting` proposals (e.g.
  `max_parallel` 1–8, `dispatch_waves` 1–4); a proposed value outside bounds
  rejects. Booleans need no bounds. Derived clamps, never hand-tuned per proposal.

### B. `reporting/weakness.py` — deterministic miner (read-only, zero LLM)
`mine_weaknesses(store, *, window=200) -> [cluster]` over the last N closed tasks
+ their evidence:
- `stage-failure`: task_evidence grouped by (action, stage) — e.g. 12× no_candidate
  at stage refusal.
- `class-misroute`: fit_rows where a class×tier pairing has ≥MIN attempts and
  blocked-rate ≥ threshold while a sibling tier succeeds.
- `scope-churn`: tasks whose result starts `scope-split`/`scope-reject` — brief
  quality weaknesses, grouped by milestone.
- `bad-lore`: learnings where `is_counterproductive(row)` — playbook rows proven
  harmful by their own outcome counters.
- `gate-flip`: gate_eval_results regressions.
- `shift-attrition`: shifts ending halted/timed_out/budget_exhausted, rate over
  window.
Each cluster: stable slug id, kind, count, exemplar row ids (task/evidence/learning
ids — the *evidence vocabulary* proposals must cite), one-line summary.
`render_weakness_table(clusters)` fixed-width like `render_fit_table`, explicit
"(no weaknesses …)" empty state. Read-only per `reporting/__init__` contract.

### C. `roles/harness_engineer/` + `orchestrator/harness.py` — bounded proposer
Prompt seams: `{MISSION}` `{WEAKNESS}` (rendered table + exemplar ids) `{SURFACE}`
(rendered manifest + sane bounds) `{SETTINGS}` (current resolved values + source)
`{MEMORY}` `{BOUNDS}` (authority line). Output: EXACTLY a JSON array, ≤5 proposals:

```json
{"weakness": "<cluster-slug>", "kind": "setting|prompt|learning_corrective",
 "target": "<spec key | roles/x/prompt.md | learning:<id>>",
 "change": {"value": ...} | {"summary": "...", "patch": "..."} |
           {"op": "archive|pin", "corrective": "<replacement lesson or ''>"},
 "rationale": "...", "evidence": ["<row ids from the weakness report>"],
 "expected_effect": "...", "risk": "..."}
```

`plan_harness(store, *, claude_fn=None, shift_id=None)` copies `plan_org`'s shape
exactly: killswitch FIRST → deferred `claude_p` import → frontier model → ledger
spend under role `harness_engineer` regardless of outcome → parse → validate →
persist (valid → `status='proposed'`; invalid → `status='rejected'` + factory
learning; unparseable → learning + None).
`validate_proposals(props)` — wholesale, every reason enumerated: kind in enum;
target passes `harness_surface.check_target`; `setting` value casts via
`_cast_setting` and sits inside `SANE_BOUNDS`; `learning:<id>` exists and is not
pinned-by-operator; evidence ids nonempty and drawn from the current weakness
report's vocabulary; ≤5 proposals; dedup by (kind, target) — later duplicate
rejects.
`maybe_plan_harness(store, *, shift_id, claude_fn)` — the gated trigger: STOP
check, config gate, and an evidence gate (runs only if ≥ `MIN_NEW_EVIDENCE=10` new
failure rows since the last proposal batch; else free no-op).

### D. `harness_proposals` table + operator-gated adoption
Schema (+ `_migrate` ALTER pattern):
`id, created_at, shift_id, weakness, kind, target, change_json, rationale,
evidence_json, status ('proposed'|'approved'|'applied'|'rejected'|'superseded'),
decided_at, decided_by, applied_at, result`.
Store CRUD stays thin (`add_harness_proposal`, `harness_proposals(status=None)`,
`set_harness_proposal_status`); all policy lives in `orchestrator/harness.py`.

Apply paths — deliberately asymmetric:
- `setting` → `store.set_setting` (the existing runtime-override seam; visible in
  `resolve_setting` source as 'override'; reversible by the same seam). Auto-apply
  on operator `apply`.
- `learning_corrective` → `archive_learning`/`pin_learning` + `record_learning`
  corrective with provenance note naming the proposal id. Auto-apply on operator
  `apply`. This is the ACE playbook-repair path the self-poisoning incident needed.
- `prompt` → **never auto-applied.** `apply` prints the patch + target and marks
  `approved`; a human/agent lands it through normal git review. v1 keeps file
  writes out of the loop entirely.

CLI `factory harness mine|plan|show|apply <id>|reject <id>` mirroring `cmd_org`;
apply/reject record `decided_by='operator-cli'`. Works regardless of the auto
trigger, like `factory org plan`.

### E. Wiring + visibility
- Config: `super_worker.harness_engineer: false` — config-only, **NOT** in
  `SETTINGS_SPEC`, comment block copying the organizer rationale verbatim-adjacent.
- Shift hook: end-of-shift in `cmd_run` after outcomes are recorded, try/except +
  factory learning on failure, exactly like the org-planner hook's failure posture.
- Dashboard: `harness` key in `fleet_json` (proposed/approved counts + newest 5)
  rendered `_org_section_html`-style; proposals also enter `derive_queue` so the
  Queue tab shows "harness proposal awaiting decision" with owning tab.

## Phasing (single branch `feat/self-harness-loop`, sequenced commits)

1. Surface manifest + tests (pure, no deps).
2. Weakness miner + renderer + tests (read-only).
3. Schema + store CRUD + migration tests.
4. Role prompt + plan/validate/maybe + tests (claude_fn monkeypatched).
5. CLI + shift hook + config + dashboard + runbook `docs/runbooks/self-harness-loop.md`.

## Out of scope (YAGNI, explicit — but on the roadmap)

- **A/B validation ladder**: applying a `setting` proposal to a shadow profile and
  benching it before operator review. Needs bench-profile plumbing; follow-up.
- **Auto-landing prompt edits** through the factory's own develop rail with the
  factory as target. That is full loop A; it needs the surface manifest (built
  here) plus a factory-adapter — separate design.
- **Learnings full lifecycle** (decay curves, periodic re-verification cadence)
  beyond the corrective path.
- **Meta-harness** (proposals that edit the miner/validator themselves) — the
  manifest freezes them; Weng's post itself flags reward-hacking risk here.
- Evolutionary search over harness variants; joint weight/harness optimization.
