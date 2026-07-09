"""The org resolver + the organizer (design: docs/plans/2026-07-09-self-organizing-
factory-design.md §2/§3): the resolver half (validate_chart/classify/task_params/
stage_on/fit_rows/render_fit_table) is pure store+config reads, no LLM — the CODE side of
the authority line that actually enforces what the organizer proposes and what dispatch
consults. A chartless mission (no active org_charts row) resolves every knob exactly as
`resolve_setting`/`resolve_model` do today: task_params returns an empty OrgParams, and
every per-task stage/tier lookup falls straight through — so Phase 1 lands with the
existing suite passing UNCHANGED as its own regression proof.

Phase 2 adds the organizer itself (plan_org/maybe_plan_org): ONE isolated, FRONTIER-tier
`claude -p` call (roles/organizer/prompt.md) that proposes a chart from the live backlog +
bench + fit evidence; validate_chart (never the organizer's own claim) decides whether it
applies. Transport/parse/validation failure never half-applies a chart — the pipeline
falls through to today's global behavior, exactly as a chartless mission always has.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..common import config, killswitch

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


def validate_chart(chart, *, max_profiles: int, active_profiles=None) -> tuple[bool, list[str]]:
    """Enforce the authority line IN CODE (never trust the organizer's own claim of
    compliance). Returns (ok, reasons) — a chart with ANY violation is rejected WHOLESALE
    (fail-closed to current global behavior); `reasons` names every violation found (not
    just the first) so a rejected-chart learning can cite the real problem.

    `active_profiles` (Phase 2 integrator-review addition A): the CURRENT active
    worker_profiles names, so a class's `profile` can be checked for self-containment
    against the store, not just the chart's own bench — optional (defaults to none) so
    every Phase-1 caller that validates a chart in isolation (no store handle) keeps
    working unchanged; a chart whose classes only reference their OWN bench still
    validates with no store context at all."""
    reasons: list[str] = []
    if not isinstance(chart, dict):
        return False, ["chart is not a dict"]

    classes = chart.get("classes")
    if not isinstance(classes, list) or not classes:
        return False, ["chart has no classes (at least one is required)"]

    # addition A: bench entries are validated (name/model/description) — and their names
    # collected — BEFORE the classes loop, so a class's `profile` can be checked for
    # self-containment (below) against bench_names ∪ active_profiles in the SAME pass.
    bench = chart.get("bench")
    if bench is not None and not isinstance(bench, list):
        reasons.append("bench must be a list")
        bench = []
    bench = bench or []
    bench_names: set = set()
    for b in bench:
        if not isinstance(b, dict):
            reasons.append("a bench entry is not a dict")
            continue
        bname = b.get("name")
        if isinstance(bname, str) and _CLASS_SLUG_RE.match(bname):
            bench_names.add(bname)
        else:
            reasons.append(f"bench entry name {bname!r} is not a valid slug "
                           f"(^[a-z0-9][a-z0-9-]{{1,31}}$)")
        bmodel = b.get("model")
        if bmodel not in TIER_PALETTE:
            reasons.append(f"bench entry {bname!r}: model {bmodel!r} is not in the "
                           f"palette {TIER_PALETTE}")
        if not isinstance(b.get("description"), str) or not b.get("description", "").strip():
            reasons.append(f"bench entry {bname!r}: description must be a non-empty string")
    if len(bench) > max_profiles:
        reasons.append(f"bench size {len(bench)} exceeds max_profiles {max_profiles}")

    known_profiles = bench_names | set(active_profiles or ())

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

        # addition A: the chart must be SELF-CONTAINED — a class naming a profile that
        # neither an existing active worker_profiles row NOR the chart's own bench will
        # supply is a dangling reference the apply step can't resolve. '' is always valid
        # (the generalist fallback — never a named profile to look up).
        profile = c.get("profile") or ""
        if profile and profile not in known_profiles:
            reasons.append(f"class {name!r}: profile {profile!r} names neither an existing "
                           f"active profile nor a bench entry (the chart must be self-contained)")

    default_class = chart.get("default_class")
    if default_class not in names:
        reasons.append(f"default_class {default_class!r} does not name a defined class "
                       f"({names})")

    retire = chart.get("retire")
    if retire is not None and not isinstance(retire, list):
        reasons.append("retire must be a list")
    elif retire:
        for r in retire:                                       # addition A: every entry a string
            if not isinstance(r, str):
                reasons.append(f"retire entry {r!r} must be a string")

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


# =============================================================================
# The organizer (design §3, Phase 2): ONE isolated, frontier-tier claude_p call that
# PROPOSES a chart; everything above (validate_chart etc.) is what actually enforces it.
# =============================================================================

# The two stage booleans that are structurally GLOBAL — dispatch (develop.py) only
# constructs the scope-judge/decomposer CALLABLE when the shift-level knob already
# resolved on, so a class can NARROW (turn off) but never CONJURE (turn on) either.
_NARROW_ONLY_STAGES = ("scope_check", "auto_decompose")


def _backlog_bullets(store, limit: int = 60) -> str:
    """The {BACKLOG} seam: open tasks id/title/detail, capped — the organizer classifies
    the WHOLE backlog at apply time (below), this is only what it gets to SEE while
    designing the chart (a large backlog would blow the prompt budget for no benefit:
    classes are reusable buckets, not a per-task decision)."""
    tasks = store.list_tasks(status="open")[:limit]
    if not tasks:
        return "(empty — nothing to organize yet)"
    lines = []
    for t in tasks:
        line = f"- {t['id']}: {t['title']}"
        detail = " ".join((t.get("detail") or "").split())[:200]
        if detail:
            line += f" — {detail}"
        lines.append(line)
    return "\n".join(lines)


def _bench_bullets(store) -> str:
    """The {BENCH} seam: the CURRENT active bench, so the organizer knows what already
    exists (and doesn't need to re-list it — the prompt tells it to list only new/changed
    profiles)."""
    profs = store.list_profiles(active_only=True)
    if not profs:
        return "(no active profiles yet — generalist only)"
    return "\n".join(
        f"- {p['name']} [{p.get('model') or 'frontier'}] — {(p.get('description') or '')[:80]}"
        for p in profs)


def _bounds_text(max_profiles: int) -> str:
    """The {BOUNDS} seam (Phase-1 integrator review, addition B): the authority line,
    stated verbatim-clear enough that the model never has to guess where it is. Every
    fact here is asserted literally by tests/test_organizer.py — this text is read by an
    LLM, not executed, so accuracy of WORDING is the whole point."""
    both_ways = sorted(ORG_BOOL_KEYS - set(_NARROW_ONLY_STAGES))
    narrow_only = ", ".join(sorted(_NARROW_ONLY_STAGES))
    both_ways_s = ", ".join(both_ways)
    all_stages = ", ".join(sorted(ORG_BOOL_KEYS))
    tiers_s = ", ".join(t or "''" for t in TIER_PALETTE)
    return (
        f"- Controllable stage booleans (per class, in `stages`): {all_stages}.\n"
        f"  - `{narrow_only}` can only be NARROWED per class (turned OFF where the "
        f"shift-level knob is on) — they can NEVER be turned on when the shift-level knob "
        f"is off, because the judge/decomposer callable simply won't exist to run.\n"
        f"  - `{both_ways_s}` work BOTH ways — a class may force any of these on or off, "
        f"independent of the shift-level default.\n"
        f"- Tiers (per class, in `tiers`, for roles worker/scope_judge/decomposer/"
        f"reviewer/investigator): the palette is {tiers_s} — '' and 'frontier' are "
        f"SYNONYMS (both mean the account's default, most capable model).\n"
        f"- Bench cap: at most {max_profiles} active profiles total (`max_profiles`) — "
        f"retire stale ones to make room; `generalist` cannot be retired.\n"
        f"- PERMANENTLY OUT OF REACH, no matter what: STOP/mode, every brake/budget "
        f"(enforce_shift_budget, loop_*, push_approval, graduation_retest, …), every "
        f"capacity INT (max_parallel, max_tasks_per_shift, refill_threshold, and "
        f"max_profiles itself), frozen paths, the sandbox/toolset boundary, and the human "
        f"promotion gate. A chart that tries to reach any of these is REJECTED WHOLESALE — "
        f"nothing in it applies, not even the parts that were fine.")


def build_organizer_prompt(store, *, mission: Optional[dict], max_profiles: int,
                           fit_text: Optional[str] = None) -> str:
    """Fill roles/organizer/prompt.md's seams from the live store. `fit_text` may be
    passed in (plan_org computes it once, to both prompt AND store as `evidence`) or left
    None to resolve it here."""
    from ..reporting import factory_memory
    from ..roles.common import _load_prompt
    fit_text = fit_text if fit_text is not None else render_fit_table(fit_rows(store))
    mission_text = ((mission or {}).get("statement") or "").strip()
    return (_load_prompt("organizer")
            .replace("{MISSION}", mission_text or "(no active mission — design a standing chart)")
            .replace("{BACKLOG}", _backlog_bullets(store))
            .replace("{BENCH}", _bench_bullets(store))
            .replace("{FIT}", fit_text)
            .replace("{MEMORY}", factory_memory.memory_card(store, "organizer"))
            .replace("{BOUNDS}", _bounds_text(max_profiles)))


def plan_org(store, *, force: bool = False, shift_id: Optional[int] = None,
            claude_fn: Optional[Callable] = None) -> Optional[dict]:
    """Run the organizer and (on a valid, VALIDATED reply) apply its chart. Returns the
    applied chart dict on success, else None (nothing changed — the pipeline falls
    through to today's global behavior, exactly as a chartless mission always has).

    Brakes (every one a MUST, mirroring the investigator's posture):
    `killswitch.is_halted()` is checked FIRST — STOP vetoes even ATTEMPTING the frontier
    call. `force=False` (the default; `factory org plan`'s posture) refuses outright — no
    call, no ledger row — when the mission already has an active chart (`factory org
    replan` passes force=True and always supersedes). A transport/parse failure records a
    factory learning and changes NO chart row. A VALIDATION failure stores the chart with
    status='rejected' (audit trail) plus a learning, but never applies it. Only a chart
    that parses AND validates is applied — atomically, in the design's order: supersede
    the mission's prior active chart, insert the new one, upsert the bench, then classify
    + assign every OPEN task. Spend is ledgered notes='organizer' regardless of outcome
    (a failed/rejected plan still spent real tokens) — WITH `shift_id` when called from
    the shift-start hook, WITHOUT it for a bare CLI invocation (None is the ledger's own
    "no shift" convention, same as `distill_learnings`)."""
    if killswitch.is_halted():                  # STOP vetoes even attempting the frontier call
        return None
    mission = store.active_mission()
    mission_id = mission["id"] if mission else None
    if not force and store.get_active_org_chart(mission_id) is not None:
        return None                              # already has an active chart — replan to force

    if claude_fn is None:                        # deferred import → tests monkeypatch claude_p
        from ..roles.common import claude_p as claude_fn
    from ..reporting import factory_memory, worker_admin
    from ..roles.common import _parse_obj

    max_profiles = int(config.resolve_setting(
        store, "super_worker.max_profiles", worker_admin.max_profiles())[0])
    fit_text = render_fit_table(fit_rows(store))
    prompt = build_organizer_prompt(store, mission=mission, max_profiles=max_profiles,
                                    fit_text=fit_text)
    model = config.resolve_model("")             # the FRONTIER tier — org design is judgment
    text, tokens, cost = claude_fn(prompt, model=model)
    store.add_budget("organizer", int(tokens or 0), float(cost or 0.0),
                     notes="organizer", shift_id=shift_id)

    chart = _parse_obj(text or "")               # strip fences defensively (belt + suspenders —
    if not isinstance(chart, dict):               # the prompt asks for none, but never trust it)
        factory_memory.record_learning(
            store, "factory",
            "the organizer returned unparseable JSON — no chart change; the pipeline falls "
            "through to today's global behavior", scope="organizer", shift_id=shift_id)
        return None

    rationale = str(chart.pop("rationale", "") or "")[:2000]
    active_profiles = {p["name"] for p in store.list_profiles(active_only=True)}
    ok, reasons = validate_chart(chart, max_profiles=max_profiles,
                                 active_profiles=active_profiles)
    if not ok:
        store.add_org_chart(mission_id, chart, rationale=rationale, evidence=fit_text,
                            status="rejected", created_by="organizer")
        factory_memory.record_learning(
            store, "factory",
            f"an organizer chart FAILED validation and was rejected — {'; '.join(reasons)}"[:1000],
            scope="organizer", shift_id=shift_id)
        return None

    # Apply ATOMICALLY, in the design's order: supersede -> chart insert -> bench ->
    # classify+assign. All main-thread store writes (Binding rule) — plan_org is only
    # ever called from the CLI or the shift-start hook, never from a worker thread.
    store.supersede_org_charts(mission_id)
    store.add_org_chart(mission_id, chart, rationale=rationale, evidence=fit_text,
                        status="active", created_by="organizer")
    for b in chart.get("bench") or []:
        if not isinstance(b, dict):
            continue
        store.add_profile(b.get("name"), description=b.get("description") or "",
                          model=b.get("model") or "", overlay=b.get("overlay") or "",
                          created_by="organizer", replace=True)
    for name in chart.get("retire") or []:
        if isinstance(name, str) and name and name != "generalist":  # unretireable fail-open floor
            store.retire_profile(name)

    classes_by_name = {c.get("name"): c for c in chart.get("classes") or [] if isinstance(c, dict)}
    for t in store.list_tasks(status="open"):
        cls_name = classify(chart, t)
        store.set_task_org_class(t["id"], cls_name)
        profile = (classes_by_name.get(cls_name) or {}).get("profile") or ""
        store.set_task_profile(t["id"], profile)

    return chart


def maybe_plan_org(store, *, shift_id: Optional[int] = None,
                   claude_fn: Optional[Callable] = None) -> Optional[dict]:
    """The automatic trigger (design §3, Task 2.2): "a shift starts with a mission that
    has NO chart" — which also covers "the active mission changed" for free, since a new
    mission_id simply has no chart of its own yet either way (`get_active_org_chart` is
    mission-scoped). STOP and "no active mission" are checked HERE, not inside `plan_org`
    (which must keep working mission-less, for a bare `factory org plan` standing-chart
    run) — no call in either case, mirroring the investigator's posture (killswitch
    checked FIRST). Otherwise delegates straight to `plan_org` (force=False), which is
    ITSELF the fast, call-free no-op once a chart exists for the mission — so the
    frontier call happens exactly ONCE per mission, then stays cached; safe to call at
    the top of every single shift."""
    if killswitch.is_halted():
        return None
    if not store.active_mission():
        return None
    return plan_org(store, shift_id=shift_id, claude_fn=claude_fn)


def cmd_org(store, action: str, *, claude_fn: Optional[Callable] = None) -> None:
    """The org chart's CLI surface:
      factory org show    # the active chart's classes/bench/rationale, or "no active org chart"
      factory org fit     # the rendered fit table (routing_outcomes aggregated by class x tier)
      factory org plan    # (Task 2.2) plan once — REFUSES if a chart already exists (points at replan)
      factory org replan  # (Task 2.2) supersede the active chart and plan fresh (force=True)
    `claude_fn` is test-only plumbing (passed straight through to plan_org) — the live CLI
    never supplies it, so plan_org's own default (the real, isolated claude_p) applies."""
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
    elif action == "plan":
        mission = store.active_mission()
        existing = store.get_active_org_chart(mission["id"] if mission else None)
        if existing is not None:
            print(f"[org] an active chart already exists (v{existing['version']}) — "
                  f"use `factory org replan` to supersede it and plan fresh")
            return
        chart = plan_org(store, claude_fn=claude_fn)
        if chart is None:
            print("[org] plan failed (transport/parse/validation) — see "
                  "`factory learn list --role factory` for why")
        else:
            print(f"[org] planned {len(chart.get('classes') or [])} class(es), "
                  f"default_class={chart.get('default_class', '')}")
    elif action == "replan":
        chart = plan_org(store, force=True, claude_fn=claude_fn)
        if chart is None:
            print("[org] replan failed (transport/parse/validation) — see "
                  "`factory learn list --role factory` for why")
        else:
            print(f"[org] replanned {len(chart.get('classes') or [])} class(es), "
                  f"default_class={chart.get('default_class', '')}")
    else:
        print(f"[org] unknown action {action!r} — use show|fit|plan|replan")
