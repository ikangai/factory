# Single-line installer + multi-instance factories — design (2026-07-09)

## Goal

One command installs a complete, runnable factory instance bound to ONE target repo:

```bash
curl -fsSL https://raw.githubusercontent.com/ikangai/factory/main/install.sh | bash
```

and the same command with flags installs FURTHER instances for OTHER target repos on the
same machine, none of them colliding:

```bash
curl -fsSL https://raw.githubusercontent.com/ikangai/factory/main/install.sh | \
  bash -s -- --target https://github.com/me/myrepo.git
```

"Does everything" = clone factory + clone target + python deps + per-instance config +
store init + runtime state + a per-instance launcher + smoke check + printed next steps.
The two remaining MANUAL steps are logins (`claude login`, `gh auth login`) — credentials
are never touched by an installer.

## Why one parent dir per instance (the load-bearing constraint)

`bin/factory` runs `python3 -m factory.<module>` from the repo's PARENT directory, so the
clone MUST be named exactly `factory`. Two instances therefore cannot share a parent.
The layout that follows — and that happens to preserve `target.root: "../<target>"` —
is one instance root per target:

```
~/factories/
  clive/            # instance "clive"
    factory/        #   clone of ikangai/factory, on local branch instance/clive
    clive/          #   clone of the target repo (dir name = repo basename)
  myrepo/           # instance "myrepo"
    factory/
    myrepo/
```

## Co-evolution model

The factory co-evolves with its target: per-instance state (store/blackboard.db learnings,
config.yaml, .factory-mode, bus, logs) lives INSIDE the clone, and factory code itself may
be locally adapted per target. Therefore:

- each instance is a FULL clone (never a shared checkout or worktree);
- each instance sits on its own local branch `instance/<name>`, created from origin/main,
  with the config overlay committed on it (same pattern as the deploy kit's `deploy` branch);
- updating an instance = re-running install.sh with the same args (idempotent): it fetches
  origin and merges `origin/main` into `instance/<name>`, re-applies the overlay, re-runs
  deps + init + smoke. Merge conflicts stop loudly for the operator — local evolution wins
  by default is NOT assumed.

## Per-instance isolation audit

| Resource | Isolation |
|---|---|
| blackboard.db, logs/, updates/, scenarios | repo-local — isolated by construction |
| agora bus (.groupchat/.agora) | repo-local (bin/factory bus + roles pin AGORA_DIR into the clone) |
| .factory-mode / STOP | repo-local; NOT git-tracked → installer writes mode=shift |
| dashboard port | config.yaml `dashboard.port` → auto-assigned per instance |
| fleet viz port | CLI arg (`viz --serve --port`) → convention: dashboard.port+1, printed + shown by `list` |
| git identity, pip user site, claude/gh logins | shared per OS user — deliberately fine |
| Guest-House `agent` user wiring | machine-specific → overlay resets to safe same-user defaults |

## Components

### 1. `install.sh` (repo root — short raw URL)

Bash, `set -euo pipefail`, no third-party deps. Steps, all idempotent:

1. **Args**: `--target <url|path>` (default `https://github.com/ikangai/clive.git`),
   `--name <n>` (default: target basename), `--root <dir>` (default `~/factories`),
   `--factory-repo <url|path>` (default `https://github.com/ikangai/factory.git` — a local
   path enables offline installs and hermetic tests), `--branch <b>` (default `main`),
   `--provider <p>` (default `clive`), `--base-branch <b>` (default: `chore/extract-factory`
   for the clive target, else `factory/base`), `--port <n>` (default: auto),
   `--skip-deps` (tests/CI), `list` (subcommand: enumerate instances under root).
2. **Preflight**: require `git` + `python3`; warn (not fail) on missing `claude`, `gh`, `tmux`.
3. **Clone factory** into `<root>/<name>/factory`; create/checkout `instance/<name>` from
   `origin/<branch>` (on re-run: fetch + merge `origin/<branch>`, conflicts abort loudly).
4. **Clone target** as sibling; ensure the base branch: check out the remote branch if it
   exists, else create it locally from the target's default branch (graduation pushes it
   to origin later, behind the push_approval gate).
5. **Deps**: `pip install --user -r requirements.txt` (fallback `--break-system-packages`);
   same for the target's requirements.txt if present. Skippable via `--skip-deps`.
6. **Configure**: run `scripts/configure_instance.py` (below); commit the patched
   config.yaml on `instance/<name>` if changed.
7. **Init**: `bin/factory init` (idempotent — champion/scenarios seed only on a fresh store);
   write `.factory-mode` = `shift` if absent (safe brake; AUTO is a conscious flip).
8. **Launcher**: write `~/.local/bin/factory-<name>` — a 2-line exec wrapper (NOT a symlink;
   bin/factory resolves `$0` without following links).
9. **Smoke**: `bin/factory status` must exit 0.
10. **Print**: paths, board/fleet ports + exact commands, manual next steps (claude login,
    gh auth, mode flip), pointer to the hardened dedicated-user kit
    (docs/runbooks/factory-user-deployment.md) for unattended deployments.

### 2. `scripts/configure_instance.py`

Comment-preserving, block-scoped config patcher — same discipline as
`deploy/user-factory/apply-config-overlay.py` (which stays untouched; it patches fixed
old→new literals, this one SETS parameterized values). Requires exactly one matching
`field:` line within the parent top-level block, else fails loudly (drift guard).
After patching, re-parses with yaml and asserts the effective values.

Sets:
- `target.root` → `"../<target-dir>"`; `target.provider`; `target.base_branch`
- `dashboard.port` → assigned port
- `autopilot.prod` → `false`, `super_worker.user` → `""`,
  `super_worker.claude_bin` → `"claude"` (fresh machines have no Guest-House user;
  the operator's own box re-enables consciously)

Port auto-assignment (`--port auto`): collect `dashboard.port` from every sibling
instance's config under `<root>`, then probe pairs (p, p+1) from 8787 upward in steps
of 10, skipping taken/bound ports (test-bind on 127.0.0.1). Deterministic and
collision-free across instances.

Also `--list`: print one line per instance under root (name, target, provider, ports,
mode) — backs `install.sh list`.

### 3. Non-clive targets (honesty clause)

Only the `clive` adapter is registered today. The installer wires config for ANY repo,
but with `--provider` ≠ a registered adapter the eval loop would fail at
`get_adapter()`. The installer validates the provider against `adapters/` and prints a
loud note for non-clive targets: the develop rail (briefs → worker → tests → gated merge)
is target-generic, the scenario-eval loop needs a new adapter under `factory/adapters/`.

## Testing

- `tests/test_configure_instance.py` — unit: block-scoped set, exactly-once drift guard,
  idempotent re-run, yaml assert, port auto-assign vs sibling instances + bound socket.
- `tests/test_install_sh.py` — `bash -n` syntax; end-to-end hermetic install into a
  tmpdir using `--factory-repo <this checkout>` + a synthetic local target repo +
  `--skip-deps`: asserts layout, instance branch, patched config, launcher, `status`
  green, second run (update path) idempotent, second INSTANCE gets non-colliding ports.

## Out of scope (YAGNI)

- launchd daemons per instance (the dedicated-user kit owns always-on; this installer is
  the attended path and prints the pointer);
- an instance registry file (instances are derived by globbing `<root>/*/factory`);
- cross-instance orchestration; uninstall (`rm -rf <root>/<name>` + the launcher, documented).
