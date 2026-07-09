"""The org resolver (design: docs/plans/2026-07-09-self-organizing-factory-design.md §2):
pure store+config reads, no LLM. This is the CODE side of the authority line — the
organizer (Phase 2) proposes a chart, but this module is what actually enforces it and
what dispatch consults. A chartless mission (no active org_charts row) resolves every
knob exactly as `resolve_setting`/`resolve_model` do today: task_params returns an empty
OrgParams, and every per-task stage/tier lookup falls straight through — so Phase 1 lands
with the existing suite passing UNCHANGED as its own regression proof.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..common import config

# Every SETTINGS_SPEC key whose value is a *boolean* — the exact "which pipeline stages
# run for this class of work" surface the design's authority line grants the organizer.
# Derived (not hand-listed) so a future SETTINGS_SPEC edit can never silently drift the
# org chart's authority out of sync with the board's own whitelist. Capacity ints
# (max_parallel, max_tasks_per_shift, refill_threshold, max_profiles) are deliberately
# EXCLUDED — global load management stays operator-owned (design §"Never org-controllable").
ORG_BOOL_KEYS = {key.split(".", 1)[1] for key, kind in config.SETTINGS_SPEC.items() if kind is bool}

# The only tier aliases resolve_model understands. '' and 'frontier' are synonyms (both
# resolve to the account default) but a chart may name either explicitly.
TIER_PALETTE = ("", "frontier", "standard", "fast")

# The pipeline roles whose MODEL TIER the design's authority line grants the organizer
# control over — the worker plus each isolated judge/reviewer role. Any other key in a
# class's `tiers` dict (e.g. 'conductor') reaches past the line and fails validation.
ROLE_TIER_KEYS = ("worker", "scope_judge", "decomposer", "reviewer", "investigator")

_CLASS_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")   # mirrors store._PROFILE_SLUG_RE


def validate_chart(chart, *, max_profiles: int) -> tuple[bool, list[str]]:
    """Enforce the authority line IN CODE (never trust the organizer's own claim of
    compliance). Returns (ok, reasons) — a chart with ANY violation is rejected WHOLESALE
    (fail-closed to current global behavior); `reasons` names every violation found (not
    just the first) so a rejected-chart learning can cite the real problem."""
    reasons: list[str] = []
    if not isinstance(chart, dict):
        return False, ["chart is not a dict"]

    classes = chart.get("classes")
    if not isinstance(classes, list) or not classes:
        return False, ["chart has no classes (at least one is required)"]

    names: list[str] = []
    for c in classes:
        if not isinstance(c, dict):
            reasons.append("a class entry is not a dict")
            continue
        name = c.get("name")
        if isinstance(name, str) and _CLASS_SLUG_RE.match(name):
            names.append(name)
        else:
            reasons.append(f"class name {name!r} is not a valid slug "
                           f"(^[a-z0-9][a-z0-9-]{{1,31}}$)")
            name = name if isinstance(name, str) else "<unnamed>"

        match = c.get("match") if isinstance(c.get("match"), dict) else {}
        any_list = match.get("any")
        if not isinstance(any_list, list) or not any_list or not all(
                isinstance(x, str) for x in any_list):
            reasons.append(f"class {name!r}: match.any must be a non-empty list of strings")

        stages = c.get("stages") if isinstance(c.get("stages"), dict) else {}
        if not isinstance(c.get("stages", {}), dict):
            reasons.append(f"class {name!r}: stages must be a dict")
        for key in stages:
            if key not in ORG_BOOL_KEYS:                      # capacity ints + unknown keys land here
                reasons.append(f"class {name!r}: stage {key!r} is not an org-controllable "
                               f"boolean (capacity ints and unknown keys are never "
                               f"org-controllable — see the design's authority line)")

        tiers = c.get("tiers") if isinstance(c.get("tiers"), dict) else {}
        if not isinstance(c.get("tiers", {}), dict):
            reasons.append(f"class {name!r}: tiers must be a dict")
        for role, tier in tiers.items():
            if role not in ROLE_TIER_KEYS:
                reasons.append(f"class {name!r}: tier role {role!r} is outside the "
                               f"authority line (only {ROLE_TIER_KEYS})")
            if tier not in TIER_PALETTE:
                reasons.append(f"class {name!r}: tier {tier!r} for role {role!r} is not "
                               f"in the palette {TIER_PALETTE}")

    default_class = chart.get("default_class")
    if default_class not in names:
        reasons.append(f"default_class {default_class!r} does not name a defined class "
                       f"({names})")

    bench = chart.get("bench")
    if bench is not None and not isinstance(bench, list):
        reasons.append("bench must be a list")
        bench = []
    bench = bench or []
    if len(bench) > max_profiles:
        reasons.append(f"bench size {len(bench)} exceeds max_profiles {max_profiles}")

    retire = chart.get("retire")
    if retire is not None and not isinstance(retire, list):
        reasons.append("retire must be a list")

    return (len(reasons) == 0), reasons


def classify(chart: dict, task: dict) -> str:
    """First class whose match.any has a case-insensitive substring hit in the task's
    title+detail; else the chart's default_class. Keyword rules only (v1 — the organizer
    WRITES them; no per-task LLM classification, per the design's explicit YAGNI)."""
    text = ((task.get("title") or "") + " " + (task.get("detail") or "")).lower()
    for c in chart.get("classes") or []:
        any_list = (c.get("match") or {}).get("any") or []
        if any(str(s).lower() in text for s in any_list):
            return c.get("name") or (chart.get("default_class") or "")
    return chart.get("default_class") or ""


@dataclass
class OrgParams:
    """One task's resolved chart overrides — the ONE consult point dispatch/gate sites
    thread through. Every field NOT set by the chart falls through to today's
    resolve_setting/resolve_model at the call site; a chartless task is an empty
    OrgParams, i.e. byte-identical to current behavior."""
    stages: dict = field(default_factory=dict)
    tiers: dict = field(default_factory=dict)
    profile: str = ""
    org_class: str = ""


def _active_row(store) -> Optional[dict]:
    """The raw active org_charts row (chart parsed under "chart") for the CURRENT mission,
    falling back to the latest active STANDING chart (mission_id NULL) when there's no
    active mission — shared by get_active_chart (dispatch) and cmd_org show (the CLI,
    which also wants version/rationale/created_by, not just the chart body)."""
    m = store.active_mission()
    row = store.get_active_org_chart(m["id"]) if m else None
    if row is None:
        row = store.get_active_org_chart(None)
    return row


def get_active_chart(store) -> Optional[dict]:
    """The parsed chart document (not the store row) for the CURRENT mission — falls back
    to the latest active STANDING chart (mission_id NULL) when there's no active mission,
    so a mission-less dev/test run can still exercise a hand-authored chart. None when
    nothing is active (the chartless-behavior path)."""
    row = _active_row(store)
    return row["chart"] if row else None


def task_params(store, task: dict, chart: Optional[dict] = None) -> OrgParams:
    """Resolve one task's OrgParams. `chart` may be passed in (dispatch loads it ONCE,
    main thread, per shift) or left None to resolve it here. A task's STAMPED org_class
    wins when it names a class the CURRENT chart still defines; otherwise (blank, or a
    stale class from a superseded chart) classify-on-the-fly from title+detail — never a
    crash, always a valid class or ''."""
    if chart is None:
        chart = get_active_chart(store)
    if not chart:
        return OrgParams()
    org_class = task.get("org_class") or ""
    classes = {c.get("name"): c for c in (chart.get("classes") or []) if isinstance(c, dict)}
    if org_class not in classes:
        org_class = classify(chart, task)
    cls = classes.get(org_class)
    if cls is None:                        # default_class itself undefined (shouldn't happen
        return OrgParams(org_class=org_class)  # post-validation, but never crash a dispatch)
    return OrgParams(stages=dict(cls.get("stages") or {}), tiers=dict(cls.get("tiers") or {}),
                     profile=cls.get("profile") or "", org_class=org_class)


def stage_on(params: OrgParams, store, key: str, default) -> bool:
    """The per-task stage-gate consult point: the chart's override for `key` wins; else
    fall through to today's global resolve_setting. `key` is the SETTINGS_SPEC leaf name
    (e.g. 'scope_check'), not the dotted 'super_worker.scope_check' key."""
    if key in params.stages:
        return bool(params.stages[key])
    return bool(config.resolve_setting(store, f"super_worker.{key}", default)[0])


def fit_rows(store) -> list[dict]:
    """Passthrough to the store's aggregation — kept here so callers (the organizer, the
    CLI) import ONE module for the whole org surface."""
    return store.fit_rows()


def render_fit_table(rows: list[dict]) -> str:
    """Compact text rendering of fit_rows() — the organizer's {FIT} seam (Phase 2) and the
    `factory org fit` CLI (Task 1.4). Empty evidence renders an explicit line rather than a
    blank table, so the organizer's contract ("cite fit rows or state 'no evidence yet'")
    has something concrete to read literally."""
    if not rows:
        return "(no evidence yet — the organizer proceeds on judgment, citing so explicitly)"
    lines = [f"{'class':<20} {'tier':<10} {'attempts':>8} {'done':>5} {'blocked':>7} "
            f"{'top-stage':<12} {'avg-tok':>8}"]
    for r in rows:
        lines.append(
            f"{r['org_class']:<20} {(r['tier'] or '(frontier)'):<10} {r['attempts']:>8} "
            f"{r['done']:>5} {r['blocked']:>7} {(r['top_stage'] or '-'): <12} "
            f"{r['avg_tokens']:>8.0f}")
    return "\n".join(lines)


def cmd_org(store, action: str) -> None:
    """The org chart's read-only CLI surface (Task 1.4):
      factory org show   # the active chart's classes/bench/rationale, or "no active org chart"
      factory org fit    # the rendered fit table (routing_outcomes aggregated by class x tier)
    plan/replan arrive in Phase 2 (the organizer) — YAGNI for now; argparse's choices stay
    exactly {show, fit} until then."""
    if action == "show":
        row = _active_row(store)
        if row is None:
            print("[org] no active org chart")
            return
        chart = row["chart"]
        print(f"[org] chart v{row['version']} (mission {row['mission_id']}, "
              f"{row['created_by']}, default_class={chart.get('default_class', '')})")
        if row.get("rationale"):
            print(f"  rationale: {row['rationale']}")
        for c in chart.get("classes") or []:
            stages = ", ".join(f"{k}={v}" for k, v in (c.get("stages") or {}).items()) or "(none)"
            tiers = ", ".join(f"{k}={v or 'frontier'}" for k, v in (c.get("tiers") or {}).items()) or "(none)"
            print(f"  class {c.get('name')}: profile={c.get('profile') or '(none)'} "
                  f"stages=[{stages}] tiers=[{tiers}]")
        bench = chart.get("bench") or []
        if bench:
            print("  bench: " + ", ".join(b.get("name", "?") for b in bench))
        retire = chart.get("retire") or []
        if retire:
            print("  retire: " + ", ".join(retire))
    elif action == "fit":
        print(render_fit_table(fit_rows(store)))
    else:
        print(f"[org] unknown action {action!r} — use show|fit")
