# Multi-instance install runbook

Companion to `install.sh` (repo root) and `scripts/configure_instance.py` — design:
docs/plans/2026-07-09-single-line-installer-design.md. This is the ATTENDED install path:
one command clones a complete, runnable factory instance bound to one target repo, and the
same command with `--target` installs further instances for OTHER targets on the same
machine. For an always-on, isolated deployment see docs/runbooks/factory-user-deployment.md.

## The one-liner

```bash
curl -fsSL https://raw.githubusercontent.com/ikangai/factory/main/install.sh | bash
```

A second instance, for a different target repo, on the same machine:

```bash
curl -fsSL https://raw.githubusercontent.com/ikangai/factory/main/install.sh | \
  bash -s -- --target https://github.com/me/myrepo.git
```

Enumerate every instance already installed:

```bash
curl -fsSL https://raw.githubusercontent.com/ikangai/factory/main/install.sh | bash -s -- list
# or, once installed: <root>/<name>/factory/scripts/configure_instance.py --list --instances-root <root>
```

## Flags

| flag | default | meaning |
|---|---|---|
| `--target <url\|path>` | `https://github.com/ikangai/clive.git` | the repo this instance optimises |
| `--name <n>` | target basename (`.git` stripped, sanitized) | instance name — the dir under `--root` |
| `--root <dir>` | `~/factories` | parent of every instance on this machine |
| `--factory-repo <url\|path>` | `https://github.com/ikangai/factory.git` | a local path enables offline/hermetic installs |
| `--branch <b>` | `main` | factory branch the instance is created/updated from |
| `--provider <p>` | `clive` | adapter under `factory/adapters/` that drives the target |
| `--base-branch <b>` | `chore/extract-factory` for a `clive` target, else `factory/base` | the graduation/release branch in the TARGET repo |
| `--port <n>` | `auto` | dashboard port; `auto` picks the first free, non-colliding pair |
| `--skip-deps` | off | skip the `pip install` steps (tests/CI, or deps already satisfied) |
| `list` | — | positional subcommand: enumerate instances under `--root` and exit |

## Layout (why one parent dir per instance)

`bin/factory` runs `python3 -m factory.<module>` from the repo's **parent** directory, so the
factory clone must be named exactly `factory`. Two instances therefore cannot share a parent —
the installer forces one instance root per target:

```
~/factories/
  clive/            instance "clive"
    factory/        clone of ikangai/factory, on local branch instance/clive
    clive/          clone of the target repo (dir name = its basename)
  myrepo/           instance "myrepo"
    factory/
    myrepo/
```

This also happens to be exactly what `config.yaml`'s `target.root: "../<target>"` expects —
the sibling layout is the load-bearing reason, not a cosmetic choice.

Per-instance state (blackboard.db, config.yaml, `.factory-mode`, bus, logs) lives INSIDE the
clone, isolated by construction. Shared-per-OS-user by design: git identity, the pip user
site, and `claude`/`gh` logins — that's deliberate, not an oversight.

## Instance branch + update flow

Each instance sits on its own local branch `instance/<name>`, created from `origin/<branch>`,
with the patched `config.yaml` committed on it (same pattern as the deploy kit's `deploy`
branch). **Updating an instance is re-running the installer with the same args** — idempotent:

- the factory clone fetches `origin` and merges `origin/<branch>` into `instance/<name>`;
- the config overlay, `bin/factory init`, and the smoke check all re-run (all are no-ops on an
  already-correct instance);
- the target clone is only fetched, never force-reset — the factory's own graduation flow
  moves its base branch forward between installer runs, and a re-run must not discard that.

**Merge conflicts stop loudly.** Local evolution winning by default is NOT assumed — a
conflicting merge into `instance/<name>` prints `resolve by hand in <dir>` and exits 1. Go
into that dir, resolve it like any other git conflict, then re-run the installer.

## Ports

`dashboard.port` is assigned per instance (config.yaml, auto by default): the installer probes
pairs `(p, p+1)` from 8787 upward in steps of 10, skipping anything a sibling instance already
claims or that fails a real bind test on 127.0.0.1. A re-run never reassigns a live instance's
port — if the current value already differs from every sibling's, it's kept unchanged.

The sibling scan only sees instances under `--root`. A process OUTSIDE it holding the assigned
port (e.g. an operator's dev-checkout board on 8787) isn't detected on the keep path — the
board then fails loudly at startup (`EADDRINUSE`); re-run the installer with an explicit
`--port` to move the instance.

The **fleet viz port is a convention, not a config field**: `dashboard.port + 1`, passed
explicitly (`viz --serve --port <port+1>`). `install.sh list` (and `configure_instance.py
--list`) print both.

## Manual steps (never touched by the installer)

- `claude login` — Claude Code auth for the workers.
- `gh auth login` — GitHub auth for graduation pushes.
- `.factory-mode` starts at `shift` (safe default). Flip to `auto` consciously when ready —
  the installer never does this for you.

## Non-clive targets (honesty clause)

Only the `clive` adapter is registered today (`common/config.py get_adapter`). The installer
wires config for **any** repo — the develop rail (briefs → worker → tests → gated merge) is
target-generic — but with `--provider` set to anything other than `clive`, the scenario-eval
loop has no adapter to run yet. `install.sh` prints a loud note for non-clive targets: write a
new adapter under `factory/adapters/` before expecting the eval loop to function.

## Uninstall

```bash
rm -rf ~/factories/<name> ~/.local/bin/factory-<name>
```

Nothing else to clean up — there is no instance registry file; `list` derives instances by
globbing `<root>/*/factory/config.yaml`.

## Hardened, unattended deployments

This installer is the **attended** path (you're at the keyboard for the two manual logins,
and `.factory-mode` stays `shift` until you flip it). For an always-on deployment under a
dedicated, isolated OS user — the posture required for genuinely unattended autonomy — see
docs/runbooks/factory-user-deployment.md.
