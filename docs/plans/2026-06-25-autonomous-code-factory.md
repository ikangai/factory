# Design — The Autonomous Code Factory

Date: 2026-06-25 · Status: **proposed** (Martin's north-star → architecture)
Companion: `ARCHITECTURE.md` (current system), `docs/2026-06-24-roadmap-generic-autonomous-research-factory.md` (prior roadmap).

## North star

A self-directed software factory whose product is **the target repo itself**. The
human **steers** (sets the mission in `MISSION.md`); the factory **builds and programs**
the target — updating its harness spec, changing its source, adding features — with
**full authority and full autonomy (auto-merge from day one)**. clive is target #1;
other repos follow via the Target Adapter.

The human is the steering wheel, not the brakes. The brakes are **automated**: a
deterministic grader, a frozen safety surface, git-reversibility, and loud transparency.

## The one insight: the champion loop generalises

Today a candidate is a **spec patch** (one field of clive's `open` block). It becomes a
**code branch / PR** on the target. Everything downstream is the existing factory,
generalised:

| Stage | Today (spec) | Generalised (code) |
|---|---|---|
| Producer | Proposer (one spec field) | **Developer super-worker** (a code branch) |
| Candidate | `spec_path` (YAML) | a git branch / patch on the target |
| Champion | reigning spec | reigning **target commit** + spec |
| Grader | scenario eval (real shell end-state) | scenario eval **+ the target's own test suite** |
| Promote | human clicks Promote | **auto-merge** the branch (full auto) |

## Components

### 1. The Developer super-worker
A super-worker (full-capability `claude -p`) whose task is a **code change** to the
target. It runs in its **own clone** of the target under the **Guest House boundary**
(a separate Standard User → OS-enforced isolation of the operator's files/creds → Bash
is safe). Interior: a **builder–reviewer / TDD loop** (write code → run the target's
tests → adversarially review → iterate), fanning out with `/workflows` + subagents in
its clone. Output: a committed branch + a patch + evidence (tests green), returned to
the conductor. Blind to held-out (its clone has no factory repo).

Handoff: the conductor (operator user) `git clone`s the target into a worker-owned dir
(self-contained `.git`, chowned to the worker user — *not* a worktree, which can't cross
the user boundary); task in via stdin, result branch fetched back; clone torn down.

### 2. The fleet + agora collaboration
Multiple super-workers — **developers, testers, reviewers** — run as a team, coordinating
on the **agora bus**: a shared task ledger (claim/divide work), soft file-claims, and
@mention review. Each works in its own clone, so they **don't interfere**; agora is how
they *don't duplicate* and *review each other*. They narrate via the **diary skill**.
The conductor seeds the ledger from the backlog (below) and collects results.

### 3. Autonomous direction-finding (the backlog)
The factory **mines** what to work on, extending today's research/scenario mining:
recorded failures, research briefs (papers/repos), and **feature/dev-direction ideas**
distilled from the mission + the target's own gaps. These become a **backlog** the
conductor assigns to the fleet. No human task-authoring required.

### 4. Full-auto authority + the automated gate set
A candidate **auto-merges** iff **ALL** hold (else: discarded, logged to the diary):
- the target's **own test suite passes** (hard correctness gate);
- scenario pass-rate **improves or holds** on the working set;
- **no held-out regression** (held-out is real + sampled — fixes the current vacuous gate);
- **no Goodhart/divergence alarm** (working up while held-out flat, panel spread, overfit);
- the diff touches **no frozen-safety path** (below);
- **no safety-battery flag**.

No human approval. The gates are the authority.

## Safety-via-automation (this replaces the human gate)

Because there is no human gate, these must be load-bearing:

1. **Frozen safety surface (structural).** A declared set of target paths/symbols — the
   command sandbox, permission/destructive-action guards, the self-mod gate — that **no
   candidate diff may modify**. A diff touching them is auto-rejected before grading,
   the same way a frozen-spec change is rejected today. The factory can make clive
   *better*, never *less safe*. **This is the one boundary "full authority" keeps.**
2. **Git reversibility.** Every auto-merge is one atomic commit on a tracked lineage
   (`main`, with a `factory/` reflog trail). Anything is revertible in one command.
3. **Auto-revert on regression.** After a merge, the next baseline re-evaluates the new
   champion. If it regresses (scenarios, held-out, tests, or a divergence alarm), the
   factory **auto-reverts the commit** — mistakes self-heal instead of needing a human.
4. **Circuit breakers + budget caps** (exist today): per-round token/time ceilings,
   no-improvement stops, consecutive-error breakers — now also a **global kill switch**
   (a `STOP` flag the human can drop to halt the fleet immediately).
5. **Transparency = the windshield.** The board + diary + agora bus are a full audit
   trail. The human steers by reading them, adjusting `MISSION.md`, reverting, or
   hitting the kill switch — never by approving each change.
6. **Oracle validation + held-out hygiene** (exist today) carry over and matter *more*:
   with no human to catch a Goodhart, the #64 validator and held-out stability are the
   last line.

## Genericity
clive is target #1. The Target Adapter (axis A) grows: `clone()`, `run_tests() ->
pass/fail+report`, `frozen_paths()`, `apply_patch()/merge()`. Pointing at another repo =
a new adapter + its frozen-safety declaration + its test command. Nothing else changes.

## First build slice (smallest end-to-end vertical)
Prove the loop on one change, behind every guardrail:
1. **Target Adapter: `run_tests()` + `frozen_paths()`** for clive (+ a `clone()` helper).
2. **Frozen-source validator** — reject a candidate diff touching a frozen path (TDD).
3. **Developer super-worker** — given a clive weakness, in a clone, produce a branch +
   passing tests (wiring/contract tested; live run is the operator's, like the spec super-worker).
4. **Code-candidate grading** — build clive from the branch, run `run_tests()` + the
   existing scenario eval, score champion-vs-challenger.
5. **Full-auto merge + auto-revert-on-regression** + the **kill switch** (TDD).
6. Diary + board surface the merge/revert.

Everything reuses the existing loop, grader, runner, envs, super-worker transport,
diary, and research. The new code is the adapter test/clone/frozen methods, the
frozen-source validator, the developer role, the code-candidate grading path, and the
auto-merge/auto-revert + kill switch.

## Open questions (to resolve as we build)
- Guest House setup: confirm a Standard User + per-user `claude login` on the same
  subscription works; the sudoers grant for `sudo -u`.
- Exactly which clive paths/symbols are the frozen safety surface (needs a clive read).
- Auto-merge target: straight to `main`, or a `factory/integration` branch the human
  fast-forwards on a cadence? (Full-auto = main; the reflog + auto-revert make it safe.)
