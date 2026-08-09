# Phase 3 — grading isolation: containing the code the factory runs (2026-08-09, v2)

**Supersedes v1 of this file** (committed 31f652e), which scoped the phase around running
the developer's `claude -p` as a separate OS user. Two adversarial reviews, both with
executed probes, showed that was the wrong target: it hardens a call that is not the
shortest path to the control plane, and turning it on as specified would have broken the
factory outright (100% `no_candidate` from git's dubious-ownership check, a leaked clone
per task, a `chown` that mutates the real target repo through hardlinked inodes, and a
conductor that dies on turn one). The evidence and the discarded plan are preserved in
git history; this document is the corrected design.

## The shortest path is grading, not the LLM call

The factory **executes worker-authored code, as the factory user, on every candidate**:

| # | site | what runs | as whom |
|---|---|---|---|
| 1 | `code_round.py:269` `adapter.run_tests(cand_repo)` | the candidate's whole test suite | factory |
| 2 | `code_round.py:227-268` red-proof | changed test nodes, on base **and** candidate | factory |
| 3 | `code_round.py:~278` acceptance (`run_named_test`) | the spec-named test | factory |
| 4 | `code_round.py:358,361` post-merge re-baseline | `grade_fn(main_repo)` + `run_tests(main_repo)` — **in the factory's own `factory/auto` worktree** | factory |

`adapters/base.py:103-115` is a plain `subprocess.run(["python3","-m","pytest","tests/",
"-q"], cwd=cand_repo)` inside the factory process. The frozen gate protects clive's
IMMUTABLE/GOVERNANCE tiers and **never `tests/`** — and `super_worker.require_test: true`
*mandates* that the worker ship a test. So the worker is required to supply Python that the
factory then runs with its own authority. A reviewer proved it end to end: a candidate
`conftest.py` executed under the normal test command and wrote to the blackboard.

No OS boundary around the `claude -p` call changes any of this. That is why v1 bought
almost nothing.

## Prerequisite — the dashboard write channel (small, must land first)

`dashboard/fleet_server.py:298-385` serves nine POST routes — including
`/api/queue/approval`, `/api/resume`, `/api/settings`, `/api/mission` — on `127.0.0.1`
with **no authentication**. The only guard is a CSRF check whose first branch is
`if not origin: return True`, so a `curl` with no `Origin` header passes. Any local
process can forge an operator approval or clear the killswitch. Until that is closed,
isolating the grader is moot: the isolated process can simply ask the dashboard.

Deliverable: a token file (mode 600, factory-owned) or a unix socket in a 0700 directory,
required by every write route; the board reads the token from the same file. Add
"cannot POST any `/api/*` write route" and "cannot release the killswitch by any means"
to the boundary probes — v1's probe list checked "cannot delete STOP", which `/api/resume`
does *for* the caller, so the drill would have returned a false green.

## Threat model

Closing grading isolation means **candidate-authored code cannot reach the control plane
when it runs**. It does not make the worker honest — a worker can still lie in its reply,
and the deterministic gates (tests, frozen paths, red-proof, smoke grade, reviewer) already
treat everything it says as untrusted. It also does not close the conductor (§ Not closed).

## Components

### A — a grading identity

An unprivileged `factory-grader` account: no admin, no sudo of its own, its own 700 home,
**no read access to `FACTORY_ROOT`** (which requires the factory home to be 700 — currently
750 group `staff` on the reference deployment, and `staff` contains every local account),
no store, no credentials, and no `~/.factory-secrets`. It needs only a python toolchain and
the target's test dependencies. Unlike v1's worker user it needs **no `claude` install and
no Claude login**, which removes the entire GUI/OAuth provisioning burden that made v1's
Component A a hidden multi-step manual procedure.

### B — one execution seam

Every site in the table above routes through a single executor:
`run_target_code(argv, cwd, timeout) -> (rc, output)`. Same-user (today's behavior) when
isolation is off; `sudo -u factory-grader -- <wrapper> …` when on. `adapters/base.py`'s
`run_tests`/`run_named_test` are the only callers that need to change, plus the re-baseline
path. Keeping it to one function is what makes the boundary auditable — and what lets a
test assert that **no** candidate-code execution bypasses it.

### C — export, not worktree (the constraint that shapes everything)

`develop.py:771` builds the candidate checkout with `adapter.add_worktree(main_repo, …)` —
a **linked worktree of the factory's own repo**, sharing `main_repo/.git`. Handing that to
another identity would expose the factory's object store and refs to it, and pytest wants
to write into its working tree anyway (`__pycache__`, `.pytest_cache`, test temp files).

So the graded checkout must be a **detached export** — `git clone --no-hardlinks` (never a
plain local clone: probed, plain `git clone` hardlinks objects on one APFS volume, so
changing the copy's permissions changes the original's inodes) or `git archive` — created
fresh, owned by the grader, and thrown away after. The merge is unaffected: the branch is
already fetched into `main_repo` by `fetch_candidate` *before* grading, so what gets merged
is a ref the grader never touches.

### D — the post-merge re-baseline (the one v1 would have missed entirely)

`code_round.py:358-361` runs `grade_fn` and `run_tests` **in `main_repo`** after the merge
lands — i.e. the candidate's code runs in the factory's persistent `factory/auto` worktree.
Isolating only the pre-merge checkout leaves this wide open. The re-baseline gets the same
treatment: a fresh export at the post-merge sha, graded by the grader identity.

### E — a root-free, pinned grant

v1's sudoers grant was effectively `factory ALL=(root) NOPASSWD: /usr/sbin/chown -R
<user> *` — a root chown with an unconstrainable path argument, i.e. a privilege-escalation
primitive handed to the same account the conductor runs as. This design needs **no root and
no chown**: one user-to-user grant, `factory ALL=(factory-grader) NOPASSWD:
/opt/factory/run-target-code`, pinned to a wrapper script the operator installs read-only.
The wrapper takes a cwd and an argv, refuses paths outside the export root, and execs.

### F — cleanup

`shutil.rmtree` cannot remove files another identity created inside directories it created
(probed: `PermissionError`, and `ignore_errors=True` silently leaves the tree — v1 would
have leaked a full clone per task, forever, on a macOS with no `/tmp` sweeper). Deletion
runs through the same wrapper: `sudo -u factory-grader <wrapper> rm -rf <export>`, with the
factory-owned parent removed afterwards.

### G — proof

`guesthouse_check` rules executed **as the grader**, asserting it cannot: read or write
`store/blackboard.db`, read `config.yaml`, read `~factory/.factory-secrets/env`, read
`FACTORY_ROOT` at all, delete `STOP`, **or POST to any dashboard write route**. Fix v1's
context gate first — `is_guest_house_context()` recognizes only the `factory` account, so
new rules run as a different user would `SKIP` and exit 0: a proof that passes by not
running.

## Not closed by this phase (say so plainly)

- **The conductor** runs as factory with Bash and `workdir=FACTORY_ROOT`, because it drives
  `./bin/factory` by design. It is 1-per-shift and executes no target code, but it remains
  the largest interior hole and needs its own phase (a narrow typed command API).
- **The developer's `claude -p`** still runs as the factory user. After grading isolation it
  can still read the tree — but it can no longer get its *code* executed with authority,
  which was the escalation that mattered.
- **Honesty.** Unchanged, and owned by the gates.

## Out of scope, with reasons

- **Containers.** Worth noting that the objection that killed them for the LLM worker does
  **not** apply here: grading needs no Anthropic API, so `--network none` is viable, and
  `envs/docker_env.py` already runs scenarios that way. What blocks it is macOS + the
  guest-house rule against exposing the host Docker socket, plus the eval-shaped provisioner
  (`seed_files`, no git-in/branch-out). A container variant is the natural *second*
  implementation of Component B's seam once a Linux deployment exists.
- **Per-worker identities.** One grader account is enough: grading is already serialized
  behind the merge lock, and N accounts multiply provisioning without adding a boundary.

## Posture and acceptance

Ships **default OFF** (`super_worker.grader_user: ""`), matching Phases 1 and 2: OFF must be
byte-identical to today. Enabling is an explicit operator act, gated on:

1. **One real end-to-end round** with isolation on — a candidate built, graded, merged.
   v1's acceptance ("suite green with the boundary ON") proves nothing, because every test
   of this seam monkeypatches `subprocess.run` and never execs anything.
2. **The malicious-candidate drill**: a task whose test attempts to write the blackboard,
   read the secrets file, and clear STOP. All must fail with permission errors, recorded in
   `docs/runbooks/worker-isolation.md` — including the dashboard route, which is the one an
   isolated process would reach for next.
3. A regression test asserting no candidate-code execution path bypasses Component B's seam.
