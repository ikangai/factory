"""Spec-driven pre-dispatch scope check — a GSD (spec-driven development) integration.

Before a developer super-worker is spent on a claimed task, judge whether the brief is ONE
bounded, landable, testable change on a single surface. Verdict: pass | split | reject.
This attacks the factory's #1 failure (no_candidate from over-bundled briefs) UPSTREAM,
where rejection is free — no clone, no worker, no wasted shift.

Fail-OPEN by design: a malformed verdict or a judge exception resolves to `pass`. A scope
checker that hiccups must never halt real work — at worst it lets an over-broad brief
through (today's behavior), it never blocks a good one.

The store writes (block / add sub-tasks) run on the caller's MAIN thread, before the worker
ThreadPoolExecutor, so the single-writer SQLite invariant holds.

design: docs/plans/2026-06-27-gsd-spec-driven-integration.md
"""
from __future__ import annotations

import os
import uuid

DECISIONS = ("pass", "split", "reject")


def _target_root() -> str:
    """Resolve the TARGET repo's root via the adapter (as develop.py does), so the judges
    Read/Grep the codebase they are judging — the prompt says "you are looking at the target
    repo", and until Task 0.3 both judges ran against the factory instead. The judges are
    Read/Grep/Glob-only, so grounding them in the operator's checkout is safe. Fail-open to
    FACTORY_ROOT when the adapter/config can't resolve (or the root doesn't exist on disk):
    a mis-grounded judge beats a dead one."""
    from ..common import config, paths
    try:
        root = os.path.abspath(config.get_adapter().entry()[0])
        if os.path.isdir(root):
            return root
    except Exception:  # noqa: BLE001 — fail open
        pass
    return str(paths.FACTORY_ROOT)


def _ledger_judge_spend(store, raw, role: str, notes: str, shift_id) -> None:
    """Ledger the spend an injected judge/decomposer reported under a reserved `_spend`
    key (Task 0.5). No-op when the store is absent or the judge reported no spend — the
    two judges lack a store handle, so they hand it up here where one is in scope."""
    if store is None or not (isinstance(raw, dict) and raw.get("_spend")):
        return
    sp = raw["_spend"]
    store.add_budget(role, int(sp.get("tokens") or 0), float(sp.get("cost") or 0.0),
                     notes=notes, shift_id=shift_id, seconds=float(sp.get("seconds") or 0.0))


def normalize_verdict(raw) -> dict:
    """Coerce a judge's raw output into {decision, reason, spec, subtasks}. Anything invalid
    → `pass` (fail-open): an unknown decision, a non-dict, or a `split` with no usable
    sub-tasks never becomes a block."""
    if not isinstance(raw, dict):
        return {"decision": "pass", "reason": "", "spec": {}, "subtasks": []}
    decision = raw.get("decision")
    if decision not in DECISIONS:
        decision = "pass"
    spec = raw.get("spec") if isinstance(raw.get("spec"), dict) else {}
    subtasks = [s for s in (raw.get("subtasks") or [])
                if isinstance(s, dict) and (s.get("title") or "").strip()]
    if decision == "split" and not subtasks:      # a split with nothing to split into is a no-op
        decision = "pass"
    return {"decision": decision, "reason": (raw.get("reason") or "").strip(),
            "spec": spec, "subtasks": subtasks}


def prefilter(store, tasks: list[dict], *, shift_id, judge) -> list[dict]:
    """Judge each claimed task; enact reject/split via the store; return the `pass` tasks
    (each with its normalized `spec` attached) to dispatch. `judge(task) -> raw_verdict` is
    injected (production: one cheap LLM call). Fail-open per task."""
    from . import factory_memory
    keep: list[dict] = []
    for t in tasks:
        try:
            raw = judge(t)
        except Exception:                          # noqa: BLE001 — a judge error must not block work
            raw = {}
        _ledger_judge_spend(store, raw, "scope_check", "scope judge", shift_id)
        v = normalize_verdict(raw)                  # {} → pass (fail-open)

        if v["decision"] == "reject":
            reason = v["reason"] or "not a single bounded, landable change"
            store.set_task_status(t["id"], "blocked",
                                  result=f"scope-reject: {reason}"[:200], shift_id=shift_id)
            factory_memory.record_learning(
                store, "factory", f"a task was scope-rejected before dispatch — {reason}",
                scope="scope_check", shift_id=shift_id)
        elif v["decision"] == "split":
            n = len(add_subtasks(store, v["subtasks"]))   # source='worker' + spec folded into detail
            store.set_task_status(
                t["id"], "blocked",
                result=f"scope-split into {n}: {v['reason']}"[:200],
                shift_id=shift_id)
            factory_memory.record_learning(
                store, "factory",
                "an over-bundled brief was split before dispatch — write tasks as ONE bounded "
                "change so they don't need splitting",
                scope="scope_check", shift_id=shift_id)
        else:                                      # pass → dispatch, carrying the sharpened spec
            if v["spec"]:                          # persist it (GSD #2 typed column) — durable
                store.set_task_spec(t["id"], v["spec"])
            kept = dict(t)
            kept["spec"] = v["spec"] or t.get("spec") or {}   # don't clobber a durable authored spec
            keep.append(kept)
    return keep


def spec_brief(spec: dict) -> str:
    """Render a normalized spec as a compact contract to append to the developer's brief
    ("" when empty), so a passed task hands the worker a sharper target than free text."""
    if not isinstance(spec, dict) or not spec:
        return ""
    bits = []
    if spec.get("target_surface"):
        bits.append(f"Target surface (stay within this): {spec['target_surface']}")
    if spec.get("acceptance"):
        bits.append(f"Acceptance (prove this): {spec['acceptance']}")
    if spec.get("out_of_scope"):
        bits.append(f"Out of scope: {spec['out_of_scope']}")
    return ("\n\nSCOPE CONTRACT (from the pre-dispatch check):\n" + "\n".join(f"- {b}" for b in bits)
            if bits else "")


def is_spec_complete(spec) -> bool:
    """A task spec is 'complete' when it names BOTH a single target_surface and an acceptance
    (the observable that proves done) — the minimum contract for a bounded, verifiable task."""
    return bool(isinstance(spec, dict) and (spec.get("target_surface") or "").strip()
                and (spec.get("acceptance") or "").strip())


def spec_detail_suffix(spec) -> str:
    """Render a spec as a suffix to fold into a task's `detail` at authorship ("" when empty),
    so target_surface + acceptance travel with the task to the developer + the scope check."""
    if not isinstance(spec, dict):
        return ""
    bits = []
    if spec.get("target_surface"):
        bits.append(f"Target surface: {spec['target_surface']}")
    if spec.get("acceptance"):
        bits.append(f"Acceptance: {spec['acceptance']}")
    if spec.get("out_of_scope"):
        bits.append(f"Out of scope: {spec['out_of_scope']}")
    return ("\n\nSPEC:\n" + "\n".join(f"- {b}" for b in bits)) if bits else ""


def _within_surface(path: str, surface: str) -> bool:
    """Is `path` within the declared `surface`? Matches by PATH COMPONENT, not bare substring
    (so 'api.py' does not match 'rapid_api.py'). A surface naming a dir/path must appear as a
    component; a bare filename matches by basename (allowing an extensionless surface, e.g.
    'llm' → 'llm.py')."""
    p = (path or "").lower().replace("\\", "/")
    s = (surface or "").lower().strip().strip("/")
    if not s:
        return True
    if "/" in s:                                   # surface names a dir/path → a path component
        return p == s or p.endswith("/" + s) or ("/" + s + "/") in p or p.startswith(s + "/")
    base, sbase = p.rsplit("/", 1)[-1], s.rsplit("/", 1)[-1]
    return base == sbase or base.startswith(sbase + ".")


def spec_fulfillment(spec, changed_paths) -> tuple[bool, str]:
    """Did the delivered diff stay within the spec's declared `target_surface`? Returns
    (matched, reason). A SOURCE path outside the declared surface is spec-creep — a signal the
    target_surface estimate was too narrow. Test/docs paths can't stray. No declared surface →
    matched (nothing to check). Feeds self-tuning learnings back to the factory."""
    surface = (spec.get("target_surface") if isinstance(spec, dict) else "") or ""
    surface = surface.strip()
    if not surface:
        return True, ""
    from . import acceptance                            # reuse the source/test classifier
    stray = [p for p in (changed_paths or [])
             if acceptance._is_source(p) and not _within_surface(p, surface)]
    if stray:
        return False, f"declared target_surface '{surface}' but the diff also touched {stray[:3]}"
    return True, ""


def add_subtasks(store, subtasks, *, milestone_id=None) -> list[str]:
    """Add the titled sub-tasks as OPEN tasks (source='worker' to satisfy the tasks.source
    CHECK) with each one's spec folded into its detail. Returns the list of NEW task ids (Task
    5.2 threads them into the bounded second wave). When `milestone_id` is given each sub-task
    INHERITS it — so wave-2 sub-tasks stay plan-linked to their parent's milestone and
    EVM/timesheet attribution survives the rail claiming the tasks itself. Shared by the
    scope-check `split` path and no_candidate decomposition."""
    ids: list[str] = []
    for s in (subtasks if isinstance(subtasks, list) else []):   # a non-list (LLM drift) → []
        if not (isinstance(s, dict) and (s.get("title") or "").strip()):
            continue
        spec = {k: v for k, v in {                               # drop blank fields → {} not {"":""}
            "target_surface": s.get("target_surface", ""), "acceptance": s.get("acceptance", ""),
            "out_of_scope": s.get("out_of_scope", "")}.items() if v}
        detail = (s.get("detail", "") or "").strip() + spec_detail_suffix(spec)
        tid = f"task-{uuid.uuid4().hex[:8]}"
        store.add_task(tid, s["title"].strip(),
                       source="worker", detail=detail, spec=spec)   # typed spec (GSD #2)
        if milestone_id is not None:                             # Task 5.2: inherit the plan link
            store.set_task_milestone(tid, milestone_id)
        ids.append(tid)
    return ids


def decompose_no_candidate(store, task: dict, *, shift_id, decomposer) -> list[str]:
    """A worker returned no_candidate (the brief was too big to land). Split it into a
    sequenced chain of single-surface sub-tasks (open, source='worker') and return their NEW
    ids — turning the failure into forward progress AND handing the ids to Task 5.2's bounded
    second wave. The sub-tasks inherit the parent's milestone_id (plan-link). `decomposer(task)
    -> raw {subtasks:…}` is injected (production: one LLM call). Returns [] on a decomposer error
    or empty result, so the caller falls back to the canned no_candidate lesson. Fail-safe."""
    try:
        raw = decomposer(task)
    except Exception:                                  # noqa: BLE001 — never block the close-out
        return []
    _ledger_judge_spend(store, raw, "decompose", "decompose judge", shift_id)
    ids = add_subtasks(store, raw.get("subtasks") if isinstance(raw, dict) else None,
                       milestone_id=task.get("milestone_id"))
    if ids:
        from . import factory_memory
        factory_memory.record_learning(
            store, "factory",
            "a no_candidate brief was auto-decomposed into smaller single-surface sub-tasks — "
            "write tasks that small up front to skip the wasted worker run",
            scope="decompose", shift_id=shift_id)
    return ids


def decompose_judge(task: dict, *, as_user=None, claude_bin: str = "claude"):
    """Production decomposer: one LLM call over roles/decompose/prompt.md → a raw dict with a
    `subtasks` chain. Returns {} on any failure so decompose_no_candidate falls back."""
    from ..roles import common
    from ..common import config
    sw = config.load_config().get("super_worker", {}) or {}
    text = task.get("title", "") + ((": " + task["detail"]) if task.get("detail") else "")
    prompt = common._load_prompt("decompose").replace("{TASK}", text)
    import time
    t0 = time.monotonic()
    try:
        reply, t, c = common.claude_super(
            prompt, workdir=_target_root(), allowed_tools=("Read", "Grep", "Glob"),
            as_user=as_user, claude_bin=claude_bin, settings=sw.get("settings", "user"),
            max_turns=int(sw.get("decompose_max_turns", 8)),
            timeout=int(sw.get("decompose_timeout_s", 240)))
        obj = common._parse_obj(reply)
        obj = obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001 — fall back
        return {}
    obj["_spend"] = {"tokens": int(t or 0), "cost": float(c or 0.0),   # ledgered by the call site
                     "seconds": round(time.monotonic() - t0, 1)}       # real duration (not 0 min)
    return obj


def scope_judge(task: dict, *, as_user=None, claude_bin: str = "claude"):
    """Production judge: one cheap LLM call over roles/scope_check/prompt.md → a raw verdict
    dict (parsed). Returns {} on any failure so prefilter fails open."""
    from ..roles import common
    from ..common import config
    sw = config.load_config().get("super_worker", {}) or {}
    text = task.get("title", "") + ((": " + task["detail"]) if task.get("detail") else "")
    prompt = common._load_prompt("scope_check").replace("{TASK}", text)
    import time
    t0 = time.monotonic()
    try:
        reply, t, c = common.claude_super(
            prompt, workdir=_target_root(), allowed_tools=("Read", "Grep", "Glob"),
            as_user=as_user, claude_bin=claude_bin, settings=sw.get("settings", "user"),
            max_turns=int(sw.get("scope_check_max_turns", 6)),
            timeout=int(sw.get("scope_check_timeout_s", 180)),
            # Task 2.4 cheap-grader knob: '' = today's frontier (byte-for-byte). resolve_model
            # fails open DOWNWARD to `standard` on an unknown tier — never silently up to frontier.
            # decompose_judge is a SEPARATE call path and is deliberately NOT threaded (spec: scope
            # only). config.yaml-only (no store handle here), so it stays out of SETTINGS_SPEC.
            model=config.resolve_model(sw.get("scope_check_tier") or ""))
        obj = common._parse_obj(reply)
        obj = obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001 — fail open
        return {}
    obj["_spend"] = {"tokens": int(t or 0), "cost": float(c or 0.0),   # ledgered by the call site
                     "seconds": round(time.monotonic() - t0, 1)}       # real duration (not 0 min)
    return obj
