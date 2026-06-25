"""Blog-post generator (presentation layer).

`generate_blog_post(store, *, since=None, mission=None) -> (slug, markdown)`

The factory turns its ongoing autonomous work into an accessible, Ars-Technica-style
article for a broad-but-tech-curious audience. The caller owns the clock and writes
`blog/<date>-<slug>.md`.

Mirrors `reporting/summary.py` / `reporting/diary.py`:
  * reuses the same deterministic, read-only `gather_summary_data`;
  * asks an ISOLATED `claude -p` (the Blogger role) to write the post;
  * never crashes — on an LLM error/empty/too-short/transport-sentinel reply it falls
    back to a deterministic templated article built straight from the gathered data
    (real title + sections, never invents);
  * read-only — never writes to the store, never promotes.
"""
from __future__ import annotations

import json
import re
from typing import Optional, Tuple

from ..roles import common as roles_common
from .diary import _slugify  # shared kebab-case helper
from .summary import gather_summary_data

_SENTINEL = "[claude -p"
_MIN_LEN = 300   # a usable post has a title + at least this much body


def _build_prompt(data: dict) -> str:
    return (roles_common._load_prompt("blogger")
            + "\n\n## GATHERED DATA (authoritative — ground yourself ONLY in this)\n"
            + "```json\n" + json.dumps(data, indent=2, default=str) + "\n```\n")


def _parse(reply: str) -> Optional[Tuple[str, str]]:
    """Pull (slug, body) from a usable Blogger reply: optional `slug:` first line,
    then a Markdown article that must start with an `# ` H1 title and be long enough.
    Returns None if the reply is unusable (empty, sentinel, no title, or too short)."""
    if not reply or not reply.strip():
        return None
    if reply.strip().startswith(_SENTINEL):
        return None
    lines = reply.strip().splitlines()
    slug = ""
    m = re.match(r"\s*slug:\s*(.+)\s*$", lines[0], re.IGNORECASE)
    if m:
        slug = _slugify(m.group(1), default="", max_words=8)
        lines = lines[1:]
    body = "\n".join(lines).strip()
    if not body.lstrip().startswith("# "):   # must lead with a real headline
        return None
    if len(body) < _MIN_LEN:                 # too short to be a real article
        return None
    if not slug:
        title = body.lstrip()[2:].splitlines()[0]
        slug = _slugify(title, default="factory-progress", max_words=8)
    return (slug, body)


def deterministic_blog(data: dict) -> Tuple[str, str]:
    """A grounded, templated article — used when the LLM is unavailable. Accessible
    framing, real headline + sections, never invents."""
    mission = data.get("mission") or "improving the harness"
    window = data.get("window") or "this window"
    runs = data.get("runs", {}) or {}
    total = int(runs.get("total", 0) or 0)
    passed = int((runs.get("by_outcome", {}) or {}).get("pass", 0) or 0)
    briefs = data.get("discoveries", {}).get("research_briefs", []) or []
    mined = data.get("discoveries", {}).get("mined_scenarios", []) or []
    gate = data.get("awaiting_gate", []) or []

    title = "A workshop that improves a command-line AI — and still asks permission"
    out: list[str] = [f"# {title}", ""]
    out.append(
        "Picture a workshop that runs overnight, tinkering with a tool to make it a "
        "little better — but which never ships a change without a human signing off "
        "first. That is, in plain terms, what this factory does. Its tool is an AI "
        f"agent that drives a real command line, and its job ({window}) is one mission: "
        f"{mission}.")
    out.append("")
    out.append("## What it actually tried")
    if briefs or mined:
        out.append(
            f"This window the factory read the literature and the logs: it staged "
            f"{len(briefs)} research idea(s) drawn from real papers or repositories, and "
            f"{len(mined)} fresh practice task(s) mined from real sessions. Research is "
            "only a hint here — a cited idea the proposer may try, never an order.")
        if briefs:
            b = briefs[0]
            out.append("")
            out.append(
                f"One idea, \"{b.get('title','(untitled)')}\", suggested {b.get('technique') or b.get('suggested_change') or 'a small change'}. "
                "The factory does not take that on faith; it turns the idea into a single, "
                "bounded change and measures whether it actually helps.")
    else:
        out.append("This was a quiet window: no new research or tasks were surfaced. "
                   "That happens, and the honest thing is to say so rather than dress it up.")
    out.append("")
    out.append("## How it knows a change helped")
    out.append(
        f"Every change is graded against the real end-state of a real shell — did the "
        f"job actually get done — across {total or 'a battery of'} test run(s)"
        + (f", {passed} of them passing" if total else "") + ". There is also a "
        "held-out set: tasks kept hidden during tinkering, like a surprise exam the "
        "student never saw while studying, so a change that merely memorised the "
        "practice questions gets caught.")
    out.append("")
    out.append("## The part that says no")
    if gate:
        ids = ", ".join(c.get("id", "?") for c in gate)
        out.append(
            f"At the end, {len(gate)} candidate change(s) had cleared the bar and were "
            f"queued for review ({ids}). Note the word *queued*: nothing was promoted "
            "automatically. A person opens the board and makes the call.")
    else:
        out.append(
            "This window nothing cleared the bar, so there was nothing to queue. The "
            "factory simply kept looking — and, as always, promoted nothing on its own.")
    out.append("")
    out.append(
        "That last point is the whole idea. A system that improves itself is only "
        "trustworthy if it stops at the same line every time and lets a human decide. "
        "The interesting part is not that the machine writes code — it is that it knows "
        "where to wait.")
    body = "\n".join(out)
    return (_slugify(f"factory-update-{window}", default="factory-update"), body)


def generate_blog_post(store, *, since: Optional[str] = None,
                       mission: Optional[str] = None) -> Tuple[str, str]:
    """Gather state + ask the Blogger to write an accessible article. Returns
    (slug, markdown). Falls back to a deterministic article (never crashes)."""
    data = gather_summary_data(store, since=since, mission=mission)

    try:
        reply, tokens, cost = roles_common.claude_p(_build_prompt(data))
    except Exception:  # noqa: BLE001 — presentation must never crash a run
        reply, tokens, cost = "", 0, 0.0

    if tokens or cost:
        try:
            store.add_budget("blogger", int(tokens), float(cost), notes="blog")
        except Exception:  # noqa: BLE001
            pass

    parsed = _parse(reply)
    if parsed:
        return parsed
    return deterministic_blog(data)
