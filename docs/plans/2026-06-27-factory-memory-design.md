# Factory memory: agents + super-workers learn across shifts

**Date:** 2026-06-27
**Status:** Approved goal (via /goal), design + build in progress

## Goal

Every agent role (conductor, developer, researcher) and super-worker gets a
persistent **memory of learnings** they (1) read back into their context to
improve, and (2) write to when they discover something reusable. Aggregated, this
is the **factory's own memory** — the factory learns shift over shift, not just
the target repo.

This mirrors how Claude Code itself keeps a `MEMORY.md` + per-fact files: a small,
curated, role-scoped set of durable lessons injected at the top of each run.

## Decisions

1. **Storage = a `learnings` table in the blackboard** (SQLite, the factory's
   single source of truth) — not loose files. Keyed by `role` and optional
   `agent` identity, with a free-text `scope` tag. CRUD lives in the store; the
   curation/format logic lives in a module.
2. **Write path = `factory learn add` CLI + auto-record.** Super-workers already
   shell out to `factory task …` / `factory issue …`; a `factory learn add
   --role … --content …` is the same seam (dedup'd like `cmd_issue`). The
   orchestrator also auto-records a `factory`-role learning from each shift's
   outcome (so the factory learns even if an agent forgets to).
3. **Read path = a "memory card" injected into each role's prompt.** A
   `memory_card(store, role)` renders the most relevant recent learnings for that
   role (+ the shared `factory` lessons) as a compact markdown block, appended
   where the role's dynamic context (mission/resume/backlog) is already assembled.
4. **Keyword-gated dedup**, like `_dup_title`: don't store a near-duplicate
   learning; bump a `uses` counter when a learning is surfaced (cheap relevance
   signal, room for decay later).
5. **`factory` is a first-class role** alongside conductor/developer/researcher —
   it holds cross-cutting lessons (e.g. "narrow no_candidate briefs",
   "graduate when a modified-on-auto file gets a follow-up") surfaced to the
   conductor, the factory's planner.

## Architecture

### Store: `learnings` table + methods

```sql
CREATE TABLE IF NOT EXISTS learnings (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  role       TEXT NOT NULL,                 -- conductor|developer|researcher|factory
  agent      TEXT NOT NULL DEFAULT '',      -- optional handle/identity
  scope      TEXT NOT NULL DEFAULT 'general',
  content    TEXT NOT NULL,
  shift_id   INTEGER,
  uses       INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
```

`add_learning(role, content, *, agent='', scope='general', shift_id=None) -> int`
`learnings_for_role(role, limit=10) -> list[dict]` (newest first)
`all_learnings(limit=50)`, `bump_learning_uses(ids)`.

### Module `reporting/factory_memory.py`

- `record_learning(store, role, content, *, agent='', scope='general', shift_id=None,
  dedup=True) -> Optional[int]` — normalized-dup guard; returns id or None if dup.
- `memory_card(store, role, *, limit=8, include_factory=True) -> str` — compact
  markdown block ("## What you've learned so far (role) …") or "" when empty;
  bumps `uses` on surfaced rows.
- `_norm()` / `_is_dup()` helpers (mirror `_dup_title`).

### CLI `factory learn`

- `factory learn add --role R --content "…" [--scope S] [--agent A]` (dedup'd)
- `factory learn list [--role R] [--limit N]`

### Prompt wiring (read + write) — exact seams pending the seam-map recon

- Read: append `memory_card(store, role)` into the conductor / developer /
  researcher prompt assembly (shared loader if one exists).
- Write: each role's `prompt.md` gains a short "record what you learned" section
  instructing `factory learn add --role <role> --content "…"` for durable,
  reusable lessons (not run-specific noise).
- Factory-level: after a shift, `cmd_run` records a `factory` learning from the
  outcome (shipped / blocked-reason), surfaced to the conductor next shift.

## Testing (TDD, hermetic — tmp store, injected runner where I/O is involved)

- store: add/list/roundtrip, role isolation, `bump_learning_uses`.
- factory_memory: record (returns id), dedup (2nd near-dup → None), memory_card
  format + empty-state "" + includes factory lessons + bumps uses.
- CLI: `learn add` dedup'd + `learn list` (monkeypatched store path).
- wiring: memory_card present in the assembled prompt for each role; shift
  auto-records a factory learning.

## Out of scope (YAGNI)

- Embeddings / semantic recall — keyword + recency is enough at this scale.
- Cross-repo memory sharing — this is the factory's own memory.
- Automatic decay/eviction — keep `uses`, defer pruning until the table is large.
