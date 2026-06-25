"""Kill switch — the human's emergency brake for the full-auto factory (design:
docs/plans/2026-06-25-autonomous-code-factory.md).

With no human promotion gate, the human steers and can stop. Dropping a `STOP` file at
the factory root halts the fleet immediately (the autonomous loop checks `is_halted()`
each round and before any auto-merge); removing it resumes. File-based so the human can
trip it from a shell with `touch STOP` — no process to signal, no daemon.
"""
from __future__ import annotations

import os

from . import paths


def stop_flag_path() -> str:
    return os.path.join(paths.FACTORY_ROOT, "STOP")


def is_halted() -> bool:
    """True iff the STOP flag is present — checked by the loop each round + pre-merge."""
    return os.path.exists(stop_flag_path())


def engage(reason: str = "") -> str:
    """Drop the STOP flag (halt the fleet). Returns its path."""
    p = stop_flag_path()
    with open(p, "w", encoding="utf-8") as fh:
        fh.write((reason or "halted") + "\n")
    return p


def release() -> None:
    """Remove the STOP flag (resume). Idempotent."""
    p = stop_flag_path()
    if os.path.exists(p):
        os.remove(p)
