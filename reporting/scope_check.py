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

import uuid

DECISIONS = ("pass", "split", "reject")


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
            v = normalize_verdict(judge(t))
        except Exception:                          # noqa: BLE001 — a judge error must not block work
            v = {"decision": "pass", "reason": "", "spec": {}, "subtasks": []}

        if v["decision"] == "reject":
            reason = v["reason"] or "not a single bounded, landable change"
            store.set_task_status(t["id"], "blocked",
                                  result=f"scope-reject: {reason}"[:200], shift_id=shift_id)
            factory_memory.record_learning(
                store, "factory", f"a task was scope-rejected before dispatch — {reason}",
                scope="scope_check", shift_id=shift_id)
        elif v["decision"] == "split":
            n = add_subtasks(store, v["subtasks"])    # source='worker' + spec folded into detail
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
            kept["spec"] = v["spec"]
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
    p = (path or "").lower().replace("\\", "/")
    s = (surface or "").lower().strip()
    if not s:
        return True
    return s in p or p.rsplit("/", 1)[-1] == s.rsplit("/", 1)[-1]


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


def add_subtasks(store, subtasks) -> int:
    """Add the titled sub-tasks as OPEN tasks (source='worker' to satisfy the tasks.source
    CHECK) with each one's spec folded into its detail. Returns the count added. Shared by the
    scope-check `split` path and no_candidate decomposition."""
    n = 0
    for s in (subtasks or []):
        if not (isinstance(s, dict) and (s.get("title") or "").strip()):
            continue
        spec = {"target_surface": s.get("target_surface", ""), "acceptance": s.get("acceptance", ""),
                "out_of_scope": s.get("out_of_scope", "")}
        detail = (s.get("detail", "") or "") + spec_detail_suffix(spec)
        store.add_task(f"task-{uuid.uuid4().hex[:8]}", s["title"].strip(),
                       source="worker", detail=detail, spec=spec)   # typed spec (GSD #2)
        n += 1
    return n


def decompose_no_candidate(store, task: dict, *, shift_id, decomposer) -> int:
    """A worker returned no_candidate (the brief was too big to land). Split it into a
    sequenced chain of single-surface sub-tasks (open, source='worker') and return the count
    added — turning the failure into forward progress. `decomposer(task) -> raw {subtasks:…}`
    is injected (production: one LLM call). Returns 0 on a decomposer error or empty result,
    so the caller falls back to the canned no_candidate lesson. Fail-safe."""
    try:
        raw = decomposer(task)
    except Exception:                                  # noqa: BLE001 — never block the close-out
        return 0
    n = add_subtasks(store, raw.get("subtasks") if isinstance(raw, dict) else None)
    if n:
        from . import factory_memory
        factory_memory.record_learning(
            store, "factory",
            "a no_candidate brief was auto-decomposed into smaller single-surface sub-tasks — "
            "write tasks that small up front to skip the wasted worker run",
            scope="decompose", shift_id=shift_id)
    return n


def decompose_judge(task: dict, *, as_user=None, claude_bin: str = "claude"):
    """Production decomposer: one LLM call over roles/decompose/prompt.md → a raw dict with a
    `subtasks` chain. Returns {} on any failure so decompose_no_candidate falls back."""
    from ..roles import common
    from ..common import config, paths
    sw = config.load_config().get("super_worker", {}) or {}
    text = task.get("title", "") + ((": " + task["detail"]) if task.get("detail") else "")
    prompt = common._load_prompt("decompose").replace("{TASK}", text)
    try:
        reply, _t, _c = common.claude_super(
            prompt, workdir=paths.FACTORY_ROOT, allowed_tools=("Read", "Grep", "Glob"),
            as_user=as_user, claude_bin=claude_bin, settings=sw.get("settings", "user"),
            max_turns=int(sw.get("decompose_max_turns", 8)),
            timeout=int(sw.get("decompose_timeout_s", 240)))
        obj = common._parse_obj(reply)
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001 — fall back
        return {}


def scope_judge(task: dict, *, as_user=None, claude_bin: str = "claude"):
    """Production judge: one cheap LLM call over roles/scope_check/prompt.md → a raw verdict
    dict (parsed). Returns {} on any failure so prefilter fails open."""
    from ..roles import common
    from ..common import config, paths
    sw = config.load_config().get("super_worker", {}) or {}
    text = task.get("title", "") + ((": " + task["detail"]) if task.get("detail") else "")
    prompt = common._load_prompt("scope_check").replace("{TASK}", text)
    try:
        reply, _t, _c = common.claude_super(
            prompt, workdir=paths.FACTORY_ROOT, allowed_tools=("Read", "Grep", "Glob"),
            as_user=as_user, claude_bin=claude_bin, settings=sw.get("settings", "user"),
            max_turns=int(sw.get("scope_check_max_turns", 6)),
            timeout=int(sw.get("scope_check_timeout_s", 180)))
        obj = common._parse_obj(reply)
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001 — fail open
        return {}
