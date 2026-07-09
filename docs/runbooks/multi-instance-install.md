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
| `--target-dir <d>` | target basename | sibling clone dir name — use when the target repo is literally named `factory` (which would collide with the factory clone dir) |
| `--name <n>` | target basename (`.git` stripped, sanitized) | instance name — the dir under `--root`; explicit names are sanitized to `[A-Za-z0-9._-]` too |
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

**config.yaml conflicts self-heal; everything else stops loudly.** The overlay commit and
upstream both touch config.yaml (and git coalesces nearby hunks), so that ONE conflict is
expected and mechanical: the update takes upstream's config.yaml wholesale and re-applies the
instance overlay right after. Likewise, an uncommitted config.yaml left by an interrupted
prior run is discarded (the overlay regenerates it). Any OTHER conflicting or dirty file is
real local evolution: the installer prints `resolve by hand in <dir>` (or lists the dirty
files) and exits 1 — resolve like any other git conflict, then re-run.

## Ports

`dashboard.port` is assigned per instance (config.yaml, auto by default), with fresh installs
and updates deliberately different:

- **fresh install** (`--port auto`): PROBE — pairs `(p, p+1)` from 8787 upward in steps of 10,
  skipping anything a sibling instance claims (its dashboard port AND its implied fleet port)
  or that fails a real bind test on 127.0.0.1. A busy 8787 on the machine is detected here.
- **re-run/update** (the installer passes `keep`): the previously-assigned port is kept
  unchanged unless it now collides with a sibling — with NO bind test, because a live
  dashboard legitimately holds its own port and a bind test can't tell "my own board" from
  "someone else's process".
- **explicit `--port N`**: used verbatim; collisions with a sibling's dashboard/fleet port
  warn but never fail.

Concurrent installs on one host are serialized around port assignment via a lock file
(`<root>/.ports.lock`), so two simultaneous runs can't walk to the same free pair.

The **fleet viz port is derived, not remembered**: `viz --serve` defaults to
`dashboard.port + 1` read from the instance's own config.yaml, so each instance's fleet
server is collision-free by construction — no `--port` needed. `install.sh list` (and
`configure_instance.py --list`) print both ports plus each instance's target upstream URL.

## Manual steps (never touched by the installer)

- `claude login` — Claude Code auth for the workers.
- `gh auth login` — GitHub auth for graduation pushes.
- `.factory-mode` starts at `shift` (safe default). Flip to `auto` consciously when ready —
  the installer never does this for you.

## Non-clive targets (honesty clause)

Two adapters ship registered (`common/config.py get_adapter`): `clive` (the reference) and
`generic` — a fully config-driven command adapter (`adapters/generic.py`), which non-clive
targets get **by default**. The develop rail (briefs → worker → tests → gated merge) is
target-generic regardless; the `generic` adapter additionally wires the scenario-eval loop:
it invokes `<target.python> <target.entry> [target.exec.args]` with candidate spec knobs as
env vars (`target.exec.spec_env_prefix`), the goal substituted for `{goal}` in the args
template, and the panel model mapped to env via `target.exec.model_env`. Before the first
eval run, set `target.entry` (and optionally the `target.exec` block) in the instance's
config.yaml — the adapter refuses to guess an entry point. For richer, target-specific
actuation write a dedicated adapter and register it in `get_adapter`; an unregistered
`--provider` gets a loud note at install time. Also note: target runtime deps are installed
only from a top-level `requirements.txt`; a target that declares deps elsewhere needs them
installed manually before the first real shift.

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
