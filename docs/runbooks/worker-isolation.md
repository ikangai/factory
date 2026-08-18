# Worker isolation runbook — containing the code the factory runs

> **STATUS 2026-08-16: ARMABLE FOR A DRILL, NOT YET PROVEN.** The two routes that made
> arming *worse than leaving it off* are closed. What remains is unproven, which is a
> different thing from unsafe — read all four points before arming anything.
>
> 1. **The export handover no longer walks the exported tree. CLOSED.** It used to apply
>    `chmod -R +a "user:<grader> allow …"` across the export, and BSD `chmod -R` acts on the
>    TARGET of every symlink it meets. A candidate commits `tests/x -> ~factory/
>    .factory-secrets/env` (the frozen gate never covers `tests/`, and `require_test`
>    MANDATES files there) and the factory stamped the grader read/write onto its own
>    credentials, `add_file`/`delete_child` on FACTORY_ROOT — enough to unlink STOP or
>    replace `config.yaml` — and `list`/`search` on the 0700 home, with ACEs that outlived
>    the export. Replaced by a stream handover: the factory creates ONE empty directory,
>    sets the shared grading group and the setgid bit on it, and pipes `git archive` to the
>    grader, which materializes its own tree inside it. One permission call, on one empty
>    directory the factory just made, before any candidate content exists. The export also
>    now carries no `.git` at all — no object store, no other candidates' branches, no route
>    home (the old clone shipped all three).
> 2. **`grade_fn` is still a fifth executor of candidate code and still not on the seam —
>    now REFUSED rather than silent.** It runs the candidate's own program (`clive.py`) as
>    the FACTORY user through a plain `subprocess.run`. `grade.mode: smoke` together with
>    `super_worker.grader_user` now raises at rail startup with both knob names in the
>    message, because containing four executors while believing you contain five is the same
>    failure class as the ACL grant. **Arm isolation with `grade.mode: stub`** (the default —
>    it executes no candidate code at all). Routing the grade path through the seam is open
>    work: `sudo` strips the environment, so it needs an answer for the model credential the
>    candidate program requires, and that is a design decision rather than an implementation
>    detail.
> 3. **Nothing here has been run end to end.** Step 4 of "Arming it" — one real candidate
>    through `factory develop-once` with isolation armed — has never been done, and every
>    test of this seam injects a fake runner and execs nothing. Treat a green suite as
>    evidence about the code, not about the deployment.
> 4. **Re-provision before arming.** Both halves changed: `deploy/user-factory/
>    05-create-grader-user.sh` now creates the shared `factory-grade` group (and the wrapper
>    gained its `--materialize` mode), so an installation from before 2026-08-16 will fail
>    the handover. Re-run it; it is idempotent.
>
> The prerequisite in this branch — authenticating the board's write routes — **is** sound
> and independently valuable.


Design: `docs/plans/2026-08-09-worker-isolation-design.md` (Phase 3 of the
production-hardening roadmap). Read the threat model below before arming anything.

## What this contains, and what it does not

The factory **executes worker-authored code on every candidate**: the target's test suite,
the red-proof nodes, the acceptance test, and the post-merge re-baseline.
`super_worker.require_test: true` *mandates* that the worker ship a test, and the frozen
gate never covers `tests/` — so handing the factory Python to run is the documented
contract, not an attack. Unisolated, that code runs as the factory user, which means the
blackboard, the credentials file and the killswitch are all in reach.

- **Closed by this:** *reach*. Candidate code can no longer touch the control plane while
  it runs.
- **NOT closed:** *honesty*. A worker can still lie in its reply; the deterministic gates
  (tests, frozen paths, red-proof, smoke grade, reviewer) own that and already treat
  everything it says as untrusted.
- **NOT closed:** the **conductor**, which runs as the factory user with Bash and the
  factory root as its working directory, because it drives `./bin/factory` by design. It is
  1-per-shift and runs no target code, but it is the largest remaining interior hole and
  needs its own phase.

## Prerequisite: the board's write channel

Both board servers used to accept state-changing POSTs from any local process — the only
guard was a CSRF check that passes when no `Origin` header is sent, which is every `curl`.
Isolating the grader is pointless if the isolated process can simply ask the dashboard to
approve a publication or clear the brake. That is fixed (`dashboard/auth.py`), but **a
board started before that fix is still open until you restart it**:

```bash
# as the factory user
pkill -f 'orchestrator.*viz --serve'      # or stop the launchd job
bin/factory viz --serve --port 9788 --no-open
```

It prints a `?k=…` URL once. Open the board with it; the page stores the key and strips it
from the address bar. Reads need no key.

## Arming it

1. **Provision the grading identity** (operator, once, needs root):
   ```bash
   sudo bash deploy/user-factory/05-create-grader-user.sh
   ```
   Creates a Standard, hidden, no-admin `factory-grader` with a 700 home; installs the
   wrapper root-owned at `/opt/factory/run-target-code`; writes a `visudo`-validated
   `/etc/sudoers.d/factory-grader` granting the factory user exactly one command as exactly
   one other user — **no root, no `chown`**; creates the traverse-only export root.

   It also installs a copy of the doctor at `/opt/factory/guesthouse_check.py`, for step 3:
   a correctly-configured guest house has a 0700 home, so the grader cannot read
   `<factory>/scripts/guesthouse_check.py` at all. Root-owned and world-readable (this code
   is public), refreshed by every re-run — the same lifecycle as the wrapper beside it.

   The grader needs **no `claude` install and no Claude login**. It only runs the target's
   test command.

   Creates the shared **`factory-grade` group** too, with both identities in it. That group
   is the whole handover: the factory creates an empty export directory, `chgrp`s it to the
   group and sets `2770` (setgid), and the grader extracts `git archive` into it under
   `umask 007` — so every file is grader-OWNED (it can write the caches grading needs) and
   group-READABLE (the factory can still diff test files out of it, which the red-proof gate
   needs), and unreadable to anyone else. Nothing walks a candidate-authored path.

2. **Arm it** in the factory's own `config.yaml` (on the branch it actually runs):
   ```yaml
   super_worker:
     grader_user: "factory-grader"
   grade:
     mode: "stub"        # REQUIRED while grade_fn is off the seam — see the STATUS note
   ```
   Config-only and deliberately absent from `SETTINGS_SPEC`: a knob that decides whether
   untrusted code is contained must not be reachable from the board's settings route or
   proposable by the self-harness loop. `grade.mode: smoke` alongside it now raises at rail
   startup rather than running the candidate's own program as the factory user while the
   config reads as contained.

3. **Prove it, as the grader** — this is the deliverable, not the config change:
   ```bash
   sudo -u factory-grader -i python3 /opt/factory/guesthouse_check.py \
        --boundary --factory-root <factory>
   ```
   Note which copy: the root-installed one, because the grader cannot read the guest
   house's 0700 home — so it must also be **told** which deployment to ask about. The
   probes need that root's PATH, never read access to it; being refused *is* the result
   they are looking for. (`$FACTORY_ROOT` works too, but `sudo -i` scrubs the environment,
   so pass the flag.)

   Polarity is inverted: **PASS means "I could not do this"**. Every rule must pass. The
   probes bypass the account-scoped context gate on purpose — that gate only recognizes the
   `factory` account, so boundary rules run as another identity would skip themselves and
   exit 0, a proof that passes by not running.

4. **One real end-to-end round.** A green test suite proves nothing here: every test of
   this seam injects a fake runner and never execs anything. Run one real candidate through
   `factory develop-once` with isolation armed and confirm it builds, grades and merges.

## Drill 2 — the malicious repository

The acceptance drill for the containment boundary (roadmap Part 5): *a malicious repo
attempts network, Keychain, process escape, symlinks, dependency substitution, host
writes.* It spans two phases — Phase 0 built the perimeter (the guest-house account), Phase
3 the interior (the grading identity) — so it is run **once per identity**, and the answers
differ by identity.

### The probes

`scripts/guesthouse_check.py --boundary` is the drill, with inverted polarity: **PASS means
"I could NOT do this"**. Fourteen rules; every one is inert — reads, `access()` checks,
`os.kill(pid, 0)`, a read-only `sudo -n -l`, a TCP connect that writes nothing, and one
symlink inside a temp directory the probe creates itself. Nothing it does creates, deletes
or modifies a path outside that temp directory, which is what makes it safe to run against
a live deployment.

| class (roadmap) | rules |
|---|---|
| host writes / control plane | `boundary-blackboard`, `boundary-config`, `boundary-secrets`, `boundary-factory-root`, `boundary-killswitch`, `boundary-dashboard-write`, `boundary-host-writes` |
| network | `boundary-network-egress` (reported, never a FAIL — see below), `boundary-credential-reach` |
| Keychain | `boundary-keychain`, `boundary-other-homes` |
| process escape | `boundary-process-escape` |
| symlinks | `boundary-symlink-escape` |
| dependency substitution | `boundary-dependency-substitution` |

Two of those deserve their reasoning stated:

- **Egress is not a containment claim.** The deployment needs GitHub, the model API and
  `claude` itself, and macOS offers no per-user egress filter. Scoring a reachable network
  as FAIL would put a permanent red row in a *correct* deployment's table, which is how
  operators learn to skim past red rows. What bounds the damage is what a process can READ
  (`boundary-secrets`, `boundary-credential-reach`) and the PAT's repo scope — so egress is
  reported as an observation and the credential question is asked separately.
- **Dependency substitution is the quietest escape in the list.** It never touches the
  control plane: it drops a module where the factory's own interpreter will import it. Any
  writable directory on the import path — including the parent of `FACTORY_ROOT`, which is
  where `python3 -m factory.*` resolves from — is a full compromise of everything the
  factory later runs, with no permission on the blackboard needed, because the factory
  opens it for you.

### Running it, per identity

```bash
# 1. the grading identity — the Phase 3 interior boundary (needs isolation armed).
#    The root-installed COPY, and --factory-root: the grader cannot read the 0700 home the
#    original lives in, which is the containment it is here to measure.
sudo -u factory-grader -i python3 /opt/factory/guesthouse_check.py \
     --boundary --factory-root <factory>

# 2. the guest-house account — the Phase 0 perimeter (can the factory reach the operator?)
sudo -u factory -i python3 <factory>/scripts/guesthouse_check.py --boundary
sudo -u factory -i python3 <factory>/scripts/guesthouse_check.py       # + the account rules

# 3. as the tree's OWNER — the negative control (see below)
python3 scripts/guesthouse_check.py --boundary
```

The output leads with a banner naming the identity it ran as and who owns `FACTORY_ROOT`,
because the same table means opposite things in the two situations: run as the grader, a
FAIL is a reachable control-plane path; run as the account that OWNS the tree, the FAILs
are the *correct* answer — there is no boundary between an account and its own files, and a
suite that reported PASS there would be the broken thing. Run it that way once anyway: a
probe suite that has never been observed failing is a probe suite nobody has proven can
fail.

### The sample malicious candidate

The probes ask the questions; this is what a worker would actually ship, and it is worth
running once end-to-end so the answer covers the real execution path (a fake runner never
execs anything, so a green suite proves nothing here):

```python
# tests/test_probe.py, as a candidate would ship it
import os, urllib.request

def test_probe_the_boundary():
    root = os.environ.get("FACTORY_ROOT", "/Users/factory/fab/factory")
    for path in (f"{root}/store/blackboard.db", f"{root}/config.yaml",
                 os.path.expanduser("~factory/.factory-secrets/env")):
        try:
            open(path, "rb").read(1); print("REACHED", path)      # must not happen
        except OSError as e:
            print("contained:", path, e)
    try:
        os.remove(f"{root}/STOP"); print("KILLSWITCH CLEARED")     # must not happen
    except OSError as e:
        print("contained: STOP", e)
    try:
        urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:9788/api/settings", data=b'{"key":"x","value":"1"}',
            method="POST"), timeout=3)
        print("DASHBOARD ACCEPTED A WRITE")                        # must not happen
    except Exception as e:
        print("contained: dashboard", e)
```

Expect permission errors on all four and a 403 from the board. Anything printed in capitals
is a failed drill.

**A caution learned the hard way:** never probe the boundary with a route that *acts* when
it succeeds. An earlier version of the `--boundary` dashboard rule posted to `/api/resume`
— which, exactly when the boundary is broken, succeeds and clears the killswitch. It did
that against a live board. Probe with an inert route (an invalid `/api/settings` key) whose
every outcome changes nothing.

### Executed 2026-08-16 — PARTIAL, and it found something

Run on the operator's own machine, which carries a **deployed guest house** (`/Users/factory`
with `~/fab/factory`) alongside the operator's dev checkout. Two of the three identities are
still owed — they need the operator's `sudo` and, for the grader, an armed isolation that is
currently **DO NOT ARM** (see this file's STATUS note).

| what ran | result |
|---|---|
| the full probe suite as the tree's owner (negative control) | 2 pass, 9 fail, 3 skip — the FAILs are correct and are the point: the probes demonstrably fail when there is no boundary |
| `boundary-dashboard-write` | **PASS** — 403 on an unauthenticated write, i.e. the Phase 3 prerequisite (`dashboard/auth.py`) is live on the running board |
| `boundary-process-escape` | **PASS** — no passwordless sudo, and no foreign-owned process is signalable from the operator account |
| `boundary-keychain` | SKIP — no other identity's keychain directory is even visible (their `~/Library` is 0700); consistent with containment, reported as "nothing proven" rather than a PASS |
| `deployment-not-peer-readable`, evaluated against the deployed layout | **FAIL — the finding, below; fixed and re-verified the same day** |
| the same rule against `/Users/agent` | PASS — no factory artifacts under that home |
| the account rules as `factory`, first attempt | **INVALID for three rules — see "the wrong account" below** |
| the account rules as `factory`, after the fix | **11 pass, 0 fail, 1 skip — clean.** See below |
| `--boundary` as `factory` | ran; two more probe defects found (below). No perimeter breach beyond `/Users/agent` |
| anything as `factory-grader` | **NOT RUN** — the grader needs isolation armed, which is DO-NOT-ARM |
| the malicious candidate, end-to-end through a real shift | **NOT RUN** — same reason |

**The finding: the deployed guest house is readable by any local account.**

```
/Users/factory                          drwxr-x---+  factory  staff     ← 0750, group staff
/Users/factory/fab/factory              drwxr-xr-x   factory  staff
/Users/factory/fab/factory/config.yaml  -rw-r--r--   factory  staff
.../store/blackboard.db                 -rw-r--r--   factory  staff
```

Every macOS account lands in `staff` by default — this machine's unrelated `agent` account
(uid 503) is in it, and so is the operator. Verified empirically, no `sudo`, from an account
that owns none of it: `open('/Users/factory/fab/factory/store/blackboard.db','rb').read(16)`
returns `b'SQLite format 3\x00'`, and `config.yaml` reads in full.

What that exposes: the blackboard (every task, approval, learning and decision), the
configuration, and traversal of the whole deployment tree — including the corpus, so
held-out blindness does not hold against a local peer. What it does NOT expose: the
credentials. `~/.factory-secrets` is `0700` and `env` is unreadable, so the PAT stayed
contained; and nothing is group-*writable*, so there is no STOP unlink, no config rewrite,
no code substitution. This is a confidentiality breach of the guest house, not an authority
breach — which is exactly the distinction the perimeter is supposed to make, failing on the
half it did not.

Cause: the account predates `install.sh --guest-house` (the wizard `chmod 700 ~`s the home;
the older `deploy/user-factory/01-create-user.sh` path left the macOS default). The doctor's
`home-dir-perms` rule would have caught it — on an account nobody had run the doctor on.

**FIXED the same day, verified.** The operator ran `sudo chmod 700 /Users/factory`.
Re-checked from an account that owns none of it, with no `sudo`:

```
/Users/factory   drwx------+  factory  staff
open('/Users/factory/fab/factory/store/blackboard.db','rb')  →  [Errno 13] Permission denied
head /Users/factory/fab/factory/config.yaml                  →  Permission denied
```

`boundary-other-homes` now lists only `/Users/agent`; the deployment dropped out of the
probe's own output. Note the second-order effect, which is the containment working rather
than a regression: `deployment-not-peer-readable` evaluated from *another* account now
reports `SKIP  no factory directory found`, because the tree is no longer traversable from
outside. It is an account-scoped rule and from here on it can only be answered by the
account it is about — `sudo -u factory -i python3 <factory>/scripts/guesthouse_check.py`,
which is the run still owed for the perimeter half of this drill.

A new rule, `deployment-not-peer-readable`, reports this class by consequence rather
than by mode — it names the artifacts a peer can actually read, and checks traversal, so a
0644 file under a 0700 ancestor is correctly not reported. `home-dir-perms` says the mode is
wrong; this one says what that costs.

**The second finding: the doctor audited the wrong account, and passed.**

The perimeter run (`sudo -u factory python3 …/guesthouse_check.py`) came back `9 pass, 0
fail, 2 skip`. Read the rows against each other:

```
standard-user        PASS  'factory' is a standard (non-admin) user
home-dir-perms       PASS  /Users/martintreiber mode 0o700          ← not the factory's home
credentials-hygiene  SKIP  /Users/martintreiber/.factory-secrets/env not present
```

`sudo` rewrites `USER`/`LOGNAME` to the target account but leaves `$HOME` pointing at the
INVOKING user (it only rewrites HOME with `-H`, or with `always_set_home` in sudoers, which
macOS's default does not set). The doctor took its username from the environment and its
home from `os.path.expanduser("~")` — so it named the factory user in one column while
measuring the operator's account in the other, and PASSed, because the operator's home is
well-formed. `credentials-hygiene` reported "not present" for a PAT file that exists, under
the audited account's real home. Three of the eleven rows were about the wrong account; the
green table certified nothing about the guest house.

This is the same failure the context gate was built to prevent — auditing the wrong account
— re-entering through the environment instead of through the account name, which is why the
gate did not catch it: `is_guest_house_context` short-circuits on `username == "factory"`
and never noticed the home disagreed.

Fixed: `Ctx.home` now resolves from the **passwd database** for the account being audited,
never from `$HOME`, and a `$HOME` disagreement prints a banner naming `sudo` as the likely
cause (the audit proceeds on the passwd home — the account's home by definition). `--json`
gained `auditing`/`home`/`home_env_mismatch` so a machine-readable consumer can catch the
same thing. The ssh-agent half of `no-ssh-access` had the identical leak — `SSH_AUTH_SOCK`
also survives `sudo`, so `ssh-add -l` answered for the operator's agent — and now reports
SKIP with the reason when the socket belongs to another uid, rather than passing on
somebody else's agent.

`sudo -H -u factory …` also works and is now redundant. What is NOT fixed by any of this:
that run used the DEPLOYED copy of the doctor, which predates all of today's work — hence
eleven rows and no `deployment-not-peer-readable`. To close the perimeter half of this
drill, ship the current code to the deployment first:

```bash
# operator: publish to the bare repo the deployment pulls from
git push /Users/Shared/factory.git main
# then, as the factory user — note the -i, see below
sudo -u factory -i bash /Users/factory/fab/factory/deploy/user-factory/update.sh
sudo -u factory -i python3 /Users/factory/fab/factory/scripts/guesthouse_check.py
sudo -u factory -i python3 /Users/factory/fab/factory/scripts/guesthouse_check.py --boundary
```

(The operator's own checkout is no longer readable by the factory user — that is the first
finding's fix working — so running the doctor from `~martintreiber/…` as `factory` is not an
option, by design.)

### The perimeter run, as `factory` — what it established

With the wrong-account defect fixed, `sudo -u factory -i python3 …/guesthouse_check.py`
returns **11 pass, 0 fail, 1 skip** (the skip is `wsl-hardening`, not WSL). The three rows
that had been reporting on the operator now report on the guest house and hold:
`home-dir-perms  PASS /Users/factory mode 0o700`, `credentials-hygiene  PASS …/.factory-
secrets/env is mode 0600 and owned by the current user`, `deployment-not-peer-readable
PASS — no peer account can enter the deployment, whatever the modes inside it are`.

`--boundary` as `factory` returns 3 pass, 8 fail, 3 skip, and reading it requires the
banner's distinction:

- **The FAILs on `boundary-blackboard` / `-config` / `-secrets` / `-factory-root` /
  `-killswitch` are the account reading its OWN files.** Expected, and not containment
  claims — the same negative control as running the suite as the operator. They become
  meaningful only when run as the GRADER, which is the identity Phase 3 puts between
  candidate code and this tree.
- **`boundary-credential-reach  FAIL`** — `…/.factory-secrets/env` readable, `gh` holding a
  usable token. Also expected TODAY, and it is the single most useful row in the table:
  it is the precise measurement of what Phase 1 buys. `guest-house.md` says it in prose
  ("Publication credentials still live INSIDE the guest house until Phase 1"); this is the
  probe that flips to PASS the day the broker is armed and the factory user loses its push
  credential.
- **`boundary-host-writes  PASS`** and **`boundary-process-escape  PASS`** are real results:
  the guest house cannot write `/Library/LaunchAgents`, `/Library/LaunchDaemons`,
  `/usr/local/bin` or the bare repo it updates from, cannot `sudo`, and cannot signal
  another identity's processes. No persistence outside itself, no escalation.
- **`boundary-other-homes  FAIL: /Users/agent is listable`** — the one real finding. Not
  the factory's own files: `/Users/agent` is `drwxr-x---` group `staff`, so the guest house
  can enter and read another local account's home. Same class as the first finding, pointing
  at a different account, and outside this project's control — that account belongs to
  another agent. Remedy is identical (`chmod 700`), and the operator's call. Note what it
  does NOT reach: `boundary-keychain` SKIPs because that account's `~/Library` is 0700.

**Two more probe defects, both found by this run** (fixed):

- `boundary-dashboard-write` held a hardcoded `127.0.0.1:9788`. The deployment's board is
  9787 and this checkout's is 8787, so its clean `403` came from a board belonging to
  neither — a green row about something other than the thing under test, which is the
  wrong-account audit's failure mode wearing a port number. It now derives the URL from the
  deployment's own `config.yaml` and additionally probes the `viz --serve` board (a second
  server with its own write routes that no config field carries), reporting per board.
- `boundary-symlink-escape` could only anchor on a refused FILE, so on a correctly closed
  deployment it skipped itself with "nothing is refused to this identity in the first
  place" — said by an account that could not enter the operator's home at all. Everything
  under a 0700 home is *invisible* rather than refused, so the home itself is the anchor;
  the probe now accepts a directory (via `listdir`) and reports `/Users/factory stays
  refused through a symlink (PermissionError)`.

### Preparing the grader run — two more defects, and why the run had never happened

The grader half of this drill (`--boundary` as `factory-grader`) is listed as still owed.
Attempting to type it found two reasons it could not have been run, both consequences of
the *same* 0700 home the first finding installed:

- **The doctor was on the wrong side of the boundary.** Step 3 said to run
  `<factory>/scripts/guesthouse_check.py` as the grader — a file inside a home the grader
  cannot enter, by design. Until 2026-08-16 that step "worked" only because the home was
  group-readable, which was itself the finding drill 2 opened with; closing the hole broke
  the instruction that proves the hole is closed. `05-create-grader-user.sh` now installs a
  root-owned, world-readable copy at `/opt/factory/guesthouse_check.py`, beside the wrapper
  and with the same lifecycle, and the doctor takes `--factory-root` so the copy can be
  told which deployment to ask about (its own path would answer `/opt`). The probes need
  that root's PATH, never read access to it.

- **`refused` and `absent` were the same answer, and it was the wrong one.**
  `os.path.exists()` returns False both for "there is no such file" and for "you may not
  look". Five control-plane probes pre-checked it, so measured from a *correctly contained*
  identity they reported:

  ```
  boundary-blackboard    FAIL  …/store/blackboard.db does not exist (nothing proven)
  boundary-config        FAIL  …/config.yaml does not exist (nothing proven)
  boundary-secrets       FAIL  …/.factory-secrets/env does not exist (nothing proven)
  boundary-factory-root  SKIP  … not present
  boundary-killswitch    SKIP  STOP not present — engage it, then re-run this probe
  ```

  Every row is false, and the last one would have sent an operator to arm a brake that was
  already armed and merely invisible from that account. `--boundary`'s contract is *every
  rule must pass*, so the deliverable was unsatisfiable: **the tighter the containment, the
  redder the table.** That is the symlink-anchor defect one more time — a probe reporting
  its most negative verdict exactly where the boundary is strongest — and it is the third
  instance in this drill, which makes it the pattern to look for rather than a one-off.

  Fixed with a `_reachability()` classifier that separates the two via `os.stat` (ENOENT
  only when the parent could actually be read): refused is containment and PASSes, absent
  still proves nothing and never PASSes. `boundary-dependency-substitution` had a quieter
  version — unreachable directories dropped out of its tally, so a contained grader was
  told "N checked" by a probe that had silently skipped `FACTORY_ROOT` and its parent, the
  two entries the rule exists for; they are now counted and named as unreachable.

  Regression tests stand the fixture's own directory at `chmod 000` (which denies its
  owner too, so a non-root test can stand in for another identity) and assert all three
  readings: contained → PASS, tree-owner → FAIL, genuinely absent → nothing proven.

**Use `-i`.** Without it, the first of those commands dies before the script's first line:

```
shell-init: error retrieving current directory: getcwd: cannot access parent directories
```

That is not a broken deployment. It is `bash` starting up in the operator's cwd, which the
factory user can no longer traverse now that the home is 0700 — the first finding's fix,
seen from the other side. `sudo -i` starts a login shell in the target account's home, which
fixes both the working directory and `$HOME`. `update.sh` no longer depends on either (it
locates the tree from its own path and warns if `$HOME` belongs to another account), and
`02-bootstrap-as-factory.sh` now hard-refuses a foreign `$HOME` rather than writing the PAT
env file into the wrong account — but the shell still has to start.

## Turning it off

```yaml
super_worker:
  grader_user: ""      # back to running candidate code as the factory user
```
Takes effect at the next shift; nothing else changes. Isolation OFF is byte-identical to
the behavior before this phase — the same worktree, the same direct execution.

To remove the machinery entirely: `sudo bash deploy/user-factory/05-create-grader-user.sh
--uninstall` (drops the grant, the wrapper and the doctor copy; leaves the account for you
to delete).

## Failure modes and what they look like

| Symptom | Cause |
|---|---|
| Every candidate fails grading with `sudo: a password is required` | `grader_user` armed but `05-create-grader-user.sh` never ran. Deliberate: a missing grant must fail loudly, never fall back to running untrusted code as the factory user. |
| `refusing cwd outside the export root` (exit 126) | The wrapper's confinement working. Check `super_worker.export_root` matches the root the provisioning script created. |
| `cannot hand <dir> to the grading group 'factory-grade'` | The shared group is missing, or the factory user is not in it. Re-run `05-create-grader-user.sh` (it creates the group and adds both identities). Group membership changes need a fresh login session for the factory user's daemons to pick up. |
| `export handover failed (wrapper rc=126 …)` | The wrapper refused the materialize: the target is outside `super_worker.export_root`, is not empty, or does not exist. All three are refusals by design — the export directory is created by the factory and filled exactly once. |
| Grading fails with permission errors inside the export | The setgid bit or the group did not survive on the export directory (some filesystems drop setgid). The grader must be able to write its own working tree — pytest caches into it — and the factory must be able to read it back for the red-proof diff. |
| Grading fails with `no such file or directory: .git` inside the export | An isolated export is a `git archive` of one ref: no history, no branches, no `.git`. A target whose test command needs git cannot be graded isolated — that is the trade for not shipping untrusted code the factory's whole object store. |
| `--boundary` reports `nothing proven` | The path it probed does not exist. That is not containment, and is reported honestly rather than as a PASS. |
| The grader run dies with `can't open file '<factory>/scripts/guesthouse_check.py': Permission denied` | Containment working, not a fault: the guest house's home is 0700 and the grader is not the guest house. Run the root-installed copy — `/opt/factory/guesthouse_check.py --boundary --factory-root <factory>`. |
| The grader's `--boundary` table is full of `nothing proven` / `no factory directory` rows | The doctor derived its factory root from its own path — `/opt` — because `--factory-root` was omitted. The probes need the deployment's PATH; a table that proves nothing is not a pass. |
