# Guest-house runbook

Companion to `install.sh --guest-house` / `install.ps1` (the guided wizards),
`scripts/guesthouse_check.py` (the deterministic doctor), and
`docs/plans/2026-08-06-production-hardening-roadmap.md` Phase 0 (the design this runbook
documents — read it for the full phased plan; this file covers only the guest house as it
exists TODAY, Phase 0). `docs/runbooks/factory-user-deployment.md` is the deeper reference
for the macOS dedicated-user deployment the wizard orchestrates — this runbook cross-links it
rather than repeating it.

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
asks for confirmation. Pass `--guest-house --yes` (bash) / `-Yes` (PowerShell) for a
non-interactive run; both wizards abort loudly rather than hang if there is no terminal to
prompt from and `--yes`/`-Yes` was not given.

## What the wizard does, step by step

### macOS (`install.sh --guest-house`)

1. **Preflight.** Refuses to run anywhere but macOS (points to `install.ps1` on Windows),
   checks `git` + Xcode Command Line Tools + `sudo` are present, and warns on low disk space.
   Also refuses to run as root itself — it only `sudo`s ONE step (next), and always explains
   why immediately before that password prompt.
2. **Create the `factory` user** (`sudo bash deploy/user-factory/01-create-user.sh`) — a
   Standard (non-admin) macOS account. Skipped if it already exists.
3. **`claude login` as `factory`** — the wizard prints the fast-user-switch instructions
   (`docs/runbooks/factory-user-deployment.md` §3) and waits for confirmation that it's done;
   this cannot be automated (it's an interactive OAuth flow).
4. **Bootstrap** (`sudo -u factory -i bash /Users/Shared/factory-kit/02-bootstrap-as-factory.sh`)
   — clones the factory + target repos, installs dependencies, and prompts for a GitHub PAT
   scoped to the target repo only, all as `factory`.
5. **Daemons — optional, default No.** Always-on LaunchDaemons
   (`deploy/user-factory/03-install-daemons.sh`) are offered but declined by default: the
   supervised smoke shift (`factory-user-deployment.md` §4) should pass, watched, before
   anything runs unattended.
6. **The doctor** (`scripts/guesthouse_check.py`, run as `factory`) prints its table.
7. **Summary** — brakes state, the runbook's next steps, and the exact command to re-run the
   doctor later.

Every step detects "already done" (user exists, kit staged, bootstrap files present) and
skips with a message instead of re-doing it — safe to re-run the whole wizard at any point.

### Windows (`install.ps1`, EXPERIMENTAL)

1. **Preflight** — Windows 10 2004+/11 and `wsl.exe` present; if WSL isn't installed, prints
   the exact `wsl --install` command (needs a reboot — the script does not try to work around
   that) and stops.
2. **Create a DEDICATED WSL distro** (`factory-guesthouse` by default) — never an existing
   daily-driver distro; the script refuses to reuse one.
3. **Harden it** — writes `/etc/wsl.conf` inside the distro: `automount` off (no Windows
   drives visible), `interop` off + `appendWindowsPath` off (no Windows `.exe` launches, no
   host `PATH` leaking in), `systemd` on (some factory tooling needs it). Terminates the
   distro so the change takes effect.
4. **Runs `install.sh --guest-house --wsl` inside the distro**, as root (root always exists
   in a fresh distro — this sidesteps a fresh Ubuntu image's interactive first-run prompt).
   That inner script then creates ITS OWN dedicated, non-admin `factory` Linux user and
   installs under that account with brakes on (mode stays `shift`) — the actual factory
   process never runs as root.
5. **Prints the same closing guidance** (doctor command, supervised-smoke pointer, and the
   EXPERIMENTAL caveat restated).

`install.sh --guest-house --wsl` is also what `install.ps1` calls — it is a real, usable
mode on its own if you're already inside a hardened WSL distro and prefer to invoke bash
directly.

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
| 3 | `sudo`/gain admin rights | Standard user, no admin group membership |
| 4 | See Windows drives or launch Windows programs (WSL route) | `/etc/wsl.conf` hardening (`automount`/`interop` off) |

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
output). It never mutates anything — every rule only reads. Exit code is `0` unless at least
one rule FAILs; a SKIP means the rule's precondition doesn't apply here (e.g. not a deployed
guest-house layout) and is not itself a problem.

| rule id | what it checks | FAIL means | fix |
|---|---|---|---|
| `standard-user` | not root, not in the macOS `admin` / Linux `sudo`\|`wheel` group | this account has admin rights | recreate the deployment as a genuinely Standard/non-sudo user |
| `no-sudo-grant` | `sudo -n -v` fails (no cached/passwordless grant) | this account can currently `sudo` without a password prompt | remove any `NOPASSWD`/cached sudo grant for this account |
| `home-dir-perms` | home directory mode is 700-ish (no group/other bits) | group or other can read/write/enter the home dir | `chmod 700 ~` |
| `no-ssh-access` | no private key material under `~/.ssh`, `SSH_AUTH_SOCK` unset | an SSH agent socket is reachable, or a private key file was found | unset `SSH_AUTH_SOCK` in this account's shell profile; remove any private keys from `~/.ssh` |
| `no-docker-socket` | `/var/run/docker.sock` absent, or present but not accessible to this user | this account can read/write the Docker socket (host-level container escape risk) | remove this account from the `docker` group, or don't run Docker on the guest-house machine |
| `runtime-read-only` | the operator-owned bare repo (`/Users/Shared/factory.git`) is NOT owned by the current user | the guest-house user owns its own "read-only" runtime source — the bare-repo split isn't in effect | re-run `deploy/user-factory/01-create-user.sh` as the operator (it sets the ownership) |
| `credentials-hygiene` | the secrets env file (`~/.factory-secrets/env`) is mode 600 and owned by the current user | wrong permissions/ownership on the file holding the PAT | `chmod 600 ~/.factory-secrets/env`; verify ownership |
| `brakes-engaged` | `STOP` file present in the factory dir AND mode is not `auto` | the deployment could run unattended right now | `touch STOP` and `bin/factory mode shift` (see `factory-user-deployment.md` §6, "the brake trap") |
| `dashboard-localhost` | `config.yaml`'s `dashboard.host` is `127.0.0.1`/`localhost` | the board is bound to a non-localhost address (reachable off-box) | set `dashboard.host: "127.0.0.1"` in `config.yaml` |
| `wsl-hardening` | (WSL only) `/etc/wsl.conf` has `automount`/`interop` both disabled | the distro can still see Windows drives or launch Windows programs | re-run `install.ps1`'s hardening step, or write `/etc/wsl.conf` by hand (see "What the wizard does" above) |

## Teardown

Full removal of the macOS deployment: `docs/runbooks/factory-user-deployment.md` §8
(uninstall daemons, revoke the PAT, delete the `factory` user, remove the bare repo/kit/seed
dirs). For the WSL route, additionally: `wsl --unregister factory-guesthouse` removes the
entire distro (irreversible — everything inside it is gone, which is exactly the "assume it
gets trashed" design point).

## EXPERIMENTAL status (Windows)

`install.ps1` is **syntax-reviewed only** — it has not been drill-tested on real Windows
hardware (roadmap Part 2, binding principle 4: "the label is removed by evidence, not by
time"). Treat every step as something to verify by hand: a fresh Ubuntu WSL distro's
one-time interactive setup prompt in particular is a known rough edge the script works
around defensively (by running as root) but cannot fully rule out. The macOS path
(`install.sh --guest-house`) is the tested, mature route; prefer it when either platform is
an option.
