# GSD spec-driven development → the factory

**Date:** 2026-06-27
**Status:** Building MVP (scope check). Source: open-gsd/gsd-core.

## What GSD is, and what's worth taking

GSD Core drives AI agents through a disciplined **Discuss → Plan → Execute → Verify →
Ship** loop, with heavy work in fresh-context subagents and durable artifacts
(STATE.md / CONTEXT.md) surviving sessions. Its essence for *autonomous* development:
**make the contract for a unit of work an explicit, machine-checkable artifact (a spec)
produced before code and enforced at every handoff** — not free text the worker
re-interprets.

The factory already has the context-engineering half (fresh-context super-workers in
clones, a blackboard, per-role memory, a gated auto-merge, mission-as-terminator). What it
lacks is the **spec/Plan discipline** and a **scope check** — and its #1 recurring failure,
`no_candidate`, is exactly an over-broad brief that was never checked for "one bounded
change" before a worker was spent on it.

## Ranked integrations (from the digest synthesis)

1. **Pre-dispatch scope CHECK** (high value / high fit) — *this MVP*.
2. Structured task spec artifact (target_surface / acceptance / in/out-of-scope).
3. Spec-bound VERIFY (gate on the task's own acceptance test, not just "suite stayed green").
4. Auto-decomposition (split, don't just block) on over-scope.
5. Spec lint in the producers (conductor + researcher emit spec-shaped tasks).
6. Spec-fulfillment feedback into factory memory (self-tuning scope check).

## MVP: spec-driven pre-dispatch scope check

Before a developer super-worker is spent on a claimed task, a judge decides whether the
brief is **one bounded, landable, testable change on a single surface**:

- **pass** — dispatch (with a normalized spec — target_surface + acceptance — woven into
  the brief, so the worker gets a sharper contract).
- **split** — the brief bundles N changes → add the sub-tasks (open) and block the original
  as `scope-split`; the smallest lands next shift. (#4, lightweight form.)
- **reject** — not landable as described → block with the reason + a factory learning.

This turns `no_candidate` from a wasted-clone post-mortem into a **free upstream
decision**. It composes with the existing memory: rejects/splits feed `factory` learnings,
so the conductor learns to write tighter briefs.

### Design (testable, fail-open)

- `reporting/scope_check.py`:
  - `normalize_verdict(raw) -> {decision, reason, spec, subtasks}` — coerce a judge's raw
    output; unknown decision or `split` with no subtasks → `pass` (never invent a block).
  - `prefilter(store, tasks, *, shift_id, judge) -> list[task]` — runs the judge per task
    on the MAIN thread (single-writer safe), enacts reject/split via the store, returns the
    `pass` tasks (with `task["spec"]` attached) to dispatch. **Fail-OPEN**: a judge
    exception → `pass` (a checker hiccup must never halt real work).
  - `scope_judge(task) -> raw` (production) — builds the `roles/scope_check/prompt.md`
    prompt + one cheap `claude_super` call + `_parse_obj`. Injected in tests.
- Wire into `orchestrator/develop.py execute_claimed_tasks`, between the task-claim/cap and
  the `ThreadPoolExecutor`, **gated by config** `super_worker.scope_check` (default OFF, so
  merging doesn't change live behavior until enabled). The kept task's `spec` is woven into
  the brief `work()` hands the developer.
- Fix the long-standing `cmd_task add` detail-drop (orchestrator.py) so a spec written into
  a task's detail survives — specs need detail.

### Testing (TDD, injected judge + tmp store)

- `normalize_verdict`: pass/split/reject; unknown→pass; split-without-subtasks→pass; non-dict→pass.
- `prefilter`: reject→blocked+learning, not dispatched; split→subtasks added + original
  blocked; pass→returned with spec; judge raises→pass (fail-open); empty input→empty.
- config gate: OFF → prefilter not invoked (dispatch unchanged).

## Out of scope (later integrations)

Spec-bound VERIFY (#3, large — needs the acceptance test wired into code_round), full
typed spec column (#2 — MVP weaves spec into the brief text), producer spec-lint (#5),
self-tuning feedback (#6). Each is a follow-on once the scope check proves out.
