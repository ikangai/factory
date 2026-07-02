"""Target-derived workforce seeding. The bench is a function of the TARGET: stack specialists
are detected from the target's manifests (deterministic — no LLM in the seeding path) and
re-derived whenever the factory is re-pointed. Additive only: a re-point ADDS missing stack
profiles; retiring stale ones is the CONDUCTOR's call (it has the outcome data). Domain
specialists (ml-expert, prompt-pro, …) are never seeded — inferring domain from files is
guesswork; the conductor generates those on demand (factory worker add).

Guarded by the settings key 'staffing.seeded_for': re-runs only when the target slug changes
or a detected stack profile is missing. 'Missing' is checked against list_profiles(active_only=
False) so a RETIRED profile counts as PRESENT — the conductor retired it deliberately; a
re-seed must never resurrect it. Inserts go through add_profile(replace=False) so a re-seed
can't clobber a conductor-tuned overlay.
"""
from __future__ import annotations

import os

_STACK_MARKERS = {          # marker file present at target root → seed this profile
    "python-dev": ("pyproject.toml", "setup.py", "requirements.txt"),
    "ts-dev":     ("tsconfig.json",),
    "node-dev":   ("package.json",),        # superseded by ts-dev when both match
    "rust-dev":   ("Cargo.toml",),
    "go-dev":     ("go.mod",),
}
_UNIVERSAL = ("generalist", "test-engineer", "docs-writer")   # every target gets these

# One tight persona paragraph per profile. model tier 'standard' for every stack/universal
# specialist; '' (account default) for the generalist. Overlays are persona/emphasis ONLY —
# never instructions to bypass tests or gates (capability stays rail-fixed).
_OVERLAYS: dict[str, tuple[str, str]] = {
    "generalist": (
        "", ""),
    "test-engineer": (
        "You are a test engineer. Lead with the failing test, cover the edge cases and error "
        "paths, and keep the change minimal and green. You may fan mechanical scaffolding out to "
        "a cheaper model, but you own the assertions.", "standard"),
    "docs-writer": (
        "You are a technical writer. Keep docs/comments accurate, concise and in the codebase's "
        "existing voice; document behavior and rationale, not restated code. Never let a doc "
        "change touch behavior.", "standard"),
    "python-dev": (
        "You are a senior Python engineer. Idiomatic stdlib-first Python, typed where it helps, "
        "pytest-driven. Respect the project's existing patterns; keep diffs tight. Delegate rote "
        "edits to a cheaper model, but review every line before you ship.", "standard"),
    "ts-dev": (
        "You are a senior TypeScript engineer. Strict types, no `any` escape hatches, match the "
        "project's module/build conventions. Test with the repo's runner. Keep changes small and "
        "reviewable.", "standard"),
    "node-dev": (
        "You are a senior Node.js engineer. Idiomatic async JS, the project's existing framework "
        "and test runner, tight reviewable diffs.", "standard"),
    "rust-dev": (
        "You are a senior Rust engineer. Safe, idiomatic Rust; let the type system and borrow "
        "checker carry invariants; cargo test green. Keep the diff minimal.", "standard"),
    "go-dev": (
        "You are a senior Go engineer. Idiomatic, simple Go; explicit error handling; table-driven "
        "tests; gofmt-clean. Small, reviewable changes.", "standard"),
}

_DESCRIPTIONS = {
    "generalist": "General-purpose developer — the default when no specialist fits.",
    "test-engineer": "Test-first specialist: failing test, edge cases, minimal green change.",
    "docs-writer": "Technical writer: accurate, concise docs/comments in the codebase's voice.",
    "python-dev": "Python/pytest specialist for the target codebase.",
    "ts-dev": "TypeScript specialist (strict types, the repo's build/test conventions).",
    "node-dev": "Node.js specialist (idiomatic async JS, the repo's framework).",
    "rust-dev": "Rust specialist (safe idiomatic Rust, cargo test).",
    "go-dev": "Go specialist (idiomatic simple Go, table-driven tests).",
}


def detect_stacks(root: str) -> list[str]:
    """Detected stack profiles for a target root, by manifest markers. ts-dev suppresses node-dev
    (a TS project IS a node project — the TS specialist supersedes the generic node one)."""
    found = []
    for name, markers in _STACK_MARKERS.items():
        if any(os.path.exists(os.path.join(root, m)) for m in markers):
            found.append(name)
    if "ts-dev" in found and "node-dev" in found:
        found.remove("node-dev")
    return found


def ensure_seeded(store, target_root: str, target_slug: str) -> list[str]:
    """Seed the profiles this target needs; return the names newly added (idempotent).

    Universal roles + detected stack specialists are added with add_profile(replace=False) so a
    re-seed never clobbers a tuned overlay. Re-runs only when the slug changed OR a detected
    profile is missing (checked incl. retired rows, so a deliberately-retired profile is never
    resurrected). The guard is the 'staffing.seeded_for' setting."""
    detected = detect_stacks(target_root)
    wanted = list(_UNIVERSAL) + detected
    existing = {p["name"] for p in store.list_profiles(active_only=False)}   # incl. retired
    missing = [n for n in wanted if n not in existing]

    if store.get_setting("staffing.seeded_for") == target_slug and not missing:
        return []                                        # already seeded for this exact target

    added = []
    for name in missing:
        overlay, model = _OVERLAYS.get(name, ("", "standard"))
        store.add_profile(name, description=_DESCRIPTIONS.get(name, name),
                          model=model, overlay=overlay, created_by="system", replace=False)
        added.append(name)
    store.set_setting("staffing.seeded_for", target_slug)
    return added
