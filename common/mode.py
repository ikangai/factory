"""Autonomy mode — the toggle that makes the factory self-drive or wait, like Claude
Code's auto-accept.

- AUTO  : the runner works shift after shift on its own — no human between shifts; the
          human just watches (the dashboard) and can interrupt (STOP / toggle back).
- SHIFT : one shift, then stop and wait for the human to start the next.

File-backed (FACTORY_ROOT/.factory-mode) so the dashboard server and the runner — separate
processes — share one source of truth. Default SHIFT (human-in-the-loop is the safe
default; the operator opts into AUTO)."""
from __future__ import annotations

import os

from . import paths

AUTO = "auto"
SHIFT = "shift"
_VALID = {AUTO, SHIFT}


def _mode_path() -> str:
    return os.path.join(paths.FACTORY_ROOT, ".factory-mode")


def read_mode() -> str:
    """The current mode; SHIFT if unset/invalid (safe default — human-in-the-loop)."""
    try:
        with open(_mode_path(), "r", encoding="utf-8") as fh:
            m = fh.read().strip().lower()
        return m if m in _VALID else SHIFT
    except OSError:
        return SHIFT


def set_mode(mode: str) -> str:
    """Set the mode (auto|shift). Returns the normalized value; raises on anything else."""
    mode = (mode or "").strip().lower()
    if mode not in _VALID:
        raise ValueError(f"mode must be 'auto' or 'shift', not {mode!r}")
    path = _mode_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(mode)
    return mode


def is_auto() -> bool:
    return read_mode() == AUTO
