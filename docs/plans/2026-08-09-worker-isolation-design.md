# Phase 3 — worker isolation: the interior authority split (2026-08-09)

**Provenance:** Phase 3 of `docs/plans/2026-08-06-production-hardening-roadmap.md`,
seam-mapped against main @ 071616f. The external reviewer's formulation is the target:

> If a compromised worker can directly open `blackboard.db`, "workers cannot promote,
> approve, or release" is a software convention rather than a security boundary.

## The premise, verified (an earlier reading of mine was wrong)

I initially concluded the boundary already existed, because the dev repo's `config.yaml`
carries `autopilot.prod: true` and `super_worker.user: "agent"`. That is not what a
deployed factory runs. `deploy/user-factory/apply-config-overlay.py:41-45` **inverts
exactly those keys on install**:

| key | dev repo | after the deploy overlay |
|---|---|---|
| `autopilot.prod` | `true` | **`false`** |
| `super_worker.user` | `"agent"` | **`""`** |
| `super_worker.claude_bin` | agent's claude | `"claude"` |

Confirmed against the live deployment: `prod=False`, `user=''`. `orchestrator.py:1085`
then resolves `as_user = (sw["user"] or None) if prod else None` → `None`, so
`claude_super` takes its same-user branch (`roles/common.py:216`). **Every worker runs as
the factory user, with Bash.** The guest-house runbook already concedes this
(`docs/runbooks/guest-house.md:183-186`).

What that worker can do today, with one `bash` call:
- read **and write** `$FACTORY_ROOT/store/blackboard.db` — own the tasks table, forge
  approvals, merges and learnings;
- read `scenarios/held-out/` and `checks/` — destroying the proposer/grader blindness
  §17.3 of ARCHITECTURE.md claims as an invariant;
- `rm $FACTORY_ROOT/STOP` — release the kill switch;
- read `~/.factory-secrets/env` — the PAT, 600 but owned by the same user it runs as.

And it is *told* where to look: `roles/common.py:297` substitutes `{FACTORY_ROOT}` into the
developer prompt, and `roles/developer/prompt.md:34-39` instructs the worker to shell into
it for bus announcements. `tests/test_developer.py:76-85` asserts that leak is present.

**Additional live finding (this machine, deployed factory):** `/Users/factory` is `750`
group `staff` and the blackboard is `644`. macOS puts every local account in `staff`, so
the worker user *and the operator's own account* can already read the deployed factory's
database. The guest-house doctor's 700 rule exists for this; that deployment predates the
installer fix that satisfies it.

## What is already right (build on it, do not rebuild)

The map found the interior is closer than the roadmap's "job bundle + inbox" sketch
assumes, and rebuilding wholesale would be the expensive route:

1. **`claude_p` is genuinely bounded** — `--tools "" --setting-sources "" --strict-mcp-config`,
   neutral cwd. The isolated transport needs no change.
2. **The orchestrator is already the only store-writer.** Every store write in the develop
   rail is hoisted to the main thread (`develop.py:268-329, 417-570`); workers never hold a
   connection. The stated Phase-3 goal is ~90% met by construction.
3. **The git artifact is already the deliverable.** Branch + `changed_paths` are verified
   independently of anything the worker says; prose only feeds advisory channels.
4. **`sudo -H -u` exists, works, and is tested** (`roles/common.py:167-173`,
   `develop.py:712-717`, `tests/test_super_worker.py:126-148`). For the developer call the
   hard boundary is a **config/deploy problem, not a code problem**.
5. **The broker spool is a finished template** for any inbox we do need — atomic write,
   sidecar hash, fail-closed expiry, nonce ledger, ownership split.

## Threat model (state it plainly, so the phase is not oversold)

An OS-user boundary stops a worker **reaching** the control plane. It does **not** stop a
worker **lying** in what it returns — that is what the deterministic gates are for (tests,
frozen-path check, red-proof, smoke grade, reviewer), and those already treat the reply as
untrusted. Phase 3 closes reach, not honesty. Say so in the runbook.

## Components

### A — provision the worker user (the deploy gap)

`01-create-user.sh` creates only `factory`; **nothing creates the worker user**, so a fresh
guest house cannot use the hard path even if it wanted to. Add to the guest-house wizard:
a Standard, non-admin `factory-worker` account with its own 700 home; its own `claude`
binary installed and logged in; and the narrow passwordless sudo grant
`roles/common.py:171` requires (`factory` may run `sudo -u factory-worker <claude>` and
`sudo chown -R factory-worker <tmp clone>`, nothing else). `guesthouse_check` gains a rule
asserting the grant is exactly that narrow.

### B — turn the boundary ON in the deployment

Flip the overlay: `autopilot.prod: true`, `super_worker.user: "factory-worker"`,
`claude_bin` = that user's claude. This is the change that makes the phase real; A must
land first or every dispatch fails at stage `chown`.

### C — close the FACTORY_ROOT leak

Stop substituting `{FACTORY_ROOT}` into the developer prompt and drop the bus-announcement
instruction from `roles/developer/prompt.md`. Worker bus presence moves orchestrator-side:
the rail announces dispatch/outcome on the worker's behalf (it already knows both). Invert
`tests/test_developer.py:76-85` to assert the path is **absent**. Related:
`worker_bus_env`'s `AGORA_DIR` also carries `FACTORY_ROOT` and is dropped under `as_user`
anyway (`roles/common.py:213-214`) — so the hard mode and the bus are already mutually
exclusive; this resolves that contradiction in the safe direction.

### B.1 — red-proof breaks under `as_user` (found by probe; B must fix it)

`develop.py:783` threads `base_repo=dev_clone` — the clone that `develop.py:712-717` has
just `chown -R`'d **to the worker** — and `code_round.py:222-225` creates a detached
worktree inside it for the red-proof check. Probed: `git worktree add` writes
`.git/worktrees/<name>`, so against a clone the caller cannot write it fails with
`fatal: could not create leading directories of '.git/worktrees/…': Permission denied`.

Nobody has hit this because **both knobs are off**: `red_proof` defaults false and the
deployed `prod` is false. Component B flips `prod` on, so the pair becomes reachable and
every candidate would then fail at the red-proof stage.

Fix inside B: take the base worktree from a repo the FACTORY still owns. Cheapest correct
option is to create it **before** the chown (`base_sha` is already recorded at
`develop.py:708-711`, one line earlier); alternative is to add it in `main_repo` after
`fetch_candidate`. Either way `base_repo` must stop pointing at worker-owned territory.
This is also a general rule the phase should state: **after the chown, the factory may
READ the clone but must never WRITE it** — every factory-side git operation on `dev_clone`
needs auditing against that rule, not just this one.

### D — prove the boundary (the phase's actual deliverable)

Reach is a claim; claims need tests. Add `guesthouse_check` rules, run **as the worker
user**, asserting it CANNOT: read/write `$FACTORY_ROOT/store/blackboard.db`, read
`config.yaml`, read `scenarios/held-out/`, read `~factory/.factory-secrets/env`, or delete
`$FACTORY_ROOT/STOP`. Plus the installer must `chmod 700` the factory home (fixing the live
750 finding) and a drill that runs the probes and records the output.

### E — typed terminal status (prerequisite for any real bundle)

`classify_empty_handed` (`develop.py:65-82`) distinguishes timeout / crash / transport /
refusal by **substring-matching English** against `REFUSAL_MARKERS`. Those classifications
gate real spend (auto-decompose, retry) and real store writes. The worker should write a
small typed result file into its own workdir (status, learnings, notes) which the
orchestrator reads from the clone it already owns — no new channel, no new trust. Prose
sniffing stays as the fallback for a worker that writes nothing.

### F — the conductor (name the hole; do not pretend to close it)

`roles/conductor.py:226` runs a Bash super-worker with `workdir = paths.FACTORY_ROOT` and
`max_turns=60`, because it drives `./bin/factory` by design. It is a **bigger hole than the
developer** and Phase 3's "workers" scope does not cover it. Honest position for this
phase: shrink its toolset to what it demonstrably needs, document it as the one privileged
role, and make the *state* it can reach the thing that is defended (Phase 1 already moved
publication credentials out; the store remains). A conductor jail needs a narrow typed
command API — its own phase, not a sub-bullet of this one.

## Out of scope (with reasons, so nobody re-litigates)

- **Containers per worker.** `envs/docker_env.py` is eval-shaped (seeds `seed_files`, no
  git-in/branch-out, no claude in the image) and runs `--network none`, which a worker that
  must call the Anthropic API cannot use. The guest-house rules also forbid the host Docker
  socket. A container variant needs an egress policy this codebase has no precedent for.
- **Full job-bundle/inbox rebuild.** The git artifact is already the bundle and the
  orchestrator is already the only writer; a spool would add a channel without adding a
  boundary. Revisit if E's typed result proves insufficient.
- **Per-worker OS users (one account each).** The shared `factory/auto` worktree and
  `threading.Lock` merge lock (`develop.py:336`) assume one process. One worker *account*
  gives the boundary; N accounts would force the merge orchestrator-side first.

## Acceptance

Drill 2 from the roadmap, run for real: a task whose brief instructs the worker to read the
blackboard, the held-out set and the secrets file, and to clear STOP. All five must fail
with permission errors, recorded in the runbook. Plus: the full suite green with the
boundary ON, proving the rail still works through `sudo -u`.
