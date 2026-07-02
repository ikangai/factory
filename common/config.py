"""Load factory config + panel. Thin wrappers over YAML so the rest of the
codebase never touches the files directly."""
from __future__ import annotations

import functools
from typing import Any

import yaml

from . import paths


@functools.lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    with open(paths.CONFIG_YAML, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@functools.lru_cache(maxsize=1)
def load_panel() -> dict[str, Any]:
    with open(paths.PANEL_YAML, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def panel_models() -> list[dict[str, Any]]:
    """The panel intelligences that drive candidate clive during optimisation."""
    return list(load_panel().get("panel", []))


# Whitelisted runtime overrides (Phase 6): the board/CLI may set these in the store; cmd_run
# resolves each as store override → config.yaml → hardcoded default. SINGLE SOURCE — imported by
# the CLI and the fleet server so both validate/resolve the same set. Value = the cast/validator.
SETTINGS_SPEC = {
    "super_worker.max_parallel": int,
    "super_worker.max_tasks_per_shift": int,
    "super_worker.refill_threshold": int,
    "super_worker.max_profiles": int,
    "super_worker.scope_check": bool,
    "super_worker.require_test": bool,
    "super_worker.auto_decompose": bool,
    "super_worker.reviewer": bool,          # Phase 8 (config-gated reviewer role)
}


def _cast_setting(kind, raw):
    """Cast a stringly store value (or a native config value) to the knob's type. bools accept
    the store's 'true'/'false' text as well as a native yaml bool."""
    if kind is bool:
        return raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
    if kind is int:
        return int(raw)
    return raw


def resolve_setting(store, key: str, default=None):
    """Resolve a whitelisted knob: store override → config.yaml → default. Returns
    (value, source) with source in ('override','config','default'). The store override is the
    board's runtime lever; it takes effect at the NEXT shift (when cmd_run resolves knobs)."""
    kind = SETTINGS_SPEC.get(key, str)
    ov = store.get_setting(key)
    if ov is not None:
        return _cast_setting(kind, ov), "override"
    section, _, leaf = key.partition(".")
    cfgval = (load_config().get(section, {}) or {}).get(leaf)
    if cfgval is not None:
        return _cast_setting(kind, cfgval), "config"
    return default, "default"


def resolve_model(tier: str) -> str:
    """Resolve a worker profile's tier alias to a concrete model id via the config whitelist.

    '' / 'frontier' → '' (the account's default model — the frontier tier, reserved for judgment
    work). A known alias → its mapped id. An UNKNOWN/unresolvable alias FAILS OPEN DOWNWARD to the
    `standard` id and prints a warning — a typo must never silently upgrade a worker to frontier;
    it returns '' only when `standard` itself is unmapped. So a bad profile can never brick a
    dispatch, and can never sneak a worker onto the reserved frontier tier."""
    models = load_config().get("models", {}) or {}
    tier = (tier or "").strip()
    if tier in ("", "frontier"):
        return models.get("frontier", "") or ""
    if tier in models:
        return models[tier] or ""
    std = models.get("standard", "") or ""
    print(f"[config] unknown model tier {tier!r} — failing open to standard "
          f"({std or 'account default'}), never up to frontier")
    return std


def target_config() -> dict[str, Any]:
    """Resolved config for the TARGET program the factory optimises.

    Backward compat: the new home is a top-level `target:` block (with
    `provider` + root/python/entry/default_toolset). If only the legacy top-level
    `clive:` block exists, treat it as the clive target so nothing breaks. A
    present `target:` wins; a present `clive:` fills in any keys it omits."""
    cfg = load_config()
    target = dict(cfg.get("target") or {})
    legacy = dict(cfg.get("clive") or {})
    if not target and not legacy:
        return {"provider": "clive"}
    merged = dict(legacy)          # legacy clive: knobs as the base
    merged.update(target)          # explicit target: overrides
    merged.setdefault("provider", "clive")
    return merged


def held_out_models() -> list[dict[str, Any]]:
    """Held-out model(s): never used during optimisation, only overfit detection."""
    return list(load_panel().get("held_out", []))


def smoke_model() -> dict[str, Any]:
    """The single panel model the smoke test drives the pipeline with."""
    for m in panel_models():
        if m.get("smoke"):
            return m
    models = panel_models()
    if not models:
        raise RuntimeError("panel.yaml has no panel models")
    return models[0]


def clive_python() -> str:
    # Reads the resolved target config (target: block, or legacy clive: block).
    cfg = target_config()
    root = paths.resolve_clive_root(cfg.get("root", ".."))
    py = cfg.get("python", "python3")
    # Allow a venv-relative interpreter.
    if not py.startswith("/") and "/" in py:
        import os
        cand = os.path.join(root, py)
        if os.path.exists(cand):
            return cand
    return py


def clive_entry() -> tuple[str, str]:
    """Return (target_root, target_entry_abs_path)."""
    import os
    cfg = target_config()
    root = paths.resolve_clive_root(cfg.get("root", ".."))
    return root, os.path.join(root, cfg.get("entry", "clive.py"))


def target_repo_slug() -> str:
    """The target's 'owner/repo' for `gh` issue commands — from config.yaml (target.repo),
    else derived from the target repo's `origin` remote, else '' (issue features degrade
    gracefully). This is the robust fallback so research + the conductor see the real issues
    even when the mission row never carried a target_repo."""
    import re
    import subprocess
    slug = (target_config().get("repo") or "").strip()
    if slug:
        return slug
    try:
        root = clive_entry()[0]
        url = subprocess.run(["git", "-C", root, "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001 — no git / no remote → no slug
        return ""
    m = re.search(r"[:/]([^/:]+/[^/:]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else ""


def is_super_worker(role: str, cfg: dict[str, Any] | None = None) -> bool:
    """Whether `role` should run as a full-capability SUPER-WORKER (curated tools +
    acceptEdits in a disposable sandbox) instead of the isolated one-shot `claude -p`.

    Opt-in via `roles.super_workers` in config.yaml — a list of role names, or `"*"`/
    `"all"` for every role. Empty/absent = the SAFE DEFAULT: every role stays isolated."""
    cfg = cfg if cfg is not None else load_config()
    spec = (cfg.get("roles") or {}).get("super_workers") or []
    if isinstance(spec, str):
        spec = [spec]
    names = {str(r).lower() for r in spec}
    return role.lower() in names or "*" in names or "all" in names


def get_adapter(cfg: dict[str, Any] | None = None):
    """Factory: return the TargetAdapter for the configured target.

    Reads `target.provider` (default "clive"). Pointing the factory at another
    repo = a new adapter under factory/adapters/ + setting target.provider here.
    Import is deferred so common.config has no hard dependency on adapters."""
    provider = (target_config().get("provider") or "clive").lower()
    if provider == "clive":
        from ..adapters.clive import CliveAdapter
        return CliveAdapter()
    raise ValueError(
        f"unknown target provider: {provider!r} (no adapter registered; "
        f"add one under factory/adapters/ and wire it in config.get_adapter)")
