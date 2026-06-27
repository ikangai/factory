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
    """Lowercase, strip punctuation → a normalized key for near-dup matching."""
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())
    # (collapse happens in _is_dup via split/join)


def _key(text: str) -> str:
    return " ".join(_norm(text).split())


def _is_dup(content: str, existing: list[dict]) -> bool:
    nc = _key(content)
    if not nc:
        return False
    for e in existing:
        ne = _key(e.get("content", ""))
        if ne and (ne == nc or ne in nc or nc in ne):
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


_HEADER_RE = re.compile(r"(?i)^#{0,3}\s*learnings?\s*:")
_SKIP = {"", "none", "n/a", "na", "nothing"}


def parse_learnings(reply: str) -> list[str]:
    """Pull the learnings a super-worker (no DB access in its sandbox) emitted in its final
    reply under a `LEARNINGS:` section — inline (`LEARNINGS: foo`) or a bullet list. The
    orchestrator records these on the main thread (the store is single-writer)."""
    if not reply:
        return []
    out: list[str] = []
    capturing = False
    for ln in reply.splitlines():
        s = ln.strip()
        if _HEADER_RE.match(s):
            capturing = True
            inline = _HEADER_RE.sub("", s).strip().lstrip("-*• ").strip()
            if inline and inline.lower() not in _SKIP:
                out.append(inline)
            continue
        if not capturing:
            continue
        if not s:                       # blank line: tolerate within the section
            continue
        if s[0] in "-*•":               # a bullet → a learning
            item = s.lstrip("-*• ").strip()
            if item and item.lower() not in _SKIP:
                out.append(item)
        else:                           # prose after the bullets ends the section
            break
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
}


def lesson_for_block(action: str) -> Optional[str]:
    """The canned, deduped factory-level lesson for a blocked-task action (the factory's
    failure-memory). Returns None for non-failure actions (merged/halted)."""
    return _BLOCK_LESSONS.get(action or "")
