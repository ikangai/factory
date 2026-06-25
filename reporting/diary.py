"""Development-diary generator (presentation layer).

`generate_diary_entry(store, *, since=None, mission=None) -> (slug, markdown)`

The factory narrates its own autonomous work as a first-person dev-diary entry in
the diary skill's voice (past tense, flowing prose — NO headers, NO bullets), so the
work of all its `claude -p` role instances is captured the way a developer writes it
up. The caller owns the clock and writes `.dev-diary/<date>-<slug>.md`.

Mirrors `reporting/summary.py`:
  * reuses the same deterministic, read-only `gather_summary_data`;
  * asks an ISOLATED `claude -p` (the Diarist role) to write the entry;
  * never crashes — on an LLM error/empty/transport-sentinel reply it falls back to
    a deterministic first-person paragraph built straight from the gathered data
    (still voice-compliant: no headers, no bullets);
  * read-only — never writes to the store, never promotes.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

from ..roles import common as roles_common
from .summary import gather_summary_data

# Transport-error sentinel from claude_p — an unusable reply, fall back.
_SENTINEL = "[claude -p"


def _slugify(text: str, *, default: str = "factory-autonomous-run",
             max_words: int = 5) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    words = [w for w in s.split("-") if w][:max_words]
    # Bound the filename component: a separator-free token must not produce a
    # 200+ char filename that overruns the OS path-component limit.
    return ("-".join(words) or default)[:80].strip("-") or default


def _build_prompt(data: dict) -> str:
    return (roles_common._load_prompt("diarist")
            + "\n\n## GATHERED DATA (authoritative — ground yourself ONLY in this)\n"
            + "```json\n" + _json(data) + "\n```\n")


def _json(data: dict) -> str:
    import json
    return json.dumps(data, indent=2, default=str)


def _parse(reply: str) -> Optional[Tuple[str, str]]:
    """Pull (slug, body) from a usable Diarist reply: the first line is
    `slug: <words>`; the rest is the entry. Returns None if the reply is unusable
    (empty or the transport sentinel)."""
    if not reply or not reply.strip():
        return None
    if reply.strip().startswith(_SENTINEL):
        return None
    lines = reply.strip().splitlines()
    slug = ""
    body_lines = lines
    m = re.match(r"\s*slug:\s*(.+)\s*$", lines[0], re.IGNORECASE)
    if m:
        slug = _slugify(m.group(1))
        body_lines = lines[1:]
    body = "\n".join(body_lines).strip()
    if not body:
        return None
    # Voice gate: the diary forbids headers/bullets. If the LLM emitted any, reject
    # the reply so we fall back to the (voice-compliant) deterministic entry — a
    # factory diary entry must always pass the diary skill's linter.
    if re.search(r"(?m)^\s*#{1,6}\s", body) or re.search(r"(?m)^\s*[-*]\s", body):
        return None
    return (slug or _slugify(body[:60]), body)


def deterministic_diary(data: dict) -> Tuple[str, str]:
    """A first-person paragraph built straight from the gathered data — used when the
    LLM is unavailable. Voice-compliant: one flowing piece, no headers, no bullets,
    never invents."""
    mission = data.get("mission") or "(no mission stated)"
    window = data.get("window") or "this window"
    runs = data.get("runs", {}) or {}
    total = int(runs.get("total", 0) or 0)
    passed = int((runs.get("by_outcome", {}) or {}).get("pass", 0) or 0)
    briefs = data.get("discoveries", {}).get("research_briefs", []) or []
    mined = data.get("discoveries", {}).get("mined_scenarios", []) or []
    gate = data.get("awaiting_gate", []) or []
    failed = (data.get("recent_decisions", {}) or {}).get("failed_gate", []) or []

    parts = [f"On {window} I ran an autonomous session toward the mission: {mission}."]
    if total:
        parts.append(f"I evaluated the champion across {total} run(s), {passed} passing.")
    else:
        parts.append("I ran no evaluations this window.")
    if briefs or mined:
        parts.append(f"I surfaced {len(briefs)} research brief(s) and "
                     f"{len(mined)} mined scenario(s) as new direction.")
    else:
        parts.append("I surfaced no new research or scenarios this window.")
    if gate:
        ids = ", ".join(c.get("id", "?") for c in gate)
        parts.append(f"{len(gate)} candidate(s) cleared the rule and now wait for the "
                     f"human at the gate ({ids}); I left them there.")
    elif failed:
        parts.append(f"{len(failed)} candidate(s) failed the gate and were rejected.")
    else:
        parts.append("No candidate cleared the gate this window.")
    parts.append("Nothing was promoted — that stays a human decision at the board.")
    parts.append("The run kept finding work without crossing the promotion line, which "
                 "is the boundary that keeps the factory safe to leave running.")
    body = " ".join(parts)
    return (_slugify(f"autonomous-run-{window}"), body)


def generate_diary_entry(store, *, since: Optional[str] = None,
                         mission: Optional[str] = None) -> Tuple[str, str]:
    """Gather state + ask the Diarist to write a first-person entry. Returns
    (slug, markdown_body). Falls back to a deterministic paragraph (never crashes)."""
    data = gather_summary_data(store, since=since, mission=mission)

    try:
        reply, tokens, cost = roles_common.claude_p(_build_prompt(data))
    except Exception:  # noqa: BLE001 — presentation must never crash a run
        reply, tokens, cost = "", 0, 0.0

    if tokens or cost:
        try:
            store.add_budget("diarist", int(tokens), float(cost), notes="diary")
        except Exception:  # noqa: BLE001
            pass

    parsed = _parse(reply)
    if parsed:
        return parsed
    return deterministic_diary(data)
