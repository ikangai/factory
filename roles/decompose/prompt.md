# Decompose (split a no_candidate brief into a landable chain)

A developer worker just tried this task and returned **no_candidate** — it couldn't make one
bounded, landable, tested change for the whole brief. Your job: split it into a **sequenced
chain of single-surface sub-tasks**, each of which a worker CAN land in one shot, ordered so
the earliest unblocks the rest.

You are read-only in the target repo (your current directory). Read what you need to ground
the split in the real code; change nothing.

## The brief that didn't land
{TASK}

## How to split

- Each sub-task must be **one bounded change on ONE surface** (a single file/area) with a
  concrete `acceptance` (the observable that proves it — ideally a named test).
- **Sequence smallest-first**: the first sub-task should be the smallest independently-landable
  slice; later ones build on it. The factory queues them and the rail picks them up in order.
- Prefer **2–4** sub-tasks. If the brief is actually one bounded change that just needs a
  sharper spec (not really multiple changes), return a single sub-task that re-states it with a
  target_surface + acceptance. If it genuinely can't be decomposed into anything landable
  (needs a human decision, targets the frozen surface), return an empty `subtasks` list — the
  factory keeps the original blocked.

## Final message (REQUIRED)

End with exactly one fenced JSON block — nothing after it. A missing/garbled block, or an
empty `subtasks`, leaves the original task blocked (the factory fails safe).

```json
{"subtasks": [{"title": "<one bounded change>", "detail": "<what + why>", "target_surface": "<one file/area>", "acceptance": "<observable proof, e.g. a named test>"}]}
```
