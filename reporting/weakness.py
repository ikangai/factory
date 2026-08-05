"""reporting/weakness.py — deterministic failure-cluster miner (design: docs/plans/
2026-08-05-self-harness-loop-design.md, Component B).

`mine_weaknesses(store, *, window=200)` clusters the factory's OWN failure telemetry —
`task_evidence` (action×stage per failed task), `routing_outcomes`/`fit_rows` (class×tier
blocked rates), `tasks.result` scope-check verdicts, `learnings` proven counterproductive
by their own outcome counters, `gate_eval_results` regressions, and `shifts` terminal
states — into a handful of stable-slug CLUSTERS. Each cluster's `evidence_ids` is the
exact citation VOCABULARY `orchestrator.harness`'s proposer prompt renders and its
validator checks proposal citations against.

Read-only, zero LLM, no store writes — per reporting/__init__'s module contract
("NEVER writes to the store"). Deterministic: same store state -> same clusters.
"""
from __future__ import annotations

import re

# -- cluster thresholds (derived once, documented — never hand-tuned per report) --------
_STAGE_MIN_COUNT = 2            # a stage-failure cluster needs at least this many rows
_MISROUTE_MIN_ATTEMPTS = 5      # a class×tier pairing needs this many attempts to judge
_MISROUTE_BLOCKED_RATE = 0.5    # …and at least this blocked-rate to be a weakness
_MISROUTE_SIBLING_DELTA = 0.3   # …while a sibling tier's blocked-rate is at least this
                                # much lower (i.e. the same class routes better elsewhere)
_SCOPE_CHURN_MIN = 2            # a scope-churn cluster needs at least this many verdicts
_SHIFT_ATTRITION_MIN_SHIFTS = 5 # need at least this many recent shifts to judge a rate
_SHIFT_ATTRITION_RATE = 0.2     # 20%+ of recent shifts ending badly is a weakness

_BAD_TERMINAL = {"halted", "timed_out", "budget_exhausted"}

_EXEMPLAR_CAP = 20   # evidence_ids per cluster — enough to cite, not a prompt-blowing dump


def _slug(*parts: str) -> str:
    s = "-".join(str(p) for p in parts if p)
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:64] or "weakness"


def _stage_failure_clusters(store, window: int) -> list[dict]:
    """task_evidence grouped by (action, stage) — e.g. "12x no_candidate at stage
    refusal". Biggest cluster first."""
    rows = store.recent_task_evidence(limit=window)
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (r.get("action") or "", r.get("stage") or "")
        groups.setdefault(key, []).append(r)
    out = []
    for (action, stage), items in groups.items():
        if len(items) < _STAGE_MIN_COUNT:
            continue
        out.append({
            "id": _slug("stage-failure", action, stage or "none"),
            "kind": "stage-failure",
            "count": len(items),
            "evidence_ids": [f"task_evidence:{r['id']}" for r in items[:_EXEMPLAR_CAP]],
            "summary": f"{len(items)}x {action or '(unknown action)'} at stage "
                      f"{stage or '(none)'}",
        })
    return out


def _class_misroute_clusters(store) -> list[dict]:
    """fit_rows where a class×tier pairing has >= MIN attempts and a blocked-rate high
    enough to call a weakness, while a SIBLING tier of the same class does meaningfully
    better — an org-chart routing mistake, not just a hard class of work."""
    rows = store.fit_rows()
    by_class: dict[str, list[dict]] = {}
    for r in rows:
        by_class.setdefault(r["org_class"], []).append(r)
    out = []
    for cls, tiers in sorted(by_class.items()):
        rated = [(r, (r["blocked"] / r["attempts"]) if r["attempts"] else 0.0)
                for r in tiers if r["attempts"] >= _MISROUTE_MIN_ATTEMPTS]
        if len(rated) < 2:
            continue
        worst = max(rated, key=lambda p: p[1])
        best = min(rated, key=lambda p: p[1])
        if worst[0] is best[0]:
            continue
        wt, wr = worst
        bt, br = best
        if wr >= _MISROUTE_BLOCKED_RATE and (wr - br) >= _MISROUTE_SIBLING_DELTA:
            out.append({
                "id": _slug("class-misroute", cls, wt["tier"] or "frontier"),
                "kind": "class-misroute",
                "count": wt["attempts"],
                "evidence_ids": [f"fit:{cls}/{wt['tier']}", f"fit:{cls}/{bt['tier']}"],
                "summary": (f"{cls}/{wt['tier'] or 'frontier'}: {wr:.0%} blocked over "
                           f"{wt['attempts']} attempts vs {cls}/{bt['tier'] or 'frontier'} "
                           f"at {br:.0%}"),
            })
    return out


def _scope_churn_clusters(store, window: int) -> list[dict]:
    """Tasks whose result starts scope-reject:/scope-split — brief-quality weaknesses,
    grouped by milestone (unassigned tasks group under 'unassigned')."""
    tasks = store.list_tasks()[-window:]
    groups: dict[str, list[dict]] = {}
    for t in tasks:
        result = t.get("result") or ""
        if result.startswith("scope-reject:") or result.startswith("scope-split"):
            key = str(t.get("milestone_id") or "unassigned")
            groups.setdefault(key, []).append(t)
    out = []
    for ms, items in groups.items():
        if len(items) < _SCOPE_CHURN_MIN:
            continue
        out.append({
            "id": _slug("scope-churn", ms),
            "kind": "scope-churn",
            "count": len(items),
            "evidence_ids": [f"task:{t['id']}" for t in items[:_EXEMPLAR_CAP]],
            "summary": f"{len(items)} scope-split/scope-reject verdict(s) in milestone {ms}",
        })
    return out


def _bad_lore_clusters(store, window: int) -> list[dict]:
    """learnings proven counterproductive by their own merged/blocked outcome counters
    (reporting.factory_memory.is_counterproductive) — the ACE-playbook corrective path's
    own evidence source.

    Excludes ARCHIVED and PINNED rows (adversarial-review fix round, BLOCKER 5a): an
    archived row is already retired — mining it forever re-proposes the SAME corrective
    every batch (its counters are frozen; the proposal can never do anything new). A
    pinned row's only legal corrective (`op='archive'`, since `op='pin'` on an already-
    counterproductive row is rejected — see orchestrator.harness.validate_proposals) is
    itself rejected by validate_proposals (a pinned learning is never a valid corrective
    target) — mining it wastes a proposal slot on something guaranteed to fail. Both
    exclusions mean bad-lore only ever surfaces a row a corrective proposal can actually
    act on."""
    from . import factory_memory
    rows = [r for r in store.all_learnings(limit=window)
           if not r.get("archived") and not r.get("pinned")
           and factory_memory.is_counterproductive(r)]
    if not rows:
        return []
    return [{
        "id": "bad-lore",
        "kind": "bad-lore",
        "count": len(rows),
        "evidence_ids": [f"learning:{r['id']}" for r in rows[:_EXEMPLAR_CAP]],
        "summary": f"{len(rows)} learning(s) proven counterproductive by their own "
                   f"outcome counters",
    }]


def _gate_flip_clusters(store, gate: str = "scope") -> list[dict]:
    """gate_eval_results regressions: a case whose LAST run flipped ok(1) -> fail(0)
    versus its previous run."""
    rows = store.all_gate_eval_results(gate=gate, limit=2000)
    by_case: dict[str, list[dict]] = {}
    for r in rows:
        by_case.setdefault(r["case_id"], []).append(r)
    flips = []
    for items in by_case.values():
        if len(items) < 2:
            continue
        items = sorted(items, key=lambda r: r["id"])
        prev, latest = items[-2], items[-1]
        if prev["ok"] and not latest["ok"]:
            flips.append(latest)
    if not flips:
        return []
    return [{
        "id": _slug("gate-flip", gate),
        "kind": "gate-flip",
        "count": len(flips),
        "evidence_ids": [f"gate_eval:{r['id']}" for r in flips[:_EXEMPLAR_CAP]],
        "summary": f"{len(flips)} golden case(s) flipped ok→fail on the {gate} gate",
    }]


def _shift_attrition_clusters(store, window: int) -> list[dict]:
    """Shifts ending halted/timed_out/budget_exhausted, rate over the last `window`
    shifts — a rail-reliability weakness, not a single task's."""
    shifts = store.list_shifts(limit=window)
    if len(shifts) < _SHIFT_ATTRITION_MIN_SHIFTS:
        return []
    bad = [s for s in shifts if s["status"] in _BAD_TERMINAL]
    rate = len(bad) / len(shifts)
    if rate < _SHIFT_ATTRITION_RATE:
        return []
    return [{
        "id": "shift-attrition",
        "kind": "shift-attrition",
        "count": len(bad),
        "evidence_ids": [f"shift:{s['id']}" for s in bad[:_EXEMPLAR_CAP]],
        "summary": (f"{len(bad)}/{len(shifts)} recent shifts ({rate:.0%}) ended "
                   f"halted/timed_out/budget_exhausted"),
    }]


def mine_weaknesses(store, *, window: int = 200) -> list[dict]:
    """Deterministic, zero-LLM failure clustering over the factory's own failure
    telemetry, per the design's six cluster kinds (stage-failure, class-misroute,
    scope-churn, bad-lore, gate-flip, shift-attrition). Each cluster:
    {id, kind, count, evidence_ids, summary} — `id` is a STABLE slug (the
    `{"weakness": "<cluster-slug>"}` field a harness proposal cites), `evidence_ids` is
    the exact vocabulary `orchestrator.harness.validate_proposals` checks proposal
    citations against. Sorted biggest-first (ties broken by id) so the prompt/render lead
    with the loudest signal. Never raises for an empty/thin store — an empty telemetry
    set simply yields no clusters."""
    clusters: list[dict] = []
    clusters += _stage_failure_clusters(store, window)
    clusters += _class_misroute_clusters(store)
    clusters += _scope_churn_clusters(store, window)
    clusters += _bad_lore_clusters(store, window)
    clusters += _gate_flip_clusters(store)
    clusters += _shift_attrition_clusters(store, window)
    clusters.sort(key=lambda c: (-c["count"], c["id"]))
    return clusters


def render_weakness_table(clusters: list[dict]) -> str:
    """Compact, fixed-width rendering (mirrors orchestrator.org.render_fit_table) — the
    harness engineer's {WEAKNESS} prompt seam. Explicit empty state so the prompt
    contract has something concrete to read literally."""
    if not clusters:
        return ("(no weaknesses mined yet — the factory's failure telemetry is too thin "
                "or too clean to cluster)")
    lines = [f"{'id':<36} {'kind':<16} {'count':>5}  summary"]
    for c in clusters:
        lines.append(f"{c['id']:<36} {c['kind']:<16} {c['count']:>5}  {c['summary']}")
    return "\n".join(lines)
