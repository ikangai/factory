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
            for sub in v["subtasks"]:                 # source must satisfy the tasks.source CHECK
                store.add_task(f"task-{uuid.uuid4().hex[:8]}", sub["title"].strip(),
                               source="worker", detail=(sub.get("detail") or "").strip())
            store.set_task_status(
                t["id"], "blocked",
                result=f"scope-split into {len(v['subtasks'])}: {v['reason']}"[:200],
                shift_id=shift_id)
            factory_memory.record_learning(
                store, "factory",
                "an over-bundled brief was split before dispatch — write tasks as ONE bounded "
                "change so they don't need splitting",
                scope="scope_check", shift_id=shift_id)
        else:                                      # pass → dispatch, carrying the sharpened spec
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
