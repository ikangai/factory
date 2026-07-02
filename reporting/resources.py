"""Resources view (Task 6.2): the role + worker-profile inventory and the resolved runtime knobs
the board's Resources tab renders. Pure gather — reuses fleet_viz.profiles_compact / live_workers
and timesheets.by_agent (no re-query) plus the resolved settings (store override → config →
default). The role registry keeps the two engines (the conductor loop vs the legacy roles)
visibly distinct so the operator can see what's actually wired."""
from __future__ import annotations

import os

from ..common import config, paths
from . import worker_admin

# The conductor-loop roles (the LIVE engine). transport: 'super' = a full claude_super instance
# (curated tools + acceptEdits in a sandbox); 'isolated' = a one-shot blind claude_p judge.
# model_tier is the DEFAULT tier the role runs on ('per-profile' for the developer).
_LIVE_ROLES = [
    {"name": "conductor",           "dir": "conductor",  "transport": "super",
     "model_tier": "frontier", "src": "roles/conductor/prompt.md"},
    {"name": "developer",           "dir": "developer",  "transport": "super",
     "model_tier": "per-profile", "src": "roles/developer/prompt.md"},
    {"name": "researcher (refill)", "dir": "researcher", "transport": "super",
     "model_tier": "frontier", "src": "roles/researcher/prompt.md"},
    {"name": "scope_check",         "dir": None,         "transport": "isolated",
     "model_tier": "frontier", "src": "reporting/scope_check.py"},
    {"name": "decompose",           "dir": None,         "transport": "isolated",
     "model_tier": "frontier", "src": "reporting/scope_check.py"},
]

_CAP_DEFAULTS = {                        # match cmd_run's hardcoded defaults
    "max_parallel": 3, "max_tasks_per_shift": 3, "refill_threshold": 2,
    "max_profiles": worker_admin.DEFAULT_MAX_PROFILES,
    "scope_check": False, "require_test": False, "auto_decompose": False, "reviewer": False,
}


def _role_row(r: dict) -> dict:
    """A role's inventory row + a wired check: a prompt-dir role is wired iff its prompt.md exists;
    the inline judges (scope_check/decompose) live in reporting/scope_check.py and are always wired."""
    if r["dir"] is None:
        wired = True
    else:
        wired = os.path.exists(os.path.join(paths.ROLES_DIR, r["dir"], "prompt.md"))
    return {"name": r["name"], "transport": r["transport"], "model_tier": r["model_tier"],
            "wired": wired, "prompt_path": r["src"]}


def _legacy_roles() -> list[str]:
    """The OTHER roles/ dirs (the pre-conductor engine), so the two engines stay visibly distinct."""
    live_dirs = {r["dir"] for r in _LIVE_ROLES if r["dir"]}
    out = []
    try:
        for d in sorted(os.listdir(paths.ROLES_DIR)):
            full = os.path.join(paths.ROLES_DIR, d)
            if (os.path.isdir(full) and d not in live_dirs
                    and os.path.exists(os.path.join(full, "prompt.md"))):
                out.append(d)
    except OSError:
        pass
    return out


def _caps(store) -> dict:
    """The resolved runtime knobs, each marked with its source (an override is board-set)."""
    caps = {}
    for key in config.SETTINGS_SPEC:
        leaf = key.split(".", 1)[1]
        val, src = config.resolve_setting(store, key, _CAP_DEFAULTS.get(leaf))
        caps[leaf] = {"value": val, "source": src, "overridden": src == "override"}
    return caps


def resources(store) -> dict:
    from . import fleet_viz, timesheets
    return {
        "roles": [_role_row(r) for r in _LIVE_ROLES],
        "legacy": _legacy_roles(),
        "profiles": fleet_viz.profiles_compact(store),   # bench + outcomes (reuse — no re-query)
        "spend_by_role": timesheets.by_agent(store),     # all-time per-role rollup (reuse)
        "caps": _caps(store),
        "live": fleet_viz.live_workers(),
    }
