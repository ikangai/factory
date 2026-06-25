# The Conductor Loop — autonomous, mission-steered code factory

*Design — 2026-06-25. Builds on `2026-06-25-autonomous-code-factory.md` (the
champion/challenger code loop, the frozen-safety surface, `develop_and_merge`, the
Guest-House super-worker transport).*

## North star

The human steers **only via a mission statement** (plus, optionally, a link to a repo
with issues). A fleet of collaborating super-workers builds and programs the target
(clive first), full-auto, gated by the target's own tests + the frozen-safety surface +
git reversibility. The factory finds its *own* work (research + issues) and runs until
the mission is reached.

## Two decisions (validated 2026-06-25)

1. **The orchestrator is an LLM conductor** — a persistent claude *lead* on agora that
   reasons, plans, spawns workers, monitors the bus, intervenes, judges the mission, and
   writes the report. *Not* a deterministic scheduler. (The user chose the boldest model
   knowingly.)
2. **It runs in bounded shifts that resume** — it wakes (daily via launchd, or
   on-demand), works one budget-bounded session, then sleeps. All state lives in the
   store, so each shift resumes where the last left off. "Continuous" in effect; bounded
   and reviewable per shift.

## Architecture — a free-thinking brain in a caged blast radius

A **shift** = the deterministic harness (Python; reuses the `factory daily` / launchd
machinery) spawns exactly **one conductor**: a claude super-worker with `settings: user`
(a real agora lead, with the diary skill + web), handed a **token + turn + wall-clock
budget** and the conductor contract as its prompt.

The conductor *thinks and acts* within the shift. It drives the factory through its own
**CLI** (`factory develop --task "…"` via Bash → `develop_and_merge`), coordinates and
watches via **agora**, narrates via **diary**, files issues via `gh`, and at end-of-shift
writes the **daily report + blog** and a **resume note** to the store.

**The rail it cannot cross:**
- **The merge gate** (frozen-check + tests + auto-merge-eligibility + auto-revert) lives
  *inside* `develop_and_merge`. No conductor reasoning can merge unsafe code.
- **The ceilings** (token budget, `STOP` kill-switch, max-cycles, wall-clock) are
  enforced by the harness that spawned it. When one trips, the harness kills the shift
  *from outside* — the conductor can't vote itself more rope.
- **Observable + reversible** — agora bus, diary/blog, the store, git.

It is "persistent" through **state, not process**.

## The loop (the conductor's within-shift cycle, budget-bounded)

1. **Orient** — read the mission, the open backlog, last shift's results + research
   digest, and the target's open issues (`gh`).
2. **Plan** — synthesize a *small* set of bounded tasks (issues + research + gaps), each
   "one bounded change."
3. **Dispatch** — per task, `develop_and_merge` (gated). **Sequential by default**; fan
   out to a parallel agora **squad** only when the work warrants it.
4. **Monitor / intervene** — *between* worker turns, read the result + the bus. Intervene
   = refine-and-retry, reassign, split, or escalate (`@human` → the report). Not mid-call.
5. **Test / expand** — workers do TDD; the conductor can spawn a **tester/reviewer** pass
   and file new issues (`gh issue create`) for found-but-not-fixed.
6. **Reflect + feed research** — write a digest of what shipped; hand it to the
   researchers so their next mining is *outcome-informed*. The generative loop.
7. **Mission-check** — assess progress vs mission → a **status** (`advancing` /
   `steady_state` / `blocked`), never a silent binary "done." Steady-state (research dry
   + backlog empty + scores plateaued K shifts) → surface *"nothing left, awaiting
   mission revision."* The **mission is the terminator — not an empty issue queue**
   (workers create issues; the queue alone would never empty).
8. **Report** — daily report + blog + resume note. Sleep.

## Components — the store is the spine

New tables (SQLite, alongside the existing champion/candidates/runs/…):
- **mission** — the human's steer (statement + optional target repo; one active).
- **tasks** — the backlog: `source ∈ {issue, research, worker, human, mission}`,
  `status ∈ {open, claimed, in_progress, done, dropped, blocked}`, result (merge sha /
  why-dropped), the shift that worked it.
- **shifts** — each bounded session: budget, tokens used, status (`running` →
  `completed` / `halted` / `timed_out` / `budget_exhausted` / `error`), report, resume
  note.
- **digests** — research↔dev feedback: what shipped this shift + a prose summary for the
  researchers (+ a `consumed` flag).
- **mission_status** — the status timeline (advancing / steady_state / blocked / reached)
  + rationale + metrics (backlog size, research-dry streak, score deltas).

Roles, all on agora: **conductor** (lead), **developer** (gated code), **researcher**
(web + propose), **tester/reviewer** (verify + expand).

## Failure handling (the rail doing its job)

- Worker produces nothing / breaks tests → `no_candidate` / `discarded:tests` → log,
  retry-refined or file an issue; nothing merges.
- Worker touches frozen surface → `discarded:frozen`; not overridable.
- Merge regresses champion → **auto-revert** (built); conductor sees `auto_reverted`.
- Conductor hangs / loops / overspends → harness ceilings kill the shift; the resume note
  + store let the next shift continue. Every merge is atomic + reversible.
- Barrier wedge → own-squad mitigation + shift wall-clock backstop. `STOP` → clean halt.
- Bad-judgment shift → all merges gated + git-reversible; the report surfaces it; the
  human re-steers via the mission.

## Testing (TDD, no token burn)

The store schema, the shift harness (ceilings/resume), the mission-status state machine,
and the plan→dispatch→reflect→mission-check flow are all **hermetic** — the conductor's
LLM calls and `develop_and_merge` are *injected fakes*, exactly like the current suite.
The conductor prompt is tested at assembly level. The *live* conductor is
operator-smoke-run, like `develop-once`.

## Build order — each step shippable + green

1. **Store schema** for the loop (mission / tasks / shifts / digests / mission_status) +
   CRUD. **← this PR.** The spine.
2. **Shift harness** — bounded launcher (budget / `STOP` / max-cycles / wall-clock) that
   spawns one conductor, collects the report, writes the resume note. Reuses `daily`.
3. **Conductor contract** — `roles/conductor/prompt.md` + wiring (agora lead, the CLI it
   drives, the budget).
4. **Research feed + tester role** + the shipped-digest → research feedback.
5. **Mission-check + steady-state** surface.
6. **Wire to `factory run` / `daily`** + launchd.

Then: a live smoke shift (operator-run) on a throwaway clone → then the real clive.
