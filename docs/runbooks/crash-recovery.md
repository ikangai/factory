# Crash-recovery runbook

Companion to `docs/plans/2026-08-08-crash-consistency-design.md` (Phase 2 of
`docs/plans/2026-08-06-production-hardening-roadmap.md`). Read the design doc's "honest
goal" section first — this runbook assumes it:

> SQLite cannot transact with git or GitHub. Exactly-once is unachievable. What is
> achievable: never silently LOSE an effect, never harmfully REPEAT an effect, never LIE
> about an effect — an unresolvable state resolves `unknown` and escalates, it never
> guesses in either direction.

## 1. The `operations` table — what each kind means

Every external effect the factory takes (a merge, a graduation push, a broker-armed
graduation prepare) gets a durable row in `operations` (`store/schema.sql`) BEFORE it
happens and after it resolves. `status` moves `planned`/`executing` (the effect is in
flight, or a crash interrupted it) → `applied`/`reconciled`/`failed` (a terminal, known
outcome) → `unknown` only when nothing — not git, not a receipt, not the ledger — could
answer the question honestly.

| `kind` | What it tracks | `idem_key` shape | Landed = | Not landed = |
|---|---|---|---|---|
| `merge` | A super-worker's candidate merging into `factory/auto` (`orchestrator/code_round.py`) | `merge:<task_id>:<cand_tip_sha>` | a commit on the auto branch carries `Factory-Task: <task_id>` and nothing later carries a matching `Factory-Revert:` trailer | no such commit, or one exists but was auto-reverted |
| `graduate_push` | The unarmed graduation push (`reporting/issue_sync.py graduate_and_push`) | `grad:<repo>:<base_sha>:<tip_sha>` | `tip_sha` is an ancestor of `origin/<base>` (a fresh `git fetch` + `merge-base --is-ancestor`) | it isn't |
| `graduate_prepare` | The broker-armed prepare step (`graduate_and_prepare_envelope`) — writes an envelope for the operator's broker, never pushes origin itself | `gradprep:<nonce>` | the broker's own receipt says `pushed`, or (no receipt yet) git shows the tip landed anyway | a `rejected`/`expired` receipt, or nothing found and the paired approval is no longer in flight |
| `issue_sync` | A GitHub issue comment/close action | `issue:<number>:<sha>` | the pair is present in the `issue_sync` ledger table | absent — **the reconciler never asks GitHub**; absent always resolves `unknown`, never "assumed not posted" |

A row's `receipt` column carries the resulting sha/nonce once known; `detail` carries the
operator-facing why (or the exact command to check by hand, for an `unknown`). `attempts`
and `updated_at` are bookkeeping, not authority — git/receipts/the ledger are.

## 2. Reading `factory reconcile --dry-run`

```
$ factory reconcile --dry-run
  #14 [merge] merge:task-a1b2c3d4:9f8e7d6 — executing
  #15 [graduate_push] grad:ikangai/clive:abc123:def456 — planned
[reconcile] 2 row(s) would be examined (dry-run — nothing changed)
```

This lists every row still `planned`/`executing`, oldest first, bounded to the first 50 —
exactly what a real sweep (`factory reconcile`, or the automatic one at shift start)
would attempt to resolve. `--dry-run` resolves nothing; it's the pre-flight check before
a `factory reconcile` for real, or a way to see whether a suspicious shift left anything
behind.

A real sweep prints what it did:

```
$ factory reconcile
  #14 [merge] -> reconciled: landed: 9f8e7d6a...
  #15 [graduate_push] -> unknown: no fetch credential / not-on-base; verify manually: ...
[reconcile] examined 2, resolved 1, escalated 1 unknown — see `factory learn list --role factory` / the backlog for the escalation(s)
```

Every `unknown` also lands a deduped backlog task (`source_ref = reconcile:<kind>-unknown`)
and a factory learning — `factory task list --status open` and `factory learn list --role
factory` both surface it. The task's `detail` carries the SAME exact command printed
above; that command is always answerable with read-only git/`gh` — run it, then resolve
the row by hand if needed (`sqlite3 store/blackboard.db "UPDATE operations SET
status='reconciled', detail='manually verified: <what you found>' WHERE id=<id>"` — a
direct SQL edit is the sanctioned manual override; there is no CLI verb for it because a
human override should leave an unmistakable trace in the row itself).

The reconciler runs automatically at the START of every shift (`orchestrator/shift.py`,
right after `reap_orphaned_shifts`, BEFORE broker receipt ingestion and the approval
reaper — it has to see an `executing` row before the reaper turns it into the lossier
`'stale'`). It has its own STOP check independent of the shift's later one, so it never
runs while the fleet is halted (`factory reconcile` the CLI honors the same brake) —
**except** during `factory db-restore`, which explicitly bypasses it (§4) because
restore's own precondition requires STOP engaged.

## 3. Restore procedure, step by step

Prerequisite: a backup exists. `scripts/backup_blackboard.sh` (scheduled via
`deploy/user-factory/com.factory.backup.plist`) takes one with `sqlite3 .backup` +
`PRAGMA integrity_check` every 6h by default, into `~/factory-db-backups/` (override with
`FACTORY_BACKUP_DIR`).

1. **Stop the fleet.** `touch STOP` (or the dashboard's brake). Confirm no runner is
   alive — `factory autopilot status`, or `ps aux | grep 'run --loop'`.
2. **Pick a snapshot.** `ls -t ~/factory-db-backups/blackboard-*.db | head -5` — the
   newest one is usually right; older ones matter if the newest itself was taken
   post-corruption.
3. **Restore.**
   ```
   factory db-restore ~/factory-db-backups/blackboard-<STAMP>.db --yes
   ```
   Omit `--yes` to get an interactive confirm first. The command:
   - refuses outright unless STOP is engaged and no runner is alive (not skippable by
     `--yes` — that flag only skips the confirm prompt);
   - `PRAGMA integrity_check`s the **snapshot** before touching anything;
   - moves the current `store/blackboard.db` (+ `-wal`/`-shm`) aside, timestamped
     `blackboard.db.bak-<UTC-STAMP>` — **never deleted**;
   - copies the snapshot in, then `PRAGMA integrity_check`s the **result** — a torn/bad
     copy rolls back automatically (the moved-aside original is copied back into place;
     the bad copy is moved aside too, as `.failed-<STAMP>`, never deleted);
   - re-runs `init_db()` (additive migrations — a snapshot taken before a schema change,
     e.g. before the `operations` table existed, gains it here with no data loss);
   - runs the reconciler (with `ignore_stop=True` — see §2's note) so anything the
     corruption window or the restore itself left ambiguous gets resolved or escalated
     immediately, not silently at the next shift;
   - prints a counts summary (tasks by status, shifts, operations by status).
4. **Cross-check against git.** The counts summary is a sanity sheet, not proof — compare
   it against reality:
   ```
   git -C <target>.factory-auto log --oneline | wc -l      # commits on the auto branch
   factory task list --status open                          # anything the reconciler reopened
   factory learn list --role factory                        # any 'unknown' escalations
   ```
   A snapshot older than the crash will show FEWER tasks/shifts than git's own history —
   that's expected, not a bug; the reconciler cannot invent rows the snapshot never had,
   only resolve the ones that exist.
5. **Resume.** `rm STOP` (or the dashboard) once satisfied. The `.bak-*`/`.failed-*`
   siblings left in `store/` are gitignored and yours to keep or clear —
   `scripts/backup_blackboard.sh` doesn't touch them.

## 4. Drill 1 — kill at each wrapped boundary

The acceptance test for Component B (`docs/plans/2026-08-08-crash-consistency-design.md`
§Acceptance / `docs/plans/2026-08-06-production-hardening-roadmap.md` Part 5, drill 1).
Run it against a **throwaway** clone/dispatch, never a real graduation:

1. Pick a boundary: before/after `adapter.merge_branch` (`orchestrator/code_round.py`),
   before/after the auto-revert (`adapter.revert_commit`), before/after the unarmed push
   (`reporting/issue_sync.py graduate_and_push`), before/after `write_envelope` (armed
   prepare), before/after `resolve_approval` in `ingest_broker_receipts` (armed
   ingestion).
2. `kill -9` the factory process at that exact point (a debugger breakpoint, or a
   monkeypatched hook that raises `os.kill(os.getpid(), signal.SIGKILL)` for a scripted
   drill — `tests/test_crash_consistency_wrap.py`'s raising-store tests are the unit-level
   equivalent of "the write after this point never happens").
3. Restart the factory (`factory reconcile --dry-run` first to see what's stuck, then
   `factory reconcile` for real, or just start a shift — it runs automatically).
4. Assert, per the design's three properties:
   - **No lost effect**: if the boundary was AFTER the external action (git already has
     the merge/push; the envelope is already on disk), the reconciler finds it via
     git/receipt and marks it `reconciled`/`applied` — nothing about it disappears.
   - **No duplicated effect**: re-dispatching the SAME task produces a NEW candidate
     (a fresh branch/tip), so `begin_operation`'s idem-key skip only fires on a literal
     retry of the identical attempt (e.g. the reconciler re-entering mid-resolution) —
     verify the git log has exactly the merges you expect, not one extra.
   - **Surfaced, never guessed**: a boundary the reconciler genuinely cannot resolve
     (no reachable worktree, no fetch credential, no receipt and no git evidence) must
     show up as `unknown` with a backlog task — never silently marked done or discarded.

## 5. Drill 4 — corrupt, restore, reconcile, verify

1. On a **disposable copy** of `store/blackboard.db` (never the live one — copy it
   aside first), corrupt it: truncate the file, or overwrite a chunk with
   `dd if=/dev/urandom of=blackboard.db bs=1024 count=1 seek=10 conv=notrunc`.
2. Confirm the corruption is real: `sqlite3 blackboard.db "PRAGMA integrity_check;"`
   should NOT print `ok`.
3. Swap it into place (STOP engaged, no runner alive), or just point
   `factory db-restore` at a real backup snapshot directly — the drill's point is the
   RESTORE path, not the corruption mechanism.
4. Run `factory db-restore <snapshot> --yes` (§3).
5. Verify: the restore's own `PRAGMA integrity_check` passed (it refuses otherwise — see
   `orchestrator/db_restore.py`'s `restored-db-corrupt` rollback path), the reconciler ran
   (`reconcile.examined`/`resolved`/`unknown` in the printed summary, not skipped for
   STOP), and the counts summary roughly matches `git log --oneline factory/auto | wc -l`
   for landed work.

## 6. Canonicality matrix (Component E)

Which store is the authority for which kind of fact — asserted where cheap
(`tests/test_canonicality.py`), not enforced by a runtime gate everywhere (some of these
are structural facts about the codebase, not something a check can veto at request time):

| Store | Canonical for | How it's enforced/checked |
|---|---|---|
| SQLite blackboard (`store/blackboard.db`) | Workflow state + decisions (task status, shifts, approvals, learnings) | The only store any role/rail writes through — `common/store.py`'s CRUD |
| git (content-addressed) | Artifacts — what actually landed | The reconciler treats git as truth OVER the DB for `merge`/`graduate_push`/`graduate_prepare` (§1); `Factory-Task:`/`Factory-Revert:` trailers make history self-describing even with zero DB |
| GitHub | Published issue state | Broker receipts + `ls-remote`/`merge-base --is-ancestor` — the reconciler NEVER probes GitHub directly for `issue_sync` (§1); a wrong close is unrecoverable, so an unanswerable row stays `unknown` rather than guess |
| agora bus (`chat.db`) | Notifications only, never workflow state | `common/bus.py` never imports `common.store`/opens the blackboard — tested statically (`tests/test_canonicality.py`) |
| dashboard | Authenticated command submission only | `dashboard/fleet_server.py`'s `do_POST` whitelist (a fixed tuple of paths, checked before any body is even read) — tested statically |

Two read-only `reporting/` modules explicitly claim "never writes to the store" in their
own docstrings (`summary.py`, `diary.py`, `blog.py`) — asserted by AST-scanning them for
any call to a `Blackboard` write method (`tests/test_canonicality.py`). This is
deliberately NOT a blanket "nothing under reporting/ writes" claim: `reporting/
approvals.py`, `issue_sync.py`, `factory_memory.py`, `scope_check.py`, and
`human_queue.py` are documented, legitimate writers (workflow modules that happen to live
in that package) — see each module's own docstring for its actual contract.

## Out of scope (restated, so nobody re-litigates it here)

Leases/heartbeats on `operations` rows, a compensation DSL, distributed anything,
hash-chained audit logs, and any claim of exactly-once. The `develop.py` `merge_lock` is
a `threading.Lock` (in-process only) — two concurrent `factory run` *processes* could
still race the shared `factory/auto` worktree; this is a pre-existing, documented,
unfixed gap (relevant to drill 1 if the drill runs two loops at once).
