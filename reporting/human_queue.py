"""reporting/human_queue.py — derive_human_queue: the operator's actionable work list.

Design: docs/plans/2026-07-08-factory-owned-bus-human-queue-design.md §2. Pure derivation
(no HTTP, no printing beyond the non-fatal degrade lines below) over three sources that each
already own a hard failure boundary of their own:
  - common.bus.open_questions — open @human escalations (bus, forgery-proof sqlite reads;
    NEVER raises, [] on any failure — see common/bus.py's module docstring).
  - store.pending_approvals — graduation/publication proposals awaiting a decision
    (Task 3, common/store.py).
  - store.recent_blocked_tasks (+ store.task_evidence) — blocked tasks with their failure
    reason and forensic evidence.

This module feeds the fleet dashboard's /api/fleet payload directly, so a bad row in any ONE
section must degrade THAT section to [] (one `[queue]`-prefixed non-fatal print, matching
common/bus.py's `_fail`/log-and-continue style) rather than blank the whole queue or crash the
endpoint — never raise.

Ordering (why, not just what):
  - escalations FIRST, oldest first: a human is being waited on; the longest wait is the
    most overdue, and escalations always outrank the other two kinds (they block a live
    worker/session, not just a proposal or a done shift).
  - approvals next, oldest first: same "longest-waiting on top" logic — an unapproved
    graduation/publication sits idle the longer it waits, and staleness (age_days > threshold)
    is the signal this section exists to surface.
  - blocked LAST, newest first: the opposite intent — blocked tasks are a backlog, not a
    live wait, and the freshest failures are the most actionable (a regression just
    introduced vs. a long-blocked task the operator has already triaged and parked).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..common import bus as _bus

_DATE_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"   # common/store.py:now_iso() format


def _age_days(ts: Optional[str], now: datetime) -> Optional[float]:
    """`ts` (store.now_iso() format) → age in days as of `now`. None on any missing/
    unparsable value — callers treat that as 'unknown age', never a crash."""
    if not ts:
        return None
    try:
        dt = datetime.strptime(ts, _DATE_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return None
    return (now - dt).total_seconds() / 86400.0


def _escalation_items(bus_dir: Optional[str]) -> list[dict]:
    try:
        qs = _bus.open_questions(bus_dir)
    except Exception as e:   # belt & suspenders: open_questions already never raises
        print(f"[queue] escalations unavailable: {e}")
        return []
    # oldest-first: bus message ids are monotonically increasing with post order, so sorting
    # by id is exactly "oldest posted first" regardless of open_questions' internal per-
    # session grouping (which is not globally id-ordered across sessions).
    items = [{"type": "escalation", "id": q["id"], "ts": q["ts"], "sender": q["sender"],
              "text": q["text"]} for q in qs]
    items.sort(key=lambda it: it["id"])
    return items


def _approval_summary(kind: str, payload: dict) -> tuple[str, Optional[int]]:
    """(summary text, n_commits) for one pending_approvals row. Defensive against missing or
    future-evolved payload keys — payload is factory-written (Task 5's proposal sites) but
    its shape may drift as those callers change; a missing key must degrade the summary, not
    raise."""
    if kind == "graduation":
        n = payload.get("n_commits")
        rng = payload.get("range")
        n_txt = str(n) if isinstance(n, int) else "?"
        rng_txt = f" ({rng})" if rng else ""
        return f"{n_txt} commit(s){rng_txt} ready to graduate", (n if isinstance(n, int) else None)
    if kind == "publication":
        ahead = payload.get("ahead")
        release = payload.get("release") or "main"
        n_txt = str(ahead) if isinstance(ahead, int) else "?"
        return (f"{n_txt} commit(s) awaiting promotion to {release}",
                (ahead if isinstance(ahead, int) else None))
    return f"{kind or 'unknown'} approval pending review", None   # a future kind must still render


def _approval_items(store, now: datetime, stale_after_days: float) -> list[dict]:
    try:
        rows = store.pending_approvals(status="pending")   # newest-first (store contract)
    except Exception as e:
        print(f"[queue] approvals unavailable: {e}")
        return []
    items = []
    for r in rows:
        try:
            kind = r.get("kind") or ""
            payload = r.get("payload") or {}
            summary, n_commits = _approval_summary(kind, payload)
            age = _age_days(r.get("created_at"), now) or 0.0
            items.append({"type": "approval", "approval_id": r.get("id"), "kind": kind,
                          "summary": summary, "n_commits": n_commits,
                          "age_days": round(age, 3), "stale": age > stale_after_days})
        except Exception as e:   # one malformed row must not blank the whole section
            print(f"[queue] approval row {r.get('id')} skipped: {e}")
    # store returns newest-first; the queue wants oldest-first (longest-waiting on top).
    items.reverse()
    return items


def _blocked_items(store, now: datetime, limit: int = 12) -> list[dict]:
    try:
        rows = store.recent_blocked_tasks(limit=limit)   # already newest-first by updated_at
    except Exception as e:
        print(f"[queue] blocked tasks unavailable: {e}")
        return []
    has_evidence = hasattr(store, "task_evidence")
    items = []
    for t in rows:
        try:
            reason = " ".join((t.get("result") or "").split())[:200]   # mirror {BLOCKED} seam
            evidence_head = ""
            if has_evidence:
                try:
                    ev = store.task_evidence(t["id"])
                    if ev:
                        # the full test-suite output is the more actionable forensic detail
                        # than the worker's own reply — prefer it, fall back to reply_head.
                        head = ev[0].get("tests_report") or ev[0].get("reply_head") or ""
                        evidence_head = head[:300]
                except Exception as e:
                    print(f"[queue] evidence unavailable for {t.get('id')}: {e}")
            # updated_at (not created_at): recent_blocked_tasks orders by it too — it marks
            # WHEN the task became/stayed blocked, which is the age the operator cares about.
            age = _age_days(t.get("updated_at"), now)
            age_days = round(age, 3) if age is not None else None
            items.append({"type": "blocked", "task_id": t.get("id"), "title": t.get("title", ""),
                          "reason": reason, "age_days": age_days, "evidence_head": evidence_head})
        except Exception as e:
            print(f"[queue] blocked row {t.get('id')} skipped: {e}")
    return items


def derive_human_queue(store, bus_dir: Optional[str] = None, *, now: Optional[datetime] = None,
                       stale_after_days: float = 3.0) -> dict:
    """The operator's actionable work list: open @human escalations + pending approvals +
    blocked tasks, ordered escalations-then-approvals-then-blocked (see module docstring for
    the per-section ordering rationale). Pure — no HTTP, no side effects beyond the
    `[queue]`-prefixed non-fatal degrade prints on a section failure. `now` is injectable
    (UTC) so age/staleness math is hermetic in tests; defaults to the real current time.
    Never raises: any store/bus failure degrades that section to [] alone."""
    now = now or datetime.now(timezone.utc)
    escalations = _escalation_items(bus_dir)
    approvals = _approval_items(store, now, stale_after_days)
    blocked = _blocked_items(store, now)
    items = [*escalations, *approvals, *blocked]
    return {"items": items,
            "counts": {"escalations": len(escalations), "approvals": len(approvals),
                       "blocked": len(blocked), "total": len(items)}}
