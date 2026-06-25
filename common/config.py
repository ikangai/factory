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
