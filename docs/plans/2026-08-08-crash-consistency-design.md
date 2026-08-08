# Phase 2 — crash consistency: intent rows, reconciler, canonical state, restore (2026-08-08)

**Provenance:** Phase 2 of `docs/plans/2026-08-06-production-hardening-roadmap.md`,
seam-mapped against main @ eb94777. The map corrected the roadmap's sketch in six places;
this doc encodes the mapped reality, not the sketch.

## The honest goal (stated first, because the obvious claim is false)

SQLite cannot transact with git or GitHub. **Exactly-once is unachievable and claiming it
would be theater.** What is achievable, and what this phase delivers:

1. **Never silently LOSE an effect** — something durably recorded that it was about to act.
2. **Never harmfully REPEAT an effect** — an idempotency key plus a check against external
   truth before re-acting.
3. **Never LIE about an effect** — a state the reconciler cannot determine resolves to
   `unknown` and escalates with the exact command to check. It never guesses in either
   direction.

The leverage is that **git is content-addressed**: "did sha X land on ref Y" stays
answerable after a crash. An intent row only has to record enough to ask that question
later. That is the whole trick.

**Binding constraint:** the intent-row wrapper must degrade to today's behavior if the
store write itself fails (log + continue, never block the rail). Crash-consistency
machinery that can itself break a merge would be a net loss.

## What the seam map found (and what it changes)

| Effect | Crash window | Consequence today |
|---|---|---|
| **Merge** (`code_round.py:274` → `develop.py:457`) | **WIDE** — spans the whole re-baseline (full grade + full suite) *and* the `ThreadPoolExecutor` join at `develop.py:409`, so **N merges can sit unrecorded at once** | merge is in git, task still in-flight → lease reap reopens it → **re-dispatch of already-merged work** |
| **Graduation, unarmed** (`issue_sync.py:253` → `approvals.py:341`) | REAL, spans the `sync_issues` loop | `reap_orphaned_approvals` marks it `'stale'` with the note *"the push may or may not have reached origin — verify with `git ls-remote`"* |
| **Graduation, armed** (`issue_sync.py:462` → `approvals.py:334`) | **NEW — not in the roadmap** | envelope in the outbox with no `broker_nonce` on the row: the broker pushes it, the receipt can never be matched (`ingest` keys on `payload.broker_nonce`), the row ages to `'stale'` though the push SUCCEEDED |
| **Issue sync, unarmed** (`issue_sync.py:109` → `:124`) | REAL, one loop iteration | duplicate *comment* on retry; `close` is naturally idempotent — **materially lower severity than the roadmap assumed** |
| **Issue sync, armed** (`approvals.py:421` → `:423`) | REAL, and **ordered worse**: the row resolves `approved` before the ledger records | no re-ingestion path exists (receipts are found only via `executing` rows) → the ledger never advances → every later envelope re-plans already-closed issues |

Two structural facts the design exploits:
- **Merges are already self-describing.** `code_round.py:272` writes a `Factory-Task: <id>`
  trailer, so `git log --grep` answers "did this land?" with no new receipt plumbing.
  But **`git revert` writes no such trailer** (`adapters/base.py:253`), so a reconciler
  cannot distinguish "merged then auto-reverted" from "never merged". Cheap fix, below.
- **`reap_orphaned_approvals` is this reconciler's spec, written in prose**
  (`store.py:1023-1024`). Ordering therefore matters: the reconciler must run **before**
  the reaper converts `executing` into the lossy `'stale'`.

## Component A — the `operations` table

New table; per `schema.sql:263-266`'s documented precedent a brand-new table needs **no
`_migrate` entry** (`init_db` re-runs the whole script).

```sql
CREATE TABLE IF NOT EXISTS operations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                    -- merge | graduate_push | graduate_prepare | issue_sync
  idem_key TEXT NOT NULL UNIQUE,         -- deterministic identity of the EFFECT, never a timestamp
  status TEXT NOT NULL DEFAULT 'planned'
      CHECK (status IN ('planned','executing','applied','reconciled','failed','unknown')),
  target_ref TEXT DEFAULT '',            -- branch / repo slug the effect touches
  base_sha TEXT DEFAULT '', tip_sha TEXT DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}',   -- task_id / approval_id / issue numbers
  receipt TEXT DEFAULT '',               -- resulting sha or url, once known
  detail TEXT DEFAULT '',                -- why unknown/failed — operator-facing
  attempts INTEGER NOT NULL DEFAULT 0,
  shift_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_operations_status ON operations(status);
```

`idem_key` shapes (deterministic, mirroring the `issue_sync` PK precedent):
`merge:<task_id>:<cand_tip_sha>` · `grad:<repo>:<base_sha>:<tip_sha>` ·
`gradprep:<envelope_nonce>` · `issue:<number>:<sha>`.

CRUD stays thin, transitions rowcount-guarded exactly like `claim_approval`
(`store.py:1289`): `begin_operation` (INSERT planned→executing, or return the existing row
if one is already `applied`/`reconciled` — **that return is the idempotency win**),
`complete_operation(id, receipt)`, `set_operation_status(id, status, detail)`,
`operations(status=...)`.

## Component B — wrapping the effects (thin, fail-soft)

`begin_operation` immediately before the irreversible act; `complete_operation` immediately
after; both inside `try/except` that logs and continues on store failure (binding
constraint above). Wrapped:

1. **Merge** — around `code_round.py:274`, keyed on task + candidate tip. If a row for that
   key is already `applied`, the merge is skipped (it already happened).
2. **Auto-revert** — same row, receipt updated; **plus** add a `Factory-Revert: <merge_sha>`
   trailer to the revert commit (`adapters/base.py:253`) so git alone can distinguish
   merged-then-reverted from never-merged.
3. **Graduation push (unarmed)** — around `issue_sync.py:253`.
4. **Graduation prepare (armed)** — around the envelope write, and **stamp `broker_nonce`
   onto the approval payload BEFORE writing the envelope** (closes the orphan-envelope
   window structurally; the intent row is the belt to that suspender).
5. **Issue sync (armed ingestion)** — swap `approvals.py:421`/`:423` so
   `_record_synced_issues` runs **before** `resolve_approval`; a crash then leaves the row
   `executing` and the receipt re-ingestible, which is the recoverable direction.

## Component C — the reconciler (`orchestrator/reconcile.py`)

Runs at shift start, inserted in `shift.py` **between line 41 (`reap_orphaned_shifts`) and
line 57** — after crashed shifts are reaped, before `ingest_broker_receipts` and
`reap_orphaned_approvals` destroy the `executing` marker. Own STOP check (the killswitch
check at `shift.py:72` comes later) and honors `filelock.repo_lock` when it fetches.

For every row in `('planned','executing')`, ask git:

- **merge** — `git log --grep="Factory-Task: <id>"` on the auto branch, plus the new
  `Factory-Revert:` trailer. Landed → `reconciled`, and the task's record is repaired to
  `done` if the crash lost it. Not landed → `reconciled` (not-landed), task returns to
  `open`. Both answerable **locally, no credential**.
- **graduate_push** — fetch (read-only credential; armed mode keeps exactly this), then
  `merge-base --is-ancestor <tip_sha> origin/<base>`. Landed → `applied` + the approval
  resolves `approved`; not landed → the approval returns to `pending`. **A factory-side
  `ls-remote`/fetch helper is new** — nothing on the factory side does either today.
- **graduate_prepare** — prefer the broker receipt; fall back to the git check.
- **issue_sync** — **do NOT probe GitHub.** In armed mode the factory has no `gh` at all,
  and a wrong close is not recoverable while a duplicate comment is merely cosmetic. Rely
  on the ledger + receipts; anything unverifiable resolves `unknown` and escalates.
- **Anything unanswerable** → `unknown` + escalation, never a silent resolution.
- Bounded: at most N rows per start (default 50); the rest stay and escalate.

**Escalation seam:** `factory_memory.record_graduation_failure`'s pattern
(`factory_memory.py:102`) — a deduped `source_ref`-scoped backlog task plus a learning.
`pending_approvals` is **not** usable: its `kind` is CHECK-constrained to
`graduation|publication` (`schema.sql:309`). Use `ref="reconcile:<kind>-unknown"`.

CLI: `factory reconcile [--dry-run]` for the manual/post-restore sweep.

## Component D — restore (the drill-4 blocker, a confirmed pre-existing defect)

The **backup is correct** — `scripts/backup_blackboard.sh` uses `sqlite3 .backup` plus
`PRAGMA integrity_check`, explicitly not a torn `cp`. The **restore is not**: the only
documented path (`factory-user-deployment.md:297`) is a bare `cp` over the live DB, with
no instruction to stop the daemons and no handling of the stale `blackboard.db-wal`/`-shm`
sidecars. A backup you cannot safely restore is not a backup.

`factory db-restore <snapshot> [--yes]`: refuse unless STOP is engaged and no runner is
live → `integrity_check` the **snapshot** before touching anything → move the current
`db` + `-wal` + `-shm` aside, timestamped, **never delete** → copy in → `integrity_check`
the result → `init_db` (runs migrations) → run the reconciler → print a counts-vs-git
summary. Runbook section replaces the `cp` line.

## Component E — canonical state, asserted where cheap

| Store | Canonical for | Enforcement |
|---|---|---|
| SQLite blackboard | workflow + decisions | — |
| git (content-addressed) | artifacts, what landed | the reconciler treats git as truth over the DB |
| GitHub | published state | broker receipts + `ls-remote` |
| agora bus | notifications only | test: no bus module writes the store |
| dashboard | authenticated command submission | existing write whitelist |

Cheap assertions worth writing as tests: `reporting/` never writes the store (already the
layer's stated contract), bus modules never write the store, dashboard writes only via the
whitelist.

## Out of scope (roadmap Part 4 already rejects these — restated so nobody gold-plates)

Leases/heartbeats on operations, compensation DSL, distributed anything, hash-chained
audit logs, exactly-once claims of any kind.

**Known, deliberately unfixed (documented, not closed here):** `develop.py:331`'s
`merge_lock` is a `threading.Lock`, so two concurrent `factory run` *processes* could merge
into the same worktree. Pre-existing, out of Phase 2's scope, and named in the runbook —
relevant to drill 1 if the drill runs two loops.

## Acceptance (drill 1, hermetic)

Kill the process at each wrapped boundary — before/after merge, before/after the store
record, before/after push, before/after receipt ingestion — and assert: no duplicated
effect, no lost effect, every unresolved case surfaced as `unknown` with an escalation.
Plus drill 4: corrupt the DB, restore from a snapshot, reconcile, verify counts against git.
