"""Target adapters: how the factory actuates a spec into, and invokes, a TARGET
program. `clive` is the default. Selected by `target.provider` in config.yaml via
common.config.get_adapter(). Pointing the factory at another repo = a new adapter
here + setting target.provider.

The factory (get_adapter) lives in common.config so that module — already the
single config gateway — owns provider selection, mirroring envs.get_provider.
Re-exported here for convenience / discoverability."""
from __future__ import annotations

from .base import TargetAdapter
from ..common.config import get_adapter

__all__ = ["TargetAdapter", "get_adapter"]
