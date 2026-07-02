"""Executive-summary generator (presentation layer).

`generate_executive_summary(store, *, since=None, mission=None) -> str`

GATHERS deterministic, read-only state from the blackboard + filesystem, then asks
an ISOLATED `claude -p` (the Presenter role) to WRITE a short plain-language
summary with EXACTLY three sections: `## Discoveries`, `## Decisions`,
`## Proposed next steps`. The LLM is grounded ONLY in the gathered data.

Guarantees:
  * read-only — never writes to the store, never promotes (no add_promotion /
    set_champion / set_stage call anywhere here);
  * never crashes — if `claude_p` errors/returns empty/omits a required section,
    we fall back to a deterministic templated summary built from the gathered data;
  * no Date.now-style calls in this LIBRARY module — `since`/`window` is passed in
    by the caller (the command/loop layer owns the real clock for filenames).
"""
from __future__ import annotations

import glob
import json
import os
from typing import Any, Optional

import yaml

from ..common import paths
# Import the MODULE (not the bound function) so tests that monkeypatch
# `factory.roles.common.claude_p` are honoured at call time.
from ..roles import common as roles_common

REQUIRED_SECTIONS = ("## Discoveries", "## Decisions", "## Proposed next steps")

# Candidate stages that represent a settled "decision" (cleared vs failed the gate).
_GATE_CLEARED = "awaiting_gate"
_GATE_FAILED = "rejected"


# ---------------------------------------------------------------------------
# data gathering (deterministic, read-only)
# ---------------------------------------------------------------------------
def _safe_json(blob: Any) -> dict:
    if isinstance(blob, dict):
        return blob
    try:
        return json.loads(blob or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _gate_deltas(scores: dict) -> dict:
    """Pull the human-relevant numbers a digest stamped onto a candidate's scores."""
    out = {k: scores.get(k) for k in ("working_set", "held_out", "n_runs") if k in scores}
    div = scores.get("divergence")
    if isinstance(div, dict):
        out["working_delta"] = div.get("working_delta")
        out["held_delta"] = div.get("held_delta")
        out["divergence_alarm"] = bool(div.get("alarm"))
    return out


def _gather_runs(store) -> dict:
    """Recent pass/fail tally per (scenario, model) and per outcome — the ground
    truth of what reality said this window. Working partition only mirrors the
    proposer's view; we report all partitions but label them."""
    rows = store.all_runs()
    by_outcome: dict[str, int] = {}
    by_scenario_model: dict[str, dict[str, int]] = {}
    for r in rows:
        oc = r.get("outcome", "?")
        by_outcome[oc] = by_outcome.get(oc, 0) + 1
        key = f"{r.get('scenario_id','?')} @ {r.get('model','?')}"
        cell = by_scenario_model.setdefault(key, {})
        cell[oc] = cell.get(oc, 0) + 1
    return {
        "total": len(rows),
        "by_outcome": by_outcome,
        "by_scenario_and_model": by_scenario_model,
    }


def _gather_awaiting_gate(store) -> list[dict]:
    """Candidates that cleared the rule and now await the human — these ARE the
    live decisions pending. Carry the change summary + the digest's key scores."""
    out = []
    for c in store.list_candidates(_GATE_CLEARED):
        scores = _safe_json(c.get("scores_json"))
        out.append({
            "id": c["id"],
            "change_summary": c.get("change_summary", ""),
            "parent": c.get("parent", ""),
            "deltas": _gate_deltas(scores),
            "digest_path": scores.get("digest_path", ""),
        })
    return out


def _gather_recent_decisions(store) -> dict:
    """What cleared / failed the gate this run, plus any human promotion records."""
    cleared = [{"id": c["id"], "change_summary": c.get("change_summary", "")}
               for c in store.list_candidates(_GATE_CLEARED)]
    failed = [{"id": c["id"], "change_summary": c.get("change_summary", "")}
              for c in store.list_candidates(_GATE_FAILED)]
    promotions = []
    try:
        for p in store.promotions()[:10]:
            promotions.append({"candidate_id": p.get("candidate_id"),
                               "decision": p.get("decision"),
                               "operator": p.get("operator"),
                               "rationale": (p.get("rationale", "") or "")[:160]})
    except Exception:  # noqa: BLE001 — promotions are best-effort context
        promotions = []
    return {"cleared_gate": cleared, "failed_gate": failed,
            "human_promotion_records": promotions}


def _load_yaml(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return {}


def gather_research_briefs() -> list[dict]:
    """Staged research briefs = DISCOVERIES from papers/repos. Title + one-line
    technique + citation, for the human to vet. Read-only from the filesystem.
    (Public: the board's Research tab imports this — Task 7.5.)"""
    out = []
    for path in sorted(glob.glob(os.path.join(paths.RESEARCH_STAGING_DIR, "*.yaml"))):
        b = _load_yaml(path)
        if not b.get("title") and not b.get("suggested_change"):
            continue  # skip empty/placeholder stubs
        out.append({
            "id": b.get("id", os.path.basename(path)[:-5]),
            "title": b.get("title", "(untitled)"),
            "technique": b.get("technique", ""),
            "suggested_change": (b.get("suggested_change", "") or "").strip(),
            "applies_to": b.get("applies_to", ""),
            "citation": b.get("url") or b.get("arxiv_id", ""),
            "provenance_warning": b.get("provenance_warning", ""),
        })
    return out


_gather_research_briefs = gather_research_briefs   # back-compat alias (internal callers)


def _gather_mined_scenarios() -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(paths.STAGING_DIR, "*.yaml"))):
        sc = _load_yaml(path)
        if not sc.get("goal"):
            continue
        out.append({
            "id": sc.get("id", os.path.basename(path)[:-5]),
            "goal": (sc.get("goal", "") or "")[:200],
            "class": sc.get("class", "single"),
            "has_check": bool((sc.get("check", "") or "").endswith(".py")),
        })
    return out


def gather_summary_data(store, *, since: Optional[str] = None,
                        mission: Optional[str] = None) -> dict:
    """Collect everything the presenter needs, deterministically + read-only.

    Exposed separately from the LLM call so tests can assert on what was gathered
    and the deterministic fallback can reuse it.
    """
    champ = store.get_champion()
    budget = store.budget_totals()
    return {
        "mission": mission or "(no mission stated)",
        "window": since or "(full run)",
        "champion": {"id": champ["id"] if champ else None,
                     "scores": _safe_json(champ.get("scores_json")) if champ else {}},
        "runs": _gather_runs(store),
        "awaiting_gate": _gather_awaiting_gate(store),
        "recent_decisions": _gather_recent_decisions(store),
        "discoveries": {
            "research_briefs": _gather_research_briefs(),
            "mined_scenarios": _gather_mined_scenarios(),
        },
        "budget": {"tokens": int(budget.get("tokens", 0) or 0),
                   "cost_usd": round(float(budget.get("cost", 0) or 0), 4)},
    }


# ---------------------------------------------------------------------------
# LLM presentation + deterministic fallback
# ---------------------------------------------------------------------------
def _looks_like_summary(text: str) -> bool:
    """A usable LLM reply must be non-empty, not an isolation-transport error, and
    contain all three required sections."""
    if not text or not text.strip():
        return False
    low = text.strip()
    if low.startswith("[claude -p"):  # transport error sentinel from claude_p
        return False
    return all(sec.lower() in low.lower() for sec in REQUIRED_SECTIONS)


def _build_prompt(data: dict) -> str:
    return (roles_common._load_prompt("presenter")
            + "\n\n## GATHERED DATA (authoritative — ground yourself ONLY in this)\n"
            + "```json\n" + json.dumps(data, indent=2, default=str) + "\n```\n")


def deterministic_summary(data: dict) -> str:
    """A plain templated summary built straight from the gathered data — used when
    the LLM is unavailable or returns something unusable. Never invents."""
    lines: list[str] = []
    lines.append(f"# Executive summary — autonomous run")
    lines.append("")
    lines.append(f"- Mission: {data.get('mission')}")
    lines.append(f"- Window: {data.get('window')}")
    b = data.get("budget", {})
    lines.append(f"- Spend: {b.get('tokens', 0)} tokens (${b.get('cost_usd', 0)})")
    lines.append("")

    # --- Discoveries ---
    lines.append("## Discoveries")
    briefs = data["discoveries"]["research_briefs"]
    mined = data["discoveries"]["mined_scenarios"]
    if briefs:
        lines.append(f"{len(briefs)} staged research brief(s) (from papers/repos):")
        for x in briefs:
            cite = f" [{x['citation']}]" if x.get("citation") else ""
            tech = x.get("technique") or x.get("suggested_change") or ""
            warn = "  ⚠ unverified citation" if x.get("provenance_warning") else ""
            lines.append(f"- **{x['title']}**{cite}: {tech}{warn}")
    else:
        lines.append("No new research briefs this window.")
    if mined:
        lines.append("")
        lines.append(f"{len(mined)} staged mined scenario(s) awaiting vetting:")
        for x in mined:
            chk = "check ready" if x["has_check"] else "needs synth-check"
            lines.append(f"- `{x['id']}` ({x['class']}, {chk}): {x['goal']}")
    runs = data.get("runs", {})
    if runs.get("total"):
        oc = ", ".join(f"{k}: {v}" for k, v in sorted(runs["by_outcome"].items()))
        lines.append("")
        lines.append(f"Evaluation runs this window: {runs['total']} ({oc}).")

    # --- Decisions ---
    lines.append("")
    lines.append("## Decisions")
    gate = data.get("awaiting_gate", [])
    if gate:
        lines.append("Awaiting the human at the gate (nothing promoted automatically):")
        for c in gate:
            d = c.get("deltas", {})
            wd = d.get("working_delta")
            wd_s = f" working Δ={wd:+.3f}" if isinstance(wd, (int, float)) else ""
            alarm = "  ⚠ divergence alarm" if d.get("divergence_alarm") else ""
            lines.append(f"- **{c['id']}** — {c.get('change_summary', '')}{wd_s}{alarm}")
    else:
        lines.append("No candidates are awaiting the human gate.")
    dec = data.get("recent_decisions", {})
    failed = dec.get("failed_gate", [])
    if failed:
        lines.append("")
        lines.append(f"Failed the gate (rejected): {', '.join(c['id'] for c in failed)}.")
    proms = dec.get("human_promotion_records", [])
    if proms:
        lines.append("")
        lines.append("Human gate decisions on record: "
                     + ", ".join(f"{p['candidate_id']}→{p['decision']}" for p in proms) + ".")

    # --- Proposed next steps ---
    lines.append("")
    lines.append("## Proposed next steps")
    steps: list[str] = []
    if gate:
        steps.append(f"Review {', '.join(c['id'] for c in gate)} at the board and "
                     "decide promote/reject (the one human lever).")
    if briefs:
        steps.append(f"Vet the {len(briefs)} staged research brief(s); promising ones "
                     "feed the Proposer.")
    if mined:
        needs = [x['id'] for x in mined if not x['has_check']]
        if needs:
            steps.append(f"Run synth-check + review for mined scenarios: "
                         f"{', '.join(needs)}.")
        steps.append("Vet + promote mined scenarios into the corpus as appropriate.")
    if not gate and not briefs and not mined:
        steps.append("No pending human actions; consider running `mine`/`research` "
                     "to surface new direction, or widen the working set.")
    for s in steps:
        lines.append(f"- {s}")

    lines.append("")
    lines.append("_Nothing was promoted automatically. Promotion stays a human "
                 "action at the board._")
    return "\n".join(lines)


def generate_executive_summary(store, *, since: Optional[str] = None,
                               mission: Optional[str] = None) -> str:
    """Gather state + ask the Presenter to write a 3-section executive summary.

    Returns markdown. Falls back to a deterministic templated summary (never
    crashes) if the LLM call errors, returns empty, or omits a required section.
    """
    data = gather_summary_data(store, since=since, mission=mission)

    prompt = _build_prompt(data)
    try:
        reply, tokens, cost = roles_common.claude_p(prompt)
    except Exception:  # noqa: BLE001 — presentation must never crash a run
        reply, tokens, cost = "", 0, 0.0

    # Record the presenter's own spend (best-effort; not a hard requirement).
    if tokens or cost:
        try:
            store.add_budget("presenter", int(tokens), float(cost), notes="report")
        except Exception:  # noqa: BLE001
            pass

    if _looks_like_summary(reply):
        return reply.strip()
    return deterministic_summary(data)
