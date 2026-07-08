# Factory-user deployment runbook

Deploys the whole clive-harness-factory (conductor + super-workers + dashboards) under a
dedicated macOS Standard user, `factory`, always-on via LaunchDaemons — isolated from the
operator's own account, credentials, and MCP servers. The factory repo itself stays local;
code moves operator → deployment through a shared bare repo, never a network hop.

Companion files, all under `deploy/user-factory/`:

| file | runs as | purpose |
|---|---|---|
| `01-create-user.sh` | operator, `sudo` | creates the `factory` user + shared bare repo |
| `02-bootstrap-as-factory.sh` | `factory` | clones, deps, config overlay, DB seed |
| `apply-config-overlay.py` | `factory` (via 02 / update.sh) | patches 4 config.yaml knobs |
| `with-env.sh` | daemon | sources secrets, execs `bin/factory` |
| `com.factory.board.plist` | daemon | operator's board, port 9787 |
| `com.factory.fleet.plist` | daemon | fleet/mission-control + autopilot supervisor, port 9788 |
| `com.factory.backup.plist` | daemon | 3h blackboard backup |
| `03-install-daemons.sh` | operator, `sudo` | installs/reloads/uninstalls the 3 daemons |
| `update.sh` | `factory` | pulls new code, keeps the config overlay |

---

## 0. Overview + topology

```
 OPERATOR (you, /Users/martintreiber)              FACTORY USER (/Users/factory)
 ┌─────────────────────────────┐                    ┌───────────────────────────────┐
 │ this repo (main)             │  git push deploy   │ fab/factory  (deploy branch)   │
 │ /Users/martintreiber/.../    │ ───────────────▶   │  = origin/main + config overlay│
 │ factory                      │   (bare repo)       │  (autopilot.prod=false,        │
 └─────────────────────────────┘                    │   super_worker.user="",        │
                    │                                 │   claude_bin="claude",         │
                    ▼                                 │   dashboard.port=9787)         │
        /Users/Shared/factory.git                     │                                │
          (bare, staff-group readable)                │  fab/clive   (sibling repo,    │
                                                        │   target.root: "../clive")     │
                                                        └───────────────┬────────────────┘
                                                                        │ HTTPS + GH_TOKEN
                                                                        ▼
                                                          GitHub: ikangai/clive
                                                       (chore/extract-factory base branch,
                                                        factory/auto merge branch, issues)

 Dashboards (operator's browser, localhost only):
   http://127.0.0.1:9787   board        (com.factory.board)
   http://127.0.0.1:9788   fleet/mission-control + autopilot supervisor (com.factory.fleet)

 Three LaunchDaemons (UserName=factory, always-on, root-installed under /Library/LaunchDaemons):
   com.factory.board    — the board server
   com.factory.fleet    — the fleet viz server; ITS poll loop IS the autopilot supervisor
                           that respawns the run-loop runner per mode/STOP
   com.factory.backup   — sqlite `.backup` snapshot of the blackboard every 3h
```

Auth model:
- **GitHub**: a fine-grained PAT, `GH_TOKEN`, scoped to `ikangai/clive` only (§2).
- **Claude**: the factory user's own subscription login (`claude login`) plus a long-lived
  `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) for headless/daemon use (§3).
- **Secrets** live in `/Users/factory/.factory-secrets/env`, mode 600, sourced by
  `with-env.sh` — never written into a `/Library/LaunchDaemons` plist, which is
  world-readable.

---

## 1. Provision the user + shared repo (operator, once)

```bash
cd /Users/martintreiber/Documents/Development/factory     # this repo
sudo bash deploy/user-factory/01-create-user.sh
```

This creates the `factory` Standard user (prompts for a password), inits
`/Users/Shared/factory.git` (bare, staff-group readable), pushes this repo's `main` into it,
and stages the kit at `/Users/Shared/factory-kit`. Optionally, to carry over existing
learnings/history:

```bash
bash scripts/backup_blackboard.sh
cp "$(ls -t ~/factory-db-backups/blackboard-*.db | head -1)" /Users/Shared/factory-seed/blackboard.db
```

---

## 2. Mint the GitHub PAT

Fine-grained PAT, minted by whoever owns/administers the `ikangai` org:

- **Resource owner**: `ikangai`
- **Repository access**: only `ikangai/clive` — not "all repositories"
- **Permissions**: Contents (Read and write), Issues (Read and write), Metadata (Read-only,
  mandatory)
- **Expiration**: 90 days. Put a reminder on the calendar — an expired token fails every gh
  call the deployment makes; there is no automatic renewal.

**Org caveat**: fine-grained PATs must be explicitly enabled for the org (Organization
settings → Personal access tokens). If `ikangai` hasn't enabled them, the mint UI won't let
you scope to a single repo — fall back to a **classic** PAT with the `repo` scope. This is
strictly broader (every repo the token owner can see or write), so treat it as a heightened
residual risk (see §9) and prefer fixing the org setting over living with a classic token
long-term.

Paste the token when `02-bootstrap-as-factory.sh` prompts for it. **Rotation**: edit
`GH_TOKEN=` in `/Users/factory/.factory-secrets/env` on the deployment machine, then as
`factory`: `gh auth setup-git` again to re-pick it up.

---

## 3. Bootstrap + manual logins

As the operator (still `sudo`-capable, but this step itself runs as `factory`):

```bash
sudo -u factory -i bash /Users/Shared/factory-kit/02-bootstrap-as-factory.sh /Users/Shared/factory-seed/blackboard.db
```

This clones `fab/factory` (from the bare repo) and `fab/clive` (from GitHub), checks out the
`deploy` branch with the config overlay applied, installs Python deps, sets git identity,
installs the `claude` CLI if absent, seeds the blackboard if a snapshot was provided, and
smoke-checks `factory status` / `gh auth status`.

Three things `02` **cannot** do headlessly and must be done via **fast user switching** into
the `factory` account (⌘ from the login window, or Apple menu → Fast User Switching once the
`factory` user is visible in the menu):

1. **`claude login`** — interactive OAuth in a browser session as `factory`. Verify:
   ```bash
   claude -p "say ok"
   ```
2. **No-session auth check** — usually NO token is needed: on this setup `claude login`'s
   credential proved reachable outside the login session (verified live 2026-07-08). Confirm
   on yours: log the `factory` user fully OUT, then from the operator's terminal:
   ```bash
   sudo -u factory -H bash -c 'cd ~ && ~/.local/bin/claude -p "say ok"'
   ```
   Answers → skip the token, the daemons authenticate with the plain login. FALLBACK if it
   fails only without a session: `claude setup-token` (still your subscription — it just
   bypasses the keychain), pasted into `/Users/factory/.factory-secrets/env`, uncommenting:
   ```
   export CLAUDE_CODE_OAUTH_TOKEN=<paste>
   ```
3. **agora plugin install** (the factory's inter-agent bus):
   ```bash
   claude plugin marketplace add <the operator's agora marketplace source>
   ```
   If the marketplace repo is unreachable from the `factory` account, fall back to copying
   the operator's own plugin cache via `/Users/Shared` (e.g. `~/.claude/plugins/`) rather than
   granting `factory` broader network/credential reach.

### Headless-auth proof — critical go/no-go gate

Everything above must ALSO work with **no keychain and no GUI session**, because that's how
the LaunchDaemons run. Prove it, still logged in as `factory`, before moving on:

```bash
env -i HOME=/Users/factory PATH="$HOME/.local/bin:/usr/bin:/bin" \
  bash -lc '. "$HOME/.factory-secrets/env" && claude -p "say ok"'

env -i HOME=/Users/factory PATH="/usr/bin:/bin" \
  bash -lc '. "$HOME/.factory-secrets/env" && gh api user -q .login'
```

Both must succeed with **no** keychain prompt and **no** browser popup. If either one hangs
or asks for keychain access, headless daemon operation will silently stall — fix auth before
enabling the daemons (§5). Do not proceed past this gate.

---

## 4. Supervised smoke shift

Still as `factory`, watched, NOT yet via daemons:

```bash
cd "$HOME/fab/factory"
./bin/factory mode shift
./bin/factory run --real
```

Verify, in order:
- the merge lands on **the deployment's own** `fab/clive` `factory/auto` (not the operator's
  clive checkout — they are separate clones)
- graduation / publication-lag lines print (the shift-end summary compares
  `origin/<release>..factory/auto`)
- `gh` comments land on the target repo's issues, or fail open cleanly (never a hard crash)
- `./bin/factory status` and the board (once running, §5) show the new task/shift rows

Only proceed to always-on once one full shift completes clean.

---

## 5. Enable always-on

```bash
cd /Users/martintreiber/Documents/Development/factory
sudo bash deploy/user-factory/03-install-daemons.sh
```

Verify:

```bash
curl -sS http://127.0.0.1:9787/ | head -1                     # board
curl -sS http://127.0.0.1:9788/ | head -1                     # fleet
ls -la /Users/factory/fab/factory/logs/daemon-*.log
sudo pmset -c sleep 0                                          # never idle-sleep on AC power
pmset -g assertions | grep -i caffeinate                       # caffeinate is holding sleep off
```

---

## 6. Steering

Watch both dashboards from the operator's own browser via `localhost` (they bind
`127.0.0.1`, not reachable off-box):

- `http://127.0.0.1:9787` — board (tasks, shifts, EVM)
- `http://127.0.0.1:9788` — fleet / mission control

**The brake trap**: with `mode=auto` and the fleet daemon alive, clearing `STOP` alone
respawns a runner within roughly the fleet daemon's poll interval (~5 min) — `KeepAlive` and
the autopilot supervisor both actively want a runner going. A **durable** brake needs BOTH:

```bash
sudo -u factory -i bash -lc 'touch $HOME/fab/factory/STOP && $HOME/fab/factory/bin/factory mode shift'
```

**Hard stop** (daemons themselves, not just the run-loop):

```bash
sudo launchctl bootout system/com.factory.board
sudo launchctl bootout system/com.factory.fleet
sudo pkill -u factory -f "run --loop"
```

Interactive CLI access into the deployment at any time:

```bash
sudo -u factory -i
```

---

## 7. Update flow

Operator side, from this repo:

```bash
git push deploy main
```

Factory side:

```bash
sudo -u factory -i bash -lc '$HOME/fab/factory/deploy/user-factory/update.sh'
```

`update.sh` merges `origin/main` into `deploy`; on a `config.yaml` conflict it auto-resolves
by taking upstream wholesale and reapplying just the 4 overlay knobs
(`apply-config-overlay.py`), then exits 1 so any OTHER conflicted file still gets a human
look. On success it prints the brake-then-kickstart sequence:

```bash
sudo -u factory -i bash -lc 'touch $HOME/fab/factory/STOP && $HOME/fab/factory/bin/factory mode shift'
sudo launchctl kickstart -k system/com.factory.board system/com.factory.fleet
```

---

## 8. Teardown / rollback

**Full teardown:**

```bash
sudo bash deploy/user-factory/03-install-daemons.sh --uninstall
```

Then, as needed:
- Revoke the PAT on GitHub (Settings → Developer settings → Fine-grained tokens).
- `sudo -u factory -i bash -lc 'claude logout'`
- `sudo sysadminctl -deleteUser factory` (add `-keepHome` to preserve `/Users/factory` for
  forensics/salvage instead of wiping it).
- `sudo rm -rf /Users/Shared/factory.git /Users/Shared/factory-seed /Users/Shared/factory-kit`
- `git remote remove deploy` (operator repo).

**Partial rollback** (deployment misbehaving, keep the user):

```bash
sudo -u factory -i
cd "$HOME/fab/factory"
git reset --hard <last-good-sha-on-deploy>
# DB restore from a known-good snapshot:
cp ~/factory-db-backups/blackboard-<STAMP>.db "$HOME/fab/factory/store/blackboard.db"
```
then brake-cycle + kickstart as in §6/§7.

---

## 9. Boundary & residual-risk

**The `factory` user CAN:**
- Read/write anywhere under `/Users/factory` (its own home, deployment tree, secrets).
- Push to `ikangai/clive` (Contents+Issues, per the PAT scope) and comment on its issues.
- Run arbitrary Bash as itself (super-worker Bash execution) — but that's `factory`'s own
  sandbox, not the operator's.
- Use the operator's Claude subscription (shared token pool — see risk below).

**The `factory` user CANNOT (by macOS Standard-user + PAT-scope enforcement):**
- Read the operator's home directory, keychain, SSH keys, or other credentials.
- Push to any GitHub repo other than `ikangai/clive`.
- `sudo` anything (no admin rights).
- Read `/Library/LaunchDaemons` plist secrets — there are none; they live in the 600 env file.

**Residual risks:**

| # | risk | mitigation / status |
|---|---|---|
| 1 | `super_worker.user=""` under the deploy overlay means workers run as `factory` itself — same-user Bash blast radius is bounded by the `factory` account, not further isolated (no nested Guest House) | acceptable because the whole deployment already IS the isolation boundary; don't further widen `factory`'s own privileges |
| 2 | shared Claude subscription — deployment + operator both draw from one rate/usage pool | watch usage (§10); split subscriptions if contention becomes real |
| 3 | headless-auth fallback: if keychain/OAuth ever silently breaks, "unlock keychain manually as a last resort" reintroduces a GUI dependency the daemons can't self-heal from | the §3 headless-auth proof is the gate that should catch this BEFORE go-live; re-run it after any macOS/Xcode/claude-CLI update |
| 4 | a classic PAT (org fine-grained fallback, §2) is scoped to every repo the token owner can touch, not just `ikangai/clive` | prefer enabling fine-grained PATs org-wide; treat a classic token as a standing widened-blast-radius risk and rotate it more aggressively |
| 5 | `/Users/martintreiber` (operator home) is, by default macOS permissions, not world-readable, but verify explicitly | belt-and-braces: `chmod go-rx /Users/martintreiber` if any group/other bits are open |
| 6 | daemon logs (`logs/daemon-*.log`) grow unbounded — always-on with no built-in rotation | add a `newsyslog.d` entry (e.g. `/etc/newsyslog.d/factory.conf`) sized/rotated to taste; not yet automated by this kit |

---

## 10. 24h observation checklist

- Shift cadence and wall-clock duration look sane for the mode (`shift` vs `auto`).
- Per-shift ledger stays ≤ 500k tokens (no runaway shift).
- The deployment's `fab/clive` `factory/auto` is advancing, and graduation is pushing to
  `origin/chore-extract-factory` on GitHub.
- No persistent ⚠ publication-lag alarm on the board.
- The error brake has not tripped (check `./bin/factory status` / board).
- 3-hourly backups are actually appearing under `~factory/factory-db-backups/`.
- `pmset -g assertions` still shows the sleep-prevention assertion while a daemon runs.
- Claude subscription usage is trending sanely against the operator's own attended use
  (risk #2 above) — not silently starving one side.
