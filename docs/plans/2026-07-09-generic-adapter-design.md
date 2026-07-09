# Generic target adapter — design (2026-07-09)

## Goal

A second registered `TargetAdapter` (`target.provider: "generic"`) so the scenario-eval
loop runs against ANY repo invocable as `<interpreter> <entry> [args…]` — closing the gap
where non-clive instances installed by install.sh get a working develop rail but a dormant
eval loop. The develop rail needs nothing: `TargetAdapter`'s base defaults (git helpers,
config-driven `frozen_paths`/`test_command`) are already target-generic.

## What the eval loop actually needs (the seam, from runner/runner.py + runner/multi_clive.py)

`actuate` (spec → env/flags), `run` (subprocess toward a goal under budgets),
`parse_session_dirs` (evidence recovery), `panel_env` (panel model → target env),
`scrub_env` (host-cred hygiene), `entry`/`interpreter` (paths). Everything else the
factory does flows through base-class defaults.

## Config surface (all under `target:`, everything optional except what already exists)

```yaml
target:
  provider: "generic"
  root: "../myrepo"
  python: "python3"          # interpreter (same key clive uses)
  entry: "main.py"           # entry file — NO default guess; run() fails actionably if unset/missing
  exec:                      # NEW block, generic-adapter conventions
    args: ["--task", "{goal}"]     # argv template after entry; every {goal} substituted;
                                   # no {goal} anywhere -> goal appended as the last arg
    spec_env_prefix: "FACTORY_"    # candidate open-block SCALARS -> env <PREFIX><KEY upper>
    max_tokens_env: "FACTORY_MAX_TOKENS"   # "" disables
    model_env:                     # panel model entry key -> target env var ({} = not actuated)
      model: "MODEL"
      provider: "LLM_PROVIDER"
      base_url: "LLM_BASE_URL"
    session_dir_regex: ""          # optional; parse_session_dirs = findall over the output
```

## Semantics

- **actuate**: `open` scalars → `env[prefix + KEY.upper()] = str(v)` (bools → "1"/"0");
  nested dict/list values → recorded in `pending` + a note ("declared but not actuatable
  generically") — richer actuation is exactly what a dedicated adapter is for. Never
  writes files, never flags.
- **run**: argv = `[interpreter, <root>/<entry>, *applied_flags, *rendered args]`; source
  root = the `clive_root` override (a candidate checkout being graded) else config root;
  cwd = the `cwd` param else that root. Env: copy of os.environ → `scrub_env` (reuse
  `clive_invoke._scrub_env` — it drops host creds/dangerous flags and is not clive-specific
  in effect) → overlay `env_vars` (sandbox handle), `applied.env`, `panel_env(model_entry)`,
  max-tokens env. Missing entry file raises FileNotFoundError with "set target.entry" —
  an actionable config error, not a silent 127. Timeout → kill + `timed_out=True`.
  Returns the existing `CliveResult` shape (the seam's contract).
- **panel_env**: `{target_env: model_entry[key] for key, target_env in model_env.items()
  if model_entry.get(key)}`.
- **parse_session_dirs**: `re.findall(session_dir_regex, text)` when configured, else `[]`
  (evidence collection simply scopes to the workdir).
- **entry/interpreter**: delegate to `config.clive_entry()`/`config.clive_python()` — both
  read the RESOLVED target config (root/python/entry) and are target-generic despite the
  legacy names.
- **test_command** default: `[interpreter, -m, pytest, tests/, -q]`, overridable via
  `target.test_command` (mirrors clive).

## Registration + installer

- `common/config.py get_adapter`: `provider == "generic"` → `GenericAdapter` (single
  registry; adapters/__init__ re-export unchanged).
- install.sh: `--provider` now DEFAULTS to `generic` for non-clive targets (clive targets
  keep `clive`), so `--target <any repo>` yields an instance whose eval loop is wired up
  to the conventions above; the honesty NOTE becomes "set target.entry (+ optional
  target.exec) in the instance config.yaml, or write a dedicated adapter for richer
  actuation". configure_instance.py is untouched (provider was always a parameter).

## Out of scope (honest edges)

- A scenario CORPUS for the new target: scenarios/checks are per-target content; the
  adapter makes the machinery runnable, not the corpus meaningful.
- Evidence beyond stdout/stderr/workdir: `_read_session_log` reads clive's session log and
  degrades to empty for other targets (fail-tolerant by construction).
- Non-python entry points: `interpreter` + `entry` already allow any executable pairing
  (`python: "node"`, `entry: "cli.js"`); no extra machinery needed.
