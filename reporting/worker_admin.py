"""Shared guardrails for worker-profile management — ONE policy for the `factory worker` CLI
(Task 5.5) and the board's POST /api/worker (Task 6.2), so a profile created from either surface
is validated identically. A profile is data (persona overlay + model tier) only; these guards
keep a conductor- or human-generated profile safe: a valid slug, a whitelisted tier, a bounded
overlay, the active-profile cap, and generalist unretireable (the fail-open default must exist).
"""
from __future__ import annotations

from ..common import config
from ..common.store import _PROFILE_SLUG_RE

KNOWN_TIERS = ("", "frontier", "standard", "fast")   # '' == frontier == account default
MAX_OVERLAY_CHARS = 2000                             # same bound as the mission editor
DEFAULT_MAX_PROFILES = 12


def max_profiles(cfg: dict | None = None) -> int:
    sw = (cfg or config.load_config()).get("super_worker", {}) or {}
    return int(sw.get("max_profiles", DEFAULT_MAX_PROFILES))


def validate_add(name: str, model: str, overlay: str) -> str | None:
    """Return an error message if an `add` would be invalid, else None. Slug + tier + overlay
    length — the checks that don't need the store."""
    if not _PROFILE_SLUG_RE.match(name or ""):
        return f"invalid profile name {name!r} — need lowercase slug ^[a-z0-9][a-z0-9-]{{1,31}}$"
    if (model or "") not in KNOWN_TIERS:
        allowed = ", ".join(t or "'' (frontier)" for t in KNOWN_TIERS)
        return f"unknown model tier {model!r} — choose one of: {allowed}"
    if len(overlay or "") > MAX_OVERLAY_CHARS:
        return f"overlay too long ({len(overlay)} > {MAX_OVERLAY_CHARS} chars)"
    return None


def cap_error(store, name: str, cfg: dict | None = None) -> str | None:
    """Return an error if adding a NEW active profile would exceed the cap, else None. generalist
    doesn't count (it's the fail-open default). Re-adding an already-active name is not a new slot."""
    active = {p["name"] for p in store.list_profiles(active_only=True) if p["name"] != "generalist"}
    if name not in active and len(active) >= max_profiles(cfg):
        return f"active profile cap reached ({max_profiles(cfg)}) — retire one first"
    return None


def retire_error(store, name: str) -> str | None:
    """Return an error if `retire` should be refused, else None. generalist is rejected first
    (checked before the lookup, since get_profile always synthesizes it)."""
    if name == "generalist":
        return "generalist cannot be retired (the fail-open default must exist)"
    if store.get_profile(name) is None:
        return f"no such profile {name!r}"
    return None
