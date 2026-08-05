"""common/harness_surface.py — the factory's OWN editable-vs-frozen surface manifest
(design: docs/plans/2026-08-05-self-harness-loop-design.md, Component A).

Pure module, no I/O: every fact here is a literal declared at import time. This is the
CODE-level authority the harness engineer's proposals are checked against — never the
harness engineer's own claim of compliance, exactly as `common/frozen_source.py` is the
authority for the TARGET's frozen surface (this module is that same idea turned on the
FACTORY's own tree). `check_target` reuses `frozen_source._is_frozen`'s matching
semantics (fnmatch glob, exact path, or directory prefix) so a factory-surface freeze
behaves identically to a target-surface one.

Belt-and-suspenders (design's authority line, binding rule 7): `FROZEN_KNOB_PREFIXES`/
`_is_frozen_knob` block `autonomy.*`, `grade.*`, and any `*.organizer`/
`*.harness_engineer` trigger even if a FUTURE `SETTINGS_SPEC` edit were to list one — the
manifest freezes them independently of the spec, so the loop can never widen its own
trigger by proposing itself into the whitelist.
"""
from __future__ import annotations

from . import config
from .frozen_source import _is_frozen

# Glob/exact/dir-prefix patterns (matched via frozen_source._is_frozen) that are NEVER a
# legal `prompt` proposal target, and describe the factory-tree surfaces the harness
# engineer can never reach no matter what kind of proposal it emits. Mirrors the design's
# authority line: "Brakes, budgets, gates, verifiers, the killswitch, the bus, the store
# schema, and this manifest itself are FROZEN".
FROZEN_SURFACES: tuple[str, ...] = (
    "common/code_gate.py",
    "common/frozen_source.py",
    "common/killswitch.py",
    "common/harness_surface.py",     # self — a proposal can never loosen its own manifest
    "store/schema.sql",
    "common/store.py",
    "vendor/",
    "reporting/approvals.py",
    "reporting/human_queue.py",
    "dashboard/",
    "common/budget*.py",             # "budget/ledger code" (design) — no dedicated module
    "tests/",                        # exists yet; glob covers one if it ever appears
)

# Config-key prefixes that are NEVER a legal `setting` proposal target, independent of
# SETTINGS_SPEC's own contents (belt-and-suspenders — see module docstring).
FROZEN_KNOB_PREFIXES: tuple[str, ...] = ("autonomy.", "grade.")

# Explicit non-prefixed frozen keys, if any ever arise that don't fit a prefix pattern.
FROZEN_KNOB_KEYS: frozenset[str] = frozenset()

# Per-knob numeric ranges for `setting` proposals whose SETTINGS_SPEC type is `int` —
# derived, hand-declared once here (never re-tuned per proposal). Every SETTINGS_SPEC int
# key has an entry; a proposal naming an int key with no entry here is out of the editable
# surface (defensive — a future int knob must be given bounds explicitly before the
# harness engineer may propose it). Boolean SETTINGS_SPEC keys need no bounds — their
# domain is exactly {true, false}, enforced by `common.config._cast_setting`.
SANE_BOUNDS: dict[str, tuple[int, int]] = {
    "super_worker.max_parallel": (1, 8),
    "super_worker.max_tasks_per_shift": (1, 20),
    "super_worker.refill_threshold": (0, 20),
    "super_worker.max_profiles": (1, 40),
    "super_worker.dispatch_waves": (1, 4),
}


def _is_frozen_knob(key: str) -> bool:
    """True iff `key` is permanently out of reach for a `setting` proposal, regardless of
    whether it appears in SETTINGS_SPEC today or is added to it later."""
    if key.endswith(".organizer") or key.endswith(".harness_engineer"):
        return True
    if key in FROZEN_KNOB_KEYS:
        return True
    return any(key.startswith(p) for p in FROZEN_KNOB_PREFIXES)


def editable_settings_keys() -> set[str]:
    """`SETTINGS_SPEC` keys minus every frozen knob — the `setting` proposal's legal
    target vocabulary (design's `EDITABLE_SURFACES`)."""
    return {k for k in config.SETTINGS_SPEC if not _is_frozen_knob(k)}


def check_target(kind: str, target: str) -> tuple[bool, str]:
    """Whether `target` is a legal proposal target for `kind`
    ('setting' | 'prompt' | 'learning_corrective'). Returns (ok, reason) — reason is ''
    when ok. Pure format/surface check only: for `learning_corrective` this validates the
    `learning:<id>` SHAPE, not whether the id actually exists (that needs a store — see
    `orchestrator.harness.validate_proposals`, which has one)."""
    target = (target or "").strip()
    if not target:
        return False, "target is empty"

    if kind == "setting":
        if target not in config.SETTINGS_SPEC:
            return False, f"{target!r} is not a SETTINGS_SPEC key"
        if _is_frozen_knob(target):
            return False, (f"{target!r} is a FROZEN knob — every autonomy.*/grade.* key "
                           f"and any *.organizer/*.harness_engineer trigger is out of "
                           f"reach even if a future SETTINGS_SPEC edit lists it")
        return True, ""

    if kind == "prompt":
        if not (target.startswith("roles/") and target.endswith("/prompt.md")):
            return False, f"{target!r} is not a roles/<x>/prompt.md path"
        if _is_frozen(target, FROZEN_SURFACES):
            return False, f"{target!r} touches a FROZEN surface"
        return True, ""

    if kind == "learning_corrective":
        if not target.startswith("learning:"):
            return False, f"{target!r} must be 'learning:<id>'"
        rest = target[len("learning:"):]
        if not rest.isdigit():
            return False, f"{target!r}: id must be a positive integer"
        return True, ""

    return False, f"unknown proposal kind {kind!r}"
