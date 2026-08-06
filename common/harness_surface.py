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
`FROZEN_KNOB_KEYS`/`_is_frozen_knob` block `autonomy.*`, `grade.*`, every GATE/VERIFIER
knob (`FROZEN_KNOB_KEYS` — adversarial-review fix round, 2026-08-05: the authority line
says "gates, verifiers... are FROZEN" but the ORIGINAL FROZEN_KNOB_KEYS was empty, so
`super_worker.require_test=false` validated clean and would have let the rail merge code
without tests from the next shift), and any `*.organizer`/`*.harness_engineer` trigger —
even if a FUTURE `SETTINGS_SPEC` edit were to list one, independent of the spec, so the
loop can never widen its own trigger OR disarm a gate by proposing either into the
whitelist.
"""
from __future__ import annotations

import posixpath
import re

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
    "common/store.py",
    "common/budget.py",              # "budget/ledger code" (design) — the ledger module
    "common/bus.py",                 # "the bus" (authority line names it explicitly)
    "store/schema.sql",
    "vendor/",
    "reporting/approvals.py",
    "reporting/human_queue.py",
    "dashboard/",
    "tests/",
)

# Config-key prefixes that are NEVER a legal `setting` proposal target, independent of
# SETTINGS_SPEC's own contents (belt-and-suspenders — see module docstring).
FROZEN_KNOB_PREFIXES: tuple[str, ...] = ("autonomy.", "grade.")

# GATE/VERIFIER SETTINGS_SPEC keys — the authority line's "gates, verifiers... are
# FROZEN" made concrete (adversarial-review fix round, 2026-08-05 — see module
# docstring). Every one of these flips a merge-quality or dispatch-quality CHECK on or
# off; letting the harness engineer tune them would let it disarm the very verification
# this loop's own evidence (task_evidence/gate_eval_results/etc.) is supposed to improve
# against, not weaken. Deliberately narrower than "every boolean": the CAPACITY ints
# (max_parallel, max_tasks_per_shift, refill_threshold, max_profiles, dispatch_waves) and
# the non-gate booleans (auto_decompose, retry_on_discard, investigate_blocked — none of
# these VERIFY anything; they route/retry/investigate) stay editable.
FROZEN_KNOB_KEYS: frozenset[str] = frozenset({
    "super_worker.require_test",       # ships-a-test gate
    "super_worker.acceptance_exec",    # spec-named acceptance-test gate
    "super_worker.scope_check",        # pre-dispatch scope-judge gate
    "super_worker.reviewer",           # pre-merge review gate
    "super_worker.milestone_verify",   # milestone-delivery grader
    "super_worker.red_proof",          # red-proof discriminating-test gate (Component E,
                                        # docs/plans/2026-08-06-publication-broker-design.md) —
                                        # born frozen: disabling it must never look like a
                                        # benign retune, exactly like require_test above
})

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
    # Component F (docs/plans/2026-08-06-publication-broker-design.md): a claim-lease TTL
    # in minutes — an EDITABLE capacity knob (not a gate/verifier), deliberately NOT in
    # FROZEN_KNOB_KEYS: the harness engineer tuning lease length is legitimate; it cannot
    # disable the sweep itself (that's shift.py's own unconditional call, not this knob).
    "super_worker.claim_lease_minutes": (10, 1440),
}

# A `prompt` target must be a SINGLE path segment under roles/ named exactly
# roles/<role>/prompt.md — no nesting, no traversal. Checked TOGETHER with a
# posixpath.normpath equality check in `check_target` (adversarial-review fix round,
# 2026-08-05): the naive `startswith("roles/") and endswith("/prompt.md")` check ALONE
# let a traversal target like "roles/../common/code_gate.py/prompt.md" pass the shape
# gate — it starts with "roles/", ends with "/prompt.md", and _is_frozen's glob match
# against the RAW (untraversed) string never fires, so it validated clean. normpath
# collapses the ".." before the regex is even consulted; a target whose normalized form
# differs from its raw form is rejected outright, traversal or not.
_PROMPT_TARGET_RE = re.compile(r"^roles/[a-z0-9_-]+/prompt\.md$")

# Role prompts that are FROZEN even though they'd otherwise pass the roles/<x>/prompt.md
# shape check (adversarial-review fix round, 2026-08-05): the harness engineer's OWN
# prompt (the design's explicit meta-harness exclusion — "proposals that edit the
# miner/validator themselves" — the loop must never tune the very prompt that states its
# own bounds) and every VERIFIER/GATE role whose prompt, if loosened ("always approve",
# "always pass"), could quietly defeat a check the authority line separately names as
# frozen. Kept as its own list (not folded into FROZEN_SURFACES, which is a path-glob
# manifest over the whole tree) so each entry's rationale stays legible; checked in
# `check_target`'s 'prompt' branch, so the frozen path is actually REACHABLE (unlike the
# dead FROZEN_SURFACES-only branch this replaces — no FROZEN_SURFACES pattern could ever
# match a roles/*/prompt.md shape).
FROZEN_ROLE_PROMPTS: frozenset[str] = frozenset({
    "roles/harness_engineer/prompt.md",   # meta-harness exclusion (design's own YAGNI)
    "roles/reviewer/prompt.md",           # pre-merge REVIEW gate
    "roles/scope_check/prompt.md",        # pre-dispatch scope-judge GATE
    "roles/decompose/prompt.md",          # post-failure decompose JUDGE
    "roles/judge/prompt.md",              # the (legacy-loop) judge role
})


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
            return False, (f"{target!r} is a FROZEN knob — every autonomy.*/grade.* key, "
                           f"every gate/verifier knob ({', '.join(sorted(FROZEN_KNOB_KEYS))}), "
                           f"and any *.organizer/*.harness_engineer trigger is out of "
                           f"reach even if a future SETTINGS_SPEC edit lists it")
        return True, ""

    if kind == "prompt":
        # Shape: EXACTLY one path segment under roles/, ending in /prompt.md, with no
        # traversal — normpath equality catches "roles/../x" / "roles/a/../../x" etc.
        # (a differing normalized form is rejected outright, regardless of what it
        # collapses to); the regex catches everything normpath wouldn't touch (nesting,
        # uppercase, symbols) — see _PROMPT_TARGET_RE's comment for the exploit this closes.
        if posixpath.normpath(target) != target or not _PROMPT_TARGET_RE.match(target):
            return False, f"{target!r} is not a roles/<x>/prompt.md path"
        if target in FROZEN_ROLE_PROMPTS:
            return False, (f"{target!r} is a FROZEN role prompt — a verifier/gate role, "
                           f"or the harness engineer's own prompt (meta-harness exclusion)")
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
