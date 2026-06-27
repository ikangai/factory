"""Factory memory: the durable learnings each agent role / super-worker reads back
to improve, and the factory's own cross-cutting memory.

The store (`learnings` table) is plain CRUD; this module holds the curation: a
keyword dedup so the same lesson isn't stored twice, and `memory_card` — the
compact markdown block injected at the top of a role's prompt (its own recent
lessons + the shared `factory` lessons). Surfacing a lesson bumps its `uses`
counter (a cheap relevance signal, room for decay later).

Design: docs/plans/2026-06-27-factory-memory-design.md
"""
from __future__ import annotations

import re
from typing import Optional

ROLES = ("conductor", "developer", "researcher", "factory")


def _norm(text: str) -> str:
    r"""Lowercase, drop punctuation → a normalized key for near-dup matching. Keeps Unicode
    word chars (\w), so an all-non-ASCII lesson (CJK/Cyrillic/…) still yields a non-empty key
    and dedups instead of escaping the guard with an empty key."""
    return re.sub(r"[^\w ]", " ", (text or "").lower())
    # (collapse happens in _key via split/join)


def _key(text: str) -> str:
    return " ".join(_norm(text).split())


# Containment counts as a dup only when the shorter key is a SUBSTANTIAL fraction of the
# longer — else a short generic lesson ("narrow the brief") swallows every longer, more
# specific lesson that merely contains it.
_DUP_RATIO = 0.6


def _is_dup(content: str, existing: list[dict]) -> bool:
    nc = _key(content)
    if not nc:
        return False
    for e in existing:
        ne = _key(e.get("content", ""))
        if not ne:
            continue
        if ne == nc:
            return True
        if ne in nc or nc in ne:
            short, lng = sorted((ne, nc), key=len)
            if len(lng) and len(short) / len(lng) >= _DUP_RATIO:
                return True
    return False


def record_learning(store, role: str, content: str, *, agent: str = "",
                    scope: str = "general", shift_id: Optional[int] = None,
                    dedup: bool = True) -> Optional[int]:
    """Record a learning for `role` (returns its id), or None if blank or a near-dup
    of an existing learning for the SAME role (dedup is role-scoped — the same lesson
    can be relevant to two roles)."""
    content = (content or "").strip()
    if not content:
        return None
    if dedup and _is_dup(content, store.learnings_for_role(role, limit=200)):
        return None
    return store.add_learning(role, content, agent=agent, scope=scope, shift_id=shift_id)


def memory_card(store, role: str, *, limit: int = 8, include_factory: bool = True) -> str:
    """Render a role's recent learnings (+ the shared factory lessons) as a compact
    markdown block to prepend to its prompt — or "" when there's nothing yet. Bumps
    `uses` on every surfaced learning."""
    rows = store.learnings_for_role(role, limit=limit)
    factory_rows = (store.learnings_for_role("factory", limit=limit)
                    if include_factory and role != "factory" else [])
    if not rows and not factory_rows:
        return ""
    lines = [f"## What you've learned so far ({role})",
             "Durable lessons from past shifts — apply them; don't relearn them.", ""]
    lines += [f"- {r['content']}" for r in rows]
    if factory_rows:
        lines += ["", "### Factory-wide lessons"]
        lines += [f"- {r['content']}" for r in factory_rows]
    store.bump_learning_uses([r["id"] for r in rows] + [r["id"] for r in factory_rows])
    return "\n".join(lines)


_HEADER_RE = re.compile(r"(?i)^#{0,3}\s*learnings?\s*:\s*(.*)$")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*\S)\s*$")   # -, *, •, "1.", or "1)"
_SKIP = {"", "none", "n/a", "na", "nothing", "n.a."}


def coerce_learnings(raw) -> list[str]:
    """Normalize a raw `learnings` value (e.g. from researcher JSON) to a list of non-empty
    strings. A scalar/None/dict yields [] — guards against an LLM emitting `learnings: "..."`
    (a string), which would otherwise be iterated character-by-character into junk rows."""
    if not isinstance(raw, list):
        return []
    return [s.strip() for s in raw if isinstance(s, str) and s.strip()]


def parse_learnings(reply: str) -> list[str]:
    """Pull the learnings a super-worker (no DB access in its sandbox) emitted under a
    `LEARNINGS:` section in its final reply — inline (`LEARNINGS: foo`), a dash/star/•
    bullet list, OR a numbered list. The prompt says to END with the section, so the LAST
    `LEARNINGS:` line wins (a stray earlier prose "Learnings: …" line is ignored). A single
    prose intro line after the header is skipped; prose AFTER the first bullet ends the
    section. The orchestrator records these on the main thread (the store is single-writer)."""
    if not reply:
        return []
    lines = reply.splitlines()
    hdr_idx, hdr_inline = None, ""
    for i, ln in enumerate(lines):
        m = _HEADER_RE.match(ln.strip())
        if m:
            hdr_idx, hdr_inline = i, m.group(1).strip()
    if hdr_idx is None:
        return []
    out: list[str] = []
    if hdr_inline:                                  # content on the header line itself
        bm = _BULLET_RE.match(hdr_inline)
        item = (bm.group(1) if bm else hdr_inline).strip()
        if item.lower() not in _SKIP:
            out.append(item)
    seen_bullet = False
    for ln in lines[hdr_idx + 1:]:
        s = ln.strip()
        if not s:                                   # blank: tolerate within the section
            continue
        bm = _BULLET_RE.match(s)
        if bm:
            seen_bullet = True
            item = bm.group(1).strip()
            if item and item.lower() not in _SKIP:
                out.append(item)
        elif seen_bullet:                           # prose after the bullets ends the section
            break
        # else: a leading prose intro line before any bullet → skip it
    return out


_BLOCK_LESSONS = {
    "no_candidate": "no_candidate usually means the brief bundled too much — narrow it to "
                    "the smallest landable + testable slice and sequence the rest.",
    "discarded": "a candidate was discarded by the gate — keep changes minimal and on a "
                 "clean-merge, non-frozen surface.",
    "auto_reverted": "a merged candidate was auto-reverted — the correctness gate caught it; "
                     "tighten the brief's tests/scope.",
    "error": "a dispatch error blocked a task — check the brief targets a pristine, "
             "non-frozen file and the clone built.",
    "revert_failed": "a merged candidate regressed AND the auto-revert FAILED — the shared "
                     "factory/auto worktree may be dirty and need operator attention; keep "
                     "candidates small and reversible.",
}

# A 'discarded' action is too coarse — the stage says WHY. Give the precise lesson when known.
_DISCARD_BY_STAGE = {
    "tests": "a candidate was discarded at the TEST gate — it didn't make the target's tests "
             "pass; encode the acceptance as a focused test FIRST, then satisfy it.",
    "frozen": "a candidate was discarded for touching the FROZEN safety surface — keep changes "
              "off frozen files entirely.",
    "no_test": "a candidate was discarded for shipping a source change with NO test — write the "
               "acceptance test FIRST, then the code (the gate requires a test).",
}


def lesson_for_block(action: str, stage: str = "") -> Optional[str]:
    """The canned, deduped factory-level lesson for a blocked-task action (the factory's
    failure-memory). For a 'discarded' action the `stage` disambiguates the cause (tests vs
    frozen vs a generic gate). Returns None for non-failure actions (merged/halted)."""
    if action == "discarded" and stage in _DISCARD_BY_STAGE:
        return _DISCARD_BY_STAGE[stage]
    return _BLOCK_LESSONS.get(action or "")
