# Guest-house runbook

Companion to `install.sh --guest-house` / `install.ps1` (the guided wizards),
`scripts/guesthouse_check.py` (the deterministic doctor), and
`docs/plans/2026-08-06-production-hardening-roadmap.md` Phase 0 (the design this runbook
documents — read it for the full phased plan; this file covers only the guest house as it
exists TODAY, Phase 0, including the same-day adversarial-review fix round that hardened it).
`docs/runbooks/factory-user-deployment.md` is the deeper reference for the macOS
dedicated-user deployment the wizard orchestrates — this runbook cross-links it rather than
repeating it.

## What the guest house is

The reviewer's sign-off condition, verbatim (roadmap, provenance section):

> Assume the guest house will eventually be trashed. Design things so it cannot enter the
> main house, forge the operator's approval, or publish anything other than the exact
> artifact that was approved.

Concretely: the factory (conductor + workers + dashboards) runs as a **dedicated,
non-admin user** — `factory` on macOS, a distro-local `factory` user inside an isolated WSL2
distro on Windows — separate from the operator's own account, files, keychain/credential
store, and SSH keys. The OS enforces this boundary (a Standard-user account, a hardened WSL
distro), not a config flag. If everything running inside the guest house were fully
compromised — a malicious dependency, a prompt-injected task, a supply-chain surprise — the
operator's own machine, credentials, and other repositories stay out of reach.

**This is Phase 0 of the roadmap: it bounds *blast radius*, not *authority*.** Binding
principle 1 (roadmap Part 2): "Perimeter ≠ authority. The dedicated user bounds blast
radius; it does not enforce the internal authority model." Read "What the boundary does NOT
give (yet)" below before trusting this for anything unattended.

## The one-liners

**macOS:**

```bash
curl -fsSL https://raw.githubusercontent.com/ikangai/factory/main/install.sh | \
  bash -s -- --guest-house
```

**Windows (EXPERIMENTAL — see the status section below):**

```powershell
irm https://raw.githubusercontent.com/ikangai/factory/main/install.ps1 | iex
```

Both are interactive by default — each step explains what it's about to do and why, then
asks for confirmation.

**The honest `--yes` / `-Yes` contract.** `--yes` (bash) / `-Yes` (PowerShell) auto-answers
only the wizard's OWN confirmation prompts. It is **not** a path to a fully unattended macOS
install: creating the `factory` account needs an administrator (`sudo`) password, the new
account needs its own login password, and the bootstrap step needs a GitHub token pasted in
— three genuinely interactive secrets, always, on the macOS (non-`--wsl`) path. `install.sh
--guest-house` therefore ALWAYS requires a real terminal, `--yes` or not, and refuses to even
start otherwise (it never silently proceeds trying to read a secret from a piped stdin — see
"Why a real terminal is non-negotiable" below). The `--wsl` variant has no such secrets in
its own flow (user creation there is a plain `useradd`), so there `--yes`/`-Yes` genuinely
can make the whole run non-interactive.

**Why a real terminal is non-negotiable (the secrets-eat-the-pipe fix).** Under
`curl | bash`, stdin IS the piped script text. Before the fix round, the kit scripts'
`read -rs` prompts (the new account's password in `01-create-user.sh`, the GitHub token in
`02-bootstrap-as-factory.sh`) silently read from that same stdin — meaning the new macOS
account's password could become a line of the *installer script itself*, or the whole wizard
would die mid-flow under `set -e`. The wizard now explicitly relays the real controlling
terminal (`< /dev/tty`) into every kit-script invocation that reads a secret, and refuses to
start the macOS path at all when no real terminal is available (`--yes` cannot skip this
check) rather than risk it.

## What the wizard does, step by step

### macOS (`install.sh --guest-house`)

1. **Preflight.** Refuses to run anywhere but macOS (points to `install.ps1` on Windows),
   checks `git` + Xcode Command Line Tools + `sudo` are present, and warns on low disk space.
   Also refuses to run as root itself — it only `sudo`s individual steps, and always explains
   why immediately before each password prompt.
2. **Create/refresh the `factory` user** (`sudo bash deploy/user-factory/01-create-user.sh
   < /dev/tty`) — a Standard (non-admin) macOS account. This step now runs even when
   `factory` already exists (01-create-user.sh is internally idempotent — it re-syncs
   current code into the shared bare repo and re-stages the deploy kit either way), closing
   a dead-end: previously, if 01 had ever failed partway through on a prior run, the wizard
   would see `factory` already existed and skip re-running it forever. Afterward the wizard
   (a) verifies `factory` is NOT an admin account (fails closed if that can't be determined)
   — this matters both for a freshly created account and one adopted from a prior/foreign
   setup — and (b) tightens `/Users/factory` to mode `700`.
3. **`claude login` as `factory`** — the wizard prints the fast-user-switch instructions
   (`docs/runbooks/factory-user-deployment.md` §3) and waits for confirmation that it's done;
   this cannot be automated (it's an interactive OAuth flow).
4. **Bootstrap** (`sudo -u factory -i bash /Users/Shared/factory-kit/02-bootstrap-as-factory.sh
   < /dev/tty`) — clones the factory + target repos, installs dependencies, and prompts for a
   GitHub PAT scoped to the target repo only, all as `factory`. **On success, the wizard
   itself drops `STOP`** in the new deployment
   (`sudo -u factory -i bash -lc 'touch $HOME/fab/factory/STOP'`) — this is a fix: the prior
   version's closing summary CLAIMED `02-bootstrap-as-factory.sh` did this, which was false
   (it only warns if `STOP` happens to already be present); nothing ever actually created it.
5. **Daemons — optional, default No.** Always-on LaunchDaemons
   (`deploy/user-factory/03-install-daemons.sh`) are offered but declined by default: the
   supervised smoke shift (`factory-user-deployment.md` §4) should pass, watched, before
   anything runs unattended.
6. **The doctor** (`scripts/guesthouse_check.py`, run as `factory`) prints its table.
7. **Summary** — an HONEST brakes-state line (conditioned on whether bootstrap actually
   succeeded THIS run, not an unconditional claim), the runbook's next steps as full GitHub
   URLs (a `curl | bash` user has no local checkout to resolve a relative path against), and
   the exact command to re-run the doctor later.

Every step detects "already done" (kit staged, bootstrap files present) and skips with a
message instead of re-doing it — safe to re-run the whole wizard at any point. An abort
(error or Ctrl-C) prints a state summary (what exists, what's staged) and the exact resume
command, via an EXIT/INT/TERM trap.

### Windows (`install.ps1`, EXPERIMENTAL)

1. **Preflight** — a REAL `wsl --status` probe (not just "does `wsl.exe` exist on disk",
   which passes even with the WSL feature fully disabled) confirms WSL2 is actually enabled;
   a `wsl --version` probe additionally rules out an older "inbox" WSL with no
   distro-management support (no `--name`, etc — attempting the modern flow against it would
   misattribute every failure to `--name` specifically). Either probe failing prints the
   exact `wsl --install` (needs a reboot) or manual-import guidance and stops — this script
   never tries to work around an unsupported WSL release.
2. **Create a DEDICATED WSL distro** (`factory-guesthouse` by default). After creation, the
   distro is re-queried to confirm it actually registered under that name (an older `wsl.exe`
   can silently ignore `--name` while still exiting 0), and a marker file
   (`/etc/factory-guesthouse.marker`) is written inside it. **Reusing an existing distro that
   lacks this marker is REFUSED outright** — without the marker, this could be your
   daily-driver distro, and hardening it (next step) would silently cut off its Windows-drive
   access and `.exe` interop.
3. **Install base dependencies** (`curl`, `git`, `python3-pip`, `python3-venv` via `apt-get`,
   as root) — a fresh Ubuntu image has none of these, and `install.sh` needs all of them.
4. **Harden it** — writes `/etc/wsl.conf` inside the distro: `automount` off (no Windows
   drives visible), `interop` off + `appendWindowsPath` off (no Windows `.exe` launches, no
   host `PATH` leaking in), `systemd` on (some factory tooling needs it). Terminates the
   distro, then **reads back `/etc/wsl.conf`** to confirm the write actually landed AND
   confirms `/mnt/c` is genuinely unreachable inside the distro (`ls /mnt/c` must fail) —
   writing the file is not itself proof the hardening took effect.
5. **Runs `install.sh --guest-house --wsl` inside the distro**, as root (root always exists
   in a fresh distro — this sidesteps a fresh Ubuntu image's interactive first-run prompt).
   That inner script then creates ITS OWN dedicated, non-admin `factory` Linux user and
   installs under that account with brakes on (mode stays `shift`) — the actual factory
   process never runs as root. This step distinguishes "the operator declined" (exit 0, no
   error) from "it ran and failed" (exit 1) — the two used to be indistinguishable.
6. **Prints the same closing guidance** (doctor command, supervised-smoke pointer, the
   EXPERIMENTAL caveat, and the ten specific things that remain unverifiable without real
   Windows hardware — see the status section below).

A bare `irm ... | iex` one-liner cannot forward `-Yes`/`-DistroName` (there is nowhere to put
them). To pass parameters, use the parameterized invocation form instead — the script's
closing summary prints this exact syntax:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/ikangai/factory/main/install.ps1))) -Yes -DistroName my-distro
```

`install.sh --guest-house --wsl` is also what `install.ps1` calls — it is a real, usable
mode on its own if you're already inside a hardened WSL distro and prefer to invoke bash
directly. It forwards `--target`/`--target-dir`/`--name`/`--root`/`--port`/`--provider`/
`--base-branch`/`--skip-deps` through to the actual per-instance install (a fix: these used
to be silently dropped, always installing the default `clive` target regardless of what was
asked for) and, absent an explicit `--name`/`--root`, installs to the unified path
`$HOME/factories/guest-house/factory` — the ONE path every doctor command above assumes.

## The rules table — what the boundary gives, and what it does NOT give (yet)

| # | The guest-house user CAN | Why that's fine |
|---|---|---|
| 1 | Run arbitrary Bash as itself (worker execution) | that's the guest house's OWN sandbox, not the operator's — the whole point |
| 2 | Read/write anywhere under its own home directory | its home IS the deployment tree |
| 3 | Push to the one target repo the deployment is scoped to | via a PAT scoped to that repo only (`factory-user-deployment.md` §2) |
| 4 | Use the operator's shared Claude subscription | a resource-pool risk, not an authority breach (`factory-user-deployment.md` §9, risk #2) |

| # | The guest-house user CANNOT (OS/PAT-scope enforced) | Enforced by |
|---|---|---|
| 1 | Read the operator's home directory, keychain, SSH keys, other credentials | macOS Standard-user permissions / WSL distro isolation |
| 2 | Push to any repo other than the one the PAT is scoped to | GitHub fine-grained PAT scope |
| 3 | `sudo`/gain admin rights | Standard user, no admin group membership (verified on account creation/adoption — see step 2 above) |
| 4 | See Windows drives or launch Windows programs (WSL route) | `/etc/wsl.conf` hardening (`automount`/`interop` off), read back and verified, not just written |
| 5 | Silently take over an operator's existing WSL distro | the ownership-marker check refuses any distro this wizard didn't create itself |

**What Phase 0 does NOT yet give — say so honestly (roadmap Part 2, principle 1 and Part 3):**

- **Interior, same-user authority stays soft until Phase 3.** Inside the guest-house user,
  every worker still shares ONE account: the same Bash environment, the same blackboard DB
  file, the same process tree. A worker cannot be confined FURTHER than "is the OS-level
  `factory`/distro user" — there is no per-worker sandbox yet. That's Phase 3's job (job-bundle
  isolation: a disposable sandbox per worker, no shared DB/tree/credential access).
- **Publication credentials still live INSIDE the guest house until Phase 1.** The GitHub
  PAT that lets the deployment push sits in the guest-house user's own `~/.factory-secrets/env`
  — a full compromise of the guest house today can still push to the one scoped repo it holds
  a token for. Phase 1 moves that credential OUTSIDE (an operator-side broker executes
  publication after re-verifying an approval envelope) so that even total guest-house
  compromise cannot silently publish.
- **The guest house bounds blast radius; it does not by itself prove the factory's internal
  decisions (merges, approvals) are safe.** Those guarantees come from the rest of the
  architecture (the code gate, the approval/promotion gate, the human queue) — the guest
  house is what keeps an adversarial OUTCOME from reaching your own account, not a
  replacement for those internal checks.

See `docs/plans/2026-08-06-production-hardening-roadmap.md` Part 3 for the phases that close
each of these gaps, and Part 5 for the six adversarial drills a guest-house install must pass
before it's called unattended-production-ready.

## The doctor — every rule, what FAIL means, how to fix

Run any time: `python3 scripts/guesthouse_check.py` (add `--json` for machine-readable
output). It never mutates anything — every rule only reads.

**The account it audits is the account it says it audits — since 2026-08-16.** `sudo -u
factory` rewrites `USER`/`LOGNAME` but not `$HOME`, and the doctor used to take its home
from `$HOME`: it reported `standard-user PASS 'factory'` in the same table as
`home-dir-perms PASS /Users/martintreiber`, and `credentials-hygiene SKIP … not present` for
a PAT file that exists. It now resolves the audited account's home from the passwd database
and prints a banner when `$HOME` disagrees (`--json` carries `auditing`, `home` and
`home_env_mismatch`). The ssh-agent check had the same leak via `SSH_AUTH_SOCK` and now
SKIPs, with the reason, rather than answering for another account's agent. Found by drill 2
— see `docs/runbooks/worker-isolation.md` §Drill 2, "the doctor audited the wrong account".

**Run it on the deployed account, not just on your own checkout.** Drill 2 found a real
perimeter hole on a deployment nobody had ever pointed the doctor at (see
`docs/runbooks/worker-isolation.md` §Drill 2, "Executed 2026-08-16" — the guest house's home
was `0750` group `staff`, so any local account could read its blackboard, config and
corpus). The eight account-scoped rules SKIP on an operator checkout by design, so a green
run there says nothing whatever about the guest house:
`sudo -u factory python3 <factory>/scripts/guesthouse_check.py`.

**The context gate.** Seven of the ten rules below are *account-scoped* — they ask "is THIS
ACCOUNT a safely isolated guest-house user". Run on an ordinary operator/developer account
(not literally named `factory`, and neither `~/fab/factory` nor the unified WSL install root
present), that is simply the wrong question — of course the operator has admin rights, SSH
keys, and Docker access; that's not a guest-house violation, it's a different account being
asked about the wrong thing. The doctor detects this (`is_guest_house_context`) and, when the
account being audited is NOT a deployed guest-house layout, prints an explicit banner, SKIPs
all seven account-scoped rules (no subprocess calls made for them at all), and **the exit
code is always `0`** in that context — auditing the wrong account is not a failure of
anything. Run on the actual deployed account, every rule runs for real and strict semantics
apply: exit `1` iff any rule FAILs. (Before this fix, running the doctor on an ordinary
operator checkout produced five-to-seven false FAILs — a real, demonstrated bug.)

| rule id | scope | what it checks | FAIL means | fix |
|---|---|---|---|---|
| `standard-user` | account | not root, not in the macOS `admin` / Linux `sudo`\|`wheel` group — **fails CLOSED**: an unresolvable/ambiguous check (e.g. `dseditgroup` erroring) is a FAIL, never a silent PASS | this account has admin rights, or membership could not be determined | recreate the deployment as a genuinely Standard/non-sudo user |
| `no-passwordless-sudo` | account | `sudo -n -v` fails (no cached/passwordless grant *right now*) — renamed from `no-sudo-grant`: it only ever detects a passwordless/cached credential, never full admin-group membership (that's `standard-user`'s job) | this account can currently `sudo` without a password prompt | remove any `NOPASSWD`/cached sudo grant for this account. Note: probing this for real can itself EXTEND an already-cached sudo timestamp — an inherent property of `sudo -v`, not a doctor bug |
| `home-dir-perms` | account | home directory mode is 700-ish (no group/other bits) | group or other can read/write/enter the home dir | `chmod 700 ~` (the wizard does this automatically after creating/adopting the account) |
| `no-ssh-access` | account | no *loaded* ssh-agent identities (`ssh-add -l`, not merely `SSH_AUTH_SOCK` being set — macOS's launchd sets that in every session regardless of whether any key is loaded) and no private-key material under `~/.ssh`, identified POSITIVELY (an `id_*` filename or a PEM/OpenSSH private-key header line — not a deny-list of "everything that isn't a known-benign filename") | an agent has identities loaded, or a private key file was found | unload the agent identity (`ssh-add -D`); remove any private keys from `~/.ssh` |
| `no-docker-socket` | account | `/var/run/docker.sock` absent, or present but not accessible to this user | this account can read/write the Docker socket (host-level container escape risk) | remove this account from the `docker` group, or don't run Docker on the guest-house machine |
| `runtime-read-only` | account | the operator-owned bare repo (`/Users/Shared/factory.git`) is neither owned by nor WRITABLE by the current user (`os.access(..., W_OK)` — ownership alone doesn't prove permission bits actually deny writes) | this account owns and/or can write to its own "read-only" runtime source — the bare-repo split isn't in effect | re-run `deploy/user-factory/01-create-user.sh` as the operator (it sets the ownership) |
| `credentials-hygiene` | account | the secrets env file (`~/.factory-secrets/env`) is mode 600 and owned by the current user | wrong permissions/ownership on the file holding the PAT | `chmod 600 ~/.factory-secrets/env`; verify ownership |
| `deployment-not-peer-readable` | account | what a PEER local account can actually READ of this deployment: if the home grants group/other traverse, it checks whether the blackboard, `config.yaml`, `corpus/` or `state/` are group/other-readable *through* traversable ancestors | another local account can read your decision history, your config and your held-out corpus. Found on the operator's own deployment by drill 2 (2026-08-16): a `0750` home group `staff` — which every macOS account is in — over a `0755` tree of `0644` files | `chmod 700 ~` (fix the cause; it makes the modes inside irrelevant). This rule is the consequence-level companion to `home-dir-perms`: that one says the mode is wrong, this one says what it costs |
| `brakes-engaged` | checkout | `STOP` file present in the factory dir AND mode is not `auto` | the deployment could run unattended right now | `touch STOP` and `bin/factory mode shift` (see `factory-user-deployment.md` §6, "the brake trap") |
| `dashboard-localhost` | checkout | `config.yaml`'s `dashboard.host` is `127.0.0.1`/`localhost` | the board is bound to a non-localhost address (reachable off-box) | set `dashboard.host: "127.0.0.1"` in `config.yaml` |
| `wsl-hardening` | platform (WSL only) | `/etc/wsl.conf` has `automount`/`interop` both disabled — detected via `/proc/version` containing "microsoft", NOT the `WSL_DISTRO_NAME`/`WSL_INTEROP` environment variables (those are stripped by `sudo` without `-E`, and `WSL_INTEROP` specifically disappears once interop hardening actually takes effect — exactly the scenario this rule most needs to detect correctly) | the distro can still see Windows drives or launch Windows programs | re-run `install.ps1`'s hardening step, or write `/etc/wsl.conf` by hand (see "What the wizard does" above) |

"checkout" and "platform" scoped rules are NOT context-gated — they still run (and their own
existing SKIP-if-not-applicable logic already handles "not a deployed checkout" honestly) and
still count toward the exit code, but ONLY in deployed context; outside it the overall exit
code is forced to `0` regardless, per the context-gate rule above.

## Teardown

Full removal of the macOS deployment: `docs/runbooks/factory-user-deployment.md` §8
(uninstall daemons, revoke the PAT, delete the `factory` user, remove the bare repo/kit/seed
dirs). For the WSL route, additionally: `wsl --unregister factory-guesthouse` removes the
entire distro (irreversible — everything inside it is gone, which is exactly the "assume it
gets trashed" design point). If a prior run's distro doesn't carry the ownership marker (see
"What the wizard does" above) and you're certain it's disposable, unregister it by hand before
re-running the wizard under the same `-DistroName` — the wizard will otherwise refuse to
touch it.

## EXPERIMENTAL status (Windows)

`install.ps1` is **syntax-reviewed only** — it has not been drill-tested on real Windows
hardware (roadmap Part 2, binding principle 4: "the label is removed by evidence, not by
time"). The macOS path (`install.sh --guest-house`) is the tested, mature route; prefer it
when either platform is an option.

Specifically **unverifiable without real Windows hardware** — the script's own closing
summary prints this list, treat every item as something to verify by hand:

1. `wsl --install --name` acceptance and its exact exit codes across different WSL versions
2. `wsl --list` stderr/encoding behavior on a given exact PowerShell/Windows build
3. `$MyInvocation`/`$PSCommandPath` behavior under a bare `iex` vs. the parameterized
   `[scriptblock]::Create(...)` invocation form
4. native-argument quoting/escaping of characters like `|` and `>` passed through `wsl.exe`
5. `/dev/tty` availability from inside a command `wsl.exe` launches non-interactively
6. whether `-u root` actually bypasses a fresh Ubuntu image's one-time OOBE (first-run setup)
   prompt in every case
7. `systemd`/`interop` settings being honored identically across WSL kernel versions
8. the Ubuntu WSL app package's image manifest (whether `-d Ubuntu` resolves consistently)
9. `[Environment]::UserInteractive` in unusual hosts (Task Scheduler, remote sessions)
10. registry `CurrentBuild` reads on Insider/ARM64/LTSC Windows builds

A fresh Ubuntu WSL distro's one-time interactive setup prompt in particular is a known rough
edge the script works around defensively (by running as root throughout) but cannot fully
rule out (item 6 above).
