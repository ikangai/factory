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

import os
import re
from pathlib import Path
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


def _is_dup(content: str, existing: list[dict]) -> Optional[dict]:
    """Return the matched existing row when `content` is a near-dup of it, else None —
    the caller needs the row's id to bump its recurrence counter (Task 0.5)."""
    nc = _key(content)
    if not nc:
        return None
    for e in existing:
        ne = _key(e.get("content", ""))
        if not ne:
            continue
        if ne == nc:
            return e
        if ne in nc or nc in ne:
            short, lng = sorted((ne, nc), key=len)
            if len(lng) and len(short) / len(lng) >= _DUP_RATIO:
                return e
    return None


def record_learning(store, role: str, content: str, *, agent: str = "",
                    scope: str = "general", shift_id: Optional[int] = None,
                    dedup: bool = True) -> Optional[tuple[int, bool]]:
    """Record a learning for `role`. Returns (id, created): created=False when the content
    deduped onto an existing learning for the SAME role — its `hits` counter is bumped so
    recurrence is COUNTED, not destroyed (Task 0.5). Returns None only for blank content.
    Dedup is role-scoped — the same lesson can be relevant to two roles.
    The dedup window INCLUDES retired (archived=1) rows on purpose (Task 1.3): the factory
    auto-records templated lessons on recurring failures, so a lesson the operator retired
    will be re-reported verbatim — it must dedup onto the archived row (hits bumped,
    created=False, still hidden from prompts), never resurrect as a fresh live row."""
    content = (content or "").strip()
    if not content:
        return None
    if dedup:
        hit = _is_dup(content, store.learnings_for_role(role, limit=200,
                                                        include_archived=True))
        if hit is not None:
            store.bump_learning_hits(hit["id"])
            return hit["id"], False
    return store.add_learning(role, content, agent=agent, scope=scope, shift_id=shift_id), True


# A lesson reported this many times (hits) is flagged in the card — recurrence is the
# cheapest severity signal the factory has (Task 0.5).
_RECURRING_MIN_HITS = 3


def _card_line(r: dict) -> str:
    hits = r.get("hits") or 0
    tag = f" (recurring x{hits})" if hits >= _RECURRING_MIN_HITS else ""
    stale = " (may be stale — cited file moved)" if r.get("stale") else ""
    return f"- {r['content']}{tag}{stale}"


# -- per-task relevance (Task 1.4): keyword-overlap scoring over a bounded window.
# No embeddings, no LLM, no gate — _key-normalized token overlap with the task's own
# title+detail, so an old-but-on-point lesson resurfaces instead of aging out of the
# newest-N card. One shared keyword is coincidence ("test", "subsystem"); two is topical.
_TOPIC_WINDOW = 50       # rows scanned per role for relevance
_TOPIC_TOP = 4           # top relevant rows kept
_TOPIC_NEWEST = 4        # newest rows always kept — a fresh lesson matters even off-topic
_MIN_TOKEN_LEN = 3       # scoring ignores 1-2 char tokens ("a", "to", "of", …)
_MIN_OVERLAP = 2         # shared keywords needed to count as relevant at all
_STOP_TOKENS = frozenset((
    "the", "and", "for", "with", "into", "from", "that", "this", "not", "but",
    "was", "are", "has", "have", "its", "when", "then", "than", "you", "your",
    "will", "must", "can", "may", "should", "would", "could", "does", "did",
))


def _tokens(text: str) -> set:
    return {t for t in _key(text).split()
            if len(t) >= _MIN_TOKEN_LEN and t not in _STOP_TOKENS}


def _select_rows(rows: list[dict], topic: str) -> list[dict]:
    """The per-task view of a newest-first window: the top-`_TOPIC_TOP` rows by keyword
    overlap with `topic` (ties → newest wins), then the `_TOPIC_NEWEST` newest rows,
    deduped. Rows sharing fewer than `_MIN_OVERLAP` keywords never enter via relevance —
    only via the newest leg."""
    tt = _tokens(topic)
    scored = [(len(tt & _tokens(r.get("content", ""))), r) for r in rows]
    relevant = sorted((p for p in scored if p[0] >= _MIN_OVERLAP),
                      key=lambda p: (-p[0], -p[1]["id"]))
    picked = [r for _, r in relevant[:_TOPIC_TOP]]
    seen = {r["id"] for r in picked}
    picked += [r for r in rows[:_TOPIC_NEWEST] if r["id"] not in seen]
    return picked


def memory_card_with_ids(store, role: str, *, topic: Optional[str] = None,
                         limit: int = 8, include_factory: bool = True) -> tuple[str, list[int]]:
    """`memory_card` + the surfaced learning ids, so the caller can attribute the task's
    eventual OUTCOME back to exactly what the worker was shown (Task 1.4 consult-
    telemetry: `bump_learning_outcomes` at close-out). With a `topic` (the task's
    title+detail) the card is PER-TASK: a `_TOPIC_WINDOW`-row window is scored by
    normalized-keyword overlap and the top-4 relevant + newest-4 surface. No topic →
    the classic newest-`limit` card. Bumps `uses` on every surfaced learning; lessons
    reported >= 3 times are flagged `(recurring xN)`."""
    if topic:
        rows = _select_rows(store.learnings_for_role(role, limit=_TOPIC_WINDOW), topic)
        factory_rows = (_select_rows(store.learnings_for_role("factory", limit=_TOPIC_WINDOW),
                                     topic)
                        if include_factory and role != "factory" else [])
    else:
        rows = store.learnings_for_role(role, limit=limit)
        factory_rows = (store.learnings_for_role("factory", limit=limit)
                        if include_factory and role != "factory" else [])
    if not rows and not factory_rows:
        return "", []
    lines = [f"## What you've learned so far ({role})",
             "Durable lessons from past shifts — apply them; don't relearn them.", ""]
    lines += [_card_line(r) for r in rows]
    if factory_rows:
        lines += ["", "### Factory-wide lessons"]
        lines += [_card_line(r) for r in factory_rows]
    ids = [r["id"] for r in rows] + [r["id"] for r in factory_rows]
    store.bump_learning_uses(ids)
    return "\n".join(lines), ids


def memory_card(store, role: str, *, limit: int = 8, include_factory: bool = True) -> str:
    """Render a role's recent learnings (+ the shared factory lessons) as a compact
    markdown block to prepend to its prompt — or "" when there's nothing yet. Thin
    wrapper over memory_card_with_ids for callers that don't attribute outcomes
    (conductor/researcher prompt builds)."""
    return memory_card_with_ids(store, role, limit=limit, include_factory=include_factory)[0]


# Consult-telemetry display floor (Task 1.4): below this many attributed outcomes the
# merged/blocked ratio is noise, not signal — `learn list` suppresses it entirely.
EFFECTIVENESS_MIN_N = 10


def effectiveness(row: dict) -> Optional[tuple[float, int]]:
    """(merged_share, n) for a learning with enough outcome attributions to mean
    anything, else None — never render a 2-of-3 'ratio'. n = merged_after +
    blocked_after (tasks whose worker card surfaced this row, by close-out outcome)."""
    merged = int(row.get("merged_after") or 0)
    n = merged + int(row.get("blocked_after") or 0)
    if n < EFFECTIVENESS_MIN_N:
        return None
    return merged / n, n


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
    # Task 3.1: the spec's OWN named acceptance test ran RED in the candidate — the change didn't
    # satisfy its declared done-condition. Create/fix exactly the contracted test ref and make it pass.
    "acceptance": "a candidate was discarded because the spec's named ACCEPTANCE test ran RED — the "
                  "change didn't satisfy its declared done-condition; create/fix exactly the "
                  "contracted test ref (tests/…::name) and make it pass before merging.",
    # Task 2.3: a reviewer reject is not the generic gate-discard — the blind pre-merge reviewer
    # judged the change out-of-scope/unsafe; its reason rides in the blocked result. Name it.
    "review": "a candidate was rejected by the blind pre-merge REVIEWER — read its reason in the "
              "blocked result and rescope the brief so the change is obviously minimal + in-scope.",
}

# Task 0.1 (P11): an 'error' action's stage disambiguates infrastructure/refusal failures.
# Neither is scope evidence — the false "brief bundled too much" lesson must not be written.
_ERROR_BY_STAGE = {
    "transport": "a worker dispatch failed at the TRANSPORT ([claude -p unavailable]) — the "
                 "brief was never attempted; check the claude binary/host, do NOT narrow the "
                 "brief or treat this as scope evidence.",
    "refusal": "a worker REFUSED a brief outright — reword it to be plainly constructive and "
               "state the legitimate goal; a refusal is not scope evidence, don't decompose it.",
}


def lesson_for_block(action: str, stage: str = "") -> Optional[str]:
    """The canned, deduped factory-level lesson for a blocked-task action (the factory's
    failure-memory). For a 'discarded' action the `stage` disambiguates the cause (tests vs
    frozen vs a generic gate); for an 'error' action a transport/refusal stage gets its own
    lesson (Task 0.1). Returns None for non-failure actions (merged/halted)."""
    if action == "discarded" and stage in _DISCARD_BY_STAGE:
        return _DISCARD_BY_STAGE[stage]
    if action == "error" and stage in _ERROR_BY_STAGE:
        return _ERROR_BY_STAGE[stage]
    return _BLOCK_LESSONS.get(action or "")


# -- learnings hygiene: deterministic staleness verify (Task 1.3) -------------
# A lesson that cites a file that no longer exists (or a line beyond EOF) is
# probably describing code that moved. Regex + stat — zero tokens, advisory only.

# File cites as they actually appear in live rows: bare basenames ("session.py:278"),
# relative paths ("reporting/scope_check.py"), optionally ":<line>". Extension-anchored
# so prose abbreviations ("e.g.", version numbers) never match.
_CITE_EXTS = ("py", "md", "sql", "html", "js", "css", "yaml", "yml", "json", "sh", "toml")
_CITE_RE = re.compile(
    r"(?<![\w.-])((?:[\w.-]+/)*[\w.-]+\.(?:%s))(?::(\d+))?\b" % "|".join(_CITE_EXTS))

# URL path segments are NOT file cites (Fix 1.3b): 'https://docs.python.org/3/library/
# re.html' would otherwise extract as a repo path that can never resolve — false-flagging
# the learning stale (or accidentally resolving via the basename fallback).
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def extract_cites(content: str) -> list[tuple[str, Optional[int]]]:
    """The (path, line) file cites in a learning's content, in order, deduped.
    line is None for a bare-path cite. Candidates inside an http(s):// URL are
    skipped by span — a URL path segment is prose, not a repo cite (Fix 1.3b)."""
    content = content or ""
    url_spans = [m.span() for m in _URL_RE.finditer(content)]
    out: list[tuple[str, Optional[int]]] = []
    for m in _CITE_RE.finditer(content):
        if any(a <= m.start() and m.end() <= b for a, b in url_spans):
            continue
        cite = (m.group(1), int(m.group(2)) if m.group(2) else None)
        if cite not in out:
            out.append(cite)
    return out


def _role_root(role: str) -> str:
    """The repo a role's file cites refer to: the factory itself for the 'factory' role
    (a live row cites reporting/scope_check.py); the TARGET checkout for every other role
    (developer/conductor/researcher lessons describe the target codebase)."""
    from ..common import paths
    if role == "factory":
        return str(paths.FACTORY_ROOT)
    from .scope_check import _target_root                   # adapter resolve, fail-open
    return _target_root()


def _cite_status(root: str, path: str, line: Optional[int]) -> str:
    """'ok' | 'stale' | 'ambiguous'. Path-prefix resolve first; live cites are mostly
    bare basenames, so fall back to a unique-basename rglob (dot-dirs like .git skipped).
    Multiple basename matches = 'ambiguous' — we can't know which file was meant, and an
    advisory tool must not false-positive, so ambiguity is NOT stale evidence."""
    p = Path(root) / path
    if not p.is_file():
        base = os.path.basename(path)
        matches = [m for m in Path(root).rglob(base)
                   if m.is_file()
                   and not any(part.startswith(".") for part in m.relative_to(root).parts)]
        if len(matches) == 1:
            p = matches[0]
        elif matches:
            return "ambiguous"
        else:
            return "stale"
    if line is not None:
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                if line > sum(1 for _ in fh):               # cite points beyond EOF
                    return "stale"
        except OSError:
            return "stale"
    return "ok"


def verify_learnings(store, *, roots: Optional[dict] = None, limit: int = 1000) -> list[dict]:
    """`factory learn verify` — deterministically check every live learning's file cites
    against the repo its role works in (`roots` overrides the per-role resolve for tests).
    A dead cite (missing path / line beyond EOF) sets stale=1; a cite that resolves again
    clears it. ADVISORY ONLY: never deletes, never archives — retiring stays the operator's
    call (`factory learn retire`). Returns one report entry per cite-carrying learning."""
    report: list[dict] = []
    for r in store.all_learnings(limit):
        if r.get("archived"):
            continue
        cites = extract_cites(r.get("content", ""))
        if not cites:
            continue
        root = (roots or {}).get(r["role"]) or _role_root(r["role"])
        dead = [f"{path}:{line}" if line else path
                for path, line in cites if _cite_status(root, path, line) == "stale"]
        stale = bool(dead)
        if stale != bool(r.get("stale")):
            store.set_learning_stale(r["id"], stale)
        report.append({"id": r["id"], "role": r["role"], "stale": stale,
                       "cites": [f"{p}:{ln}" if ln else p for p, ln in cites],
                       "stale_cites": dead})
    return report
